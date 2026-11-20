from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.youtube_import import YoutubeImportError


VIDEO_URL = "https://www.youtube.com/watch?v=abcdefghijk"


@pytest.fixture
def api(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "data", frontend_dist=tmp_path / "missing", minimum_free_bytes=0)
    app = create_app(settings, start_worker=False)
    with TestClient(app) as client:
        yield app, client


def test_channel_browse_normalizes_url_and_returns_only_listing(api, monkeypatch):
    app, client = api

    @dataclass
    class Listing:
        channel_title: str
        channel_url: str
        videos: list
        has_more: bool

    calls = []

    def list_channel(url, limit, offset):
        calls.append((url, limit, offset))
        return Listing("Kanalım", url, [dict(id="abcdefghijk", title="Yolculuk", url=VIDEO_URL, duration=320)], True)

    monkeypatch.setattr("app.api.list_youtube_channel_videos", list_channel)
    response = client.post("/api/youtube/channel/videos", json={"url": "https://youtube.com/@mychannel?feature=shared"})
    assert response.status_code == 200
    result = response.json()
    assert calls == [("https://www.youtube.com/@mychannel/videos", 30, 0)]
    assert result["channel_title"] == "Kanalım"
    assert result["videos"][0]["title"] == "Yolculuk"
    assert result["has_more"] is True
    assert result["next_offset"] == 30
    assert app.state.database.list_jobs() == []
    assert app.state.work_queue._queue.empty()


def test_channel_browse_reports_extraction_failure(api, monkeypatch):
    _, client = api

    def unavailable(*args, **kwargs):
        raise YoutubeImportError("Kanalın videoları alınamadı.")

    monkeypatch.setattr("app.api.list_youtube_channel_videos", unavailable)
    response = client.post("/api/youtube/channel/videos", json={"url": "https://youtube.com/@mychannel"})
    assert response.status_code == 502
    assert "alınamadı" in response.json()["detail"]


def test_channel_browse_passes_page_offset_and_does_not_fetch_each_video(api, monkeypatch):
    _, client = api

    @dataclass
    class Listing:
        channel_title: str = "Kanalım"
        channel_url: str = "https://www.youtube.com/@mychannel/videos"
        videos: tuple = ()
        has_more: bool = False

    calls = []

    def list_channel(url, limit, offset):
        calls.append((url, limit, offset))
        return Listing()

    monkeypatch.setattr("app.api.list_youtube_channel_videos", list_channel)
    response = client.post("/api/youtube/channel/videos", json={"url": "https://youtube.com/@mychannel", "offset": 30})
    assert response.status_code == 200
    assert calls == [("https://www.youtube.com/@mychannel/videos", 30, 30)]
    assert response.json()["next_offset"] is None
    assert client.post("/api/youtube/channel/videos", json={"url": "https://youtube.com/@mychannel", "offset": -1}).status_code == 422


@pytest.mark.parametrize("route", ["/api/youtube/channel/videos", "/api/jobs/youtube"])
@pytest.mark.parametrize("url", ["http://127.0.0.1:8000/private", "https://youtube.com.evil.test/watch?v=abcdefghijk", ""])
def test_invalid_source_is_rejected_before_creating_work(api, route, url):
    app, client = api
    response = client.post(route, json={"url": url})
    assert response.status_code == 422
    assert app.state.database.list_jobs() == []
    assert app.state.work_queue._queue.empty()


def test_import_queues_without_network_and_persists_source(api):
    app, client = api
    response = client.post("/api/jobs/youtube", json={"url": "https://youtu.be/abcdefghijk?si=tracking"})
    assert response.status_code == 202
    job = response.json()
    assert job["status"] == "queued"
    assert job["source_type"] == "youtube"
    assert job["source_url"] == VIDEO_URL
    work = app.state.work_queue._queue.get_nowait()
    assert work.kind == "youtube_import"
    assert work.item_id == job["id"]
    assert client.get("/api/jobs").json()[0]["source_url"] == VIDEO_URL
    assert client.get(f"/api/jobs/{job['id']}/source").status_code == 404
    assert not Path(app.state.database.get_job_record(job["id"])["source_path"]).exists()


