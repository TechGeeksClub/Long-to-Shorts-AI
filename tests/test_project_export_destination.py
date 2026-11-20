import shutil
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.local_files import FileBrowserError
from app.local_video_picker import PickerError
from app.main import create_app
from app.merging import VideoInfo


@pytest.fixture
def destination_app(tmp_path, monkeypatch):
    app = create_app(Settings(
        data_dir=tmp_path / "data", frontend_dist=tmp_path / "absent", minimum_free_bytes=0,
    ), start_worker=False)
    monkeypatch.setattr("app.projects.probe_video", lambda *_: VideoInfo(60, 320, 180, True, 25))
    destination = tmp_path / "Dışa aktarılan videolar"
    destination.mkdir()
    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 12345)) as client:
        response = client.post("/api/projects", files=[
            ("videos", ("first.mp4", b"first")), ("videos", ("second.mp4", b"second")),
        ])
        assert response.status_code == 201
        yield app, client, response.json(), destination


def queue_cut(client, project, destination, **options):
    first = project["videos"][0]["id"]
    return client.post(f"/api/projects/{project['id']}/merges", json={
        "video_ids": [first], "cuts": {first: [{"start": 10, "end": 20}]},
        "export_mode": "fast", "output_directory": str(destination), **options,
    })


def renderer_stub(sources, output, work_dir, progress, **options):
    output.write_bytes(b"exported video")
    progress(0.5)
    return VideoInfo(50, 320, 180, True, 25)


def test_custom_destination_direct_render_metadata_reveal_and_project_delete(destination_app, monkeypatch):
    app, client, project, destination = destination_app
    unrelated = destination / "other.mp4"
    unrelated.write_bytes(b"unrelated")
    sources = [Path(app.state.database.get_job_record(video["id"])["source_path"]) for video in project["videos"]]
    source_content = [source.read_bytes() for source in sources]
    queued = queue_cut(client, project, destination)
    assert queued.status_code == 202, queued.text
    task = queued.json()
    assert task["output_directory"] == str(destination.resolve())
    assert task["output_path"] is None
    app.state.projects.initialize()
    assert client.get(f"/api/projects/{project['id']}").json()["merges"][0]["output_directory"] == task["output_directory"]
    calls = []

    def render(sources, output, work_dir, progress, **options):
        calls.append((output, work_dir))
        return renderer_stub(sources, output, work_dir, progress, **options)

    monkeypatch.setattr(app.state.projects.renderer, "render", render)
    app.state.projects.render(task["id"])
    result = client.get(f"/api/projects/{project['id']}").json()["merges"][0]
    output = Path(result["output_path"])
    assert result["status"] == "completed"
    assert output.parent == destination.resolve()
    assert task["id"] in output.name and output.suffix == ".mp4"
    assert calls[0][0] == output
    assert calls[0][1].parent == app.state.settings.data_dir / "jobs" / result["output_video_id"]
    assert not list(calls[0][1].parent.rglob("*.mp4")), "No second copy should be stored in the project workspace"
    assert app.state.database.get_job_record(result["output_video_id"])["source_path"] == str(output)
    assert client.get(f"/api/jobs/{result['output_video_id']}/source").content == b"exported video"
    base = f"/api/projects/{project['id']}/merges/{task['id']}"
    assert client.get(base + "/download").content == b"exported video"
    revealed = []
    monkeypatch.setattr("app.project_api.reveal_file", lambda path: revealed.append(path))
    assert client.post(base + "/reveal").status_code == 204
    assert revealed == [output]
    assert [source.read_bytes() for source in sources] == source_content
    assert client.delete(f"/api/projects/{project['id']}").status_code == 204
    assert output.read_bytes() == b"exported video" and unrelated.read_bytes() == b"unrelated"


@pytest.mark.parametrize("value", ["", "   ", "relative/path", "missing", "file"])
def test_bad_destinations_do_not_queue(destination_app, value):
    app, client, project, destination = destination_app
    if value == "missing":
        value = str(destination / "missing")
    elif value == "file":
        file = destination / "existing.mp4"
        file.write_bytes(b"existing")
        value = str(file)
    response = queue_cut(client, project, value)
    assert response.status_code == 422, response.text
    assert app.state.work_queue._queue.empty()


