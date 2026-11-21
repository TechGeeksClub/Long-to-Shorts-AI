from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import shutil
import subprocess
import threading
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.config import Settings
from app.database import Database, utc_now
from app.job_workspace import speech_transcript_path
from app.media import MediaError, extract_audio, run_command
from app.models import CutRange
from app.speech_detection import SpeechActivityError, detect_speech_activity
from app.speech_storage import remove_speech_export_partials, speech_export_workspace
from app.subtitles import kept_ranges_after_cuts, normalize_cut_ranges
from app.transcription import TranscriptionError, read_transcript_segments, read_transcript_text, transcribe


logger = logging.getLogger(__name__)
SpeechMode = Literal["cut", "mute"]
ACTIVE_SOURCE_STATUSES = {"queued", "importing", "analyzing"}
RENDER_BATCH_SIZE = 8
MUTE_AUDIO_BIT_RATE = 192_000
MUTE_PADDING_BEFORE_SECONDS = 0.25
MUTE_PADDING_AFTER_SECONDS = 0.4
MUTE_MERGE_GAP_SECONDS = 0.15
NON_SPEECH_TEXT = re.compile(r"^(?:\[[^\]]*\]|\([^)]*\)|[♪♫].*)$")


class SpeechCleanupError(ValueError):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


class SpeechRegion(BaseModel):
    id: str
    start: float
    end: float
    text: str
    manual: bool = False


class SpeechExport(BaseModel):
    id: str
    status: Literal["queued", "rendering", "completed", "failed"]
    progress: float
    error: str | None = None
    filename: str | None = None
    duration: float
    mode: SpeechMode
    created_at: datetime


class SpeechCleanupState(BaseModel):
    job_id: str
    transcript_available: bool = False
    status: Literal["idle", "queued", "detecting", "ready", "failed"] = "idle"
    progress: float = 0
    error: str | None = None
    duration: float | None = None
    regions: list[SpeechRegion] = Field(default_factory=list)
    removed_region_ids: list[str] = Field(default_factory=list)
    mute_ranges: list[CutRange] = Field(default_factory=list)
    mute_padding_before: float = MUTE_PADDING_BEFORE_SECONDS
    mute_padding_after: float = MUTE_PADDING_AFTER_SECONDS
    exports: list[SpeechExport] = Field(default_factory=list)


class SpeechSelectionUpdate(BaseModel):
    removed_region_ids: list[str] = Field(max_length=100000)


class SpeechManualRegionRequest(BaseModel):
    start: float
    end: float
    text: str = Field(default="", max_length=500)


class SpeechExportRequest(BaseModel):
    mode: SpeechMode = "mute"


def build_speech_regions(
    segments: list[dict[str, Any]], duration: float, *, gap_seconds: float = 0.5,
    max_region_seconds: float = 15,
) -> list[SpeechRegion]:
    """Group actual timed words, keeping long pauses out of removable speech."""
    tokens: list[tuple[float, float, str]] = []
    for segment in segments:
        token_count = len(tokens)
        words = segment.get("words") or []
        for item in words or [segment]:
            try:
                start, end = float(item["start"]), float(item["end"])
            except (KeyError, TypeError, ValueError):
                continue
            text = str(item.get("text") or "").strip()
            if not text or NON_SPEECH_TEXT.fullmatch(text) or not math.isfinite(start) or not math.isfinite(end):
                continue
            start, end = max(0, start), min(duration, end)
            if end > start:
                tokens.append((start, end, text))
        # Some valid utterances have no usable per-word timings. Retain their
        # segment rather than silently losing the entire spoken sentence.
        if words and len(tokens) == token_count:
            try:
                start, end = float(segment["start"]), float(segment["end"])
            except (KeyError, TypeError, ValueError):
                continue
            text = str(segment.get("text") or "").strip()
            if text and not NON_SPEECH_TEXT.fullmatch(text) and math.isfinite(start) and math.isfinite(end):
                start, end = max(0, start), min(duration, end)
                if end > start:
                    tokens.append((start, end, text))
    tokens.sort(key=lambda item: (item[0], item[1]))
    groups: list[tuple[float, float, list[str]]] = []
    for start, end, text in tokens:
        if groups:
            previous_start, previous_end, previous_text = groups[-1]
            if start < previous_end or (
                start - previous_end <= gap_seconds
                and end - previous_start <= max_region_seconds
            ):
                groups[-1] = (previous_start, max(previous_end, end), [*previous_text, text])
                continue
        groups.append((start, end, [text]))
    return [
        SpeechRegion(
            id=hashlib.sha256(f"{start:.3f}:{end:.3f}".encode()).hexdigest()[:20],
            start=round(start, 3), end=round(end, 3), text=" ".join(text),
        )
        for start, end, text in groups if round(end, 3) > round(start, 3)
    ]


