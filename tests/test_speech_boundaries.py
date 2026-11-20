import json
import shutil
import wave
from pathlib import Path

import numpy as np
import pytest

from app.config import Settings
from app.database import Database
from app.media import probe_media, run_command
from app.speech_cleanup import SpeechCleanupService, SpeechRegion, build_mute_ranges


def region(identifier: str, start: float, end: float) -> SpeechRegion:
    return SpeechRegion(id=identifier, start=start, end=end, text=identifier)


def spans(ranges) -> list[tuple[float, float]]:
    return [(item.start, item.end) for item in ranges]


def ready_service(tmp_path: Path, source: Path, regions: list[SpeechRegion], selected: list[str]):
    database = Database(tmp_path / "app.db")
    database.initialize()
    service = SpeechCleanupService(database, Settings(data_dir=tmp_path, minimum_free_bytes=0))
    service.initialize()
    database.create_job("video", source.name, source)
    database.update_job("video", status="ready", duration=4)
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO speech_cleanup(job_id,status,duration,regions_json,removed_json) "
            "VALUES (?,?,?,?,?)",
            ("video", "ready", 4, json.dumps([item.model_dump() for item in regions]), json.dumps(selected)),
        )
    return service


def test_padding_covers_both_ends_without_extending_past_the_video():
    regions = [region("beginning", .1, .8), region("ending", 3.4, 3.9)]
    assert spans(build_mute_ranges(regions, ["beginning", "ending"], 4)) == pytest.approx([
        (0, 1.2), (3.15, 4),
    ])
    assert build_mute_ranges(regions, [], 4) == []


def test_nearby_selected_speech_forms_one_continuous_mute():
    regions = [region("first", 1, 2), region("second", 2.75, 3.1)]
    assert spans(build_mute_ranges(regions, ["first", "second"], 4)) == pytest.approx([(.75, 3.5)])


def test_gap_bridging_preserves_unselected_speech_between_selected_regions():
    regions = [region("first", 1, 2), region("keep", 2.42, 2.51), region("last", 2.78, 3.1)]
    ranges = build_mute_ranges(regions, ["first", "last"], 4)
    assert spans(ranges) == pytest.approx([(.75, 2.4), (2.53, 3.5)])
    assert not any(item.start < 2.47 < item.end for item in ranges)


@pytest.mark.parametrize("protected_start,protected_end,last_start", [
    (2.1, 2.3, 2.4),
    (2.1, 2.2, 2.3),
])
def test_overlapping_padding_on_both_sides_preserves_unselected_speech(
    protected_start, protected_end, last_start,
):
    regions = [region("first", 1, 2), region("keep", protected_start, protected_end),
               region("last", last_start, 3)]
    ranges = build_mute_ranges(regions, ["first", "last"], 4)
    assert spans(ranges) == pytest.approx([(.75, protected_start), (protected_end, 3.4)])
    assert all(item.end <= protected_start or item.start >= protected_end for item in ranges)


def test_padding_for_one_selection_keeps_its_unselected_neighbor_audible():
    regions = [region("first", 1, 2), region("keep", 2.1, 2.3), region("last", 2.4, 3)]
    ranges = build_mute_ranges(regions, ["first"], 4)
    assert spans(ranges) == pytest.approx([(.75, 2.1), (2.3, 2.4)])
    assert all(item.end <= 2.1 or item.start >= 2.3 for item in ranges)
    assert all(item.end <= 2.4 for item in ranges)


def test_preview_ranges_match_export_snapshot_and_keep_original_region_selection(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source video")
    regions = [region("selected", 1, 2), region("keep", 3, 3.5)]
    service = ready_service(tmp_path, source, regions, ["selected"])
    before = service.state("video")
    exported = service.queue_export("video", "mute", lambda _: None)
    record = service.export_record("video", exported.id)
    assert json.loads(record["ranges_json"]) == [item.model_dump() for item in before.mute_ranges]
    assert spans(before.mute_ranges) == pytest.approx([(.75, 2.4)])
    after = service.state("video")
    assert after.regions == regions
    assert after.removed_region_ids == ["selected"]
    assert exported.duration == 4


def test_cut_mode_retains_original_picture_cut_boundaries(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source video")
    service = ready_service(tmp_path, source, [region("selected", 1, 2)], ["selected"])
    exported = service.queue_export("video", "cut", lambda _: None)
    record = service.export_record("video", exported.id)
    assert json.loads(record["ranges_json"]) == [{"start": 1, "end": 2}]
    assert exported.duration == 3


def read_samples(path: Path) -> np.ndarray:
    with wave.open(str(path)) as reader:
        assert reader.getframerate() == 16000
        return np.frombuffer(reader.readframes(reader.getnframes()), dtype=np.int16).astype(float)


def rms(samples: np.ndarray, start: float, end: float) -> float:
    return float(np.sqrt(np.mean(samples[int(start * 16000):int(end * 16000)] ** 2)))


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                    reason="FFmpeg is not installed")
def test_real_export_silences_speech_on_both_sides_of_imprecise_word_timestamps(tmp_path):
    # Approximate a recognizer reporting 1–2s for speech that starts at .82s
    # and finishes at 2.28s. A quieter continuous tone represents background.
    rate = 48000
    timestamps = np.arange(rate * 4) / rate
    speech = (timestamps >= .82) & (timestamps < 2.28)
    samples = 1000 * np.sin(2 * np.pi * 220 * timestamps)
    samples += speech * 7000 * np.sin(2 * np.pi * 880 * timestamps)
    waveform = tmp_path / "source.wav"
    with wave.open(str(waveform), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(samples.astype("<i2").tobytes())
    source = tmp_path / "source.mp4"
    run_command([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25",
        "-i", str(waveform), "-t", "4", "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "aac", "-b:a", "192k", str(source),
    ])
    service = ready_service(tmp_path, source, [region("speech", 1, 2)], ["speech"])
    exported = service.queue_export("video", "mute", lambda _: None)
    service.render(exported.id)
    record = service.export_record("video", exported.id)
    assert record["status"] == "completed", record["error"]
    output = Path(record["path"])
    assert abs(probe_media(output, "ffprobe").duration - 4) < .1
    decoded = {}
    for name, path in [("before", source), ("after", output)]:
        audio = tmp_path / f"{name}.wav"
        run_command([
            "ffmpeg", "-v", "error", "-y", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", str(audio),
        ])
        decoded[name] = read_samples(audio)
    for start, end in [(.85, .95), (1.25, 1.75), (2.15, 2.25)]:
        assert rms(decoded["before"], start, end) > 1000
        assert rms(decoded["after"], start, end) < 10
    for start, end in [(.3, .55), (2.6, 2.9), (3.4, 3.8)]:
        assert rms(decoded["after"], start, end) > 300
