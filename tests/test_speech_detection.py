import json
import wave
from types import SimpleNamespace

import numpy as np
import pytest

from app.speech_detection import SpeechActivityError, detect_speech_activity
from app.speech_cleanup import SpeechRegion, build_speech_regions, include_speech_activity


def mock_audio(monkeypatch, samples):
    monkeypatch.setattr("faster_whisper.audio.decode_audio", lambda *args, **kwargs: samples)


def test_activity_finds_short_quiet_speech_without_transcribed_words(tmp_path, monkeypatch):
    mock_audio(monkeypatch, np.zeros(16_000, dtype=np.float32))
    # A quiet 96 ms utterance: below Whisper's VAD default start threshold,
    # followed by enough silence to terminate it. Exercise the real VAD timing
    # logic with deterministic model probabilities rather than synthetic speech.
    probabilities = np.array([0.1] * 5 + [0.4] * 3 + [0.1] * 24)
    monkeypatch.setattr("faster_whisper.vad.get_vad_model", lambda: lambda _: probabilities)
    assert detect_speech_activity(tmp_path / "audio.wav") == [(0.08, 0.336)]


def test_activity_removes_invalid_timings_and_merges_overlaps(tmp_path, monkeypatch):
    mock_audio(monkeypatch, np.zeros(32_000, dtype=np.float32))
    chunks = [
        {"start": 8000, "end": 24000},
        {"start": -1600, "end": 12000},
        {"start": 24000, "end": 48000},
        {"start": float("nan"), "end": 8000},
        {"start": 9000, "end": 2000},
        {"start": 8000},
        {"start": "bad", "end": 8000},
    ]
    monkeypatch.setattr("faster_whisper.vad.get_speech_timestamps", lambda *args, **kwargs: chunks)
    assert detect_speech_activity(tmp_path / "audio.wav") == [(0, 2)]


def test_empty_audio_does_not_start_vad(tmp_path, monkeypatch):
    mock_audio(monkeypatch, np.array([], dtype=np.float32))
    monkeypatch.setattr("faster_whisper.vad.get_speech_timestamps", lambda *args, **kwargs: pytest.fail("empty input"))
    assert detect_speech_activity(tmp_path / "audio.wav") == []


@pytest.mark.parametrize("samples", [np.array([float("nan")]), np.zeros((2, 16_000))])
def test_activity_rejects_unusable_audio(tmp_path, monkeypatch, samples):
    mock_audio(monkeypatch, samples)
    with pytest.raises(SpeechActivityError, match="ek zaman kontrolü"):
        detect_speech_activity(tmp_path / "audio.wav")


