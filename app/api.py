from __future__ import annotations

import mimetypes
import json
import re
import shutil
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse

from app.config import Settings
from app.codex_runner import CodexError, codex_status
from app.database import Database
from app.job_workspace import job_workspace, remove_job_workspace
from app.local_requests import require_local_request
from app.local_video_picker import PickerError, choose_video_paths
from app.editing import hook_suggestions, validate_edit_source
from app.models import (
    ClipCandidate,
    ClipUpdate,
    CutRange,
    ExportRequest,
    ExportResponse,
    JobDetail,
    JobSummary,
    InsertRange,
    ManualClipCreate,
    SubtitleCue,
    SubscriptionSuggestRequest,
    SubscriptionSuggestions,
    SubscriptionSettings,
    YoutubePackage,
    YoutubeUpdate,
    YoutubeSourceRequest,
    YoutubeChannelBrowseResponse,
    YoutubeChannelBrowseRequest,
)
from app.queue import WorkQueue
from app.uploads import SafeUploadRoute, UploadPreflightRequest, validate_upload_space
from app.subscriptions import generate_subscription_suggestions
from app.subscription_card import AVATAR_PATH, render_card_preview
from app.transcript_correction import correct_saved_cues, transcript_fingerprint
from app.youtube import package_revision, render_thumbnail
from app.youtube_import import (
    YoutubeImportError, list_youtube_channel_videos,
    normalize_youtube_channel_url, normalize_youtube_url,
)
from app.subtitles import (
    apply_cut_ranges_to_subtitles,
    build_speech_gap_cut_ranges,
    build_subtitle_cues,
    normalize_cues,
    normalize_cut_ranges,
    remaining_duration_after_cuts,
    retime_cues_from_transcript,
)


ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm"}
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
MANUAL_MIN_CLIP_SECONDS = 1.0
END_TIME_TOLERANCE_SECONDS = 1.0
MIN_INSERT_RANGE_SECONDS = 0.1
MAX_INSERT_RANGES = 20


def format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            precision = 0 if unit in {"B", "KB"} else 1
            return f"{amount:.{precision}f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TB"


def required_upload_space(content_length: int, reserve_bytes: int) -> int:
    return max(0, content_length) + max(0, reserve_bytes)


def transcript_segments(job: Any) -> list[dict[str, Any]] | None:
    transcript_value = job["transcript_path"]
    transcript_path = Path(transcript_value) if transcript_value else None
    if transcript_path is None or not transcript_path.is_file():
        return None
    import json

    transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    return transcript["segments"]


def rebuild_subtitles_from_transcript(job: Any, start: float, end: float) -> list | None:
    segments = transcript_segments(job)
    if segments is None:
        return None
    return build_subtitle_cues(segments, start, end)


def retime_subtitles_from_transcript(
    job: Any,
    cues: list[SubtitleCue],
    start: float,
    end: float,
) -> list | None:
    segments = transcript_segments(job)
    if segments is None:
        return None
    return retime_cues_from_transcript(cues, segments, start, end)


def speech_gap_cut_ranges_from_transcript(job: Any, start: float, end: float) -> list[CutRange] | None:
    segments = transcript_segments(job)
    if segments is None:
        return None
    return build_speech_gap_cut_ranges(segments, start, end)


def manual_clip_title(subtitles: list[SubtitleCue], rank: int) -> str:
    text = " ".join(cue.text.strip() for cue in subtitles if cue.text.strip())
    words = text.split()
    if not words:
        return f"Manuel bölüm {rank}"
    title = " ".join(words[:10])
    return f"{title}…" if len(words) > 10 else title


def validate_cut_ranges(
    cut_ranges: list[CutRange],
    start: float,
    end: float,
) -> list[CutRange]:
    for cut in cut_ranges:
        if cut.start < start or cut.end > end or cut.end <= cut.start:
            raise HTTPException(
                status_code=422,
                detail="Kesilecek aralıklar klip başlangıç ve bitişi içinde olmalıdır.",
            )
    normalized = normalize_cut_ranges(cut_ranges, start, end)
    if remaining_duration_after_cuts(start, end, normalized) < MANUAL_MIN_CLIP_SECONDS:
        raise HTTPException(
            status_code=422,
            detail="Kesitler çıkarıldıktan sonra klip süresi en az 1 saniye kalmalıdır.",
        )
    return normalized


