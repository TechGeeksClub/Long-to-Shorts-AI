from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.merging import VideoInfo


@pytest.fixture
def path_app(tmp_path, monkeypatch):
    settings = Settings(
        data_dir=tmp_path / "data",
        frontend_dist=tmp_path / "absent",
        minimum_free_bytes=0,
    )
    application = create_app(settings, start_worker=False)

    def probe(path, _):
        if path.read_bytes() == b"bad":
            raise ValueError("invalid video")
        return VideoInfo(4, 640, 360, True, 30)

    monkeypatch.setattr("app.projects.probe_video", probe)
    original_dir = tmp_path / "Özgün Video Dosyaları"
    original_dir.mkdir()
    with TestClient(application, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 12345)) as client:
        yield application, client, original_dir


def _source(folder, name, body=b"video"):
    path = folder / name
    path.write_bytes(body)
    return path


def test_linked_project_reuses_original_files_and_adds_in_order(path_app):
    app, client, folder = path_app
    first = _source(folder, "İlk kayıt.MP4", b"first")
    second = _source(folder, "Sonraki kayıt.mov", b"second")
    third = _source(folder, "Bir kayıt daha.webm", b"third")

    response = client.post("/api/projects/from-paths", json={"paths": [f'"{first}"', str(second)]})
    assert response.status_code == 201, response.text
    project = response.json()
    first_video, second_video = project["videos"]
    assert project["active_video_id"] == first_video["id"]
    base = f"/api/projects/{project['id']}"
    selected = client.put(base + "/active-video", json={"video_id": second_video["id"]})
    assert selected.status_code == 200

    added = client.post(base + "/videos/from-paths", json={"paths": [str(third)]})
    assert added.status_code == 201, added.text
    project = added.json()
    assert project["active_video_id"] == second_video["id"]
    assert [item["filename"] for item in project["videos"]] == [first.name, second.name, third.name]
    for item, source in zip(project["videos"], (first, second, third)):
        assert item["duration"] == 4 and item["status"] == "ready"
        assert Path(app.state.database.get_job_record(item["id"])["source_path"]) == source.resolve()
        assert client.get(f"/api/jobs/{item['id']}/source").content == source.read_bytes()

    assert not (app.state.settings.data_dir / "jobs").exists()
    assert app.state.work_queue._queue.empty()


def test_failed_link_batch_leaves_existing_project_unchanged(path_app):
    app, client, folder = path_app
    existing = _source(folder, "existing.mp4")
    good = _source(folder, "good.mp4")
    bad = _source(folder, "bad.mp4", b"bad")
    creation = client.post("/api/projects/from-paths", json={"paths": [str(existing)]})
    assert creation.status_code == 201
    project = creation.json()
    create = client.post("/api/projects/from-paths", json={"paths": [str(good), str(bad)]})
    added = client.post(
        f"/api/projects/{project['id']}/videos/from-paths",
        json={"paths": [str(good), str(folder / "moved.mp4")]},
    )
    assert create.status_code == 422
    assert added.status_code == 404
    assert client.get(f"/api/projects/{project['id']}").json() == project
    assert len(client.get("/api/projects").json()) == 1
    assert len(app.state.database.list_jobs()) == 1


def test_equivalent_paths_cannot_add_duplicate_source(path_app):
    _, client, folder = path_app
    source = _source(folder, "source.mp4")
    alternate = folder / "subfolder"
    alternate.mkdir()
    aliased_path = str(alternate / ".." / source.name)
    duplicate = client.post("/api/projects/from-paths", json={"paths": [str(source), aliased_path]})
    assert duplicate.status_code == 422
    assert client.get("/api/projects").json() == []

    project = client.post("/api/projects/from-paths", json={"paths": [str(source)]}).json()
    added = client.post(
        f"/api/projects/{project['id']}/videos/from-paths", json={"paths": [aliased_path]},
    )
    assert added.status_code == 409
    assert client.get(f"/api/projects/{project['id']}").json()["video_count"] == 1


