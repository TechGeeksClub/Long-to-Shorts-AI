import json
import shutil
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import Database
from app.main import create_app
from app.media import probe_media, run_command
from app.models import ClipCandidate, CutRange
from app.speech_cleanup import SpeechCleanupService, build_mute_expression, build_speech_regions
from app.transcription import TranscriptionError


def transcript() -> dict:
    return {"language": "tr", "segments": [
        {"start": 1, "end": 3, "text": "Merhaba dünya", "words": [
            {"text": "Merhaba", "start": 1, "end": 1.4},
            {"text": "dünya", "start": 1.6, "end": 2},
        ]},
        {"start": 5, "end": 6, "text": "Devam", "words": [
            {"text": "Devam", "start": 5, "end": 6},
        ]},
        {"start": 7, "end": 9, "text": "[noise]", "words": []},
    ]}


@pytest.fixture
def cleanup(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data", frontend_dist=tmp_path / "absent", minimum_free_bytes=0)
    app = create_app(settings, start_worker=False)
    source_dir = settings.data_dir / "jobs" / "project"
    source_dir.mkdir(parents=True)
    source = source_dir / "source.mp4"
    source.write_bytes(b"unchanged source")
    saved_transcript = source_dir / "transcript.json"
    saved_transcript.write_text(json.dumps(transcript()), encoding="utf-8")
    database = app.state.database
    database.create_job("project", source.name, source)
    database.update_job("project", status="failed", stage="Codex failed", error="Codex unavailable",
                        transcript_path=str(saved_transcript))
    database.create_clip(ClipCandidate(id="clip", job_id="project", rank=1, title="Existing clip",
                                     start=0, end=10, score=70, reasons=[], subtitles=[]))
    monkeypatch.setattr("app.speech_cleanup.run_command", lambda args: SimpleNamespace(stdout=json.dumps({
        "streams": [{"codec_type": "video", "width": 640, "height": 360}, {"codec_type": "audio"}],
        "format": {"duration": "10"},
    })))
    with TestClient(app) as client:
        yield app, client, source, saved_transcript


def detect(cleanup):
    app, client, *_ = cleanup
    response = client.post("/api/jobs/project/speech-cleanup/detect")
    assert response.status_code == 202
    app.state.speech_cleanup.detect("project")
    state = client.get("/api/jobs/project/speech-cleanup").json()
    assert state["status"] == "ready"
    return state


def test_speech_regions_use_word_boundaries_skip_noise_and_preserve_real_pauses():
    regions = build_speech_regions(transcript()["segments"], 10)
    assert [(item.start, item.end, item.text) for item in regions] == [
        (1, 2, "Merhaba dünya"), (5, 6, "Devam"),
    ]
    # Corrections change displayed text without invalidating saved selections.
    changed = transcript()["segments"]
    changed[0]["words"][0]["text"] = "Selam"
    assert regions[0].id == build_speech_regions(changed, 10)[0].id


def test_continuous_speech_has_manageable_regions_and_invalid_words_are_ignored():
    words = [{"start": index, "end": index + 1, "text": str(index)} for index in range(40)]
    words.extend([{"start": float("nan"), "end": 8, "text": "invalid"},
                  {"start": 9, "end": 3, "text": "reversed"},
                  {"start": 0, "end": 0.2, "text": "[music]"}])
    regions = build_speech_regions([{"words": words}], 40)
    assert [(region.start, region.end) for region in regions] == [(0, 15), (15, 30), (30, 40)]


def test_cached_speech_available_after_codex_failure_without_mutating_shorts(cleanup, monkeypatch):
    app, client, source, saved_transcript = cleanup
    before_clip = app.state.database.get_clip("clip").model_dump()
    before_transcript = saved_transcript.read_bytes()
    monkeypatch.setattr("app.speech_cleanup.transcribe", lambda *_: pytest.fail("cached transcript must be reused"))
    state = detect(cleanup)
    assert len(state["regions"]) == 2
    assert state["removed_region_ids"] == []
    assert state["duration"] == 10
    assert app.state.database.get_job("project").status == "failed"
    assert app.state.database.get_job("project").error == "Codex unavailable"
    assert app.state.database.get_clip("clip").model_dump() == before_clip
    assert source.read_bytes() == b"unchanged source"
    assert saved_transcript.read_bytes() == before_transcript
    assert client.get("/api/jobs/missing/speech-cleanup").status_code == 404


def test_individual_all_undo_persist_and_exports_snapshot_selection(cleanup):
    app, client, *_ = cleanup
    regions = detect(cleanup)["regions"]
    url = "/api/jobs/project/speech-cleanup"
    first = regions[0]["id"]
    second = regions[1]["id"]
    assert client.put(url + "/selection", json={"removed_region_ids": [first]}).json()["removed_region_ids"] == [first]
    response = client.post(url + "/exports", json={"mode": "cut"})
    assert response.status_code == 202
    export = response.json()
    assert export["duration"] == 9
    # Pending exports retain their own ranges while the next edit is changed.
    assert client.put(url + "/selection", json={"removed_region_ids": [first, second]}).status_code == 200
    record = app.state.speech_cleanup.export_record("project", export["id"])
    assert json.loads(record["ranges_json"]) == [{"start": 1, "end": 2}]
    assert client.put(url + "/selection", json={"removed_region_ids": []}).json()["removed_region_ids"] == []
    recreated = SpeechCleanupService(app.state.database, app.state.settings)
    assert recreated.state("project").removed_region_ids == []
    assert recreated.state("project").exports[0].id == export["id"]
    assert client.put(url + "/selection", json={"removed_region_ids": ["foreign"]}).status_code == 422
    assert client.get(url + f"/exports/{export['id']}/download").status_code == 409
    app.state.database.create_job("other", "other.mp4", Path("other.mp4"))
    assert client.get(f"/api/jobs/other/speech-cleanup/exports/{export['id']}/download").status_code == 404
    assert client.delete("/api/jobs/project").status_code == 409


def test_empty_selection_full_cut_validation_and_mute_allowed(cleanup):
    app, client, *_ = cleanup
    detect(cleanup)
    url = "/api/jobs/project/speech-cleanup"
    assert client.post(url + "/exports", json={"mode": "cut"}).status_code == 422
    with app.state.database.connect() as connection:
        connection.execute("UPDATE speech_cleanup SET regions_json=? WHERE job_id='project'",
                           (json.dumps([{"id": "all", "start": 0, "end": 10, "text": "All speech"}]),))
    client.put(url + "/selection", json={"removed_region_ids": ["all"]})
    assert client.post(url + "/exports", json={"mode": "cut"}).status_code == 422
    muted = client.post(url + "/exports", json={})
    assert muted.status_code == 202
    assert muted.json()["mode"] == "mute"
    assert muted.json()["duration"] == 10


def test_active_source_detection_and_restart_recovery(cleanup):
    app, client, *_ = cleanup
    database = app.state.database
    database.update_job("project", status="analyzing")
    assert client.post("/api/jobs/project/speech-cleanup/detect").status_code == 409
    database.update_job("project", status="ready")
    assert client.post("/api/jobs/project/speech-cleanup/detect").status_code == 202
    assert client.post("/api/jobs/project/speech-cleanup/detect").status_code == 409
    app.state.speech_cleanup.interrupt_active()
    state = app.state.speech_cleanup.state("project")
    assert state.status == "failed"
    assert "yarıda" in state.error
    app.state.speech_cleanup.detect("project")
    regions = app.state.speech_cleanup.state("project").regions
    app.state.speech_cleanup.save_selection("project", [regions[0].id])
    export = app.state.speech_cleanup.queue_export("project", "cut", lambda _: None)
    app.state.speech_cleanup.interrupt_active()
    assert app.state.speech_cleanup.export_record("project", export.id)["status"] == "failed"


def test_speechless_audio_is_empty_success_and_render_failure_is_reported(cleanup, monkeypatch):
    app, client, *_ = cleanup
    app.state.database.update_job("project", transcript_path=None)
    monkeypatch.setattr("app.speech_cleanup.extract_audio", lambda *args: args[1].write_bytes(b"audio"))
    def no_speech(*args):
        raise TranscriptionError("Videoda yazıya dönüştürülebilecek konuşma bulunamadı.")
    monkeypatch.setattr("app.speech_cleanup.transcribe", no_speech)
    state = detect(cleanup)
    assert state["regions"] == []
    own_cache = Path(app.state.database.get_job_record("project")["source_path"]).parent / "speech-cleanup" / "transcript.json"
    assert json.loads(own_cache.read_text(encoding="utf-8"))["segments"] == []


def test_failed_render_does_not_publish_partial_output(cleanup, monkeypatch):
    app, client, *_ = cleanup
    state = detect(cleanup)
    app.state.speech_cleanup.save_selection("project", [state["regions"][0]["id"]])
    export = app.state.speech_cleanup.queue_export("project", "cut", lambda _: None)
    def fail_render(*args):
        raise RuntimeError("FFmpeg render failed")
    monkeypatch.setattr(app.state.speech_cleanup, "_render_cuts", fail_render)
    app.state.speech_cleanup.render(export.id)
    result = app.state.speech_cleanup.state("project").exports[0]
    assert result.status == "failed"
    assert "FFmpeg" in result.error
    assert result.filename is None
    assert app.state.database.get_job("project").status == "failed"


def test_render_batches_bound_inputs_and_store_filter_graphs_in_files(tmp_path, monkeypatch):
    database = Database(tmp_path / "app.db")
    database.initialize()
    service = SpeechCleanupService(database, Settings(data_dir=tmp_path))
    commands = []
    def fake_ffmpeg(arguments, duration, progress):
        commands.append(arguments)
        Path(arguments[-1]).write_bytes(b"rendered")
        progress(1)
    monkeypatch.setattr(service, "_ffmpeg", fake_ffmpeg)
    kept = [(index * 2, index * 2 + 1) for index in range(25)]
    output = tmp_path / "output.mp4"
    service._render_cuts(tmp_path / "source.mp4", output, kept, tmp_path, lambda _: None)
    assert output.read_bytes() == b"rendered"
    assert max(command.count("-i") for command in commands) <= 8
    assert all("-/filter_complex" in command for command in commands[:-1])
    assert len(commands) == 5


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                    reason="FFmpeg is not installed")
def test_mute_expression_handles_thousands_of_selected_speech_ranges(tmp_path):
    service = SpeechCleanupService(Database(tmp_path / "app.db"), Settings(data_dir=tmp_path))
    cuts = [CutRange(start=index * 2, end=index * 2 + 1) for index in range(2500)]
    graph = tmp_path / "mute.ffgraph"
    graph.write_text(f"[0:a]volume=0:enable='{build_mute_expression(cuts)}'[aout]", encoding="utf-8")
    service._ffmpeg([
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
        "-/filter_complex", str(graph), "-map", "[aout]", "-f", "null", "-",
    ], 1, lambda _: None)


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                    reason="FFmpeg is not installed")
def test_real_cut_and_mute_preserve_dimensions_audio_and_expected_timing(tmp_path, monkeypatch):
    database = Database(tmp_path / "app.db")
    database.initialize()
    service = SpeechCleanupService(database, Settings(data_dir=tmp_path))
    service.initialize()
    source_dir = tmp_path / "original videos"
    source_dir.mkdir()
    source = source_dir / "kaynak türkçe.mp4"
    run_command(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
                 "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "4",
                 "-c:v", "libx264", "-c:a", "aac", str(source)])
    cache = tmp_path / "transcript.json"
    cache.write_text(json.dumps({"segments": [{"start": 1, "end": 2, "text": "speech"}]}), encoding="utf-8")
    database.create_job("project", source.name, source)
    database.update_job("project", status="ready", transcript_path=str(cache))
    service.queue_detection("project", lambda _: None)
    service.detect("project")
    service.save_selection("project", [service.state("project").regions[0].id])
    # Force multi-batch concatenation to exercise its A/V timestamp handling.
    monkeypatch.setattr("app.speech_cleanup.RENDER_BATCH_SIZE", 1)
    for mode, expected_duration in [("cut", 3), ("mute", 4)]:
        export = service.queue_export("project", mode, lambda _: None)
        service.render(export.id)
        record = service.export_record("project", export.id)
        assert record["status"] == "completed", record["error"]
        output = Path(record["path"])
        assert output.is_relative_to(tmp_path / "jobs" / "project" / "speech-cleanup")
        assert list(source_dir.iterdir()) == [source]
        info = probe_media(output, "ffprobe")
        assert (info.width, info.height) == (320, 180)
        assert abs(info.duration - expected_duration) < 0.15
        assert info.has_audio
        if mode == "mute":
            audio = tmp_path / "muted.wav"
            run_command(["ffmpeg", "-y", "-i", str(output), "-vn", "-ac", "1", "-ar", "16000",
                         "-c:a", "pcm_s16le", str(audio)])
            with wave.open(str(audio)) as reader:
                samples = np.frombuffer(reader.readframes(reader.getnframes()), dtype=np.int16).astype(float)
            assert np.sqrt(np.mean(samples[4000:12000] ** 2)) > 500
            assert np.sqrt(np.mean(samples[20000:28000] ** 2)) < 10