def validate_insert_ranges(
    insert_ranges: list[InsertRange],
    *,
    clip_start: float,
    clip_end: float,
    source_duration: float,
    cut_ranges: list[CutRange],
) -> list[InsertRange]:
    if len(insert_ranges) > MAX_INSERT_RANGES:
        raise HTTPException(
            status_code=422,
            detail=f"Bir klibe en fazla {MAX_INSERT_RANGES} parça eklenebilir.",
        )
    normalized: list[InsertRange] = []
    for insert in insert_ranges:
        if (
            insert.source_end - insert.source_start < MIN_INSERT_RANGE_SECONDS
            or insert.source_end > source_duration
        ):
            raise HTTPException(
                status_code=422,
                detail="Eklenecek kaynak aralığı video süresi içinde ve en az 0.1 saniye olmalıdır.",
            )
        if not clip_start <= insert.insert_at <= clip_end:
            raise HTTPException(
                status_code=422,
                detail="Yerleştirme noktası hedef klibin zaman aralığı içinde olmalıdır.",
            )
        if any(cut.start < insert.insert_at < cut.end for cut in cut_ranges):
            raise HTTPException(
                status_code=422,
                detail="Yerleştirme noktası kesilecek bir aralığın içinde olamaz.",
            )
        normalized.append(
            InsertRange(
                source_start=round(insert.source_start, 3),
                source_end=round(insert.source_end, 3),
                insert_at=round(insert.insert_at, 3),
            )
        )
    return [
        value
        for _, value in sorted(
            enumerate(normalized),
            key=lambda item: (item[1].insert_at, item[0]),
        )
    ]


