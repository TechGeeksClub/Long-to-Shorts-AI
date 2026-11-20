import json
from types import SimpleNamespace

import pytest

from app.speech_cleanup import SpeechCleanupService
from test_speech_cleanup import cleanup, detect  # noqa: F401


URL = "/api/jobs/project/speech-cleanup"


def add_manual(client, start=7, end=8, text="Atlanan konuşma"):
    response = client.post(URL + "/manual-regions", json={"start": start, "end": end, "text": text})
    assert response.status_code == 201, response.text
    state = response.json()
    region = next(item for item in state["regions"] if item["manual"] and item["start"] == start)
    assert region["id"] in state["removed_region_ids"]
    return state, region


def test_manual_range_works_before_detection_and_persists_without_changing_source(cleanup):
    app, client, source, saved_transcript = cleanup
    before_transcript = saved_transcript.read_bytes()
    before_clip = app.state.database.get_clip("clip").model_dump()

    state, region = add_manual(client, 7.125, 8.875, "  Atlanan konuşma  ")

    assert state["status"] == "ready"
    assert state["duration"] == 10
    assert region["text"] == "Atlanan konuşma"
    assert [(item["start"], item["end"]) for item in state["regions"]] == [(7.125, 8.875)]
    recreated = SpeechCleanupService(app.state.database, app.state.settings)
    restored = recreated.state("project")
    assert restored.regions[0].model_dump() == region
    assert restored.removed_region_ids == [region["id"]]
    assert source.read_bytes() == b"unchanged source"
    assert saved_transcript.read_bytes() == before_transcript
    assert app.state.database.get_clip("clip").model_dump() == before_clip
    assert app.state.database.get_job("project").status == "failed"


def test_manual_only_first_scan_keeps_manual_selection_without_selecting_new_speech(cleanup):
    _, client, *_ = cleanup
    _, manual = add_manual(client)

    rescanned = detect(cleanup)

    assert len(rescanned["regions"]) == 3
    assert rescanned["removed_region_ids"] == [manual["id"]]
    assert next(item for item in rescanned["regions"] if item["manual"]) == manual


def test_rescan_preserves_selected_and_deselected_manual_ranges(cleanup):
    _, client, *_ = cleanup
    initial = detect(cleanup)
    _, selected_manual = add_manual(client, 7, 8)
    _, deselected_manual = add_manual(client, 8.5, 9)
    selected = [initial["regions"][0]["id"], selected_manual["id"]]
    response = client.put(URL + "/selection", json={"removed_region_ids": selected})
    assert response.status_code == 200

    rescanned = detect(cleanup)

    assert set(rescanned["removed_region_ids"]) == set(selected)
    assert {item["id"] for item in rescanned["regions"] if item["manual"]} == {
        selected_manual["id"], deselected_manual["id"],
    }


def test_rescan_all_detected_speech_selection_keeps_manual_range_deselected(cleanup):
    _, client, _, saved_transcript = cleanup
    initial = detect(cleanup)
    _, manual = add_manual(client, 8.5, 9)
    automatic_ids = [item["id"] for item in initial["regions"]]
    assert client.put(URL + "/selection", json={"removed_region_ids": automatic_ids}).status_code == 200
    transcript = json.loads(saved_transcript.read_text(encoding="utf-8"))
    transcript["segments"].append({"start": 3, "end": 4, "text": "Yeni bulunan konuşma"})
    saved_transcript.write_text(json.dumps(transcript), encoding="utf-8")

    rescanned = detect(cleanup)

    assert len([item for item in rescanned["regions"] if not item["manual"]]) == 3
    assert set(rescanned["removed_region_ids"]) == {
        item["id"] for item in rescanned["regions"] if not item["manual"]
    }
    assert manual["id"] not in rescanned["removed_region_ids"]


def test_selected_manual_overlap_mutes_core_even_if_detected_speech_is_unselected(cleanup):
    _, client, *_ = cleanup
    initial = detect(cleanup)
    _, manual = add_manual(client, 1.25, 1.75)

    state = client.get(URL).json()

    assert state["removed_region_ids"] == [manual["id"]]
    assert initial["regions"][0]["id"] not in state["removed_region_ids"]
    assert any(item["start"] <= 1.25 and item["end"] >= 1.75 for item in state["mute_ranges"])