def _public_export(row: Any) -> SpeechExport:
    return SpeechExport(
        id=row["id"], status=row["status"], progress=row["progress"], error=row["error"],
        filename=Path(row["path"]).name if row["path"] and row["status"] == "completed" else None,
        duration=row["duration"], mode=row["mode"], created_at=row["created_at"],
    )


def build_mute_ranges(
    regions: list[SpeechRegion], removed_ids: list[str], duration: float,
) -> list[CutRange]:
    """Cover word timing errors and short gaps without altering saved selections."""
    selected = set(removed_ids)
    padded = normalize_cut_ranges([
        CutRange(start=max(0, region.start - MUTE_PADDING_BEFORE_SECONDS),
                 end=min(duration, region.end + MUTE_PADDING_AFTER_SECONDS))
        for region in regions if region.id in selected
    ], 0, duration)
    protected = normalize_cut_ranges([
        CutRange(start=region.start, end=region.end)
        for region in regions if region.id not in selected
    ], 0, duration)
    bounded: list[CutRange] = []
    for cut in padded:
        local_protected = normalize_cut_ranges(protected, cut.start, cut.end)
        bounded.extend(CutRange(start=start, end=end) for start, end in
                       kept_ranges_after_cuts(cut.start, cut.end, local_protected))
    merged: list[CutRange] = []
    for cut in bounded:
        previous = merged[-1] if merged else None
        if previous and cut.start - previous.end <= MUTE_MERGE_GAP_SECONDS and not any(
            region.id not in selected and region.start < cut.start and region.end > previous.end
            for region in regions
        ):
            merged[-1] = CutRange(start=previous.start, end=cut.end)
        else:
            merged.append(cut)
    # Always mute the core of an explicit manual selection, including overlaps
    # with an unselected detected speech region.
    manual = [CutRange(start=region.start, end=region.end)
              for region in regions if region.manual and region.id in selected]
    combined: list[CutRange] = []
    for cut in sorted([*merged, *manual], key=lambda item: item.start):
        if combined and cut.start <= combined[-1].end:
            combined[-1] = CutRange(start=combined[-1].start, end=max(combined[-1].end, cut.end))
        else:
            combined.append(cut)
    return combined


def include_speech_activity(
    regions: list[SpeechRegion], activity: list[tuple[float, float]], duration: float,
) -> list[SpeechRegion]:
    """Keep recognized words and add speech that never became transcript text."""
    refined = [region.model_copy() for region in regions]
    acoustic = normalize_cut_ranges([
        CutRange(start=max(0, start), end=min(duration, end))
        for start, end in activity if math.isfinite(start) and math.isfinite(end)
        and min(duration, end) > max(0, start)
    ], 0, duration)
    # Extend nearby word boundaries to the detected voice edge. The stable word
    # IDs keep existing individual selections valid after a more precise scan.
    for voice in acoustic:
        overlapping = [region for region in refined if region.start < voice.end and region.end > voice.start]
        if overlapping:
            first, last = overlapping[0], overlapping[-1]
            if 0 < first.start - voice.start <= 0.5:
                first.start = voice.start
            if 0 < voice.end - last.end <= 0.5:
                last.end = voice.end
    coverage = normalize_cut_ranges([CutRange(start=r.start, end=r.end) for r in refined], 0, duration)
    extra: list[SpeechRegion] = []
    for voice in acoustic:
        local_coverage = normalize_cut_ranges(coverage, voice.start, voice.end)
        for start, end in kept_ranges_after_cuts(voice.start, voice.end, local_coverage):
            if end - start < 0.08:
                continue
            while start < end:
                stop = min(end, start + 15)
                extra.append(SpeechRegion(
                    id=hashlib.sha256(f"activity:{start:.3f}:{stop:.3f}".encode()).hexdigest()[:20],
                    start=round(start, 3), end=round(stop, 3), text="Konuşma (metin çözümlenemedi)",
                ))
                start = stop
    return sorted([*refined, *extra], key=lambda region: (region.start, region.end))