def create_router(database: Database, work_queue: WorkQueue, settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api", route_class=SafeUploadRoute)

    @router.post("/uploads/check")
    def check_upload(payload: UploadPreflightRequest) -> dict[str, bool]:
        validate_upload_space(settings, payload.size_bytes + payload.file_count * 1024)
        return {"ok": True}

    @router.post("/videos/pick-local")
    def pick_local_videos(request: Request) -> dict[str, list[str]]:
        require_local_request(request)
        try:
            return {"paths": choose_video_paths()}
        except PickerError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @router.get("/health")
    def health() -> dict[str, object]:
        missing = settings.validate_binaries()
        return {"ok": not missing, "missing": missing}

    @router.get("/analysis/status")
    def analysis_status() -> dict[str, Any]:
        return codex_status(settings)

    @router.get("/branding/subscription-avatar")
    def subscription_avatar() -> FileResponse:
        if not AVATAR_PATH.is_file():
            raise HTTPException(status_code=404, detail="Kanal fotoğrafı bulunamadı.")
        return FileResponse(AVATAR_PATH, media_type="image/jpeg")

    @router.get("/branding/subscription-preview")
    def subscription_preview(
        channel_name: str = Query(default="Oban Otağı", max_length=50),
        text: str = Query(default="", max_length=90),
        show_tagline: bool = False, use_photo: bool = True, click_effect: bool = True,
        duration: float = Query(default=4.2, ge=1, le=5, allow_inf_nan=False),
        animation: str = Query(default="slide", pattern="^(fade|slide)$"),
        position: str = Query(default="above_subtitles", pattern="^(above_subtitles|top)$"),
        animated: bool = True,
    ) -> Response:
        card = SubscriptionSettings(
            enabled=True, channel_name=channel_name.strip() or "Oban Otağı", text=text,
            show_tagline=show_tagline, use_photo=use_photo, click_effect=click_effect,
            duration=duration, animation=animation, position=position,
        )
        return Response(render_card_preview(card, animated), media_type="image/webp",
            headers={"Cache-Control": "no-store"})

    @router.get("/jobs", response_model=list[JobSummary])
    def list_jobs() -> list[JobSummary]:
        return database.list_jobs()

    @router.post("/youtube/channel/videos", response_model=YoutubeChannelBrowseResponse)
    def browse_youtube_channel(payload: YoutubeChannelBrowseRequest) -> dict[str, Any]:
        try:
            url = normalize_youtube_channel_url(payload.url)
        except YoutubeImportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            result = asdict(list_youtube_channel_videos(url, limit=30, offset=payload.offset))
            result["next_offset"] = payload.offset + 30 if result["has_more"] and payload.offset + 30 <= 10000 else None
            return result
        except YoutubeImportError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.post("/jobs/youtube", response_model=JobDetail, status_code=status.HTTP_202_ACCEPTED)
    def create_youtube_job(payload: YoutubeSourceRequest) -> JobDetail:
        try:
            url = normalize_youtube_url(payload.url)
        except YoutubeImportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if shutil.disk_usage(settings.data_dir).free < settings.minimum_free_bytes:
            raise HTTPException(
                status_code=507,
                detail=f"YouTube videosu için en az {format_bytes(settings.minimum_free_bytes)} boş disk alanı gerekiyor.",
            )
        job_id = str(uuid.uuid4())
        job_dir = settings.data_dir / "jobs" / job_id
        try:
            job_dir.mkdir(parents=True)
            database.create_job(
                job_id, f"YouTube · {url.rsplit('=', 1)[-1]}", job_dir / "youtube-source.mp4",
                source_type="youtube", source_url=url,
            )
            database.update_job(job_id, stage="YouTube içe aktarma sırada")
            work_queue.enqueue_youtube_import(job_id)
        except (OSError, RuntimeError) as exc:
            database.delete_job(job_id)
            shutil.rmtree(job_dir, ignore_errors=True)
            raise HTTPException(status_code=503, detail="YouTube içe aktarma başlatılamadı. Yeniden deneyebilirsin.") from exc
        job = database.get_job(job_id)
        assert job is not None
        return job

    @router.post("/jobs", response_model=JobDetail, status_code=status.HTTP_202_ACCEPTED)
    async def create_job(request: Request, video: UploadFile = File(...)) -> JobDetail:
        original_name = Path(video.filename or "video.mp4").name
        suffix = Path(original_name).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=415,
                detail="Desteklenen dosya türleri: MP4, MOV, MKV, M4V, AVI ve WEBM.",
            )
        free_bytes = shutil.disk_usage(settings.data_dir).free
        content_length = int(request.headers.get("content-length") or 0)
        required = required_upload_space(content_length, settings.minimum_free_bytes)
        if free_bytes < required:
            raise HTTPException(
                status_code=507,
                detail=(
                    "Video için yeterli boş disk alanı yok. "
                    f"Kullanılabilir: {format_bytes(free_bytes)}, "
                    f"gereken: yaklaşık {format_bytes(required)}."
                ),
            )

        job_id = str(uuid.uuid4())
        job_dir = settings.data_dir / "jobs" / job_id
        job_dir.mkdir(parents=True)
        safe_stem = SAFE_NAME.sub("-", Path(original_name).stem).strip(".-") or "source"
        source = job_dir / f"{safe_stem}{suffix}"
        try:
            bytes_written = 0
            with source.open("wb") as destination:
                while chunk := await video.read(settings.upload_chunk_size):
                    destination.write(chunk)
                    bytes_written += len(chunk)
                    if (
                        content_length == 0
                        and bytes_written % (64 * settings.upload_chunk_size)
                        < settings.upload_chunk_size
                        and shutil.disk_usage(settings.data_dir).free
                        < settings.minimum_free_bytes
                    ):
                        raise HTTPException(
                            status_code=507,
                            detail=(
                                "Yükleme durduruldu: video işleme için ayrılması gereken "
                                f"{format_bytes(settings.minimum_free_bytes)} disk rezervi kaldı."
                            ),
                        )
            if source.stat().st_size == 0:
                raise HTTPException(status_code=400, detail="Yüklenen dosya boş.")
            database.create_job(job_id, original_name, source)
            work_queue.enqueue_analysis(job_id)
            job = database.get_job(job_id)
            assert job is not None
            return job
        except HTTPException:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise
        except OSError as exc:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise HTTPException(status_code=500, detail=f"Dosya kaydedilemedi: {exc}") from exc
        finally:
            await video.close()

    @router.get("/jobs/{job_id}", response_model=JobDetail)
    def get_job(job_id: str) -> JobDetail:
        job = database.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="İş bulunamadı.")
        return job

    @router.post(
        "/jobs/{job_id}/reanalyze",
        response_model=JobDetail,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def reanalyze_job(job_id: str) -> JobDetail:
        record = database.get_job_record(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail="İş bulunamadı.")
        if record["status"] in {"queued", "importing", "analyzing", "exporting"}:
            raise HTTPException(status_code=409, detail="İşlem devam ederken yeniden analiz yapılamaz.")
        needs_import = not Path(record["source_path"]).is_file()
        if needs_import and (record["source_type"] != "youtube" or not record["source_url"]):
            raise HTTPException(status_code=404, detail="Kaynak video bulunamadı. Dosyanın kayıtlı konumunda olduğunu kontrol et.")
        database.update_job(
            job_id,
            status="queued",
            progress=0.0,
            stage="YouTube içe aktarma sırada" if needs_import else "Konu analizi sırada",
            error=None,
        )
        try:
            if needs_import:
                work_queue.enqueue_youtube_import(job_id)
            else:
                work_queue.enqueue_analysis(job_id)
        except RuntimeError as exc:
            database.update_job(job_id, status="failed", stage="İşlem başlatılamadı", error=str(exc))
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        job = database.get_job(job_id)
        assert job is not None
        return job

    @router.get("/jobs/{job_id}/source")
    def get_source(job_id: str, request: Request) -> FileResponse:
        record = database.get_job_record(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail="İş bulunamadı.")
        path = Path(record["source_path"])
        linked = not path.is_relative_to(job_workspace(settings, job_id))
        if linked:
            require_local_request(request)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Kaynak video bulunamadı. Dosyanın kayıtlı konumunda olduğunu kontrol et.")
        media_type, _ = mimetypes.guess_type(path.name)
        headers = {"Cross-Origin-Resource-Policy": "same-origin"} if linked else None
        return FileResponse(path, media_type=media_type or "video/mp4", headers=headers)

    @router.post("/jobs/{job_id}/transcript/correct", response_model=JobDetail, status_code=202)
    def correct_job_subtitles(job_id: str) -> JobDetail:
        job = database.get_job_record(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Video bulunamadı.")
        if job["status"] in {"queued", "importing", "analyzing", "exporting"}:
            raise HTTPException(status_code=409, detail="Önce devam eden işlemin tamamlanmasını bekle.")
        if not job["transcript_path"] or not Path(job["transcript_path"]).is_file():
            raise HTTPException(status_code=422, detail="Önce konuşma metni oluşturulmalı.")
        database.update_job(job_id, status="queued", stage="Altyazı düzeltme sırada", progress=0, error=None)
        work_queue.enqueue_correction(job_id)
        return database.get_job(job_id)

    @router.post("/jobs/{job_id}/transcript/corrections/{word_id}/revert", response_model=JobDetail)
    def revert_correction(job_id: str, word_id: int) -> JobDetail:
        job = database.get_job(job_id)
        record = database.get_job_record(job_id)
        if job is None or job.correction is None:
            raise HTTPException(status_code=404, detail="Düzeltme bulunamadı.")
        if job.status in {"queued", "importing", "analyzing", "exporting"}:
            raise HTTPException(status_code=409, detail="Devam eden işlemin tamamlanmasını bekle.")
        correction = next((item for item in job.correction.corrections if item.word_id == word_id), None)
        if correction is None:
            raise HTTPException(status_code=404, detail="Düzeltme bulunamadı.")
        path = Path(record["transcript_path"])
        transcript = json.loads(path.read_text(encoding="utf-8"))
        word = next((w for segment in transcript["segments"] for w in segment["words"] if w.get("id") == word_id), None)
        if word is None or word["text"] != correction.replacement:
            raise HTTPException(status_code=409, detail="Bu kelime sonradan değiştirilmiş; otomatik geri alınmadı.")
        word["text"] = correction.original
        for segment in transcript["segments"]:
            segment["text"] = " ".join(w["text"] for w in segment["words"])
        temporary = path.with_suffix(".reverting.json")
        temporary.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        reverse = correction.model_copy(update={"original": correction.replacement, "replacement": correction.original})
        for clip in job.clips:
            database.update_clip(clip.id, subtitles=correct_saved_cues(clip.subtitles, [reverse], clip.start, clip.cut_ranges))
        report = job.correction.model_copy(update={
            "corrections": [item for item in job.correction.corrections if item.word_id != word_id],
            "fingerprint": transcript_fingerprint(transcript["segments"]),
        })
        database.update_job(job_id, correction_json=report.model_dump_json())
        return database.get_job(job_id)

    @router.post(
        "/jobs/{job_id}/clips",
        response_model=ClipCandidate,
        status_code=status.HTTP_201_CREATED,
    )
    def create_manual_clip(job_id: str, payload: ManualClipCreate) -> ClipCandidate:
        job_record = database.get_job_record(job_id)
        job = database.get_job(job_id)
        if job_record is None or job is None:
            raise HTTPException(status_code=404, detail="İş bulunamadı.")
        if job_record["status"] in {"queued", "importing", "analyzing"}:
            raise HTTPException(
                status_code=409,
                detail="Analiz tamamlanmadan manuel klip oluşturulamaz.",
            )

        source_duration = float(job_record["duration"] or 0)
        end = payload.end
        if (
            source_duration > 0
            and end > source_duration
            and end - source_duration <= END_TIME_TOLERANCE_SECONDS
        ):
            end = source_duration
        if source_duration <= 0 or payload.start >= source_duration or end > source_duration:
            raise HTTPException(status_code=422, detail="Klip zaman aralığı video süresinin dışında.")
        if end <= payload.start:
            raise HTTPException(
                status_code=422,
                detail="Bitiş zamanı başlangıçtan sonra olmalıdır.",
            )
        if end - payload.start < MANUAL_MIN_CLIP_SECONDS:
            raise HTTPException(
                status_code=422,
                detail="Klip süresi en az 1 saniye olmalıdır.",
            )

        subtitles = rebuild_subtitles_from_transcript(job_record, payload.start, end)
        if subtitles is None:
            raise HTTPException(
                status_code=422,
                detail="Manuel klip altyazıları için transkript bulunamadı.",
            )

        rank = max((clip.rank for clip in job.clips), default=0) + 1
        topic = next((topic for topic in job.analysis.topics if topic.id == payload.topic_id), None) if job.analysis else None
        if payload.topic_id and topic is None:
            raise HTTPException(status_code=404, detail="Konu bulunamadı.")
        clip = ClipCandidate(
            id=str(uuid.uuid4()),
            job_id=job_id,
            rank=rank,
            title=topic.title if topic else manual_clip_title(subtitles, rank),
            start=round(payload.start, 3),
            end=round(end, 3),
            score=0,
            reasons=["Manuel aralık"],
            subtitles=subtitles,
            selection_method="manual",
            topic_id=payload.topic_id,
            selected=True,
        )
        database.create_clip(clip)
        return clip

    def youtube_clip(job_id: str, clip_id: str):
        job, clip = database.get_job_record(job_id), database.get_clip(clip_id)
        if job is None or clip is None or clip.job_id != job_id:
            raise HTTPException(status_code=404, detail="Klip bulunamadı.")
        return job, clip

    @router.patch("/jobs/{job_id}/clips/{clip_id}/youtube", response_model=ClipCandidate)
    def save_youtube(job_id: str, clip_id: str, payload: YoutubeUpdate) -> ClipCandidate:
        job, clip = youtube_clip(job_id, clip_id)
        if clip.youtube.status in {"queued", "generating"}:
            raise HTTPException(status_code=409, detail="Öneriler hazırlanırken ayarları değiştiremezsin.")
        if payload.brief.frame_time is not None and payload.brief.frame_time >= float(job["duration"] or 0):
            raise HTTPException(status_code=422, detail="Kapak karesi kaynak videonun içinde olmalı.")
        package = YoutubePackage.model_validate({**clip.youtube.model_dump(), **payload.model_dump()})
        revision = package_revision(package)
        destination = job_workspace(settings, job_id) / "youtube" / clip.id / "thumbnail.jpg"
        if revision != clip.youtube.revision or not destination.is_file():
            try:
                render_thumbnail(Path(job["source_path"]), destination, clip, package, settings)
            except Exception as exc:
                raise HTTPException(status_code=422, detail="Kapak karesi oluşturulamadı. Zamanı ve kaynak videoyu kontrol et.") from exc
        package.revision = revision
        database.update_clip(clip_id, youtube=package)
        return database.get_clip(clip_id)

    @router.post("/jobs/{job_id}/clips/{clip_id}/youtube/generate", response_model=ClipCandidate, status_code=202)
    def generate_youtube(job_id: str, clip_id: str) -> ClipCandidate:
        job, clip = youtube_clip(job_id, clip_id)
        if job["status"] in {"queued", "importing", "analyzing", "exporting"} or clip.youtube.status in {"queued", "generating"}:
            raise HTTPException(status_code=409, detail="Devam eden işlemin tamamlanmasını bekle.")
        database.update_clip(clip_id, youtube=clip.youtube.model_copy(update={"status": "queued", "error": None}))
        work_queue.enqueue_youtube(clip_id)
        return database.get_clip(clip_id)

    @router.get("/jobs/{job_id}/clips/{clip_id}/youtube/thumbnail")
    def youtube_thumbnail(job_id: str, clip_id: str) -> FileResponse:
        job, clip = youtube_clip(job_id, clip_id)
        path = job_workspace(settings, job_id) / "youtube" / clip.id / "thumbnail.jpg"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Önce YouTube ayarlarını kaydet.")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})

    @router.get("/jobs/{job_id}/clips/{clip_id}/hooks")
    def suggest_hooks(job_id: str, clip_id: str) -> list[dict[str, str]]:
        clip = database.get_clip(clip_id)
        if clip is None or clip.job_id != job_id:
            raise HTTPException(status_code=404, detail="Klip bulunamadı.")
        return hook_suggestions(clip.subtitles)

    @router.post("/jobs/{job_id}/clips/{clip_id}/subscription-suggestions",
                 response_model=SubscriptionSuggestions)
    def suggest_subscription(
        job_id: str, clip_id: str, payload: SubscriptionSuggestRequest,
    ) -> SubscriptionSuggestions:
        job, clip = youtube_clip(job_id, clip_id)
        edit = payload.edit_settings or clip.edit_settings
        try:
            validate_edit_source(edit, float(job["duration"] or 0))
            return generate_subscription_suggestions(
                clip, edit, payload.playback_rate, transcript_segments(job) or [],
                settings, job_workspace(settings, job_id),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except CodexError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.patch("/jobs/{job_id}/clips/{clip_id}", response_model=ClipCandidate)
    def update_clip(job_id: str, clip_id: str, payload: ClipUpdate) -> ClipCandidate:
        job = database.get_job_record(job_id)
        clip = database.get_clip(clip_id)
        if job is None or clip is None or clip.job_id != job_id:
            raise HTTPException(status_code=404, detail="Klip bulunamadı.")
        start = payload.start if payload.start is not None else clip.start
        end = payload.end if payload.end is not None else clip.end
        source_duration = float(job["duration"] or 0)
        if (
            source_duration > 0
            and end > source_duration
            and end - source_duration <= END_TIME_TOLERANCE_SECONDS
        ):
            end = source_duration
        duration = end - start
        if start < 0 or (source_duration > 0 and end > source_duration) or end <= start:
            raise HTTPException(status_code=422, detail="Klip zaman aralığı geçersiz.")
        if duration < MANUAL_MIN_CLIP_SECONDS:
            raise HTTPException(
                status_code=422,
                detail="Klip süresi en az 1 saniye olmalıdır.",
            )

        try:
            validate_edit_source(payload.edit_settings or clip.edit_settings, source_duration)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        requested_cut_ranges = payload.cut_ranges if payload.cut_ranges is not None else clip.cut_ranges
        if payload.auto_cut_silence:
            auto_cut_ranges = speech_gap_cut_ranges_from_transcript(job, start, end)
            if auto_cut_ranges is None:
                raise HTTPException(
                    status_code=422,
                    detail="Konuşmasız kısımlar için transkript bulunamadı.",
                )
            if not auto_cut_ranges:
                raise HTTPException(
                    status_code=422,
                    detail="Kesilecek uzun konuşmasız bölüm bulunamadı.",
                )
            requested_cut_ranges = [*requested_cut_ranges, *auto_cut_ranges]
        cut_ranges = validate_cut_ranges(requested_cut_ranges, start, end)
        requested_insert_ranges = (
            payload.insert_ranges if payload.insert_ranges is not None else clip.insert_ranges
        )
        insert_ranges = validate_insert_ranges(
            requested_insert_ranges,
            clip_start=start,
            clip_end=end,
            source_duration=source_duration,
            cut_ranges=cut_ranges,
        )
        bounds_changed = start != clip.start or end != clip.end
        cut_ranges_changed = cut_ranges != clip.cut_ranges
        if payload.reset_subtitles and payload.subtitles is not None:
            source_subtitles = retime_subtitles_from_transcript(job, payload.subtitles, start, end)
            if source_subtitles is None:
                source_subtitles = normalize_cues(payload.subtitles, start, end)
            subtitles = apply_cut_ranges_to_subtitles(source_subtitles, start, end, cut_ranges)
        elif bounds_changed or cut_ranges_changed or payload.reset_subtitles:
            source_subtitles = rebuild_subtitles_from_transcript(job, start, end)
            if source_subtitles is None:
                source_subtitles = normalize_cues(clip.subtitles, start, end)
            subtitles = apply_cut_ranges_to_subtitles(source_subtitles, start, end, cut_ranges)
        elif payload.subtitles is not None:
            subtitles = normalize_cues(payload.subtitles, start, end)
        else:
            subtitles = clip.subtitles

        values: dict[str, object] = {
            "start": round(start, 3),
            "end": round(end, 3),
            "subtitles": subtitles,
            "cut_ranges": cut_ranges,
            "insert_ranges": insert_ranges,
            "crop_keyframes": [],
        }
        next_framing_mode = payload.framing_mode if payload.framing_mode is not None else clip.framing_mode
        next_face_tracking = (
            payload.face_tracking_enabled
            if payload.face_tracking_enabled is not None
            else clip.face_tracking_enabled
        )
        if payload.selected is not None:
            values["selected"] = payload.selected
        if payload.edit_settings is not None:
            values["edit_settings"] = payload.edit_settings
        if payload.framing_mode is not None:
            values["framing_mode"] = payload.framing_mode
        if payload.face_tracking_enabled is not None or next_framing_mode != "fill":
            values["face_tracking_enabled"] = bool(next_framing_mode == "fill" and next_face_tracking)
        database.update_clip(clip_id, **values)
        updated = database.get_clip(clip_id)
        assert updated is not None
        return updated

    @router.post(
        "/jobs/{job_id}/exports",
        response_model=ExportResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def create_exports(job_id: str, payload: ExportRequest) -> ExportResponse:
        job = database.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="İş bulunamadı.")
        clip_map = {clip.id: clip for clip in job.clips}
        unique_ids = list(dict.fromkeys(payload.clip_ids))
        if any(clip_id not in clip_map for clip_id in unique_ids):
            raise HTTPException(status_code=404, detail="Kliplerden biri bulunamadı.")
        try:
            for clip_id in unique_ids:
                validate_edit_source(clip_map[clip_id].edit_settings, float(job.duration or 0))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        export_ids: list[str] = []
        for clip_id in unique_ids:
            export_id = str(uuid.uuid4())
            database.create_export(
                export_id,
                job_id,
                clip_id,
                llm_seo_enabled=payload.llm_seo_enabled,
                playback_rate=payload.playback_rate,
                subtitle_margin_v=payload.subtitle_margin_v,
                subtitle_font_family=payload.subtitle_font_family,
                balanced_vertical_offset=payload.balanced_vertical_offset,
                edit_settings=clip_map[clip_id].edit_settings,
                is_preview=payload.is_preview,
                youtube=clip_map[clip_id].youtube,
            )
            export_ids.append(export_id)
            work_queue.enqueue_export(export_id)
        updated = database.get_job(job_id)
        assert updated is not None
        exports = [export for export in updated.exports if export.id in export_ids]
        return ExportResponse(exports=exports)

    @router.get("/exports/{export_id}/download")
    def download_export(export_id: str) -> FileResponse:
        record = database.get_export_record(export_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Dışa aktarma bulunamadı.")
        if record["status"] != "completed" or not record["path"]:
            raise HTTPException(status_code=409, detail="Video henüz hazır değil.")
        path = Path(record["path"])
        if not path.exists():
            raise HTTPException(status_code=404, detail="Çıktı dosyası bulunamadı.")
        return FileResponse(path, media_type="video/mp4", filename=path.name)

    @router.get("/exports/{export_id}/metadata")
    def download_export_metadata(export_id: str) -> FileResponse:
        record = database.get_export_record(export_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Dışa aktarma bulunamadı.")
        if record["status"] != "completed" or not record["path"]:
            raise HTTPException(status_code=409, detail="SEO dosyası henüz hazır değil.")
        path = Path(record["path"]).with_name("youtube-seo.md")
        if not path.exists():
            raise HTTPException(status_code=404, detail="SEO dosyası bulunamadı.")
        return FileResponse(path, media_type="text/markdown", filename=path.name)

    @router.get("/exports/{export_id}/package")
    def download_package(export_id: str) -> FileResponse:
        record = database.get_export_record(export_id)
        if record is None or record["status"] != "completed" or not record["path"]:
            raise HTTPException(status_code=404, detail="Paket henüz hazır değil.")
        path = Path(record["path"]).parent / "youtube-package.zip"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Bu eski çıktıda paket yok. Yeniden dışa aktar.")
        return FileResponse(path, media_type="application/zip", filename=f"{Path(record['path']).stem}-youtube.zip")

    @router.get("/exports/{export_id}/thumbnail")
    def download_thumbnail(export_id: str) -> FileResponse:
        record = database.get_export_record(export_id)
        if record is None or record["status"] != "completed" or not record["path"]:
            raise HTTPException(status_code=404, detail="Kapak henüz hazır değil.")
        path = Path(record["path"]).parent / "thumbnail.jpg"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Kapak bulunamadı.")
        return FileResponse(path, media_type="image/jpeg", filename="thumbnail.jpg")

    @router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_job(job_id: str, request: Request) -> None:
        record = database.get_job_record(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail="İş bulunamadı.")
        if record["status"] in {"queued", "importing", "analyzing", "exporting"}:
            raise HTTPException(status_code=409, detail="Devam eden bir iş silinemez.")
        if request.app.state.speech_cleanup.has_active_work(job_id):
            raise HTTPException(status_code=409, detail="Konuşma temizleme işlemi devam eden proje silinemez.")
        if request.app.state.projects.is_video_busy(job_id):
            raise HTTPException(status_code=409, detail="Birleştirme işleminde kullanılan video silinemez.")
        try:
            job_workspace(settings, job_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        database.delete_job(job_id)
        remove_job_workspace(settings, job_id)

    return router
