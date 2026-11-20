import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.merging import VideoInfo
from app.models import ClipCandidate


@pytest.fixture
def project_app(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data", frontend_dist=tmp_path / "absent", minimum_free_bytes=0)
    app = create_app(settings, start_worker=False)
    monkeypatch.setattr("app.projects.probe_video", lambda path, _: VideoInfo(
        duration=4, width=640, height=360, has_audio=path.read_bytes() != b"silent", fps=30,
    ))
    with TestClient(app) as client:
        yield app, client


def upload(client, names=("first.mp4", "second.mov"), project_id=None):
    url = "/api/projects" if project_id is None else f"/api/projects/{project_id}/videos"
    response = client.post(url, files=[("videos", (name, name.encode(), "video/mp4")) for name in names])
    assert response.status_code == 201, response.text
    return response.json()


def test_legacy_jobs_are_adopted_without_changing_saved_edits(project_app):
    app, client = project_app
    database = app.state.database
    source = app.state.settings.data_dir / "jobs" / "legacy" / "source.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"original")
    transcript = source.with_name("transcript.json")
    transcript.write_text('{"segments":[]}', encoding="utf-8")
    database.create_job("legacy", "original.mp4", source)
    database.update_job("legacy", status="completed", transcript_path=str(transcript), duration=10)
    database.create_clip(ClipCandidate(id="saved", job_id="legacy", rank=1, title="Edited clip",
                                     start=1, end=4, score=90, reasons=[], subtitles=[]))
    with database.connect() as connection:
        connection.execute("INSERT INTO speech_cleanup(job_id,status,removed_json) VALUES ('legacy','ready','[\"saved-region\"]')")
    before = database.get_job("legacy").model_dump()
    projects = client.get("/api/projects").json()
    assert len(projects) == 1
    assert projects[0]["id"] == "legacy"
    assert projects[0]["active_video_id"] == "legacy"
    assert client.get("/api/projects/legacy").json()["videos"][0]["kind"] == "source"
    app.state.projects.initialize()
    assert len(client.get("/api/projects").json()) == 1
    assert database.get_job("legacy").model_dump() == before
    assert source.read_bytes() == b"original"
    assert transcript.read_text(encoding="utf-8") == '{"segments":[]}'
    assert app.state.speech_cleanup.state("legacy").removed_region_ids == ["saved-region"]


def test_batch_upload_addition_and_selection_stay_in_one_project_without_codex(project_app):
    app, client = project_app
    project = upload(client)
    assert project["video_count"] == 2
    first, second = project["videos"]
    assert project["active_video_id"] == first["id"]
    assert first["status"] == second["status"] == "ready"
    assert first["duration"] == 4
    assert app.state.work_queue._queue.empty()
    selected = client.put(f"/api/projects/{project['id']}/active-video", json={"video_id": second["id"]})
    assert selected.json()["active_video_id"] == second["id"]
    added = upload(client, ("third.webm", "fourth.mkv"), project["id"])
    assert added["video_count"] == 4
    assert added["active_video_id"] == second["id"]
    assert len(client.get("/api/projects").json()) == 1
    assert len(client.get("/api/jobs").json()) == 4
    for video in added["videos"]:
        detail = client.get(f"/api/jobs/{video['id']}").json()
        assert detail["clips"] == [] and detail["exports"] == []
        assert client.get(f"/api/jobs/{video['id']}/source").content == video["filename"].encode()


def test_switching_video_does_not_transfer_clip_or_speech_state(project_app):
    app, client = project_app
    project = upload(client)
    first, second = project["videos"]
    app.state.database.create_clip(ClipCandidate(id="only-first", job_id=first["id"], rank=1,
        title="First video's manual clip", start=0, end=2, score=0, reasons=[], subtitles=[]))
    with app.state.database.connect() as connection:
        connection.execute("INSERT INTO speech_cleanup(job_id,status,removed_json) VALUES (?,'ready','[\"first-region\"]')",
                           (first["id"],))
    client.put(f"/api/projects/{project['id']}/active-video", json={"video_id": second["id"]})
    assert client.get(f"/api/jobs/{second['id']}").json()["clips"] == []
    assert app.state.speech_cleanup.state(second["id"]).removed_region_ids == []
    client.put(f"/api/projects/{project['id']}/active-video", json={"video_id": first["id"]})
    assert client.get(f"/api/jobs/{first['id']}").json()["clips"][0]["id"] == "only-first"
    assert app.state.speech_cleanup.state(first["id"]).removed_region_ids == ["first-region"]


def test_foreign_selection_and_merge_inputs_are_rejected(project_app):
    app, client = project_app
    project = upload(client)
    other = upload(client, ("other.mp4",))
    own = project["videos"][0]["id"]
    foreign = other["videos"][0]["id"]
    url = f"/api/projects/{project['id']}"
    assert client.put(url + "/active-video", json={"video_id": foreign}).status_code == 422
    assert client.post(url + "/merges", json={"video_ids": [own, foreign]}).status_code == 422
    assert client.post(url + "/merges", json={"video_ids": [own, own]}).status_code == 422
    assert client.post(url + "/merges", json={"video_ids": [own]}).status_code == 422
    assert app.state.projects.detail(project["id"]).active_video_id == own
    assert client.get("/api/projects/missing").status_code == 404
    assert client.post("/api/projects/missing/videos", files={"videos": ("x.mp4", b"x")}).status_code == 404


def test_bad_batch_rolls_back_files_jobs_and_membership(project_app, monkeypatch):
    app, client = project_app
    project = upload(client, ("existing.mp4",))
    jobs_root = app.state.settings.data_dir / "jobs"
    before = {path.name for path in jobs_root.iterdir()}
    def probe(path, _):
        if path.read_bytes() == b"bad":
            raise ValueError("invalid video")
        return VideoInfo(4, 640, 360, True, 30)
    monkeypatch.setattr("app.projects.probe_video", probe)
    response = client.post(f"/api/projects/{project['id']}/videos", files=[
        ("videos", ("good.mp4", b"good")), ("videos", ("bad.mp4", b"bad")),
    ])
    assert response.status_code == 422
    assert {path.name for path in jobs_root.iterdir()} == before
    assert client.get(f"/api/projects/{project['id']}").json()["video_count"] == 1
    assert client.post("/api/projects", files={"videos": ("empty.mp4", b"")}).status_code == 400
    assert client.post("/api/projects", files={"videos": ("bad.txt", b"bad")}).status_code == 415
    assert len(client.get("/api/projects").json()) == 1


def test_audio_less_video_can_be_uploaded_for_merging(project_app):
    app, client = project_app
    response = client.post("/api/projects", files={"videos": ("silent.mp4", b"silent")})
    assert response.status_code == 201
    video = response.json()["videos"][0]
    assert app.state.database.get_job_record(video["id"])["has_audio"] == 0
    assert video["status"] == "ready"


def test_merge_snapshots_order_publishes_new_selectable_video_and_keeps_sources(project_app, monkeypatch):
    app, client = project_app
    project = upload(client)
    first, second = project["videos"]
    source_bytes = {video["id"]: Path(app.state.database.get_job_record(video["id"])["source_path"]).read_bytes()
                    for video in project["videos"]}
    seen = []
    def render(sources, output, work_dir, progress):
        seen.extend(Path(source).read_bytes() for source in sources)
        output.write_bytes(b"merged output")
        progress(0.5)
        return VideoInfo(8, 640, 360, True, 30)
    monkeypatch.setattr(app.state.projects.renderer, "render", render)
    url = f"/api/projects/{project['id']}"
    response = client.post(url + "/merges", json={"video_ids": [second["id"], first["id"]]})
    assert response.status_code == 202
    task = response.json()
    assert task["video_ids"] == [second["id"], first["id"]]
    assert client.delete(f"/api/jobs/{first['id']}").status_code == 409
    assert client.delete(url).status_code == 409
    assert client.get(url + f"/merges/{task['id']}/download").status_code == 409
    client.put(url + "/active-video", json={"video_id": second["id"]})
    app.state.projects.render(task["id"])
    app.state.projects.render(task["id"])
    result = client.get(url).json()
    assert seen == [source_bytes[second["id"]], source_bytes[first["id"]]]
    assert result["video_count"] == 3
    assert result["active_video_id"] == second["id"]
    task = result["merges"][0]
    assert task["status"] == "completed"
    output = next(video for video in result["videos"] if video["id"] == task["output_video_id"])
    assert output["kind"] == "merged" and output["duration"] == 8
    assert client.get(url + f"/merges/{task['id']}/download").content == b"merged output"
    assert client.put(url + "/active-video", json={"video_id": output["id"]}).json()["active_video_id"] == output["id"]
    for video_id, original in source_bytes.items():
        assert Path(app.state.database.get_job_record(video_id)["source_path"]).read_bytes() == original
    assert len(client.get("/api/projects").json()) == 1


def test_failed_merge_and_restart_never_publish_partial_videos(project_app, monkeypatch):
    app, client = project_app
    project = upload(client)
    ids = [video["id"] for video in project["videos"]]
    url = f"/api/projects/{project['id']}"
    task = client.post(url + "/merges", json={"video_ids": ids}).json()
    def failure(sources, output, work_dir, progress):
        output.write_bytes(b"partial")
        raise ValueError("FFmpeg failed")
    monkeypatch.setattr(app.state.projects.renderer, "render", failure)
    row = app.state.projects.merge_record(project["id"], task["id"])
    app.state.projects.render(task["id"])
    failed = client.get(url).json()
    assert failed["video_count"] == 2
    assert failed["merges"][0]["status"] == "failed"
    assert failed["merges"][0]["output_video_id"] is None
    assert not (app.state.settings.data_dir / "jobs" / row["reserved_video_id"]).exists()
    task = client.post(url + "/merges", json={"video_ids": ids}).json()
    row = app.state.projects.merge_record(project["id"], task["id"])
    partial_dir = app.state.settings.data_dir / "jobs" / row["reserved_video_id"]
    partial_dir.mkdir()
    (partial_dir / "partial.mp4").write_bytes(b"partial")
    app.state.projects.interrupt_active()
    assert app.state.projects.merge_record(project["id"], task["id"])["status"] == "failed"
    assert not partial_dir.exists()
    assert not app.state.projects.is_video_busy(ids[0])


def test_enqueue_failure_is_visible_and_downloads_are_project_scoped(project_app, monkeypatch):
    app, client = project_app
    project = upload(client)
    def stopped(_):
        raise RuntimeError("worker stopped")
    monkeypatch.setattr(app.state.work_queue, "enqueue_merge", stopped)
    response = client.post(f"/api/projects/{project['id']}/merges",
                           json={"video_ids": [video["id"] for video in project["videos"]]})
    assert response.status_code == 503
    task = client.get(f"/api/projects/{project['id']}").json()["merges"][0]
    assert task["status"] == "failed"
    other = upload(client, ("other.mp4",))
    assert client.get(f"/api/projects/{other['id']}/merges/{task['id']}/download").status_code == 404


def test_delete_project_blocks_speech_then_removes_all_owned_videos(project_app):
    app, client = project_app
    project = upload(client)
    first = project["videos"][0]["id"]
    url = f"/api/projects/{project['id']}"
    with app.state.database.connect() as connection:
        connection.execute("INSERT INTO speech_cleanup(job_id,status) VALUES (?,'queued')", (first,))
    assert client.delete(url).status_code == 409
    app.state.speech_cleanup.interrupt_active()
    directories = [Path(app.state.database.get_job_record(video["id"])["source_path"]).parent for video in project["videos"]]
    assert client.delete(url).status_code == 204
    assert client.get("/api/projects").json() == []
    assert client.get("/api/jobs").json() == []
    assert all(not directory.exists() for directory in directories)


def test_legacy_job_delete_repairs_active_selection_and_empty_projects(project_app):
    app, client = project_app
    project = upload(client)
    first, second = project["videos"]
    assert client.delete(f"/api/jobs/{first['id']}").status_code == 204
    remaining = client.get(f"/api/projects/{project['id']}").json()
    assert remaining["active_video_id"] == second["id"]
    assert remaining["video_count"] == 1
    assert client.delete(f"/api/jobs/{second['id']}").status_code == 204
    assert client.get("/api/projects").json() == []
