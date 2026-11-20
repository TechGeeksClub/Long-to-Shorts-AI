import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.merging import VideoInfo


@pytest.fixture
def cut_app(tmp_path, monkeypatch):
    app = create_app(Settings(
        data_dir=tmp_path / "data", frontend_dist=tmp_path / "absent", minimum_free_bytes=0,
    ), start_worker=False)
    monkeypatch.setattr("app.projects.probe_video", lambda *_: VideoInfo(60, 320, 180, True, 25))
    with TestClient(app) as client:
        project = client.post("/api/projects", files=[
            ("videos", ("first.mp4", b"first")), ("videos", ("second.mp4", b"second")),
        ]).json()
        yield app, client, project


def test_single_video_cut_snapshots_ranges_and_exports_without_changing_source(cut_app, monkeypatch):
    app, client, project = cut_app
    video_id = project["videos"][0]["id"]
    base = f"/api/projects/{project['id']}"
    cuts = {video_id: [{"start": 20, "end": 25}, {"start": 15, "end": 22}]}
    response = client.post(base + "/merges", json={
        "video_ids": [video_id], "cuts": cuts, "export_mode": "fast",
    })
    assert response.status_code == 202, response.text
    task = response.json()
    expected = {video_id: [{"start": 15.0, "end": 25.0}]}
    assert task["cuts"] == expected and task["export_mode"] == "fast"
    assert client.delete(f"/api/jobs/{video_id}").status_code == 409
    calls = []

    def render(sources, output, work_dir, progress, **options):
        calls.append((sources, options))
        output.write_bytes(b"exported cut")
        progress(0.5)
        return VideoInfo(50, 320, 180, True, 25)

    monkeypatch.setattr(app.state.projects.renderer, "render", render)
    app.state.projects.initialize()
    app.state.projects.render(task["id"])
    result = client.get(base).json()
    assert result["active_video_id"] == video_id
    assert result["video_count"] == 3
    completed = result["merges"][0]
    assert completed["status"] == "completed"
    assert completed["cuts"] == expected and completed["export_mode"] == "fast"
    assert calls[0][1]["export_mode"] == "fast"
    assert [[cut.model_dump() for cut in group] for group in calls[0][1]["cut_ranges"]] == [expected[video_id]]
    assert Path(calls[0][0][0]).read_bytes() == b"first"
    download = client.get(base + f"/merges/{task['id']}/download")
    assert download.status_code == 200 and download.content == b"exported cut"
    output_video = next(video for video in result["videos"] if video["id"] == completed["output_video_id"])
    assert output_video["duration"] == 50
    # The exported result can itself be cut without merging another video.
    response = client.post(base + "/merges", json={
        "video_ids": [output_video["id"]],
        "cuts": {output_video["id"]: [{"start": 5, "end": 10}]},
    })
    assert response.status_code == 202, response.text


def test_multiple_source_cuts_keep_input_order_and_precise_mode(cut_app, monkeypatch):
    app, client, project = cut_app
    first, second = [video["id"] for video in project["videos"]]
    response = client.post(f"/api/projects/{project['id']}/merges", json={
        "video_ids": [second, first], "cuts": {first: [{"start": 0, "end": 10}]},
        "export_mode": "precise",
    })
    assert response.status_code == 202
    seen = []

    def render(sources, output, work_dir, progress, **options):
        seen.append(([path.read_bytes() for path in sources], options))
        output.write_bytes(b"combined")
        return VideoInfo(110, 320, 180, True, 25)

    monkeypatch.setattr(app.state.projects.renderer, "render", render)
    app.state.projects.render(response.json()["id"])
    assert seen[0][0] == [b"second", b"first"]
    assert seen[0][1]["export_mode"] == "precise"
    assert seen[0][1]["cut_ranges"][0] == []
    assert seen[0][1]["cut_ranges"][1][0].model_dump() == {"start": 0.0, "end": 10.0}


@pytest.mark.parametrize("ranges", [
    [{"start": -1, "end": 10}], [{"start": 15, "end": 10}],
    [{"start": 20, "end": 61}], [{"start": 0, "end": 60}],
    [{"start": 0, "end": 40}, {"start": 30, "end": 60}],
    [{"start": 5, "end": 5.01}], [],
])
def test_invalid_or_empty_single_video_cuts_never_enqueue(cut_app, ranges):
    app, client, project = cut_app
    video_id = project["videos"][0]["id"]
    response = client.post(f"/api/projects/{project['id']}/merges", json={
        "video_ids": [video_id], "cuts": {video_id: ranges},
    })
    assert response.status_code == 422, response.text
    assert app.state.work_queue._queue.empty()
    assert client.get(f"/api/projects/{project['id']}").json()["merges"] == []


def test_unselected_cut_sources_and_invalid_mode_are_rejected(cut_app):
    _, client, project = cut_app
    first, second = [video["id"] for video in project["videos"]]
    base = f"/api/projects/{project['id']}/merges"
    assert client.post(base, json={
        "video_ids": [first], "cuts": {second: [{"start": 1, "end": 2}]},
    }).status_code == 422
    assert client.post(base, json={"video_ids": [first, second], "export_mode": "unknown"}).status_code == 422


def test_minimum_cut_survives_floating_point_normalization(cut_app):
    _, client, project = cut_app
    video_id = project["videos"][0]["id"]
    response = client.post(f"/api/projects/{project['id']}/merges", json={
        "video_ids": [video_id], "cuts": {video_id: [{"start": 5, "end": 5.05}]},
    })
    assert response.status_code == 202, response.text
    assert response.json()["cuts"][video_id] == [{"start": 5.0, "end": 5.05}]


def test_minimum_remaining_duration_survives_floating_point_rounding(cut_app):
    _, client, project = cut_app
    video_id = project["videos"][0]["id"]
    response = client.post(f"/api/projects/{project['id']}/merges", json={
        "video_ids": [video_id], "cuts": {video_id: [{"start": 0, "end": 59.95}]},
        "export_mode": "precise",
    })
    assert response.status_code == 202, response.text


def test_unreadable_legacy_duration_returns_actionable_error(cut_app, monkeypatch):
    app, client, project = cut_app
    video_id = project["videos"][0]["id"]
    app.state.database.update_job(video_id, duration=None)

    def broken(*_):
        from app.media import MediaError
        raise MediaError("kaynak video bozuk")

    monkeypatch.setattr("app.projects.probe_video", broken)
    response = client.post(f"/api/projects/{project['id']}/merges", json={
        "video_ids": [video_id], "cuts": {video_id: [{"start": 5, "end": 10}]},
    })
    assert response.status_code == 422 and "okunamadı" in response.json()["detail"]
    assert app.state.work_queue._queue.empty()


def test_legacy_merge_table_migrates_existing_tasks_with_defaults(cut_app):
    app, client, project = cut_app
    base = f"/api/projects/{project['id']}"
    task = client.post(base + "/merges", json={
        "video_ids": [video["id"] for video in project["videos"]],
    }).json()
    with app.state.database.connect() as connection:
        connection.execute("ALTER TABLE project_merges DROP COLUMN cuts_json")
        connection.execute("ALTER TABLE project_merges DROP COLUMN export_mode")
    app.state.projects.initialize()
    app.state.projects.initialize()
    migrated = client.get(base).json()["merges"][0]
    assert migrated["id"] == task["id"]
    assert migrated["cuts"] == {} and migrated["export_mode"] == "auto"
    assert json.loads(app.state.projects.merge_record(project["id"], task["id"])["sources_json"])
