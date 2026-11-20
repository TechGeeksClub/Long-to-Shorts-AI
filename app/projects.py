from __future__ import annotations

import json
import logging
import math
import re
import shutil
import tempfile
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import UploadFile
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.config import Settings
from app.database import Database, utc_now
from app.job_workspace import job_workspace, remove_job_workspace
from app.media import MediaError
from app.merging import MergeRenderer, VideoInfo, probe_video
from app.models import CutRange, JobSummary
from app.subtitles import MIN_CUT_RANGE_SECONDS


logger = logging.getLogger(__name__)
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm"}
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
ACTIVE_JOB_STATUSES = {"queued", "importing", "analyzing", "exporting"}


class ProjectError(ValueError):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


class ProjectSummary(BaseModel):
    id: str
    name: str
    active_video_id: str | None
    video_count: int
    created_at: datetime
    updated_at: datetime


class ProjectVideo(JobSummary):
    kind: Literal["source", "merged"] = "source"


class MergeTask(BaseModel):
    id: str
    status: Literal["queued", "rendering", "completed", "failed"]
    progress: float
    error: str | None = None
    video_ids: list[str]
    cuts: dict[str, list[CutRange]] = Field(default_factory=dict)
    export_mode: Literal["auto", "fast", "precise"] = "auto"
    output_directory: str | None = None
    output_path: str | None = None
    output_video_id: str | None = None
    filename: str | None = None
    created_at: datetime


class ProjectDetail(ProjectSummary):
    videos: list[ProjectVideo] = Field(default_factory=list)
    merges: list[MergeTask] = Field(default_factory=list)


class ActiveVideoUpdate(BaseModel):
    video_id: str = Field(min_length=1, max_length=100)


class MergeCutRange(CutRange):
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)


class MergeRequest(BaseModel):
    video_ids: list[str] = Field(min_length=1, max_length=100)
    cuts: dict[str, Annotated[list[MergeCutRange], Field(max_length=1000)]] = Field(
        default_factory=dict, max_length=100,
    )
    export_mode: Literal["auto", "fast", "precise"] = "auto"
    output_directory: str | None = Field(default=None, min_length=1, max_length=32767)


class ProjectPathsRequest(BaseModel):
    paths: list[Annotated[str, Field(min_length=1, max_length=32767)]] = Field(
        min_length=1, max_length=100,
    )


@dataclass(frozen=True)
class PreparedProjectVideo:
    id: str
    name: str
    source: Path
    info: VideoInfo


def _normalize_merge_cuts(ranges: list[CutRange]) -> list[CutRange]:
    # Validation already enforces the minimum length. Do not drop a valid 50ms
    # cut because binary floating point represents 5.05 - 5 as slightly smaller.
    normalized: list[CutRange] = []
    for cut in sorted(ranges, key=lambda value: value.start):
        if normalized and cut.start <= normalized[-1].end:
            normalized[-1] = CutRange(start=normalized[-1].start, end=max(normalized[-1].end, cut.end))
        else:
            normalized.append(CutRange(start=cut.start, end=cut.end))
    return normalized


