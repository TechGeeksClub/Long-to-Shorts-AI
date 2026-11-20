import asyncio
import errno
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI, File, HTTPException, UploadFile
from fastapi.testclient import TestClient
from starlette.datastructures import Headers
import starlette.formparsers

from app.config import Settings
from app.main import create_app
from app.merging import VideoInfo
from app.uploads import SafeUploadRoute, validate_upload_space
import app.uploads as uploads


@pytest.fixture
def storage(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data", frontend_dist=tmp_path / "missing", minimum_free_bytes=100)
    settings.data_dir.mkdir()
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    monkeypatch.setattr(uploads.tempfile, "gettempdir", lambda: str(temporary))
    monkeypatch.setattr(uploads, "_same_volume", lambda *_: True)
    monkeypatch.setattr(uploads.shutil, "disk_usage", lambda _: SimpleNamespace(free=10**12))
    return settings, temporary


def test_shared_volume_reserves_both_spool_and_destination_copies(storage, monkeypatch):
    settings, _ = storage
    monkeypatch.setattr(uploads.shutil, "disk_usage", lambda _: SimpleNamespace(free=499))
    with pytest.raises(HTTPException) as result:
        validate_upload_space(settings, 200)
    assert result.value.status_code == 507
    assert "geçici" in result.value.detail.lower()
    monkeypatch.setattr(uploads.shutil, "disk_usage", lambda _: SimpleNamespace(free=500))
    validate_upload_space(settings, 200)


def test_separate_volumes_check_temporary_space_and_destination_reserve(storage, monkeypatch):
    settings, temporary = storage
    monkeypatch.setattr(uploads, "_same_volume", lambda *_: False)
    free = {settings.data_dir.resolve(): 300, temporary.resolve(): 200}
    monkeypatch.setattr(uploads.shutil, "disk_usage", lambda path: SimpleNamespace(free=free[Path(path)]))
    validate_upload_space(settings, 200)
    free[temporary.resolve()] = 199
    with pytest.raises(HTTPException) as result:
        validate_upload_space(settings, 200)
    assert result.value.status_code == 507
    assert "geçici" in result.value.detail.lower()
    free[temporary.resolve()] = 200
    free[settings.data_dir.resolve()] = 299
    with pytest.raises(HTTPException) as result:
        validate_upload_space(settings, 200)
    assert result.value.status_code == 507
    assert "Video klasörünün" in result.value.detail


def make_upload_app(settings):
    app = FastAPI()
    app.state.settings = settings
    app.state.endpoint_calls = 0
    app.state.body_reads = 0
    router = APIRouter(route_class=SafeUploadRoute)

    @router.post("/files")
    async def accept(videos: list[UploadFile] = File(...)):
        app.state.endpoint_calls += 1
        return {"sizes": [len(await item.read()) for item in videos]}

    @router.post("/json")
    async def accept_json(payload: dict):
        return payload

    app.include_router(router)

    class TrackReceive:
        async def __call__(self, scope, receive, send):
            async def counted_receive():
                if scope["type"] == "http":
                    app.state.body_reads += 1
                return await receive()
            return await app(scope, counted_receive, send)

    return app, TrackReceive()


def test_rejects_insufficient_space_before_reading_request_body(storage, monkeypatch):
    settings, _ = storage
    app, tracked = make_upload_app(settings)
    monkeypatch.setattr(uploads.shutil, "disk_usage", lambda _: SimpleNamespace(free=99))
    with TestClient(tracked) as client:
        response = client.post("/files", files={"videos": ("video.mp4", b"payload", "video/mp4")})
    assert response.status_code == 507
    assert app.state.body_reads == 0
    assert app.state.endpoint_calls == 0


def test_large_multipart_files_roll_to_disk_without_changing_file_validation(storage, monkeypatch):
    settings, _ = storage
    app, tracked = make_upload_app(settings)
    created = []
    real_factory = starlette.formparsers.SpooledTemporaryFile

    def capture(*args, **kwargs):
        file = real_factory(*args, **kwargs)
        created.append(file)
        return file

    monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", capture)
    with TestClient(tracked) as client:
        response = client.post("/files", files=[
            ("videos", ("Türkçe birinci.mp4", b"A" * (2 * 1024 * 1024), "video/mp4")),
            ("videos", ("second.mp4", b"B" * (2 * 1024 * 1024), "video/mp4")),
        ])
        assert response.status_code == 200
        assert response.json() == {"sizes": [2 * 1024 * 1024, 2 * 1024 * 1024]}
        invalid = client.post("/files", files={"wrong_name": ("video.mp4", b"a", "video/mp4")})
        assert invalid.status_code == 422
        assert client.post("/json", json={"unchanged": True}).json() == {"unchanged": True}
    assert created and all(file.closed for file in created)
    assert all(file._rolled for file in created[:2])
    assert app.state.endpoint_calls == 1


@pytest.mark.parametrize("failure,status", [(OSError(errno.ENOSPC, "synthetic full disk"), 507),
                                           (PermissionError(errno.EACCES, "synthetic permission denial"), 500)])
def test_rollover_errors_return_actionable_status_and_close_all_files_without_jobs(storage, monkeypatch, failure, status):
    settings, _ = storage
    app = create_app(settings, start_worker=False)
    created = []
    real_factory = starlette.formparsers.SpooledTemporaryFile

    def broken_factory(*args, **kwargs):
        file = real_factory(*args, **kwargs)
        created.append(file)
        def broken_rollover():
            raise failure
        file.rollover = broken_rollover
        return file

    monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", broken_factory)
    with TestClient(app) as client:
        response = client.post("/api/projects", files=[
            ("videos", ("small.mp4", b"small", "video/mp4")),
            ("videos", ("large.mp4", b"B" * (2 * 1024 * 1024), "video/mp4")),
        ])
        assert response.status_code == status
        assert "There was an error parsing" not in response.json()["detail"]
        assert ("boş yer" in response.json()["detail"] if status == 507 else "yazma izinlerini" in response.json()["detail"])
        assert client.get("/api/projects").json() == []
        assert client.get("/api/jobs").json() == []
    assert created and all(file.closed for file in created)
    assert list((settings.data_dir / "jobs").glob("*")) == []


def test_project_batch_upload_beyond_spool_threshold_succeeds(storage, monkeypatch):
    settings, _ = storage
    app = create_app(settings, start_worker=False)
    monkeypatch.setattr("app.projects.probe_video", lambda *_: VideoInfo(5, 320, 180, True, 25))
    with TestClient(app) as client:
        response = client.post("/api/projects", files=[
            ("videos", ("first.mp4", b"A" * (2 * 1024 * 1024), "video/mp4")),
            ("videos", ("second.mp4", b"B" * (2 * 1024 * 1024), "video/mp4")),
        ])
    assert response.status_code == 201
    assert response.json()["video_count"] == 2
    for video in response.json()["videos"]:
        assert Path(app.state.database.get_job_record(video["id"])["source_path"]).stat().st_size == 2 * 1024 * 1024


def run_raw_request(app, messages, *, headers=None):
    responses = []
    pending = iter(messages)
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "http",
             "method": "POST", "path": "/files", "raw_path": b"/files", "query_string": b"",
             "headers": headers or [(b"content-type", b"multipart/form-data; boundary=test")],
             "server": ("testserver", 80), "client": ("testclient", 1), "root_path": ""}
    async def receive():
        return next(pending, {"type": "http.disconnect"})
    async def send(message):
        responses.append(message)
    asyncio.run(app(scope, receive, send))
    return next(message["status"] for message in responses if message["type"] == "http.response.start"), responses


