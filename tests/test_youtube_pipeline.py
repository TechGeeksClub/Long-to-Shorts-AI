from pathlib import Path
import sqlite3
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import Database
from app.main import create_app
from app.processor import Processor
from app.queue import WorkQueue
from app.youtube_import import YoutubeImportError


VIDEO_URL = "https://www.youtube.com/watch?v=abcdefghijk"


@pytest.fixture
def pipeline(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "data", minimum_free_bytes=0)
    database = Database(settings.database_path)
    database.initialize()
    job_dir = settings.data_dir / "jobs" / "job"
    job_dir.mkdir(parents=True)
    database.create_job("job", "YouTube video", job_dir / "youtube-source.mp4", source_type="youtube", source_url=VIDEO_URL)
    return Processor(database, settings), database, job_dir


def test_download_finishes_before_analysis_and_keeps_title_and_source(pipeline, monkeypatch):
    processor, database, job_dir = pipeline
    calls = []

    def download(url, destination, settings, progress_callback, cancel_event):
        assert database.get_job("job").status == "importing"
        assert destination == job_dir
        assert url == VIDEO_URL
        assert not cancel_event.is_set()
        progress_callback(.4, "YouTube videosu indiriliyor")
        assert database.get_job("job").progress == .4
        source = destination / "youtube-source.webm"
        source.write_bytes(b"downloaded-video")
        calls.append("download")
        return SimpleNamespace(source_path=source, title="Yol günlüğüm", url=url, video_id="abcdefghijk")

    def analyze(job_id):
        record = database.get_job_record(job_id)
        assert Path(record["source_path"]).read_bytes() == b"downloaded-video"
        assert record["filename"] == "Yol günlüğüm.webm"
        assert record["source_url"] == VIDEO_URL
        calls.append("analyze")

    monkeypatch.setattr("app.processor.download_youtube_video", download)
    monkeypatch.setattr(processor, "analyze", analyze)
    processor.import_youtube("job")
    assert calls == ["download", "analyze"]


def test_download_failure_is_saved_without_starting_analysis(pipeline, monkeypatch):
    processor, database, _ = pipeline

    def download(*args, **kwargs):
        raise YoutubeImportError("Video özel veya kullanılamıyor.")

    monkeypatch.setattr("app.processor.download_youtube_video", download)
    monkeypatch.setattr(processor, "analyze", lambda _: pytest.fail("Failed downloads cannot be analyzed"))
    processor.import_youtube("job")
    job = database.get_job("job")
    assert job.status == "failed"
    assert job.error == "Video özel veya kullanılamıyor."
    assert job.source_url == VIDEO_URL


def test_shutdown_cancellation_blocks_following_analysis(pipeline, monkeypatch):
    processor, database, job_dir = pipeline

    def download(*args, **kwargs):
        source = job_dir / "youtube-source.mkv"
        source.write_bytes(b"downloaded")
        processor.cancel_imports()
        assert kwargs["cancel_event"].is_set()
        return SimpleNamespace(source_path=source, title="Video", url=VIDEO_URL, video_id="abcdefghijk")

    monkeypatch.setattr("app.processor.download_youtube_video", download)
    monkeypatch.setattr(processor, "analyze", lambda _: pytest.fail("Analysis must not start during shutdown"))
    processor.import_youtube("job")
    assert database.get_job("job").status == "interrupted"
    record = database.get_job_record("job")
    assert Path(record["source_path"]) == job_dir / "youtube-source.mkv"
    assert database.get_job("job").filename == "Video.mkv"
    # A restart retries analysis against the completed source rather than trying
    # to download another container over the file that was already saved.
    app = create_app(processor.settings, start_worker=False)
    with TestClient(app) as client:
        response = client.post("/api/jobs/job/reanalyze")
    assert response.status_code == 202
    assert app.state.work_queue._queue.get_nowait().kind == "analyze"


def test_cancelled_import_never_starts_downloader(pipeline, monkeypatch):
    processor, database, _ = pipeline
    processor.cancel_imports()
    monkeypatch.setattr("app.processor.download_youtube_video", lambda *a, **kw: pytest.fail("Import was cancelled"))
    processor.import_youtube("job")
    assert database.get_job("job").status == "interrupted"


def test_unexpected_download_error_does_not_leave_import_active(pipeline, monkeypatch):
    processor, database, _ = pipeline

    def download(*args, **kwargs):
        raise OSError("network details")

    monkeypatch.setattr("app.processor.download_youtube_video", download)
    processor.import_youtube("job")
    assert database.get_job("job").status == "failed"
    assert "beklenmeyen" in database.get_job("job").error


def test_queue_dispatches_youtube_imports_to_their_own_handler():
    completed = Event()
    seen = []

    def imported(job_id):
        seen.append(job_id)
        completed.set()

    queue = WorkQueue(lambda _: pytest.fail("Wrong analysis handler"), lambda _: None, youtube_import_handler=imported)
    queue.start()
    queue.enqueue_youtube_import("youtube-job")
    assert completed.wait(timeout=3)
    queue.stop()
    assert seen == ["youtube-job"]
    with pytest.raises(RuntimeError):
        queue.enqueue_youtube_import("after-shutdown")


def test_existing_database_migrates_upload_sources_without_losing_jobs(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL, source_path TEXT NOT NULL,
                status TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0, stage TEXT NOT NULL,
                error TEXT, duration REAL, width INTEGER, height INTEGER, has_audio INTEGER,
                language TEXT, transcript_path TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            INSERT INTO jobs (id, filename, source_path, status, stage, created_at, updated_at)
            VALUES ('old', 'original.mp4', 'original.mp4', 'ready', 'Hazır',
                    '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
        """)
    database = Database(path)
    database.initialize()
    old = database.get_job("old")
    assert old.filename == "original.mp4"
    assert old.status == "ready"
    assert old.source_type == "upload"
    assert old.source_url is None
    # Repeated application startup remains safe after the columns have been added.
    database.initialize()
    assert len(database.list_jobs()) == 1