def test_activity_failure_can_be_handled_without_retranscribing(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError("Unavailable decoder")

    monkeypatch.setattr("faster_whisper.audio.decode_audio", unavailable)
    with pytest.raises(SpeechActivityError) as raised:
        detect_speech_activity(tmp_path / "audio.wav")
    assert isinstance(raised.value.__cause__, RuntimeError)


def test_real_bundled_vad_and_decoder_leave_silence_untouched(tmp_path):
    audio = tmp_path / "silence.wav"
    with wave.open(str(audio), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16_000)
        writer.writeframes(bytes(32_000))
    assert detect_speech_activity(audio) == []


def test_untranscribed_speech_stays_within_acoustic_bounds_despite_distant_words():
    words = [
        SpeechRegion(id="before", start=0, end=1, text="Before"),
        SpeechRegion(id="after", start=9, end=10, text="After"),
    ]
    refined = include_speech_activity(words, [(4, 5)], 10)
    unknown = [region for region in refined if region.id not in {"before", "after"}]
    assert [(region.start, region.end) for region in unknown] == [(4, 5)]
    assert "metin çözümlenemedi" in unknown[0].text
    assert [(region.start, region.end) for region in words] == [(0, 1), (9, 10)]


def test_acoustic_edges_extend_nearby_words_without_changing_ids():
    original = SpeechRegion(id="stable-word", start=2, end=3, text="Merhaba")
    refined = include_speech_activity([original], [(1.6, 3.5)], 5)
    assert [(region.id, region.start, region.end) for region in refined] == [("stable-word", 1.6, 3.5)]
    assert (original.start, original.end) == (2, 3)


def test_long_uncovered_voice_before_and_after_words_is_not_lost():
    original = SpeechRegion(id="word", start=20, end=21, text="Merhaba")
    refined = include_speech_activity([original], [(1, 40)], 50)
    assert [(region.start, region.end) for region in refined] == [
        (1, 16), (16, 20), (20, 21), (21, 36), (36, 40),
    ]
    assert sum(region.end - region.start for region in refined) == 39
    assert next(region for region in refined if region.id == "word").start == 20
    assert all(region.end - region.start <= 15 for region in refined)


def test_unusable_word_timestamps_fall_back_to_valid_spoken_segment():
    regions = build_speech_regions([
        {"start": 1, "end": 3, "text": "Sonda kalan kelime", "words": [
            {"start": float("nan"), "end": 2, "text": "Sonda"},
            {"start": 2, "end": 2, "text": "kelime"},
        ]},
        {"start": 5, "end": 6, "text": "[noise]", "words": [
            {"start": 5, "end": 5, "text": "noise"},
        ]},
    ], 10)
    assert [(region.start, region.end, region.text) for region in regions] == [
        (1, 3, "Sonda kalan kelime"),
    ]


@pytest.fixture
def acoustic_service(tmp_path, monkeypatch):
    from app.config import Settings
    from app.main import create_app

    settings = Settings(data_dir=tmp_path / "data", frontend_dist=tmp_path / "absent")
    application = create_app(settings, start_worker=False)
    source = tmp_path / "original.mp4"
    source.write_bytes(b"unchanged source")
    segments = [
        {"start": 1, "end": 2, "text": "Merhaba"},
        {"start": 8, "end": 9, "text": "Devam"},
    ]
    transcript = tmp_path / "cached-transcript.json"
    transcript.write_text(json.dumps({"segments": segments}), encoding="utf-8")
    database = application.state.database
    database.create_job("video", source.name, source)
    database.update_job("video", status="ready", transcript_path=str(transcript))
    regions = build_speech_regions(segments, 12)
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO speech_cleanup(job_id,status,duration,regions_json,removed_json) VALUES (?,?,?,?,?)",
            ("video", "ready", 12, json.dumps([region.model_dump() for region in regions]),
             json.dumps([region.id for region in regions])),
        )
    monkeypatch.setattr("app.speech_cleanup.run_command", lambda *args: SimpleNamespace(stdout=json.dumps({
        "streams": [{"codec_type": "video", "width": 640, "height": 360}, {"codec_type": "audio"}],
        "format": {"duration": "12"},
    })))
    calls = {"extract": 0, "activity": 0}
    acoustic = {"ranges": [(0.8, 2.3), (5, 6), (7.8, 9.2)]}

    def extract(source_path, audio_path, *_):
        assert source_path == source
        calls["extract"] += 1
        audio_path.write_bytes(b"test audio")

    def activity(audio_path):
        assert audio_path.read_bytes() == b"test audio"
        calls["activity"] += 1
        return acoustic["ranges"]

    monkeypatch.setattr("app.speech_cleanup.extract_audio", extract)
    monkeypatch.setattr("app.speech_cleanup.detect_speech_activity", activity)
    monkeypatch.setattr("app.speech_cleanup.transcribe", lambda *args: pytest.fail("cached words must be reused"))
    return application.state.speech_cleanup, settings, source, regions, calls, acoustic


def test_repeat_detection_reuses_source_fingerprinted_activity_and_preserves_select_all(acoustic_service):
    service, settings, source, original, calls, acoustic = acoustic_service
    service.detect("video")
    first = service.state("video")
    assert first.status == "ready", first.error
    assert len(first.regions) == 3
    assert set(first.removed_region_ids) == {region.id for region in first.regions}
    assert {region.id for region in original}.issubset(first.removed_region_ids)
    assert calls == {"extract": 1, "activity": 1}
    cache = settings.data_dir / "jobs" / "video" / "speech-cleanup" / "speech-activity.json"
    fingerprint = json.loads(cache.read_text(encoding="utf-8"))["source"]
    assert fingerprint["size"] == source.stat().st_size
    assert fingerprint["mtime_ns"] == source.stat().st_mtime_ns
    assert not (cache.parent / "audio.wav").exists()

    service.detect("video")
    second = service.state("video")
    assert second.regions == first.regions
    assert second.removed_region_ids == first.removed_region_ids
    assert calls == {"extract": 1, "activity": 1}

    source.write_bytes(source.read_bytes() + b"changed")
    acoustic["ranges"] = [*acoustic["ranges"], (10, 11)]
    service.detect("video")
    third = service.state("video")
    assert len(third.regions) == 4
    assert set(third.removed_region_ids) == {region.id for region in third.regions}
    assert calls == {"extract": 2, "activity": 2}


def test_individual_selection_survives_refinement_without_selecting_new_speech(acoustic_service):
    service, _, _, original, calls, _ = acoustic_service
    selected = original[0].id
    service.save_selection("video", [selected])
    service.detect("video")
    refined = service.state("video")
    assert refined.status == "ready", refined.error
    assert refined.removed_region_ids == [selected]
    word = next(region for region in refined.regions if region.id == selected)
    assert (word.start, word.end) == (0.8, 2.3)
    assert len(refined.regions) == 3
    assert calls == {"extract": 1, "activity": 1}
