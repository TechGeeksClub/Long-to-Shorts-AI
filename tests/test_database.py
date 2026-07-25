from pathlib import Path

from app.database import Database
from app.models import ClipCandidate, InsertRange


def test_active_jobs_are_marked_interrupted(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    database.create_job("job", "video.mp4", tmp_path / "video.mp4")
    database.update_job("job", status="analyzing", stage="Çalışıyor")

    database.interrupt_active_jobs()

    job = database.get_job("job")
    assert job is not None
    assert job.status == "interrupted"
    assert job.error is not None


def test_face_tracking_is_disabled_by_default_and_persisted(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    database.create_job("job", "video.mp4", tmp_path / "video.mp4")
    clip = ClipCandidate(
        id="clip",
        job_id="job",
        rank=1,
        title="Klip",
        start=0,
        end=40,
        score=90,
        reasons=[],
        subtitles=[],
    )
    database.replace_clips("job", [clip])

    saved = database.get_clip("clip")
    assert saved is not None
    assert saved.framing_mode == "fit"
    assert saved.face_tracking_enabled is False

    database.update_clip("clip", framing_mode="fill", face_tracking_enabled=True)
    updated = database.get_clip("clip")
    assert updated is not None
    assert updated.framing_mode == "fill"
    assert updated.face_tracking_enabled is True

    database.update_clip("clip", framing_mode="balanced", face_tracking_enabled=False)
    balanced = database.get_clip("clip")
    assert balanced is not None
    assert balanced.framing_mode == "balanced"


def test_insert_ranges_are_persisted(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    database.create_job("job", "video.mp4", tmp_path / "video.mp4")
    clip = ClipCandidate(
        id="clip",
        job_id="job",
        rank=1,
        title="Klip",
        start=630,
        end=690,
        score=90,
        reasons=[],
        subtitles=[],
        insert_ranges=[
            InsertRange(source_start=80, source_end=90, insert_at=644)
        ],
    )

    database.replace_clips("job", [clip])
    saved = database.get_clip("clip")

    assert saved is not None
    assert saved.insert_ranges == [
        InsertRange(source_start=80, source_end=90, insert_at=644)
    ]