def test_unknown_length_client_disconnect_closes_rolled_temp_files(storage, monkeypatch):
    settings, _ = storage
    app, tracked = make_upload_app(settings)
    created = []
    real_factory = starlette.formparsers.SpooledTemporaryFile
    def capture(*args, **kwargs):
        file = real_factory(*args, **kwargs)
        created.append(file)
        return file
    monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", capture)
    payload = b'--test\r\nContent-Disposition: form-data; name="videos"; filename="test.mp4"\r\nContent-Type: video/mp4\r\n\r\n' + b"A" * (2 * 1024 * 1024)
    status, _ = run_raw_request(tracked, [{"type": "http.request", "body": payload, "more_body": True},
                                          {"type": "http.disconnect"}])
    assert status == 400
    assert created and all(file.closed for file in created)
    assert created[0]._rolled
    assert app.state.endpoint_calls == 0


def test_unknown_length_space_recheck_closes_partial_files(storage, monkeypatch):
    settings, _ = storage
    app, tracked = make_upload_app(settings)
    created = []
    real_factory = starlette.formparsers.SpooledTemporaryFile
    def capture(*args, **kwargs):
        file = real_factory(*args, **kwargs)
        created.append(file)
        return file
    monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", capture)
    monkeypatch.setattr(uploads, "SPACE_CHECK_INTERVAL", 1024)
    header = b'--test\r\nContent-Disposition: form-data; name="videos"; filename="test.mp4"\r\n\r\n'
    calls = 0
    def available_space(_):
        nonlocal calls
        calls += 1
        return SimpleNamespace(free=10**12 if calls <= 2 else 100)
    monkeypatch.setattr(uploads.shutil, "disk_usage", available_space)
    status, _ = run_raw_request(tracked, [
        {"type": "http.request", "body": header + b"A" * 100, "more_body": True},
        {"type": "http.request", "body": b"B" * 2048, "more_body": True},
        {"type": "http.request", "body": b"\r\n--test--\r\n", "more_body": False},
    ])
    assert status == 507
    assert created and all(file.closed for file in created)
    assert app.state.endpoint_calls == 0


@pytest.mark.parametrize("length", [b"-1", b"invalid", b""])
def test_invalid_content_length_rejected_before_parsing(storage, length):
    settings, _ = storage
    app, tracked = make_upload_app(settings)
    status, _ = run_raw_request(tracked, [], headers=[
        (b"content-type", b"multipart/form-data; boundary=test"), (b"content-length", length),
    ])
    assert status == 400
    assert app.state.body_reads == 0


def test_invalid_multipart_has_actionable_error(storage):
    settings, _ = storage
    app, tracked = make_upload_app(settings)
    with TestClient(tracked) as client:
        response = client.post("/files", content=b"invalid", headers={"content-type": "multipart/form-data"})
    assert response.status_code == 400
    assert "Video yükleme formu" in response.json()["detail"]


def test_cancelled_parse_closes_temp_files_and_preserves_cancellation(storage, monkeypatch):
    created = []
    real_factory = starlette.formparsers.SpooledTemporaryFile
    def capture(*args, **kwargs):
        file = real_factory(*args, **kwargs)
        created.append(file)
        return file
    monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", capture)
    async def stream():
        yield b'--test\r\nContent-Disposition: form-data; name="videos"; filename="test.mp4"\r\n\r\n' + b"A" * (2 * 1024 * 1024)
        raise asyncio.CancelledError()
    async def parse():
        parser = uploads.SafeMultiPartParser(Headers({"content-type": "multipart/form-data; boundary=test"}), stream())
        await parser.parse()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(parse())
    assert created and all(file.closed for file in created)