class ProjectsService:
    def __init__(
        self, database: Database, settings: Settings,
        speech_busy: Callable[[str], bool] | None = None,
    ):
        self.database = database
        self.settings = settings
        self.renderer = MergeRenderer(settings)
        self._speech_busy = speech_busy or (lambda _: False)
        self._lock = threading.RLock()
        self._uploading: set[str] = set()

    def initialize(self) -> None:
        with self.database.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    active_video_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project_videos (
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL, kind TEXT NOT NULL DEFAULT 'source',
                    PRIMARY KEY(project_id, job_id)
                );
                CREATE TABLE IF NOT EXISTS project_merges (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    status TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0, error TEXT,
                    video_ids_json TEXT NOT NULL, sources_json TEXT NOT NULL,
                    reserved_video_id TEXT NOT NULL,
                    output_video_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
                    path TEXT, created_at TEXT NOT NULL,
                    cuts_json TEXT NOT NULL DEFAULT '{}', export_mode TEXT NOT NULL DEFAULT 'auto',
                    output_directory TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_project_videos_project ON project_videos(project_id);
                CREATE INDEX IF NOT EXISTS idx_project_merges_project ON project_merges(project_id);
            """)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(project_merges)")}
            if "cuts_json" not in columns:
                connection.execute("ALTER TABLE project_merges ADD COLUMN cuts_json TEXT NOT NULL DEFAULT '{}'")
            if "export_mode" not in columns:
                connection.execute("ALTER TABLE project_merges ADD COLUMN export_mode TEXT NOT NULL DEFAULT 'auto'")
            if "output_directory" not in columns:
                connection.execute("ALTER TABLE project_merges ADD COLUMN output_directory TEXT")
        self.sync_legacy()

    def sync_legacy(self) -> None:
        """Keep old per-video job IDs and edits intact when adding project grouping."""
        with self._lock, self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            jobs = connection.execute(
                "SELECT jobs.* FROM jobs LEFT JOIN project_videos ON jobs.id=project_videos.job_id "
                "WHERE project_videos.job_id IS NULL"
            ).fetchall()
            for job in jobs:
                connection.execute(
                    "INSERT INTO projects(id,name,active_video_id,created_at,updated_at) VALUES (?,?,?,?,?)",
                    (job["id"], job["filename"], job["id"], job["created_at"], job["updated_at"]),
                )
                connection.execute(
                    "INSERT INTO project_videos(project_id,job_id,position) VALUES (?,?,0)",
                    (job["id"], job["id"]),
                )
            connection.execute(
                "DELETE FROM projects WHERE NOT EXISTS "
                "(SELECT 1 FROM project_videos WHERE project_id=projects.id)"
            )
            connection.execute(
                "UPDATE projects SET active_video_id=(SELECT job_id FROM project_videos "
                "WHERE project_id=projects.id ORDER BY position LIMIT 1) "
                "WHERE NOT EXISTS (SELECT 1 FROM project_videos "
                "WHERE project_id=projects.id AND job_id=projects.active_video_id)"
            )

    @staticmethod
    def _summary(row: Any, count: int) -> ProjectSummary:
        return ProjectSummary(
            id=row["id"], name=row["name"], active_video_id=row["active_video_id"],
            video_count=count, created_at=row["created_at"], updated_at=row["updated_at"],
        )

    @staticmethod
    def _merge(row: Any) -> MergeTask:
        return MergeTask(
            id=row["id"], status=row["status"], progress=row["progress"], error=row["error"],
            video_ids=json.loads(row["video_ids_json"]), output_video_id=row["output_video_id"],
            cuts=json.loads(row["cuts_json"]), export_mode=row["export_mode"],
            output_directory=row["output_directory"],
            output_path=str(Path(row["path"]).resolve()) if row["status"] == "completed" and row["path"] else None,
            filename=Path(row["path"]).name if row["status"] == "completed" and row["path"] else None,
            created_at=row["created_at"],
        )

    def list_projects(self) -> list[ProjectSummary]:
        self.sync_legacy()
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT projects.*,COUNT(project_videos.job_id) AS video_count FROM projects "
                "JOIN project_videos ON project_videos.project_id=projects.id "
                "GROUP BY projects.id ORDER BY projects.updated_at DESC,projects.id"
            ).fetchall()
        return [self._summary(row, row["video_count"]) for row in rows]

    def detail(self, project_id: str) -> ProjectDetail:
        self.sync_legacy()
        with self.database.connect() as connection:
            project = connection.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if project is None:
                raise ProjectError("Proje bulunamadı.", 404)
            videos = connection.execute(
                "SELECT jobs.*,project_videos.kind FROM project_videos JOIN jobs ON jobs.id=project_videos.job_id "
                "WHERE project_id=? ORDER BY project_videos.position, jobs.created_at", (project_id,),
            ).fetchall()
            merges = connection.execute(
                "SELECT * FROM project_merges WHERE project_id=? ORDER BY created_at DESC", (project_id,),
            ).fetchall()
        return ProjectDetail(
            **self._summary(project, len(videos)).model_dump(),
            videos=[ProjectVideo(**self.database._job_summary(row).model_dump(), kind=row["kind"]) for row in videos],
            merges=[self._merge(row) for row in merges],
        )

    def select_video(self, project_id: str, video_id: str) -> ProjectDetail:
        with self._lock:
            project = self.detail(project_id)
            if video_id not in {video.id for video in project.videos}:
                raise ProjectError("Seçilen video bu projeye ait değil.", 422)
            with self.database.connect() as connection:
                connection.execute("UPDATE projects SET active_video_id=?,updated_at=? WHERE id=?",
                                   (video_id, utc_now(), project_id))
        return self.detail(project_id)

    def _remove_directory(self, directory: Path) -> None:
        root = (self.settings.data_dir / "jobs").resolve()
        resolved = directory.resolve()
        if resolved.parent != root or resolved == root:
            raise ProjectError("Uygulamaya ait olmayan video klasörü silinemez.", 422)
        shutil.rmtree(resolved, ignore_errors=True)

    def _register_videos(
        self, project_id: str, videos: list[PreparedProjectVideo], *, creating: bool,
        linked: bool = False,
    ) -> None:
        with self._lock, self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            if creating:
                connection.execute(
                    "INSERT INTO projects(id,name,created_at,updated_at) VALUES (?,?,?,?)",
                    (project_id, Path(videos[0].name).stem, now, now),
                )
            elif connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
                raise ProjectError("Proje bulunamadı.", 404)
            if linked and not creating:
                rows = connection.execute(
                    "SELECT source_path FROM jobs JOIN project_videos ON jobs.id=project_videos.job_id "
                    "WHERE project_id=?", (project_id,),
                )
                existing_sources = {Path(row["source_path"]) for row in rows}
                for video in videos:
                    if video.source in existing_sources:
                        raise ProjectError(f"Bu video zaten projeye ekli: {video.name}")
            position = connection.execute(
                "SELECT COALESCE(MAX(position),-1)+1 FROM project_videos WHERE project_id=?", (project_id,),
            ).fetchone()[0]
            for offset, video in enumerate(videos):
                info = video.info
                connection.execute(
                    "INSERT INTO jobs(id,filename,source_path,source_type,status,progress,stage,duration,width,height,"
                    "has_audio,created_at,updated_at) VALUES (?,?,?,'upload','ready',1,?,?,?,?,?,?,?)",
                    (video.id, video.name, str(video.source), "Video hazır; bir araç seç", info.duration,
                     info.width, info.height, int(info.has_audio), now, now),
                )
                connection.execute(
                    "INSERT INTO project_videos(project_id,job_id,position,kind) VALUES (?,?,?,'source')",
                    (project_id, video.id, position + offset),
                )
            connection.execute(
                "UPDATE projects SET updated_at=?,active_video_id=COALESCE(active_video_id,?) WHERE id=?",
                (now, videos[0].id, project_id),
            )

    def link_paths(self, paths: list[str], project_id: str | None = None) -> ProjectDetail:
        creating = project_id is None
        project_id = project_id or str(uuid.uuid4())
        self.sync_legacy()
        prepared: list[PreparedProjectVideo] = []
        seen: set[Path] = set()
        managed_sources = (self.settings.data_dir / "jobs").resolve()
        if not paths or len(paths) > 100:
            raise ProjectError("Bir seferde 1 ile 100 arasında video yolu gir.", 422)
        with self._lock:
            if not creating:
                with self.database.connect() as connection:
                    if connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
                        raise ProjectError("Proje bulunamadı.", 404)
            if project_id in self._uploading:
                raise ProjectError("Bu projeye video ekleniyor. İşlem tamamlanınca tekrar dene.")
            self._uploading.add(project_id)
        try:
            for raw_path in paths:
                value = raw_path.strip()
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                path = Path(value)
                if not path.is_absolute() or value.startswith(("\\", "//")) or "\x00" in value:
                    raise ProjectError("Yerel bir videonun tam dosya yolunu gir.", 422)
                if path.suffix.lower() not in ALLOWED_EXTENSIONS:
                    raise ProjectError("Desteklenen dosya türleri: MP4, MOV, MKV, M4V, AVI ve WEBM.", 415)
                try:
                    source = path.resolve(strict=True)
                    is_file = source.is_file()
                except (FileNotFoundError, NotADirectoryError) as exc:
                    raise ProjectError(f"Video dosyası bulunamadı: {path.name}", 404) from exc
                except (OSError, RuntimeError, ValueError) as exc:
                    raise ProjectError(f"Video yoluna erişilemedi: {path.name}", 422) from exc
                if str(source).startswith(("\\", "//")):
                    raise ProjectError("Yerel bir videonun tam dosya yolunu gir.", 422)
                if not is_file:
                    raise ProjectError(f"Video dosyası bulunamadı: {path.name}", 404)
                if source.is_relative_to(managed_sources):
                    raise ProjectError("Uygulamanın proje klasöründeki videoları kendi projelerinden aç.", 422)
                if source in seen:
                    raise ProjectError(f"Aynı video yolu birden fazla kez girildi: {path.name}", 422)
                seen.add(source)
                try:
                    info = probe_video(source, self.settings.ffprobe)
                except Exception as exc:
                    raise ProjectError(f"Video okunamadı ({path.name}): {exc}", 422) from exc
                prepared.append(PreparedProjectVideo(str(uuid.uuid4()), path.name, source, info))
            self._register_videos(project_id, prepared, creating=creating, linked=True)
            return self.detail(project_id)
        finally:
            with self._lock:
                self._uploading.discard(project_id)

    async def upload(
        self, videos: list[UploadFile], content_length: int = 0, project_id: str | None = None,
    ) -> ProjectDetail:
        prepared: list[PreparedProjectVideo] = []
        directories: list[Path] = []
        adding_to_existing = project_id is not None
        project_id = project_id or str(uuid.uuid4())
        creating = False
        registered = False
        committed = False
        try:
            if not videos or len(videos) > 100:
                raise ProjectError("Bir yüklemede 1 ile 100 arasında video seç.", 422)
            for video in videos:
                if Path(video.filename or "").suffix.lower() not in ALLOWED_EXTENSIONS:
                    raise ProjectError("Desteklenen dosya türleri: MP4, MOV, MKV, M4V, AVI ve WEBM.", 415)
            self.sync_legacy()
            with self._lock:
                with self.database.connect() as connection:
                    creating = connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None
                if adding_to_existing and creating:
                    raise ProjectError("Proje bulunamadı.", 404)
                if project_id in self._uploading:
                    raise ProjectError("Bu projeye video yükleniyor. Yükleme tamamlanınca tekrar dene.")
                self._uploading.add(project_id)
                registered = True
            reserve = max(0, self.settings.minimum_free_bytes)
            expected = max(content_length, sum(video.size or 0 for video in videos))
            if shutil.disk_usage(self.settings.data_dir).free < expected + reserve:
                raise ProjectError("Videolar için yeterli boş disk alanı yok.", 507)
            for upload in videos:
                job_id = str(uuid.uuid4())
                name = Path((upload.filename or "video.mp4").replace("\\", "/")).name
                stem = SAFE_NAME.sub("-", Path(name).stem).strip(".-") or "video"
                directory = self.settings.data_dir / "jobs" / job_id
                directory.mkdir(parents=True)
                source = directory / f"{stem}{Path(name).suffix.lower()}"
                directories.append(directory)
                written = 0
                with source.open("wb") as destination:
                    while chunk := await upload.read(self.settings.upload_chunk_size):
                        destination.write(chunk)
                        written += len(chunk)
                        if written % (64 * self.settings.upload_chunk_size) < self.settings.upload_chunk_size:
                            if shutil.disk_usage(self.settings.data_dir).free < reserve:
                                raise ProjectError("Yükleme durduruldu: video işleme için boş disk alanı kalmadı.", 507)
                if written == 0:
                    raise ProjectError(f"Yüklenen dosya boş: {name}", 400)
                try:
                    info = await run_in_threadpool(probe_video, source, self.settings.ffprobe)
                except Exception as exc:
                    raise ProjectError(f"Video okunamadı ({name}): {exc}", 422) from exc
                prepared.append(PreparedProjectVideo(job_id, name, source, info))
            self._register_videos(project_id, prepared, creating=creating)
            committed = True
            return self.detail(project_id)
        except Exception:
            if not committed:
                for directory in directories:
                    self._remove_directory(directory)
            raise
        finally:
            with self._lock:
                if registered:
                    self._uploading.discard(project_id)
            for upload in videos:
                await upload.close()

    def has_active_work(self, project_id: str) -> bool:
        with self._lock:
            if project_id in self._uploading:
                return True
            with self.database.connect() as connection:
                return connection.execute(
                    "SELECT 1 FROM project_merges WHERE project_id=? AND status IN ('queued','rendering')",
                    (project_id,),
                ).fetchone() is not None

    def is_video_busy(self, video_id: str) -> bool:
        with self._lock, self.database.connect() as connection:
            membership = connection.execute(
                "SELECT project_id FROM project_videos WHERE job_id=?", (video_id,),
            ).fetchone()
            if membership and membership["project_id"] in self._uploading:
                return True
            rows = connection.execute(
                "SELECT video_ids_json FROM project_merges WHERE status IN ('queued','rendering')"
            ).fetchall()
        return any(video_id in json.loads(row["video_ids_json"]) for row in rows)

    def queue_merge(
        self, project_id: str, video_ids: list[str], enqueue: Callable[[str], None], *,
        cuts: dict[str, list[CutRange]] | None = None,
        export_mode: Literal["auto", "fast", "precise"] = "auto",
        output_directory: str | None = None,
    ) -> MergeTask:
        with self._lock:
            project = self.detail(project_id)
            if not 1 <= len(video_ids) <= 100 or len(set(video_ids)) != len(video_ids):
                raise ProjectError("Bir ile 100 arasında farklı video seç.", 422)
            if export_mode not in {"auto", "fast", "precise"}:
                raise ProjectError("Dışa aktarma yöntemi geçersiz.", 422)
            cuts = cuts or {}
            if set(cuts) - set(video_ids):
                raise ProjectError("Kesilecek aralıklar yalnızca seçilen videolara ait olmalıdır.", 422)
            if sum(len(ranges) for ranges in cuts.values()) > 1000:
                raise ProjectError("Bir işlemde en fazla 1000 kesim aralığı kullanılabilir.", 422)
            known = {video.id: video for video in project.videos}
            if set(video_ids) - known.keys():
                raise ProjectError("Birleştirilecek videolar aynı projeye ait olmalıdır.", 422)
            if self.has_active_work(project_id):
                raise ProjectError("Bu projede video yükleme veya birleştirme işlemi devam ediyor.")
            sources = []
            normalized_cuts: dict[str, list[CutRange]] = {}
            for video_id in video_ids:
                job = self.database.get_job_record(video_id)
                if job is None or job["status"] == "importing" or (
                    job["status"] == "queued" and job["source_type"] == "youtube"
                ):
                    raise ProjectError("Önce kaynak videoların indirilmesini bekle.")
                path = Path(job["source_path"])
                if not path.is_file():
                    raise ProjectError(f"Kaynak video bulunamadı: {known[video_id].filename}", 404)
                ranges = cuts.get(video_id, [])
                if ranges:
                    duration = job["duration"]
                    if duration is None:
                        try:
                            duration = probe_video(path, self.settings.ffprobe).duration
                        except (MediaError, OSError) as exc:
                            raise ProjectError(f"Kesilecek video okunamadı: {exc}", 422) from exc
                    if not math.isfinite(duration) or duration <= 0:
                        raise ProjectError("Kesilecek videonun süresi belirlenemedi.", 422)
                    if any(not math.isfinite(cut.start) or not math.isfinite(cut.end)
                           or cut.start < 0 or cut.end > duration
                           or cut.end - cut.start < MIN_CUT_RANGE_SECONDS - 1e-9
                           for cut in ranges):
                        raise ProjectError(
                            "Kesim aralıkları video sınırları içinde ve en az 0,05 saniye olmalıdır.", 422,
                        )
                    normalized = _normalize_merge_cuts(ranges)
                    if duration - sum(cut.end - cut.start for cut in normalized) < MIN_CUT_RANGE_SECONDS - 1e-9:
                        raise ProjectError("Kesimden sonra videoda en az 0,05 saniye kalmalıdır.", 422)
                    normalized_cuts[video_id] = normalized
                sources.append(str(path))
            if len(video_ids) == 1 and not normalized_cuts:
                raise ProjectError("Tek videoyu dışa aktarmak için en az bir kesim aralığı ekle.", 422)
            destination = self._validate_export_directory(output_directory) if output_directory is not None else None
            output_disk = destination or self.settings.data_dir
            if shutil.disk_usage(output_disk).free < self.settings.minimum_free_bytes:
                raise ProjectError("Birleştirme için yeterli boş disk alanı yok.", 507)
            needs_normalization_space = export_mode == "precise" or (export_mode == "auto" and not normalized_cuts)
            if destination and needs_normalization_space and shutil.disk_usage(self.settings.data_dir).free < self.settings.minimum_free_bytes:
                raise ProjectError("Video dönüştürmenin geçici dosyaları için proje diskinde yeterli boş alan yok.", 507)
            merge_id = str(uuid.uuid4())
            with self.database.connect() as connection:
                connection.execute(
                    "INSERT INTO project_merges(id,project_id,status,video_ids_json,sources_json,reserved_video_id,created_at,"
                    "cuts_json,export_mode,output_directory) VALUES (?,?,'queued',?,?,?,?,?,?,?)",
                    (merge_id, project_id, json.dumps(video_ids), json.dumps(sources), str(uuid.uuid4()), utc_now(),
                     json.dumps({key: [cut.model_dump() for cut in ranges]
                                 for key, ranges in normalized_cuts.items()}), export_mode,
                     str(destination) if destination else None),
                )
                connection.execute("UPDATE projects SET updated_at=? WHERE id=?", (utc_now(), project_id))
            try:
                enqueue(merge_id)
            except RuntimeError as exc:
                self._update_merge(merge_id, status="failed", error=str(exc))
                raise ProjectError(str(exc), 503) from exc
            return self._merge(self.merge_record(project_id, merge_id))

    def merge_record(self, project_id: str, merge_id: str) -> Any:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM project_merges WHERE id=? AND project_id=?", (merge_id, project_id),
            ).fetchone()
        if row is None:
            raise ProjectError("Birleştirme kaydı bulunamadı.", 404)
        return row

    def _update_merge(self, merge_id: str, **values: Any) -> None:
        with self.database.connect() as connection:
            fields = ",".join(f"{key}=?" for key in values)
            connection.execute(f"UPDATE project_merges SET {fields} WHERE id=?", (*values.values(), merge_id))

    def _validate_export_directory(self, value: str) -> Path:
        try:
            raw = value.strip().strip('"')
            if not raw or any(character in raw for character in "\r\n\0"):
                raise ValueError
            directory = Path(raw).expanduser()
            if not directory.is_absolute():
                raise ValueError
            directory = directory.resolve(strict=True)
            if not directory.is_dir():
                raise ValueError
        except (OSError, ValueError, RuntimeError) as exc:
            raise ProjectError("Kayıt konumu var olan bir klasörün tam yolu olmalıdır.", 422) from exc
        if directory.is_relative_to((self.settings.data_dir / "jobs").resolve()):
            raise ProjectError("Uygulamanın geçici video klasörleri kayıt konumu olarak seçilemez. Başka bir klasör seç.", 422)
        try:
            # An access bit alone does not establish Windows ACL permissions.
            with tempfile.TemporaryFile(prefix=".lts-export-write-", dir=directory):
                pass
        except OSError as exc:
            raise ProjectError("Seçilen kayıt klasörüne yazılamıyor. Başka bir klasör seç.", 422) from exc
        return directory

    @staticmethod
    def _external_merge_output(row: Any) -> Path:
        directory = Path(row["output_directory"])
        identifier = str(uuid.UUID(row["id"]))
        stem = "kesilmis-video" if json.loads(row["cuts_json"]) else "birlesmis-video"
        return directory / f"{stem}-{identifier}.mp4"

    def _remove_external_merge_output(self, row: Any) -> None:
        # Only paths persisted after this task exclusively reserved its unique
        # output may be cleaned. Never recurse into the user's chosen folder.
        if not row["output_directory"] or not row["path"]:
            return
        try:
            output = self._external_merge_output(row)
            if Path(row["path"]) != output or output.parent.resolve() != output.parent:
                return
            output.unlink(missing_ok=True)
            for partial in output.parent.glob(f".{output.stem}-*.partial.mp4"):
                token = partial.name.removeprefix(f".{output.stem}-").removesuffix(".partial.mp4")
                if re.fullmatch(r"[0-9a-f]{32}", token):
                    partial.unlink(missing_ok=True)
        except (OSError, RuntimeError, ValueError) as exc:
            # A removed external drive must not prevent application startup or
            # cleanup of the managed workspace after a failed export.
            logger.warning("External merge partial cleanup unavailable (%s)", type(exc).__name__)

    def render(self, merge_id: str) -> None:
        directory: Path | None = None
        owned_external_output: dict[str, Any] | None = None
        try:
            with self.database.connect() as connection:
                row = connection.execute("SELECT * FROM project_merges WHERE id=?", (merge_id,)).fetchone()
            if row is None or row["status"] != "queued":
                return
            self._update_merge(merge_id, status="rendering", progress=0, error=None)
            directory = self.settings.data_dir / "jobs" / row["reserved_video_id"]
            directory.mkdir(parents=True, exist_ok=True)
            cuts = json.loads(row["cuts_json"])
            video_ids = json.loads(row["video_ids_json"])
            output = directory / ("kesilmis-video.mp4" if cuts else "birlesmis-video.mp4")
            if row["output_directory"]:
                self._validate_export_directory(row["output_directory"])
                output = self._external_merge_output(row)
                try:
                    with output.open("xb"):
                        pass
                except FileExistsError as exc:
                    raise ProjectError("Bu dışa aktarma dosyası zaten var. Üzerine yazılmadı; yeniden dışa aktar.", 409) from exc
                owned_external_output = {**dict(row), "path": str(output)}
                self._update_merge(merge_id, path=str(output))
            options = {}
            if cuts or row["export_mode"] != "auto":
                options = {
                    "cut_ranges": [[CutRange(**cut) for cut in cuts.get(video_id, [])] for video_id in video_ids],
                    "export_mode": row["export_mode"],
                }
            info = self.renderer.render(
                [Path(source) for source in json.loads(row["sources_json"])], output, directory / "merge-work",
                lambda value: self._update_merge(merge_id, progress=min(0.99, max(0, value))),
                **options,
            )
            filename = "Kesilmiş video.mp4" if cuts and len(video_ids) == 1 else (
                "Birleştirilmiş ve kesilmiş video.mp4" if cuts else "Birleştirilmiş video.mp4"
            )
            with self._lock, self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                now = utc_now()
                position = connection.execute(
                    "SELECT COALESCE(MAX(position),-1)+1 FROM project_videos WHERE project_id=?", (row["project_id"],),
                ).fetchone()[0]
                connection.execute(
                    "INSERT INTO jobs(id,filename,source_path,source_type,status,progress,stage,duration,width,height,has_audio,"
                    "created_at,updated_at) VALUES (?,?,?,'upload','ready',1,?,?,?,?,?,?,?)",
                    (row["reserved_video_id"], filename, str(output), "Dışa aktarılan video hazır", info.duration,
                     info.width, info.height, int(info.has_audio), now, now),
                )
                connection.execute(
                    "INSERT INTO project_videos(project_id,job_id,position,kind) VALUES (?,?,?,'merged')",
                    (row["project_id"], row["reserved_video_id"], position),
                )
                connection.execute(
                    "UPDATE project_merges SET status='completed',progress=1,output_video_id=?,path=? WHERE id=?",
                    (row["reserved_video_id"], str(output), merge_id),
                )
                connection.execute("UPDATE projects SET updated_at=? WHERE id=?", (now, row["project_id"]))
        except Exception as exc:
            logger.warning("Video merge failed for %s: %s", merge_id, exc)
            self._update_merge(merge_id, status="failed", error=str(exc))
            if owned_external_output:
                self._remove_external_merge_output(owned_external_output)
            if directory:
                self._remove_directory(directory)

    def interrupt_active(self) -> None:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM project_merges WHERE status IN ('queued','rendering')"
            ).fetchall()
            connection.execute(
                "UPDATE project_merges SET status='failed',error=? WHERE status IN ('queued','rendering')",
                ("Birleştirme uygulama kapanırken yarıda kaldı. Yeniden başlatabilirsin.",),
            )
        for row in rows:
            self._remove_external_merge_output(row)
            self._remove_directory(self.settings.data_dir / "jobs" / row["reserved_video_id"])

    def cancel(self) -> None:
        self.renderer.cancel()

    def delete(self, project_id: str) -> None:
        with self._lock:
            project = self.detail(project_id)
            if self.has_active_work(project_id):
                raise ProjectError("Video yükleme veya birleştirme işlemi devam eden proje silinemez.")
            for video in project.videos:
                if video.status in ACTIVE_JOB_STATUSES or self._speech_busy(video.id) or self.is_video_busy(video.id):
                    raise ProjectError("Devam eden video işlemleri tamamlanmadan proje silinemez.")
            with self.database.connect() as connection:
                active = connection.execute(
                    "SELECT 1 FROM exports JOIN project_videos ON exports.job_id=project_videos.job_id "
                    "WHERE project_id=? AND exports.status IN ('queued','rendering')", (project_id,),
                ).fetchone()
                active_youtube = connection.execute(
                    "SELECT 1 FROM clips JOIN project_videos ON clips.job_id=project_videos.job_id WHERE project_id=? "
                    "AND json_extract(youtube_json,'$.status') IN ('queued','generating')", (project_id,),
                ).fetchone()
                if active or active_youtube:
                    raise ProjectError("Dışa aktarma veya YouTube hazırlığı sürerken proje silinemez.")
                records = connection.execute(
                    "SELECT jobs.id FROM jobs JOIN project_videos ON jobs.id=project_videos.job_id WHERE project_id=?",
                    (project_id,),
                ).fetchall()
                try:
                    for record in records:
                        job_workspace(self.settings, record["id"])
                except (ValueError, OSError, RuntimeError) as exc:
                    raise ProjectError("Projenin çalışma klasörü doğrulanamadı.", 409) from exc
                connection.execute("DELETE FROM projects WHERE id=?", (project_id,))
                connection.executemany("DELETE FROM jobs WHERE id=?", [(record["id"],) for record in records])
            for record in records:
                remove_job_workspace(self.settings, record["id"])
