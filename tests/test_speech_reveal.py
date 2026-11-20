from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import utc_now
from app.local_files import FileBrowserError
from app.main import create_app


@pytest.fixture
def reveal_app(tmp_path, monkeypatch):
    settings = Settings(
        data_dir=tmp_path / "data",
        frontend_dist=tmp_path / "absent",
        speech_export_dir=tmp_path / "external exports",
        minimum_free_bytes=0,
    )
    application = create_app(settings, start_worker=False)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"original video")
    application.state.database.create_job("video", source.name, source)
    application.state.database.update_job("video", status="ready", duration=3)
    output = settings.speech_export_dir / "video" / "export" / "İşlenmiş video.mp4"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"completed export")
    with application.state.database.connect() as connection:
        connection.execute(
            "INSERT INTO speech_cleanup_exports "
            "(id,job_id,mode,status,progress,duration,source_duration,ranges_json,path,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("export", "video", "mute", "completed", 1, 3, 3, "[]", str(output), utc_now()),
        )
    revealed = []
    monkeypatch.setattr("app.speech_api.reveal_file", lambda path: revealed.append(path))
    with TestClient(application, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 12345)) as client:
        yield application, client, source, output, revealed


ENDPOINT = "/api/jobs/video/speech-cleanup/exports/export/reveal"


def test_reveal_completed_external_export_without_copying_or_removing_files(reveal_app):
    _, client, source, output, revealed = reveal_app
    response = client.post(ENDPOINT, headers={"Origin": "http://127.0.0.1:8000"})
    assert response.status_code == 204, response.text
    assert response.content == b""
    assert revealed == [output]
    assert isinstance(revealed[0], Path)
    assert list(output.parent.iterdir()) == [output]
    assert output.read_bytes() == b"completed export"
    assert source.read_bytes() == b"original video"


@pytest.mark.parametrize("status,path", [("queued", "existing"), ("rendering", "existing"),
                                         ("failed", "existing"), ("completed", None)])
def test_reveal_rejects_exports_that_are_not_ready(reveal_app, status, path):
    application, client, _, output, revealed = reveal_app
    with application.state.database.connect() as connection:
        connection.execute("UPDATE speech_cleanup_exports SET status=?, path=? WHERE id='export'",
                           (status, str(output) if path else None))
    assert client.post(ENDPOINT).status_code == 409
    assert revealed == []


def test_reveal_reports_missing_output_without_opening_explorer(reveal_app):
    _, client, _, output, revealed = reveal_app
    output.unlink()
    assert client.post(ENDPOINT).status_code == 404
    assert revealed == []


@pytest.mark.parametrize("endpoint", [
    "/api/jobs/video/speech-cleanup/exports/unknown/reveal",
    "/api/jobs/other/speech-cleanup/exports/export/reveal",
])
def test_reveal_rejects_unknown_or_foreign_export(reveal_app, endpoint):
    application, client, source, _, revealed = reveal_app
    application.state.database.create_job("other", source.name, source)
    assert client.post(endpoint).status_code == 404
    assert revealed == []


@pytest.mark.parametrize("headers", [
    {"Origin": "https://other.invalid"},
    {"Host": "rebound.invalid"},
])
def test_reveal_rejects_nonlocal_host_or_origin(reveal_app, headers):
    _, client, _, _, revealed = reveal_app
    assert client.post(ENDPOINT, headers=headers).status_code == 403
    assert revealed == []


def test_reveal_rejects_remote_client(reveal_app):
    application, _, _, _, revealed = reveal_app
    with TestClient(application, base_url="http://127.0.0.1:8000", client=("192.0.2.1", 12345)) as client:
        assert client.post(ENDPOINT).status_code == 403
    assert revealed == []


def test_reveal_reports_unavailable_file_browser(reveal_app, monkeypatch):
    _, client, _, output, revealed = reveal_app

    def unavailable(path):
        assert path == output
        raise FileBrowserError("Dosya Gezgini açılamadı. Yeniden dene.")

    monkeypatch.setattr("app.speech_api.reveal_file", unavailable)
    response = client.post(ENDPOINT)
    assert response.status_code == 503
    assert response.json()["detail"] == "Dosya Gezgini açılamadı. Yeniden dene."
    assert revealed == []
