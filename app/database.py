from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from app.models import (
    ClipCandidate,
    CropKeyframe,
    CutRange,
    Export,
    JobDetail,
    JobSummary,
    SubtitleCue,
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    stage TEXT NOT NULL,
                    error TEXT,
                    duration REAL,
                    width INTEGER,
                    height INTEGER,
                    has_audio INTEGER,
                    language TEXT,
                    transcript_path TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS clips (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    rank INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    start REAL NOT NULL,
                    end REAL NOT NULL,
                    score REAL NOT NULL,
                    reasons_json TEXT NOT NULL,
                    subtitles_json TEXT NOT NULL,
                    cut_ranges_json TEXT NOT NULL DEFAULT '[]',
                    crop_keyframes_json TEXT NOT NULL DEFAULT '[]',
                    framing_mode TEXT NOT NULL DEFAULT 'fit',
                    face_tracking_enabled INTEGER NOT NULL DEFAULT 0,
                    selected INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS exports (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    clip_id TEXT NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    llm_seo_enabled INTEGER NOT NULL DEFAULT 0,
                    playback_rate REAL NOT NULL DEFAULT 1.0,
                    subtitle_margin_v INTEGER NOT NULL DEFAULT 420,
                    subtitle_font_family TEXT NOT NULL DEFAULT 'Arial',
                    balanced_vertical_offset INTEGER NOT NULL DEFAULT 0,
                    path TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_clips_job ON clips(job_id);
                CREATE INDEX IF NOT EXISTS idx_exports_job ON exports(job_id);
                """
            )
            clip_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(clips)").fetchall()
            }
            if "face_tracking_enabled" not in clip_columns:
                connection.execute(
                    """
                    ALTER TABLE clips
                    ADD COLUMN face_tracking_enabled INTEGER NOT NULL DEFAULT 0
                    """
                )
            if "framing_mode" not in clip_columns:
                connection.execute(
                    """
                    ALTER TABLE clips
                    ADD COLUMN framing_mode TEXT NOT NULL DEFAULT 'fit'
                    """
                )
            if "cut_ranges_json" not in clip_columns:
                connection.execute(
                    """
                    ALTER TABLE clips
                    ADD COLUMN cut_ranges_json TEXT NOT NULL DEFAULT '[]'
                    """
                )
            export_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(exports)").fetchall()
            }
            if "llm_seo_enabled" not in export_columns:
                connection.execute(
                    """
                    ALTER TABLE exports
                    ADD COLUMN llm_seo_enabled INTEGER NOT NULL DEFAULT 0
                    """
                )
            if "playback_rate" not in export_columns:
                connection.execute(
                    """
                    ALTER TABLE exports
                    ADD COLUMN playback_rate REAL NOT NULL DEFAULT 1.0
                    """
                )
            if "subtitle_margin_v" not in export_columns:
                connection.execute(
                    """
                    ALTER TABLE exports
                    ADD COLUMN subtitle_margin_v INTEGER NOT NULL DEFAULT 420
                    """
                )
            if "subtitle_font_family" not in export_columns:
                connection.execute(
                    """
                    ALTER TABLE exports
                    ADD COLUMN subtitle_font_family TEXT NOT NULL DEFAULT 'Arial'
                    """
                )
            if "balanced_vertical_offset" not in export_columns:
                connection.execute(
                    """
                    ALTER TABLE exports
                    ADD COLUMN balanced_vertical_offset INTEGER NOT NULL DEFAULT 0
                    """
                )

    def interrupt_active_jobs(self) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'interrupted', stage = 'Uygulama yeniden başlatıldı',
                    error = 'İşlem uygulama kapanırken yarıda kaldı.', updated_at = ?
                WHERE status IN ('queued', 'analyzing', 'exporting')
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE exports
                SET status = 'failed', error = 'Uygulama kapanırken dışa aktarma yarıda kaldı.',
                    updated_at = ?
                WHERE status IN ('queued', 'rendering')
                """,
                (now,),
            )

    def create_job(self, job_id: str, filename: str, source_path: Path) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, filename, source_path, status, progress, stage, created_at, updated_at
                ) VALUES (?, ?, ?, 'queued', 0, 'Sırada bekliyor', ?, ?)
                """,
                (job_id, filename, str(source_path), now, now),
            )

    def update_job(self, job_id: str, **values: Any) -> None:
        if not values:
            return
        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE jobs SET {assignments} WHERE id = ?",
                (*values.values(), job_id),
            )

    def replace_clips(self, job_id: str, clips: list[ClipCandidate]) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM clips WHERE job_id = ?", (job_id,))
            connection.executemany(
                """
                INSERT INTO clips (
                    id, job_id, rank, title, start, end, score, reasons_json,
                    subtitles_json, cut_ranges_json, crop_keyframes_json, framing_mode,
                    face_tracking_enabled, selected
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        clip.id,
                        clip.job_id,
                        clip.rank,
                        clip.title,
                        clip.start,
                        clip.end,
                        clip.score,
                        json.dumps(clip.reasons, ensure_ascii=False),
                        json.dumps(
                            [cue.model_dump() for cue in clip.subtitles], ensure_ascii=False
                        ),
                        json.dumps(
                            [cut.model_dump() for cut in clip.cut_ranges], ensure_ascii=False
                        ),
                        json.dumps(
                            [keyframe.model_dump() for keyframe in clip.crop_keyframes]
                        ),
                        clip.framing_mode,
                        int(clip.face_tracking_enabled),
                        int(clip.selected),
                    )
                    for clip in clips
                ],
            )

    def create_clip(self, clip: ClipCandidate) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO clips (
                    id, job_id, rank, title, start, end, score, reasons_json,
                    subtitles_json, cut_ranges_json, crop_keyframes_json, framing_mode,
                    face_tracking_enabled, selected
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clip.id,
                    clip.job_id,
                    clip.rank,
                    clip.title,
                    clip.start,
                    clip.end,
                    clip.score,
                    json.dumps(clip.reasons, ensure_ascii=False),
                    json.dumps(
                        [cue.model_dump() for cue in clip.subtitles], ensure_ascii=False
                    ),
                    json.dumps(
                        [cut.model_dump() for cut in clip.cut_ranges], ensure_ascii=False
                    ),
                    json.dumps(
                        [keyframe.model_dump() for keyframe in clip.crop_keyframes]
                    ),
                    clip.framing_mode,
                    int(clip.face_tracking_enabled),
                    int(clip.selected),
                ),
            )

    def update_clip(self, clip_id: str, **values: Any) -> None:
        encoded: dict[str, Any] = {}
        for key, value in values.items():
            if key == "subtitles":
                encoded["subtitles_json"] = json.dumps(
                    [cue.model_dump() for cue in value], ensure_ascii=False
                )
            elif key == "cut_ranges":
                encoded["cut_ranges_json"] = json.dumps(
                    [cut.model_dump() for cut in value], ensure_ascii=False
                )
            elif key == "crop_keyframes":
                encoded["crop_keyframes_json"] = json.dumps(
                    [frame.model_dump() for frame in value]
                )
            elif key in {"selected", "face_tracking_enabled"}:
                encoded[key] = int(value)
            else:
                encoded[key] = value
        if not encoded:
            return
        assignments = ", ".join(f"{key} = ?" for key in encoded)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE clips SET {assignments} WHERE id = ?",
                (*encoded.values(), clip_id),
            )

    def create_export(
        self,
        export_id: str,
        job_id: str,
        clip_id: str,
        llm_seo_enabled: bool = False,
        playback_rate: float = 1.0,
        subtitle_margin_v: int = 420,
        subtitle_font_family: str = "Arial",
        balanced_vertical_offset: int = 0,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO exports (
                    id, job_id, clip_id, status, progress, llm_seo_enabled,
                    playback_rate, subtitle_margin_v, subtitle_font_family,
                    balanced_vertical_offset, created_at, updated_at
                ) VALUES (?, ?, ?, 'queued', 0, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    export_id,
                    job_id,
                    clip_id,
                    int(llm_seo_enabled),
                    playback_rate,
                    subtitle_margin_v,
                    subtitle_font_family,
                    balanced_vertical_offset,
                    now,
                    now,
                ),
            )

    def update_export(self, export_id: str, **values: Any) -> None:
        if not values:
            return
        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE exports SET {assignments} WHERE id = ?",
                (*values.values(), export_id),
            )

    def list_jobs(self) -> list[JobSummary]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC"
            ).fetchall()
        return [self._job_summary(row) for row in rows]

    def get_job(self, job_id: str) -> JobDetail | None:
        with self.connect() as connection:
            job = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                return None
            clips = connection.execute(
                "SELECT * FROM clips WHERE job_id = ? ORDER BY rank", (job_id,)
            ).fetchall()
            exports = connection.execute(
                "SELECT * FROM exports WHERE job_id = ? ORDER BY created_at DESC", (job_id,)
            ).fetchall()
        return JobDetail(
            **self._job_summary(job).model_dump(),
            clips=[self._clip(row) for row in clips],
            exports=[self._export(row) for row in exports],
        )

    def get_job_record(self, job_id: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()

    def get_clip(self, clip_id: str) -> ClipCandidate | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
        return self._clip(row) if row else None

    def get_export_record(self, export_id: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM exports WHERE id = ?", (export_id,)
            ).fetchone()

    def delete_job(self, job_id: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    @staticmethod
    def _job_summary(row: sqlite3.Row) -> JobSummary:
        return JobSummary(
            id=row["id"],
            filename=row["filename"],
            status=row["status"],
            progress=row["progress"],
            stage=row["stage"],
            error=row["error"],
            duration=row["duration"],
            width=row["width"],
            height=row["height"],
            language=row["language"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _clip(row: sqlite3.Row) -> ClipCandidate:
        return ClipCandidate(
            id=row["id"],
            job_id=row["job_id"],
            rank=row["rank"],
            title=row["title"],
            start=row["start"],
            end=row["end"],
            score=row["score"],
            reasons=json.loads(row["reasons_json"]),
            subtitles=[SubtitleCue.model_validate(cue) for cue in json.loads(row["subtitles_json"])],
            cut_ranges=[
                CutRange.model_validate(cut)
                for cut in json.loads(row["cut_ranges_json"])
            ],
            crop_keyframes=[
                CropKeyframe.model_validate(frame)
                for frame in json.loads(row["crop_keyframes_json"])
            ],
            framing_mode=row["framing_mode"],
            face_tracking_enabled=bool(row["face_tracking_enabled"]),
            selected=bool(row["selected"]),
        )

    @staticmethod
    def _export(row: sqlite3.Row) -> Export:
        path = Path(row["path"]) if row["path"] else None
        metadata_path = path.with_name("youtube-seo.md") if path else None
        return Export(
            id=row["id"],
            job_id=row["job_id"],
            clip_id=row["clip_id"],
            status=row["status"],
            progress=row["progress"],
            llm_seo_enabled=bool(row["llm_seo_enabled"]),
            playback_rate=float(row["playback_rate"]),
            subtitle_margin_v=int(row["subtitle_margin_v"]),
            subtitle_font_family=str(row["subtitle_font_family"]),
            balanced_vertical_offset=int(row["balanced_vertical_offset"]),
            filename=path.name if path else None,
            metadata_filename=metadata_path.name if metadata_path and metadata_path.exists() else None,
            error=row["error"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