def build_mute_expression(cuts: list[CutRange]) -> str:
    """Keep FFmpeg's expression depth bounded for thousands of speech ranges."""
    terms = [f"between(t,{cut.start:.6f},{cut.end:.6f})" for cut in cuts]
    while len(terms) > 1:
        terms = [
            f"({terms[index]}+{terms[index + 1]})" if index + 1 < len(terms) else terms[index]
            for index in range(0, len(terms), 2)
        ]
    return terms[0] if terms else "0"


def mute_export_space_required(source: Path, duration: float, reserve: int) -> int:
    # The original file is a conservative upper bound for copied video bytes.
    return source.stat().st_size + math.ceil(duration * MUTE_AUDIO_BIT_RATE / 8) + max(0, reserve)


class SpeechCleanupService:
    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings
        self._lock = threading.RLock()
        self._cancelled = threading.Event()
        self._process: subprocess.Popen[str] | None = None

    def initialize(self) -> None:
        with self.database.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS speech_cleanup (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'idle', progress REAL NOT NULL DEFAULT 0,
                    error TEXT, duration REAL, regions_json TEXT NOT NULL DEFAULT '[]',
                    removed_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE IF NOT EXISTS speech_cleanup_exports (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    mode TEXT NOT NULL, status TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0,
                    error TEXT, duration REAL NOT NULL, source_duration REAL NOT NULL,
                    ranges_json TEXT NOT NULL, path TEXT, created_at TEXT NOT NULL
                );
            """)

    def interrupt_active(self) -> None:
        with self.database.connect() as connection:
            interrupted_exports = connection.execute(
                "SELECT id, job_id FROM speech_cleanup_exports WHERE status='rendering'"
            ).fetchall()
            connection.execute(
                "UPDATE speech_cleanup SET status='failed', error=?, progress=0 "
                "WHERE status IN ('queued','detecting')",
                ("Konuşma tespiti uygulama kapanırken yarıda kaldı. Yeniden başlatabilirsin.",),
            )
            connection.execute(
                "UPDATE speech_cleanup_exports SET status='failed', error=? "
                "WHERE status IN ('queued','rendering')",
                ("Konuşma temizleme çıktısı uygulama kapanırken yarıda kaldı.",),
            )
        for export in interrupted_exports:
            locations = (False, True) if self.settings.speech_export_dir is not None else (True,)
            for default_location in locations:
                try:
                    output_dir = speech_export_workspace(
                        self.settings, export["job_id"], export["id"], default_location=default_location,
                    )
                    remove_speech_export_partials(output_dir)
                except (OSError, ValueError) as exc:
                    logger.warning("Could not remove partial speech export %s: %s", export["id"], exc)

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                self._process.terminate()

    def _job(self, job_id: str) -> Any:
        job = self.database.get_job_record(job_id)
        if job is None:
            raise SpeechCleanupError("Proje bulunamadı.", 404)
        return job

    def _source_ready(self, job_id: str) -> Any:
        job = self._job(job_id)
        if job["status"] in ACTIVE_SOURCE_STATUSES:
            raise SpeechCleanupError("Kaynak videonun hazırlanması tamamlanınca bu işlemi başlatabilirsin.")
        if not Path(job["source_path"]).is_file():
            raise SpeechCleanupError("Kaynak video bulunamadı. Dosyanın kayıtlı konumunda olduğunu kontrol et.", 404)
        return job

    def has_active_work(self, job_id: str) -> bool:
        with self.database.connect() as connection:
            detection = connection.execute(
                "SELECT 1 FROM speech_cleanup WHERE job_id=? AND status IN ('queued','detecting')",
                (job_id,),
            ).fetchone()
            rendering = connection.execute(
                "SELECT 1 FROM speech_cleanup_exports WHERE job_id=? AND status IN ('queued','rendering')",
                (job_id,),
            ).fetchone()
        return bool(detection or rendering)

    def state(self, job_id: str) -> SpeechCleanupState:
        self._job(job_id)
        transcript_available = bool(read_transcript_text(speech_transcript_path(self.settings, job_id)))
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM speech_cleanup WHERE job_id=?", (job_id,)).fetchone()
            exports = connection.execute(
                "SELECT * FROM speech_cleanup_exports WHERE job_id=? ORDER BY created_at DESC", (job_id,),
            ).fetchall()
        if row is None:
            return SpeechCleanupState(
                job_id=job_id, transcript_available=transcript_available,
                exports=[_public_export(item) for item in exports],
            )
        regions = [SpeechRegion(**item) for item in json.loads(row["regions_json"])]
        removed_ids = json.loads(row["removed_json"])
        return SpeechCleanupState(
            job_id=job_id, status=row["status"], progress=row["progress"], error=row["error"],
            duration=row["duration"], regions=regions,
            transcript_available=transcript_available,
            removed_region_ids=removed_ids,
            mute_ranges=build_mute_ranges(regions, removed_ids, row["duration"]) if row["duration"] else [],
            exports=[_public_export(item) for item in exports],
        )

    def queue_detection(self, job_id: str, enqueue: Callable[[str], None]) -> SpeechCleanupState:
        with self._lock:
            self._source_ready(job_id)
            if self.has_active_work(job_id):
                raise SpeechCleanupError("Bu projede konuşma temizleme işlemi zaten devam ediyor.")
            with self.database.connect() as connection:
                connection.execute(
                    "INSERT INTO speech_cleanup(job_id,status) VALUES (?,'queued') "
                    "ON CONFLICT(job_id) DO UPDATE SET status='queued',progress=0,error=NULL", (job_id,),
                )
            try:
                enqueue(job_id)
            except RuntimeError as exc:
                self._update_detection(job_id, status="failed", error=str(exc))
                raise SpeechCleanupError(str(exc), 503) from exc
        return self.state(job_id)

    def save_selection(self, job_id: str, removed_ids: list[str]) -> SpeechCleanupState:
        with self._lock:
            state = self.state(job_id)
            if state.status != "ready":
                raise SpeechCleanupError("Önce videodaki konuşmaları tespit et.")
            known = {region.id for region in state.regions}
            if set(removed_ids) - known:
                raise SpeechCleanupError("Seçimde bu projeye ait olmayan konuşma aralığı var.", 422)
            removed_set = set(removed_ids)
            ordered = [region.id for region in state.regions if region.id in removed_set]
            with self.database.connect() as connection:
                connection.execute("UPDATE speech_cleanup SET removed_json=? WHERE job_id=?",
                                   (json.dumps(ordered), job_id))
        return self.state(job_id)

    def add_manual_region(
        self, job_id: str, payload: SpeechManualRegionRequest,
    ) -> SpeechCleanupState:
        with self._lock:
            job = self._source_ready(job_id)
            state = self.state(job_id)
            if state.status in {"queued", "detecting"}:
                raise SpeechCleanupError("Konuşma tespiti tamamlanınca elle aralık ekleyebilirsin.")
            if not math.isfinite(payload.start) or not math.isfinite(payload.end):
                raise SpeechCleanupError("Başlangıç ve bitiş geçerli bir zaman olmalı.", 422)
            if payload.start < 0 or payload.end <= payload.start:
                raise SpeechCleanupError("Başlangıç sıfırdan küçük olamaz; bitiş başlangıçtan sonra olmalı.", 422)
            if job["has_audio"] is not None and not job["has_audio"]:
                raise SpeechCleanupError("Bu videoda ses akışı yok; susturulacak bir aralık eklenemez.", 422)
            duration = state.duration or job["duration"]
            if not duration or not math.isfinite(duration) or duration <= 0 or job["has_audio"] is None:
                try:
                    metadata = json.loads(run_command([
                        self.settings.ffprobe, "-v", "error", "-show_streams", "-show_format",
                        "-of", "json", str(job["source_path"]),
                    ]).stdout)
                    video = next((stream for stream in metadata.get("streams", [])
                                  if stream.get("codec_type") == "video"), None)
                    if video is None:
                        raise ValueError("Dosyada video akışı bulunamadı.")
                    duration = float(metadata.get("format", {}).get("duration")
                                     or video.get("duration") or 0)
                    if not math.isfinite(duration) or duration <= 0:
                        raise ValueError("Video süresi belirlenemedi.")
                    has_audio = any(stream.get("codec_type") == "audio"
                                    for stream in metadata.get("streams", []))
                except (MediaError, OSError, ValueError, TypeError) as exc:
                    raise SpeechCleanupError("Video süresi belirlenemedi. Kaynak videoyu kontrol et.", 422) from exc
                if not has_audio:
                    raise SpeechCleanupError("Bu videoda ses akışı yok; susturulacak bir aralık eklenemez.", 422)
                self.database.update_job(job_id, duration=duration, has_audio=1)
            if payload.end > duration:
                raise SpeechCleanupError("Elle eklenen aralık video süresini aşamaz.", 422)
            region = SpeechRegion(
                id=f"manual-{uuid.uuid4()}", start=payload.start, end=payload.end,
                text=payload.text.strip() or "Elle eklenen aralık", manual=True,
            )
            regions = sorted([*state.regions, region], key=lambda item: (item.start, item.end))
            selected = {*state.removed_region_ids, region.id}
            with self.database.connect() as connection:
                connection.execute(
                    "INSERT INTO speech_cleanup "
                    "(job_id,status,progress,error,duration,regions_json,removed_json) "
                    "VALUES (?,'ready',1,NULL,?,?,?) ON CONFLICT(job_id) DO UPDATE SET "
                    "status='ready',progress=1,error=NULL,duration=excluded.duration,"
                    "regions_json=excluded.regions_json,removed_json=excluded.removed_json",
                    (job_id, duration,
                     json.dumps([item.model_dump() for item in regions], ensure_ascii=False),
                     json.dumps([item.id for item in regions if item.id in selected])),
                )
        return self.state(job_id)

    def delete_manual_region(self, job_id: str, region_id: str) -> SpeechCleanupState:
        with self._lock:
            state = self.state(job_id)
            if state.status in {"queued", "detecting"}:
                raise SpeechCleanupError("Konuşma tespiti tamamlanınca elle eklenen aralığı silebilirsin.")
            region = next((item for item in state.regions if item.id == region_id), None)
            if region is None:
                raise SpeechCleanupError("Elle eklenen aralık bulunamadı.", 404)
            if not region.manual:
                raise SpeechCleanupError("Yalnızca elle eklenen aralıklar silinebilir.", 422)
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE speech_cleanup SET regions_json=?,removed_json=? WHERE job_id=?",
                    (json.dumps([item.model_dump() for item in state.regions if item.id != region_id],
                                ensure_ascii=False),
                     json.dumps([item_id for item_id in state.removed_region_ids if item_id != region_id]),
                     job_id),
                )
        return self.state(job_id)

    def queue_export(
        self, job_id: str, mode: SpeechMode, enqueue: Callable[[str], None],
    ) -> SpeechExport:
        with self._lock:
            job = self._source_ready(job_id)
            if job["has_audio"] is not None and not job["has_audio"]:
                raise SpeechCleanupError("Bu videoda ses akışı yok; konuşma temizleme çıktısı oluşturulamaz.", 422)
            state = self.state(job_id)
            if state.status != "ready" or not state.duration:
                raise SpeechCleanupError("Önce videodaki konuşmaları tespit et.")
            if self.has_active_work(job_id):
                raise SpeechCleanupError("Bu projede konuşma temizleme işlemi zaten devam ediyor.")
            selected = set(state.removed_region_ids)
            cuts = build_mute_ranges(state.regions, state.removed_region_ids, state.duration) if mode == "mute" else normalize_cut_ranges([
                CutRange(start=region.start, end=region.end)
                for region in state.regions if region.id in selected
            ], 0, state.duration)
            if not cuts:
                raise SpeechCleanupError("Temizlemek için en az bir konuşma aralığı seç.", 422)
            kept = kept_ranges_after_cuts(0, state.duration, cuts)
            duration = state.duration if mode == "mute" else sum(end - start for start, end in kept)
            if mode == "cut" and duration < 0.05:
                raise SpeechCleanupError("Tüm görüntü kesiliyor. En az bir bölümü koru veya sesi susturmayı seç.", 422)
            export_id = str(uuid.uuid4())
            with self.database.connect() as connection:
                connection.execute(
                    "INSERT INTO speech_cleanup_exports "
                    "(id,job_id,mode,status,duration,source_duration,ranges_json,created_at) "
                    "VALUES (?,?,?,'queued',?,?,?,?)",
                    (export_id, job_id, mode, duration, state.duration,
                     json.dumps([cut.model_dump() for cut in cuts]), utc_now()),
                )
            try:
                enqueue(export_id)
            except RuntimeError as exc:
                self._update_export(export_id, status="failed", error=str(exc))
                raise SpeechCleanupError(str(exc), 503) from exc
        record = self.export_record(job_id, export_id)
        return _public_export(record)

    def export_record(self, job_id: str, export_id: str) -> Any:
        self._job(job_id)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM speech_cleanup_exports WHERE id=? AND job_id=?", (export_id, job_id),
            ).fetchone()
        if row is None:
            raise SpeechCleanupError("Konuşma temizleme çıktısı bulunamadı.", 404)
        return row

    def _update_detection(self, job_id: str, **values: Any) -> None:
        assignments = ",".join(f"{key}=?" for key in values)
        with self.database.connect() as connection:
            connection.execute(f"UPDATE speech_cleanup SET {assignments} WHERE job_id=?",
                               (*values.values(), job_id))

    def _update_export(self, export_id: str, **values: Any) -> None:
        assignments = ",".join(f"{key}=?" for key in values)
        with self.database.connect() as connection:
            connection.execute(f"UPDATE speech_cleanup_exports SET {assignments} WHERE id=?",
                               (*values.values(), export_id))

    def _speech_activity(
        self, source: Path, audio: Path, work_dir: Path, duration: float,
    ) -> list[tuple[float, float]]:
        source_stat = source.stat()
        fingerprint = {
            "version": 1, "size": source_stat.st_size,
            "mtime_ns": source_stat.st_mtime_ns, "duration": duration,
        }
        cache = work_dir / "speech-activity.json"
        if cache.is_file():
            try:
                cached = json.loads(cache.read_text(encoding="utf-8"))
                if isinstance(cached, dict) and cached.get("source") == fingerprint:
                    activity = [(float(start), float(end)) for start, end in cached["ranges"]]
                    if all(math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= duration
                           for start, end in activity):
                        return activity
            except (OSError, ValueError, TypeError, KeyError):
                pass
        if not audio.is_file():
            extract_audio(source, audio, self.settings.ffmpeg)
        activity = [(max(0, start), min(duration, end)) for start, end in detect_speech_activity(audio)
                    if min(duration, end) > max(0, start)]
        cache.write_text(json.dumps({"source": fingerprint, "ranges": activity}), encoding="utf-8")
        return activity

    def detect(self, job_id: str) -> None:
        audio: Path | None = None
        try:
            job = self._job(job_id)
            source = Path(job["source_path"])
            own_cache = speech_transcript_path(self.settings, job_id)
            work_dir = own_cache.parent
            work_dir.mkdir(parents=True, exist_ok=True)
            self._update_detection(job_id, status="detecting", progress=0.03, error=None)
            payload = json.loads(run_command([
                self.settings.ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(source),
            ]).stdout)
            video = next((item for item in payload["streams"] if item.get("codec_type") == "video"), None)
            if video is None:
                raise MediaError("Dosyada video akışı bulunamadı.")
            duration = float(payload.get("format", {}).get("duration") or video.get("duration") or 0)
            if not math.isfinite(duration) or duration <= 0:
                raise MediaError("Video süresi belirlenemedi.")
            has_audio = any(item.get("codec_type") == "audio" for item in payload["streams"])
            self.database.update_job(job_id, duration=duration, width=int(video.get("width") or 0),
                                     height=int(video.get("height") or 0), has_audio=int(has_audio))
            cached_path = Path(job["transcript_path"]) if job["transcript_path"] else None
            if cached_path is None or not cached_path.is_file():
                cached_path = own_cache if own_cache.is_file() else None
            if not has_audio:
                segments = []
            elif cached_path:
                segments = read_transcript_segments(cached_path)
            elif has_audio:
                audio = work_dir / "audio.wav"
                extract_audio(source, audio, self.settings.ffmpeg)
                try:
                    segments, language = transcribe(
                        audio, self.settings.whisper_model,
                        lambda value: self._update_detection(job_id, progress=0.1 + 0.85 * value),
                    )
                except TranscriptionError as exc:
                    if "konuşma bulunamadı" not in str(exc):
                        raise
                    segments, language = [], "unknown"
                own_cache.write_text(json.dumps({"language": language, "segments": segments},
                                               ensure_ascii=False), encoding="utf-8")
            regions = build_speech_regions(segments, duration)
            if has_audio:
                audio = work_dir / "audio.wav"
                self._update_detection(job_id, progress=0.96)
                try:
                    activity = self._speech_activity(source, audio, work_dir, duration)
                    regions = include_speech_activity(regions, activity, duration)
                except (SpeechActivityError, MediaError, OSError) as exc:
                    logger.warning("Additional voice timing unavailable for %s: %s", job_id, exc)
            previous = self.state(job_id)
            previous_ids = set(previous.removed_region_ids)
            automatic = [region for region in previous.regions if not region.manual]
            all_selected = bool(automatic) and all(region.id in previous_ids for region in automatic)
            manual = [region.model_copy(update={"end": min(region.end, duration)})
                      for region in previous.regions if region.manual and region.start < duration]
            regions = sorted([*regions, *manual], key=lambda region: (region.start, region.end))
            self._update_detection(
                job_id, status="ready", progress=1, error=None, duration=duration,
                regions_json=json.dumps([region.model_dump() for region in regions], ensure_ascii=False),
                removed_json=json.dumps([region.id for region in regions
                                         if region.id in previous_ids or (all_selected and not region.manual)]),
            )
        except Exception as exc:
            logger.warning("Speech detection failed for %s: %s", job_id, exc)
            self._update_detection(job_id, status="failed", error=str(exc))
        finally:
            if audio:
                audio.unlink(missing_ok=True)

    def render(self, export_id: str) -> None:
        output: Path | None = None
        temporary: Path | None = None
        try:
            with self.database.connect() as connection:
                record = connection.execute("SELECT * FROM speech_cleanup_exports WHERE id=?", (export_id,)).fetchone()
            if record is None:
                return
            job = self._job(record["job_id"])
            source = Path(job["source_path"])
            output_dir = speech_export_workspace(self.settings, record["job_id"], export_id)
            output_dir.mkdir(parents=True, exist_ok=True)
            output = output_dir / f"konusma-temizleme-{record['mode']}.mp4"
            temporary = output_dir / "render.partial.mp4"
            cuts = [CutRange.model_validate(value) for value in json.loads(record["ranges_json"])]
            self._update_export(export_id, status="rendering", progress=0, error=None)
            progress = lambda value: self._update_export(export_id, progress=value)
            if record["mode"] == "mute":
                expression = build_mute_expression(cuts)
                script = output_dir / "mute.ffgraph"
                script.write_text(f"[0:a:0]volume=0:enable='{expression}'[aout]", encoding="utf-8")
                self._render_mute(source, temporary, script, record["duration"], progress)
            else:
                kept = kept_ranges_after_cuts(0, record["source_duration"], cuts)
                self._render_cuts(source, temporary, kept, output_dir, progress)
            temporary.replace(output)
            self._update_export(export_id, status="completed", progress=1, path=str(output))
        except Exception as exc:
            logger.warning("Speech export failed for %s: %s", export_id, exc)
            self._update_export(export_id, status="failed", error=str(exc))
            if temporary:
                temporary.unlink(missing_ok=True)
                for chunk in temporary.parent.glob("cut-*.mp4"):
                    chunk.unlink(missing_ok=True)

    @staticmethod
    def _encoding_options() -> list[str]:
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", str(MUTE_AUDIO_BIT_RATE), "-movflags", "+faststart"]

    def _mute_video_copy_options(self, source: Path) -> list[str] | None:
        """Limit direct MP4 remuxing to known AVC and HEVC sample entries."""
        try:
            payload = json.loads(run_command([
                self.settings.ffprobe, "-v", "error", "-select_streams", "V:0",
                "-show_entries", "stream=codec_name,codec_tag_string,time_base:format=format_name",
                "-of", "json", str(source),
            ]).stdout)
        except (MediaError, OSError, ValueError):
            return None
        if "mp4" not in payload.get("format", {}).get("format_name", "").split(","):
            return None
        streams = payload.get("streams", [])
        if not streams:
            return None
        video = streams[0]
        tag = video.get("codec_tag_string")
        if (video.get("codec_name"), tag) not in {
            ("h264", "avc1"), ("h264", "avc3"), ("hevc", "hvc1"), ("hevc", "hev1"),
        }:
            return None
        options = ["-c:v", "copy", "-tag:v", tag]
        match = re.fullmatch(r"1/([1-9][0-9]{0,9})", video.get("time_base", ""))
        if match and int(match[1]) <= 2**31 - 1:
            options.extend(["-video_track_timescale", match[1]])
        return options

    def _render_mute(
        self, source: Path, output: Path, script: Path, duration: float,
        progress: Callable[[float], None],
    ) -> None:
        arguments = [
            "-i", str(source), "-/filter_complex", str(script),
            "-map", "0:V:0", "-map", "[aout]",
        ]
        copy_options = self._mute_video_copy_options(source)
        if copy_options is not None:
            required = mute_export_space_required(source, duration, self.settings.minimum_free_bytes)
            available = shutil.disk_usage(output.parent).free
            if available < required:
                raise MediaError(
                    f"Çıktı diskinde {available / 1024**3:.1f} GB boş alan var. "
                    f"Hızlı dışa aktarma için yaklaşık {required / 1024**3:.1f} GB gerekiyor."
                )
            try:
                # Keeping the MP4 index at the end avoids rewriting the complete file.
                self._ffmpeg([
                    *arguments, *copy_options, "-c:a", "aac", "-b:a", str(MUTE_AUDIO_BIT_RATE),
                    "-write_tmcd", "0", str(output),
                ], duration, progress)
                return
            except MediaError as exc:
                detail = str(exc).casefold()
                mux_incompatible = any(message in detail for message in (
                    "could not find tag for codec", "codec not currently supported in container",
                    "incompatible with output codec",
                ))
                if self._cancelled.is_set() or not mux_incompatible:
                    raise
                logger.info("MP4 rejected the copied video stream, using video encoding")
                output.unlink(missing_ok=True)
                progress(0)
        self._ffmpeg([*arguments, *self._encoding_options(), str(output)], duration, progress)

    def _render_cuts(
        self, source: Path, output: Path, kept: list[tuple[float, float]],
        work_dir: Path, progress: Callable[[float], None],
    ) -> None:
        # Bound decoder count and command length even for hours of word-timed speech.
        total_duration = sum(end - start for start, end in kept)
        completed = 0.0
        chunks: list[Path] = []
        for offset in range(0, len(kept), RENDER_BATCH_SIZE):
            batch = kept[offset:offset + RENDER_BATCH_SIZE]
            command: list[str] = []
            filters: list[str] = []
            labels: list[str] = []
            batch_duration = sum(end - start for start, end in batch)
            for index, (start, end) in enumerate(batch):
                command.extend(["-ss", f"{start:.6f}", "-t", f"{end - start:.6f}",
                                "-threads", "1", "-i", str(source)])
                filters.extend([
                    f"[{index}:v:0]trim=duration={end-start:.6f},setpts=PTS-STARTPTS[v{index}]",
                    f"[{index}:a:0]atrim=duration={end-start:.6f},asetpts=PTS-STARTPTS[a{index}]",
                ])
                labels.append(f"[v{index}][a{index}]")
            filters.append(f"{''.join(labels)}concat=n={len(batch)}:v=1:a=1[vout][aout]")
            script = work_dir / f"cut-{offset}.ffgraph"
            script.write_text(";".join(filters), encoding="utf-8")
            chunk = work_dir / f"cut-{offset}.mp4"
            command.extend(["-filter_complex_threads", "1", "-/filter_complex", str(script),
                            "-map", "[vout]", "-map", "[aout]", *self._encoding_options(), str(chunk)])
            self._ffmpeg(command, batch_duration,
                         lambda value: progress(0.95 * (completed + value * batch_duration) / total_duration))
            completed += batch_duration
            chunks.append(chunk)
        if len(chunks) == 1:
            chunks[0].replace(output)
        else:
            manifest = work_dir / "concat.txt"
            # Generated relative filenames contain no user data or quoting-sensitive characters.
            manifest.write_text("".join(f"file '{chunk.name}'\n" for chunk in chunks), encoding="utf-8")
            self._ffmpeg(["-f", "concat", "-safe", "1", "-i", str(manifest),
                          "-map", "0:v:0", "-map", "0:a:0", "-c", "copy",
                          "-movflags", "+faststart", str(output)], total_duration,
                         lambda value: progress(0.95 + value * 0.05))
            for chunk in chunks:
                chunk.unlink(missing_ok=True)

    def _ffmpeg(self, arguments: list[str], duration: float, progress: Callable[[float], None]) -> None:
        command = [self.settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                   "-progress", "pipe:1", "-nostats", *arguments]
        with self._lock:
            if self._cancelled.is_set():
                raise MediaError("Uygulama kapanırken işlem durduruldu.")
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding="utf-8", errors="replace",
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self._process = process
        errors: list[str] = []
        try:
            assert process.stdout is not None
            for line in process.stdout:
                key, _, value = line.strip().partition("=")
                if key == "out_time_us":
                    try:
                        progress(min(0.99, max(0, float(value) / 1_000_000 / duration)))
                    except ValueError:
                        pass
                elif "=" not in line:
                    errors.append(line.strip())
                    errors = errors[-10:]
            return_code = process.wait()
            if self._cancelled.is_set():
                raise MediaError("Uygulama kapanırken işlem durduruldu.")
            if return_code != 0:
                detail = " ".join(errors)
                if "-/filter_complex" in arguments and "Unrecognized option" in detail:
                    # FFmpeg 9 removed the old script flag. Older installations
                    # without the generic file-argument syntax still support it.
                    return self._ffmpeg([
                        "-filter_complex_script" if item == "-/filter_complex" else item
                        for item in arguments
                    ], duration, progress)
                raise MediaError(detail or "Konuşma temizleme çıktısı oluşturulamadı.")
            progress(1)
        finally:
            with self._lock:
                self._process = None