def test_delete_manual_range_cleans_selection_and_preserves_export_snapshot(cleanup):
    app, client, *_ = cleanup
    detected = detect(cleanup)
    _, manual = add_manual(client, 7, 8)
    exported = client.post(URL + "/exports", json={"mode": "mute"})
    assert exported.status_code == 202
    export_id = exported.json()["id"]
    before_ranges = json.loads(app.state.speech_cleanup.export_record("project", export_id)["ranges_json"])
    assert before_ranges and any(item["start"] <= 7 and item["end"] >= 8 for item in before_ranges)

    deleted = client.delete(URL + f"/manual-regions/{manual['id']}")

    assert deleted.status_code == 200
    assert deleted.json()["removed_region_ids"] == []
    assert [item["id"] for item in deleted.json()["regions"]] == [
        item["id"] for item in detected["regions"]
    ]
    assert json.loads(app.state.speech_cleanup.export_record("project", export_id)["ranges_json"]) == before_ranges
    assert client.delete(URL + f"/manual-regions/{manual['id']}").status_code == 404
    assert client.delete(URL + f"/manual-regions/{detected['regions'][0]['id']}").status_code == 422


@pytest.mark.parametrize("start,end", [
    (-0.1, 1), (2, 2), (3, 2), (9, 10.01), (10, 11),
    ("NaN", 1), (0, "Infinity"), ("-Infinity", 1), ("invalid", 1),
])
def test_invalid_manual_ranges_are_rejected_without_changing_state(cleanup, start, end):
    _, client, *_ = cleanup
    before = client.get(URL).json()

    response = client.post(URL + "/manual-regions", json={"start": start, "end": end})

    assert response.status_code == 422
    assert client.get(URL).json() == before


def test_manual_range_accepts_video_edges_and_optional_description(cleanup):
    _, client, *_ = cleanup

    response = client.post(URL + "/manual-regions", json={"start": 0, "end": 10})

    assert response.status_code == 201
    state = response.json()
    assert state["regions"][0]["manual"] is True
    assert state["regions"][0]["text"]
    assert state["mute_ranges"] == [{"start": 0, "end": 10}]


@pytest.mark.parametrize("cached_duration,known_has_audio", [(None, None), (10, None), (10, 0)])
def test_manual_add_rejects_video_without_audio_before_export(cleanup, monkeypatch, cached_duration, known_has_audio):
    app, client, *_ = cleanup
    app.state.database.update_job("project", duration=cached_duration, has_audio=known_has_audio)
    before = client.get(URL).json()
    probes = []

    def no_audio_metadata(arguments):
        probes.append(arguments)
        return SimpleNamespace(stdout=json.dumps({
            "streams": [{"codec_type": "video", "width": 640, "height": 360}],
            "format": {"duration": "10"},
        }))

    monkeypatch.setattr("app.speech_cleanup.run_command", no_audio_metadata)

    response = client.post(URL + "/manual-regions", json={"start": 0, "end": 1})

    assert response.status_code == 422
    assert "ses akışı yok" in response.json()["detail"]
    assert client.get(URL).json() == before
    assert bool(probes) is (known_has_audio is None)


def test_detection_blocks_manual_mutations_until_finished(cleanup):
    app, client, *_ = cleanup
    _, manual = add_manual(client)
    assert client.post(URL + "/detect").status_code == 202

    assert client.post(URL + "/manual-regions", json={"start": 3, "end": 4}).status_code == 409
    assert client.delete(URL + f"/manual-regions/{manual['id']}").status_code == 409

    app.state.speech_cleanup.detect("project")
    assert client.delete(URL + f"/manual-regions/{manual['id']}").status_code == 200


def test_manual_mutations_respect_source_status_and_project_identity(cleanup):
    app, client, *_ = cleanup
    assert client.post("/api/jobs/missing/speech-cleanup/manual-regions", json={"start": 0, "end": 1}).status_code == 404
    assert client.delete("/api/jobs/missing/speech-cleanup/manual-regions/unknown").status_code == 404

    app.state.database.update_job("project", status="analyzing")
    assert client.post(URL + "/manual-regions", json={"start": 0, "end": 1}).status_code == 409
