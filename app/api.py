from __future__ import annotations

import mimetypes
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse

from app.config import Settings
from app.database import Database
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
)
from app.queue import WorkQueue
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
    router = APIRouter(prefix="/api")

    @router.get("/health")
    def health() -> dict[str, object]:
        missing = settings.validate_binaries()
        return {"ok": not missing, "missing": missing}

    @router.get("/jobs", response_model=list[JobSummary])
    def list_jobs() -> list[JobSummary]:
        return database.list_jobs()

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
        if record["status"] in {"queued", "analyzing", "exporting"}:
            raise HTTPException(status_code=409, detail="İşlem devam ederken yeniden analiz yapılamaz.")
        if not Path(record["source_path"]).exists():
            raise HTTPException(status_code=404, detail="Kaynak video bulunamadı.")
        database.update_job(
            job_id,
            status="queued",
            progress=0.0,
            stage="Konu analizi sırada",
            error=None,
        )
        work_queue.enqueue_analysis(job_id)
        job = database.get_job(job_id)
        assert job is not None
        return job

    @router.get("/jobs/{job_id}/source")
    def get_source(job_id: str) -> FileResponse:
        record = database.get_job_record(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail="İş bulunamadı.")
        path = Path(record["source_path"])
        if not path.exists():
            raise HTTPException(status_code=404, detail="Kaynak video bulunamadı.")
        media_type, _ = mimetypes.guess_type(path.name)
        return FileResponse(path, media_type=media_type or "video/mp4", filename=None)

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
        if job_record["status"] in {"queued", "analyzing"}:
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
        clip = ClipCandidate(
            id=str(uuid.uuid4()),
            job_id=job_id,
            rank=rank,
            title=manual_clip_title(subtitles, rank),
            start=round(payload.start, 3),
            end=round(end, 3),
            score=0,
            reasons=["Manuel aralık"],
            subtitles=subtitles,
            selection_method="manual",
            selected=True,
        )
        database.create_clip(clip)
        return clip

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

    @router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_job(job_id: str) -> None:
        record = database.get_job_record(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail="İş bulunamadı.")
        if record["status"] in {"queued", "analyzing", "exporting"}:
            raise HTTPException(status_code=409, detail="Devam eden bir iş silinemez.")
        job_dir = Path(record["source_path"]).parent
        database.delete_job(job_id)
        shutil.rmtree(job_dir, ignore_errors=True)

    return router
