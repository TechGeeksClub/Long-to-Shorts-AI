from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from app.models import (
    AnalysisReport,
    CorrectionReport,
    ClipCandidate,
    CropKeyframe,
    CutRange,
    EditSettings,
    Export,
    InsertRange,
    JobDetail,
    JobSummary,
    SubtitleCue,
    YoutubePackage,
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
                    content_score REAL NOT NULL DEFAULT 0,
                    integrity_score REAL NOT NULL DEFAULT 0,
                    selection_method TEXT NOT NULL DEFAULT 'heuristic',
                    reasons_json TEXT NOT NULL,
                    subtitles_json TEXT NOT NULL,
                    cut_ranges_json TEXT NOT NULL DEFAULT '[]',
                    insert_ranges_json TEXT NOT NULL DEFAULT '[]',
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
            job_columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
            if "source_type" not in job_columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN source_type TEXT NOT NULL DEFAULT 'upload'")
            if "source_url" not in job_columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN source_url TEXT")
            if "analysis_json" not in job_columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN analysis_json TEXT")
            if "correction_json" not in job_columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN correction_json TEXT")
            if "youtube_json" not in clip_columns:
                connection.execute("ALTER TABLE clips ADD COLUMN youtube_json TEXT NOT NULL DEFAULT '{}'")
            if "topic_id" not in clip_columns:
                connection.execute("ALTER TABLE clips ADD COLUMN topic_id TEXT")
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
            if "content_score" not in clip_columns:
                connection.execute(
                    "ALTER TABLE clips ADD COLUMN content_score REAL NOT NULL DEFAULT 0"
                )
            if "integrity_score" not in clip_columns:
                connection.execute(
                    "ALTER TABLE clips ADD COLUMN integrity_score REAL NOT NULL DEFAULT 0"
                )
            if "selection_method" not in clip_columns:
                connection.execute(
                    """
                    ALTER TABLE clips
                    ADD COLUMN selection_method TEXT NOT NULL DEFAULT 'heuristic'
                    """
                )
            if "insert_ranges_json" not in clip_columns:
                connection.execute(
                    """
                    ALTER TABLE clips
                    ADD COLUMN insert_ranges_json TEXT NOT NULL DEFAULT '[]'
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
            for table, columns in (("clips", clip_columns), ("exports", export_columns)):
                if "edit_settings_json" not in columns:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN edit_settings_json TEXT NOT NULL DEFAULT '{{}}'"
                    )
            if "is_preview" not in export_columns:
                connection.execute(
                    "ALTER TABLE exports ADD COLUMN is_preview INTEGER NOT NULL DEFAULT 0"
                )
            if "youtube_json" not in export_columns:
                connection.execute("ALTER TABLE exports ADD COLUMN youtube_json TEXT NOT NULL DEFAULT '{}'")

    def interrupt_active_jobs(self) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'interrupted', stage = 'Uygulama yeniden başlatıldı',
                    error = 'İşlem uygulama kapanırken yarıda kaldı.', updated_at = ?
                WHERE status IN ('queued', 'importing', 'analyzing', 'exporting')
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
            connection.execute(
                """UPDATE clips SET youtube_json = json_set(youtube_json, '$.status', 'failed',
                    '$.error', 'Uygulama kapanırken YouTube hazırlığı yarıda kaldı; yeniden üretebilirsin.')
                    WHERE json_extract(youtube_json, '$.status') IN ('queued', 'generating')"""
            )

    def create_job(
        self, job_id: str, filename: str, source_path: Path, *,
        source_type: str = "upload", source_url: str | None = None,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, filename, source_path, source_type, source_url,
                    status, progress, stage, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', 0, 'Sırada bekliyor', ?, ?)
                """,
                (job_id, filename, str(source_path), source_type, source_url, now, now),
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
                    id, job_id, rank, title, start, end, score, content_score,
                    integrity_score, selection_method, reasons_json,
                    subtitles_json, cut_ranges_json, insert_ranges_json,
                    crop_keyframes_json, framing_mode,
                    face_tracking_enabled, selected, edit_settings_json, topic_id, youtube_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        clip.content_score,
                        clip.integrity_score,
                        clip.selection_method,
                        json.dumps(clip.reasons, ensure_ascii=False),
                        json.dumps(
                            [cue.model_dump() for cue in clip.subtitles], ensure_ascii=False
                        ),
                        json.dumps(
                            [cut.model_dump() for cut in clip.cut_ranges], ensure_ascii=False
                        ),
                        json.dumps(
                            [insert.model_dump() for insert in clip.insert_ranges],
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            [keyframe.model_dump() for keyframe in clip.crop_keyframes]
                        ),
                        clip.framing_mode,
                        int(clip.face_tracking_enabled),
                        int(clip.selected),
                        clip.edit_settings.model_dump_json(),
                        clip.topic_id,
                        clip.youtube.model_dump_json(),
                    )
                    for clip in clips
                ],
            )

    def create_clip(self, clip: ClipCandidate) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO clips (
                    id, job_id, rank, title, start, end, score, content_score,
                    integrity_score, selection_method, reasons_json,
                    subtitles_json, cut_ranges_json, insert_ranges_json,
                    crop_keyframes_json, framing_mode,
                    face_tracking_enabled, selected, edit_settings_json, topic_id, youtube_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clip.id,
                    clip.job_id,
                    clip.rank,
                    clip.title,
                    clip.start,
                    clip.end,
                    clip.score,
                    clip.content_score,
                    clip.integrity_score,
                    clip.selection_method,
                    json.dumps(clip.reasons, ensure_ascii=False),
                    json.dumps(
                        [cue.model_dump() for cue in clip.subtitles], ensure_ascii=False
                    ),
                    json.dumps(
                        [cut.model_dump() for cut in clip.cut_ranges], ensure_ascii=False
                    ),
                    json.dumps(
                        [insert.model_dump() for insert in clip.insert_ranges],
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        [keyframe.model_dump() for keyframe in clip.crop_keyframes]
                    ),
                    clip.framing_mode,
                    int(clip.face_tracking_enabled),
                    int(clip.selected),
                    clip.edit_settings.model_dump_json(),
                    clip.topic_id,
                    clip.youtube.model_dump_json(),
                ),
            )

    def merge_agent_analysis(
        self, job_id: str, clips: list[ClipCandidate], report: AnalysisReport,
    ) -> None:
        """Commit a new analysis without replacing user edits or cascading exports."""
        with self.connect() as connection:
            existing = {
                row["id"] for row in connection.execute("SELECT id FROM clips WHERE job_id = ?", (job_id,))
            }
            rank = connection.execute(
                "SELECT COALESCE(MAX(rank), 0) FROM clips WHERE job_id = ?", (job_id,),
            ).fetchone()[0]
            for clip in clips:
                if clip.job_id != job_id or clip.selection_method != "agent":
                    raise ValueError("Konu analizi başka bir işe ait kesit içeriyor.")
                if clip.id in existing:
                    continue
                rank += 1
                connection.execute(
                    """INSERT INTO clips (
                        id, job_id, rank, title, start, end, score, content_score,
                        integrity_score, selection_method, reasons_json, subtitles_json,
                        topic_id, selected, edit_settings_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (clip.id, job_id, rank, clip.title, clip.start, clip.end, clip.score,
                     clip.content_score, clip.integrity_score, clip.selection_method,
                     json.dumps(clip.reasons, ensure_ascii=False),
                     json.dumps([cue.model_dump() for cue in clip.subtitles], ensure_ascii=False),
                     clip.topic_id, int(clip.selected), clip.edit_settings.model_dump_json()),
                )
                existing.add(clip.id)
            if any(clip_id not in existing for clip_id in report.clip_ids):
                raise ValueError("Konu planındaki kesitlerden biri kaydedilemedi.")
            connection.execute(
                """UPDATE jobs SET analysis_json = ?, status = 'ready', progress = 1,
                   stage = ?, error = NULL, updated_at = ? WHERE id = ?""",
                (report.model_dump_json(),
                 f"Codex {len(report.topics)} konu ve {len(report.clip_ids)} kesit hazırladı",
                 utc_now(), job_id),
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
            elif key == "insert_ranges":
                encoded["insert_ranges_json"] = json.dumps(
                    [insert.model_dump() for insert in value], ensure_ascii=False
                )
            elif key == "crop_keyframes":
                encoded["crop_keyframes_json"] = json.dumps(
                    [frame.model_dump() for frame in value]
                )
            elif key == "youtube":
                encoded["youtube_json"] = YoutubePackage.model_validate(value).model_dump_json()
            elif key == "edit_settings":
                encoded["edit_settings_json"] = EditSettings.model_validate(value).model_dump_json()
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
        edit_settings: EditSettings | None = None,
        is_preview: bool = False,
        youtube: YoutubePackage | None = None,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO exports (
                    id, job_id, clip_id, status, progress, llm_seo_enabled,
                    playback_rate, subtitle_margin_v, subtitle_font_family,
                    balanced_vertical_offset, edit_settings_json, is_preview, youtube_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'queued', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    (edit_settings or EditSettings()).model_dump_json(),
                    int(is_preview),
                    (youtube or YoutubePackage()).model_dump_json(),
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
            analysis=AnalysisReport.model_validate_json(job["analysis_json"]) if job["analysis_json"] else None,
            correction=CorrectionReport.model_validate_json(job["correction_json"]) if job["correction_json"] else None,
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
            source_type=row["source_type"],
            source_url=row["source_url"],
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
            content_score=row["content_score"],
            integrity_score=row["integrity_score"],
            selection_method=row["selection_method"],
            topic_id=row["topic_id"],
            youtube=YoutubePackage.model_validate_json(row["youtube_json"]),
            reasons=json.loads(row["reasons_json"]),
            subtitles=[SubtitleCue.model_validate(cue) for cue in json.loads(row["subtitles_json"])],
            cut_ranges=[
                CutRange.model_validate(cut)
                for cut in json.loads(row["cut_ranges_json"])
            ],
            insert_ranges=[
                InsertRange.model_validate(insert)
                for insert in json.loads(row["insert_ranges_json"])
            ],
            crop_keyframes=[
                CropKeyframe.model_validate(frame)
                for frame in json.loads(row["crop_keyframes_json"])
            ],
            framing_mode=row["framing_mode"],
            face_tracking_enabled=bool(row["face_tracking_enabled"]),
            selected=bool(row["selected"]),
            edit_settings=EditSettings.model_validate_json(row["edit_settings_json"]),
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
            edit_settings=EditSettings.model_validate_json(row["edit_settings_json"]),
            filename=path.name if path else None,
            is_preview=bool(row["is_preview"]),
            metadata_filename=metadata_path.name if metadata_path and metadata_path.exists() else None,
            thumbnail_filename="thumbnail.jpg" if path and (path.parent / "thumbnail.jpg").is_file() else None,
            package_filename="youtube-package.zip" if path and (path.parent / "youtube-package.zip").is_file() else None,
            error=row["error"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