def test_unwritable_destination_is_actionable(destination_app, monkeypatch):
    app, client, project, destination = destination_app

    def denied(**kwargs):
        raise PermissionError("read only")

    monkeypatch.setattr("app.projects.tempfile.TemporaryFile", denied)
    response = queue_cut(client, project, destination)
    assert response.status_code == 422 and "yazılamıyor" in response.json()["detail"]
    assert app.state.work_queue._queue.empty()


def test_managed_job_workspaces_cannot_be_chosen_as_permanent_destination(destination_app):
    app, client, project, _ = destination_app
    managed = app.state.settings.data_dir / "jobs" / project["videos"][0]["id"]
    assert managed.is_dir()
    response = queue_cut(client, project, managed)
    assert response.status_code == 422 and "geçici video" in response.json()["detail"]
    assert app.state.work_queue._queue.empty()


def test_fast_export_checks_target_disk_and_precise_checks_work_disk(destination_app, monkeypatch):
    app, client, project, destination = destination_app
    object.__setattr__(app.state.settings, "minimum_free_bytes", 100)
    usage = shutil._ntuple_diskusage
    monkeypatch.setattr("app.projects.shutil.disk_usage", lambda path: usage(1000, 950, 50) if Path(path) == destination else usage(1000, 0, 1000))
    assert queue_cut(client, project, destination).status_code == 507
    monkeypatch.setattr("app.projects.shutil.disk_usage", lambda path: usage(1000, 0, 1000) if Path(path) == destination else usage(1000, 950, 50))
    assert queue_cut(client, project, destination, export_mode="precise").status_code == 507
    assert queue_cut(client, project, destination).status_code == 202


def test_preexisting_final_name_is_never_overwritten(destination_app, monkeypatch):
    app, client, project, destination = destination_app
    task = queue_cut(client, project, destination).json()
    row = app.state.projects.merge_record(project["id"], task["id"])
    output = app.state.projects._external_merge_output(row)
    output.write_bytes(b"existing file")
    monkeypatch.setattr(app.state.projects.renderer, "render", lambda *_a, **_k: pytest.fail("Must not render over an existing file"))
    app.state.projects.render(task["id"])
    failed = client.get(f"/api/projects/{project['id']}").json()["merges"][0]
    assert failed["status"] == "failed" and failed["output_path"] is None
    assert output.read_bytes() == b"existing file"


def test_failed_export_cleans_own_file_only(destination_app, monkeypatch):
    app, client, project, destination = destination_app
    unrelated = destination / "unrelated.mp4"
    unrelated.write_bytes(b"keep")
    task = queue_cut(client, project, destination).json()

    def fail(sources, output, *args, **options):
        output.write_bytes(b"incomplete")
        raise ValueError("encoding failure")

    monkeypatch.setattr(app.state.projects.renderer, "render", fail)
    app.state.projects.render(task["id"])
    assert list(destination.iterdir()) == [unrelated]
    result = client.get(f"/api/projects/{project['id']}").json()
    assert result["video_count"] == 2 and result["merges"][0]["status"] == "failed"


def test_restart_cleans_recorded_owned_external_partial_only(destination_app):
    app, client, project, destination = destination_app
    task = queue_cut(client, project, destination).json()
    row = app.state.projects.merge_record(project["id"], task["id"])
    output = app.state.projects._external_merge_output(row)
    output.write_bytes(b"reserved output")
    partial = destination / f".{output.stem}-{uuid.uuid4().hex}.partial.mp4"
    partial.write_bytes(b"partial")
    unrelated = destination / f".{output.stem}-unrelated.partial.mp4"
    unrelated.write_bytes(b"keep")
    app.state.projects._update_merge(task["id"], status="rendering", path=str(output))
    app.state.projects.interrupt_active()
    assert list(destination.iterdir()) == [unrelated]
    assert client.get(f"/api/projects/{project['id']}").json()["merges"][0]["status"] == "failed"