def test_importing_job_cannot_be_deleted_or_reanalyzed(api):
    app, client = api
    job = client.post("/api/jobs/youtube", json={"url": VIDEO_URL}).json()
    app.state.database.update_job(job["id"], status="importing", stage="İndiriliyor")
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 409
    assert client.post(f"/api/jobs/{job['id']}/reanalyze").status_code == 409
    assert client.post(f"/api/jobs/{job['id']}/transcript/correct").status_code == 409


@pytest.mark.parametrize("downloaded", [False, True])
def test_retry_downloads_missing_source_or_reuses_existing_file(api, downloaded):
    app, client = api
    job = client.post("/api/jobs/youtube", json={"url": VIDEO_URL}).json()
    app.state.work_queue._queue.get_nowait()
    app.state.database.update_job(job["id"], status="failed", stage="Hata", error="Bağlantı kesildi")
    if downloaded:
        source = Path(app.state.database.get_job_record(job["id"])["source_path"])
        source.write_bytes(b"existing-source")
    response = client.post(f"/api/jobs/{job['id']}/reanalyze")
    assert response.status_code == 202
    assert response.json()["error"] is None
    work = app.state.work_queue._queue.get_nowait()
    assert work.kind == ("analyze" if downloaded else "youtube_import")


def test_failed_queue_submission_removes_job_and_directory(api, monkeypatch):
    app, client = api

    def stopped(_):
        raise RuntimeError("Uygulama kapanıyor")

    monkeypatch.setattr(app.state.work_queue, "enqueue_youtube_import", stopped)
    response = client.post("/api/jobs/youtube", json={"url": VIDEO_URL})
    assert response.status_code == 503
    assert app.state.database.list_jobs() == []
    assert list((app.state.settings.data_dir / "jobs").iterdir()) == []


def test_import_rejects_insufficient_disk_space(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data", frontend_dist=tmp_path / "missing", minimum_free_bytes=100)
    app = create_app(settings, start_worker=False)
    monkeypatch.setattr("app.api.shutil.disk_usage", lambda _: SimpleNamespace(free=99))
    with TestClient(app) as client:
        response = client.post("/api/jobs/youtube", json={"url": VIDEO_URL})
    assert response.status_code == 507
    assert app.state.database.list_jobs() == []


def test_upload_jobs_still_expose_default_source_type(api):
    _, client = api
    response = client.post("/api/jobs", files={"video": ("sample.mp4", b"video", "video/mp4")})
    assert response.status_code == 202
    assert response.json()["source_type"] == "upload"
    assert response.json()["source_url"] is None


def test_restart_interrupts_importing_job(api):
    app, client = api
    job = client.post("/api/jobs/youtube", json={"url": VIDEO_URL}).json()
    app.state.database.update_job(job["id"], status="importing", stage="İndiriliyor")
    app.state.database.interrupt_active_jobs()
    restored = app.state.database.get_job(job["id"])
    assert restored.status == "interrupted"
    assert restored.source_url == VIDEO_URL


def test_retry_queue_failure_leaves_a_recoverable_failed_job(api, monkeypatch):
    app, client = api
    job = client.post("/api/jobs/youtube", json={"url": VIDEO_URL}).json()
    app.state.database.update_job(job["id"], status="failed", stage="Hata")

    def stopped(_):
        raise RuntimeError("Uygulama kapanıyor")

    monkeypatch.setattr(app.state.work_queue, "enqueue_youtube_import", stopped)
    response = client.post(f"/api/jobs/{job['id']}/reanalyze")
    assert response.status_code == 503
    assert app.state.database.get_job(job["id"]).status == "failed"