def test_deleting_linked_project_only_removes_its_workspace(path_app):
    app, client, folder = path_app
    original = _source(folder, "original.mp4")
    unrelated = _source(folder, "unrelated.mov", b"other video")
    project = client.post("/api/projects/from-paths", json={"paths": [str(original)]}).json()
    workspace = app.state.settings.data_dir / "jobs" / project["videos"][0]["id"]
    workspace.mkdir(parents=True)
    (workspace / "transcript.json").write_text("{}", encoding="utf-8")
    assert client.delete(f"/api/projects/{project['id']}").status_code == 204
    assert not workspace.exists()
    assert original.read_bytes() == b"video"
    assert unrelated.read_bytes() == b"other video"


def test_link_routes_validate_file_type_location_and_project(path_app):
    app, client, folder = path_app
    endpoint = "/api/projects/from-paths"
    assert client.post(endpoint, json={"paths": ["video.mp4"]}).status_code == 422
    assert client.post(endpoint, json={"paths": [r"\\unreachable\share\video.mp4"]}).status_code == 422
    assert client.post(endpoint, json={"paths": [str(folder / "text.txt")]}).status_code == 415
    assert client.post(endpoint, json={"paths": [str(folder / "nonexistent.mp4")]}).status_code == 404
    directory = folder / "folder.mp4"
    directory.mkdir()
    assert client.post(endpoint, json={"paths": [str(directory)]}).status_code == 404
    managed = app.state.settings.data_dir / "jobs" / "managed"
    managed.mkdir(parents=True)
    source = _source(managed, "source.mp4")
    assert client.post(endpoint, json={"paths": [str(source)]}).status_code == 422

    original = _source(folder, "original.mp4")
    assert client.post("/api/projects/unknown/videos/from-paths", json={"paths": [str(original)]}).status_code == 404
    assert client.get("/api/projects").json() == []


def test_local_links_reject_nonlocal_origin_host_and_client(path_app):
    app, client, folder = path_app
    original = _source(folder, "original.mp4")
    path = "/api/projects/from-paths"
    payload = {"paths": [str(original)]}
    assert client.post(path, json=payload, headers={"Origin": "https://other.invalid"}).status_code == 403
    assert client.post(path, json=payload, headers={"Host": "rebound.invalid"}).status_code == 403
    with TestClient(app, base_url="http://127.0.0.1:8000", client=("192.0.2.1", 12345)) as remote_client:
        assert remote_client.post(path, json=payload).status_code == 403
    assert client.get("/api/projects").json() == []
    assert client.post(path, json=payload, headers={"Origin": "http://127.0.0.1:5173"}).status_code == 201


def test_linked_inputs_are_rendered_from_their_original_paths(path_app, monkeypatch):
    app, client, folder = path_app
    first = _source(folder, "first.mp4", b"first")
    second = _source(folder, "second.mp4", b"second")
    project = client.post("/api/projects/from-paths", json={"paths": [str(first), str(second)]}).json()
    first_id, second_id = (video["id"] for video in project["videos"])

    def render(sources, output, work_dir, progress):
        assert sources == [second.resolve(), first.resolve()]
        assert output.is_relative_to(app.state.settings.data_dir)
        output.write_bytes(b"merged")
        return VideoInfo(8, 640, 360, True, 30)

    monkeypatch.setattr(app.state.projects.renderer, "render", render)
    base = f"/api/projects/{project['id']}"
    queued = client.post(base + "/merges", json={"video_ids": [second_id, first_id]})
    assert queued.status_code == 202
    app.state.projects.render(queued.json()["id"])
    assert client.get(base).json()["merges"][0]["status"] == "completed"
    assert client.delete(base).status_code == 204
    assert first.read_bytes() == b"first" and second.read_bytes() == b"second"
