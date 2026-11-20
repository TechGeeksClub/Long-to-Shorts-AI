import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.database import Database
from app.main import create_app
from app.models import ClipCandidate, EditSettings, HookRange
from app.processor import Processor


TRANSITIONS = [
    "none", "fade_black", "fade_white", "zoom_blur", "slide", "rgb_split", "diagonal",
    "dissolve", "smooth_wipe", "circle_reveal", "pixelize",
]
VIDEO_EFFECTS = ["none", "cinematic", "vivid", "warm", "cool", "monochrome", "vintage"]


@pytest.fixture
def effect_app(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, frontend_dist=tmp_path / "missing", ollama_seo_enabled=False)
    app = create_app(settings, start_worker=False)
    database = app.state.database
    source = tmp_path / "source.mp4"
    source.write_bytes(b"test")
    database.create_job("job", "source.mp4", source)
    database.update_job("job", status="ready", stage="Hazır", duration=60, width=640, height=360)
    database.create_clip(ClipCandidate(
        id="clip", job_id="job", rank=1, title="Test", start=10, end=30,
        score=80, reasons=[], subtitles=[],
    ))
    return app, database, settings


@pytest.mark.parametrize("transition", TRANSITIONS)
def test_all_transition_names_are_valid_at_each_position(transition: str) -> None:
    edit = EditSettings(intro_effect=transition, outro_effect=transition, hook_transition=transition)
    assert edit.intro_effect == edit.outro_effect == edit.hook_transition == transition


@pytest.mark.parametrize("effect", VIDEO_EFFECTS)
def test_video_presets_accept_full_intensity_range(effect: str) -> None:
    for intensity in (0, .65, 1):
        assert EditSettings(video_effect=effect, effect_intensity=intensity).effect_intensity == intensity


@pytest.mark.parametrize("field,values", [
    ("hook_transition_duration", [0, .09, 2.01, float("inf"), float("-inf"), float("nan")]),
    ("effect_intensity", [-.01, 1.01, float("inf"), float("-inf"), float("nan")]),
])
def test_effect_controls_reject_invalid_and_nonfinite_values(field: str, values: list[float]) -> None:
    for value in values:
        with pytest.raises(ValidationError):
            EditSettings.model_validate({field: value})


def test_legacy_json_defaults_and_export_snapshots_survive_later_edits(effect_app) -> None:
    app, database, settings = effect_app
    # Persisted settings from older versions acquire the new defaults on read.
    with database.connect() as connection:
        connection.execute("UPDATE clips SET edit_settings_json = '{}' WHERE id = 'clip'")
    legacy = database.get_clip("clip").edit_settings
    assert legacy.hook_transition == "fade_white" and legacy.hook_transition_duration == .5
    assert legacy.video_effect == "none" and legacy.effect_intensity == .65

    payload = {
        "hook_range": {"source_start": 40, "source_end": 43},
        "hook_transition": "smooth_wipe", "hook_transition_duration": .7,
        "intro_effect": "circle_reveal", "outro_effect": "pixelize",
        "video_effect": "cinematic", "effect_intensity": .8,
    }
    expected = EditSettings.model_validate(payload).model_dump()
    snapshot_ids = []
    with TestClient(app) as client:
        saved = client.patch("/api/jobs/job/clips/clip", json={"edit_settings": payload})
        assert saved.status_code == 200
        assert saved.json()["edit_settings"] == expected
        for preview in (True, False):
            queued = client.post("/api/jobs/job/exports", json={
                "clip_ids": ["clip"], "is_preview": preview, "playback_rate": 1.5,
            })
            assert queued.status_code == 202
            item = queued.json()["exports"][0]
            assert item["is_preview"] is preview
            assert item["edit_settings"] == expected
            snapshot_ids.append(item["id"])
        changed = client.patch("/api/jobs/job/clips/clip", json={"edit_settings": {
            "hook_transition": "none", "video_effect": "warm", "effect_intensity": .2,
        }})
        assert changed.status_code == 200

    reopened = Database(settings.database_path)
    reopened.initialize()
    assert reopened.get_clip("clip").edit_settings.video_effect == "warm"
    for export_id in snapshot_ids:
        snapshot = json.loads(reopened.get_export_record(export_id)["edit_settings_json"])
        assert snapshot == expected


def test_api_rejects_unknown_names_and_invalid_effect_values_without_saving(effect_app) -> None:
    app, database, _ = effect_app
    invalid_controls = [
        {"intro_effect": "unknown"}, {"outro_effect": "unknown"},
        {"hook_transition": "unknown"}, {"video_effect": "unknown"},
        {"hook_transition_duration": 0}, {"hook_transition_duration": 2.01},
        {"hook_transition_duration": "NaN"}, {"hook_transition_duration": "Infinity"},
        {"effect_intensity": -.01}, {"effect_intensity": 1.01},
        {"effect_intensity": "NaN"}, {"effect_intensity": "-Infinity"},
    ]
    original = database.get_clip("clip").edit_settings
    with TestClient(app) as client:
        for invalid in invalid_controls:
            response = client.patch("/api/jobs/job/clips/clip", json={"edit_settings": invalid})
            assert response.status_code == 422, invalid
    assert database.get_clip("clip").edit_settings == original


def test_hook_covering_entire_body_has_no_transition_boundary(effect_app, monkeypatch) -> None:
    _, database, settings = effect_app
    database.update_clip("clip", start=10, end=12)
    edit = EditSettings(hook_range=HookRange(source_start=10, source_end=12))
    database.create_export("export", "job", "clip", edit_settings=edit,
                           playback_rate=1.5, is_preview=True)

    def compose(**kwargs):
        assert kwargs["kept_ranges"] == [(10, 12)]
        kwargs["output"].write_bytes(b"composed")
        return 2

    def render(**kwargs):
        assert kwargs["hook_boundary_seconds"] is None
        assert kwargs["start"] == 0 and kwargs["end"] == 2
        assert kwargs["playback_rate"] == 1.5
        kwargs["output"].write_bytes(b"mp4")

    monkeypatch.setattr("app.processor.render_cut_source", compose)
    monkeypatch.setattr("app.processor.render_video", render)
    Processor(database, settings).export("export")
    assert database.get_export_record("export")["status"] == "completed"