def test_unavailable_external_drive_does_not_block_interrupted_task_recovery(destination_app, monkeypatch):
    app, client, project, destination = destination_app
    task = queue_cut(client, project, destination).json()
    row = app.state.projects.merge_record(project["id"], task["id"])
    output = app.state.projects._external_merge_output(row)
    app.state.projects._update_merge(task["id"], status="rendering", path=str(output))
    original_unlink = Path.unlink

    def unavailable(path, *args, **kwargs):
        if path == output:
            raise OSError("drive unavailable")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unavailable)
    app.state.projects.interrupt_active()
    assert app.state.projects.merge_record(project["id"], task["id"])["status"] == "failed"


def test_export_directory_migration_retains_legacy_completed_path(destination_app, monkeypatch):
    app, client, project, _ = destination_app
    task = queue_cut(client, project, None, output_directory=None).json()
    monkeypatch.setattr(app.state.projects.renderer, "render", renderer_stub)
    app.state.projects.render(task["id"])
    with app.state.database.connect() as connection:
        connection.execute("ALTER TABLE project_merges DROP COLUMN output_directory")
    app.state.projects.initialize()
    app.state.projects.initialize()
    migrated = client.get(f"/api/projects/{project['id']}").json()["merges"][0]
    assert migrated["output_directory"] is None
    assert Path(migrated["output_path"]).read_bytes() == b"exported video"


def test_picker_cancellation_and_unavailability(destination_app, monkeypatch):
    _, client, _, _ = destination_app
    monkeypatch.setattr("app.project_api.choose_export_directory", lambda: None)
    assert client.post("/api/projects/pick-export-directory").json() == {"directory": None}

    def unavailable():
        raise PickerError("picker unavailable")

    monkeypatch.setattr("app.project_api.choose_export_directory", unavailable)
    assert client.post("/api/projects/pick-export-directory").status_code == 503


@pytest.mark.parametrize("headers", [{"Origin": "https://foreign.invalid"}, {"Host": "foreign.invalid"}])
def test_native_operations_and_custom_destination_reject_foreign_origin(destination_app, headers):
    _, client, project, destination = destination_app
    assert client.post("/api/projects/pick-export-directory", headers=headers).status_code == 403
    video = project["videos"][0]["id"]
    response = client.post(f"/api/projects/{project['id']}/merges", headers=headers, json={
        "video_ids": [video], "cuts": {video: [{"start": 1, "end": 2}]},
        "output_directory": str(destination),
    })
    assert response.status_code == 403
    assert client.post(f"/api/projects/{project['id']}/merges/missing/reveal", headers=headers).status_code == 403


def test_reveal_scopes_ready_and_missing_files(destination_app, monkeypatch):
    app, client, project, destination = destination_app
    task = queue_cut(client, project, destination).json()
    base = f"/api/projects/{project['id']}/merges/{task['id']}/reveal"
    assert client.post(base).status_code == 409
    assert client.post(f"/api/projects/other/merges/{task['id']}/reveal").status_code == 404
    monkeypatch.setattr(app.state.projects.renderer, "render", renderer_stub)
    app.state.projects.render(task["id"])

    def unavailable(path):
        raise FileBrowserError("Dosya Gezgini açılamadı", 503)

    monkeypatch.setattr("app.project_api.reveal_file", unavailable)
    assert client.post(base).status_code == 503
    monkeypatch.undo()
    row = app.state.projects.merge_record(project["id"], task["id"])
    Path(row["path"]).unlink()
    assert client.post(base).status_code == 404


def test_native_actions_and_external_destination_reject_remote_client(destination_app):
    app, _, project, destination = destination_app
    with TestClient(app, base_url="http://127.0.0.1:8000", client=("192.0.2.1", 12345)) as remote:
        assert remote.post("/api/projects/pick-export-directory").status_code == 403
        assert queue_cut(remote, project, destination).status_code == 403
        assert remote.post(f"/api/projects/{project['id']}/merges/missing/reveal").status_code == 403
