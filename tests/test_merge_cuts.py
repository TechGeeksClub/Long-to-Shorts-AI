import hashlib
import json
import shutil
import wave
from pathlib import Path

import numpy as np
import pytest

from app.config import Settings
from app.media import MediaError, run_command
from app.merging import MergeRenderer, VideoInfo
from app.models import CutRange


FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
requires_ffmpeg = pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="FFmpeg is not installed")


@pytest.mark.parametrize("start,end", [
    (float("nan"), 1), (0, float("inf")), (-1, 2), (2, 1), (1, 1), (0, 11), (0, 10),
])
def test_cut_renderer_rejects_invalid_or_complete_cuts_before_running_ffmpeg(tmp_path, monkeypatch, start, end):
    renderer = MergeRenderer(Settings(data_dir=tmp_path))
    monkeypatch.setattr("app.merging.probe_video", lambda *_: VideoInfo(10, 160, 96, True, 25))
    monkeypatch.setattr(renderer, "_ffmpeg", lambda *_: pytest.fail("Invalid cuts must not start FFmpeg"))
    with pytest.raises(MediaError):
        renderer.render([tmp_path / "source.mp4"], tmp_path / "out.mp4", tmp_path / "work",
                        lambda _: None, cut_ranges=[[CutRange(start=start, end=end)]])
    assert not (tmp_path / "out.mp4").exists()


def test_cut_renderer_requires_aligned_cuts_and_real_single_video_edits(tmp_path):
    renderer = MergeRenderer(Settings(data_dir=tmp_path))
    args = ([tmp_path / "source.mp4"], tmp_path / "out.mp4", tmp_path / "work", lambda _: None)
    with pytest.raises(MediaError, match="eşleşmiyor"):
        renderer.render(*args, cut_ranges=[[], []])
    with pytest.raises(MediaError, match="en az iki"):
        renderer.render(*args, cut_ranges=[[]])
    with pytest.raises(MediaError, match="Geçersiz"):
        renderer.render(*args, export_mode="unknown")


def test_cut_renderer_preserves_fifty_millisecond_edits():
    kept = MergeRenderer._kept_ranges(
        [VideoInfo(10, 160, 96, True, 25)], [[CutRange(start=5, end=5.05)]],
    )
    assert kept == [[(0, 5), (5.05, 10)]]


def test_cut_renderer_accepts_fifty_milliseconds_remaining_despite_float_roundoff():
    kept = MergeRenderer._kept_ranges(
        [VideoInfo(60, 160, 96, True, 25)], [[CutRange(start=0, end=59.95)]],
    )
    assert kept == [[(59.95, 60)]]


def test_explicit_fast_mode_never_falls_back_to_full_encoding(tmp_path, monkeypatch):
    renderer = MergeRenderer(Settings(data_dir=tmp_path))
    monkeypatch.setattr("app.merging.probe_video", lambda *_: VideoInfo(10, 160, 96, True, 25))
    monkeypatch.setattr(renderer, "_ffmpeg", lambda *_: pytest.fail("Fast mode must not normalize incompatible media"))
    with pytest.raises(MediaError, match="Hassas"):
        renderer.render([tmp_path / "one.mp4", tmp_path / "two.mp4"], tmp_path / "out.mp4",
                        tmp_path / "work", lambda _: None, export_mode="fast")


@pytest.fixture
def cut_source(tmp_path):
    source = tmp_path / "İlk'nin videosu.mp4"
    run_command([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=160x96:rate=25",
        "-f", "lavfi", "-i", "aevalsrc=if(between(t\\,6.2\\,6.5)\\,0.6*sin(2*PI*440*t)\\,0):s=48000",
        "-t", "8", "-c:v", "libx264", "-g", "50", "-keyint_min", "50", "-sc_threshold", "0",
        "-c:a", "aac", str(source),
    ])
    return source


def video_packets(path: Path) -> list[dict]:
    return json.loads(run_command([
        "ffprobe", "-v", "error", "-select_streams", "V:0", "-show_packets", "-show_data_hash", "sha256",
        "-show_entries", "packet=pts_time,data_hash", "-of", "json", str(path),
    ]).stdout)["packets"]


@requires_ffmpeg
def test_fast_off_grid_cut_copies_original_b_frame_packets_and_preserves_audio_sync(tmp_path, monkeypatch, cut_source):
    source_hash = hashlib.sha256(cut_source.read_bytes()).hexdigest()
    renderer = MergeRenderer(Settings(data_dir=tmp_path, minimum_free_bytes=0))
    commands, progress = [], []
    run_ffmpeg = renderer._ffmpeg

    def record(arguments, *args):
        commands.append(arguments)
        return run_ffmpeg(arguments, *args)

    monkeypatch.setattr(renderer, "_ffmpeg", record)
    output = tmp_path / "cut.mp4"
    info = renderer.render([cut_source], output, tmp_path / "work", progress.append,
                           cut_ranges=[[CutRange(start=2.1, end=5.7)]], export_mode="fast")
    assert info.duration == pytest.approx(4, abs=0.04)
    assert info.has_audio
    assert len(commands) == 1 and commands[0][commands[0].index("-c:v") + 1] == "copy"
    assert "libx264" not in commands[0]
    original = video_packets(cut_source)
    expected = [packet["data_hash"] for packet in original if float(packet["pts_time"]) < 2 or float(packet["pts_time"]) >= 6]
    assert [packet["data_hash"] for packet in video_packets(output)] == expected
    decoded = run_command(["ffmpeg", "-v", "error", "-i", str(output), "-f", "null", "-"])
    assert not decoded.stderr.strip()
    audio = tmp_path / "audio.wav"
    run_command(["ffmpeg", "-y", "-i", str(output), "-vn", "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(audio)])
    with wave.open(str(audio)) as stream:
        samples = np.frombuffer(stream.readframes(stream.getnframes()), dtype=np.int16)
    audible = np.flatnonzero(np.abs(samples.astype(float)) > 2000) / 48000
    assert audible[0] == pytest.approx(2.2, abs=0.04)
    assert audible[-1] == pytest.approx(2.5, abs=0.04)
    assert len(samples) / 48000 == pytest.approx(4, abs=0.025)
    assert hashlib.sha256(cut_source.read_bytes()).hexdigest() == source_hash
    assert list((tmp_path / "work").iterdir()) == []
    assert progress == sorted(progress) and progress[0] == 0 and progress[-1] == 1


@requires_ffmpeg
def test_fast_cut_supports_silent_sources(tmp_path, cut_source):
    silent = tmp_path / "silent.mp4"
    run_command(["ffmpeg", "-y", "-i", str(cut_source), "-map", "0:v:0", "-c", "copy", str(silent)])
    output = tmp_path / "cut.mp4"
    info = MergeRenderer(Settings(data_dir=tmp_path, minimum_free_bytes=0)).render(
        [silent], output, tmp_path / "work", lambda _: None,
        cut_ranges=[[CutRange(start=0, end=2), CutRange(start=6, end=8)]], export_mode="fast",
    )
    assert not info.has_audio
    assert info.duration == pytest.approx(4, abs=0.04)
    assert len(video_packets(output)) == 100


@requires_ffmpeg
def test_precise_single_video_cut_uses_requested_boundaries(tmp_path, cut_source):
    info = MergeRenderer(Settings(data_dir=tmp_path, minimum_free_bytes=0)).render(
        [cut_source], tmp_path / "precise.mp4", tmp_path / "work", lambda _: None,
        cut_ranges=[[CutRange(start=2.1, end=5.7)]], export_mode="precise",
    )
    assert info.duration == pytest.approx(4.4, abs=0.04)
    assert info.has_audio


@requires_ffmpeg
def test_keyframe_alignment_cannot_silently_remove_whole_source(tmp_path, cut_source):
    with pytest.raises(MediaError, match="tamamını"):
        MergeRenderer(Settings(data_dir=tmp_path, minimum_free_bytes=0)).render(
            [cut_source], tmp_path / "cut.mp4", tmp_path / "work", lambda _: None,
            cut_ranges=[[CutRange(start=0.1, end=7.9)]],
        )
    assert not (tmp_path / "cut.mp4").exists()


@requires_ffmpeg
def test_fast_cut_failure_preserves_existing_output_and_cleans_partial(tmp_path, monkeypatch, cut_source):
    output = tmp_path / "cut.mp4"
    output.write_bytes(b"existing export")
    renderer = MergeRenderer(Settings(data_dir=tmp_path, minimum_free_bytes=0))

    def fail(arguments, *_):
        assert arguments[arguments.index("-c:v") + 1] == "copy"
        Path(arguments[-1]).write_bytes(b"failed partial")
        raise MediaError("copy rejected")

    monkeypatch.setattr(renderer, "_ffmpeg", fail)
    with pytest.raises(MediaError, match="copy rejected"):
        renderer.render([cut_source], output, tmp_path / "work", lambda _: None,
                        cut_ranges=[[CutRange(start=2, end=6)]])
    assert output.read_bytes() == b"existing export"
    assert list((tmp_path / "work").iterdir()) == []
    assert not list(tmp_path.glob(".*.partial.mp4"))


@requires_ffmpeg
def test_repeated_cuts_do_not_shift_44100_hz_audio_content(tmp_path):
    source, output = tmp_path / "noise.mp4", tmp_path / "cut.mp4"
    run_command([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=128x72:rate=25",
        "-f", "lavfi", "-i", "anoisesrc=color=white:sample_rate=44100:seed=42:amplitude=0.2",
        "-t", "12", "-c:v", "libx264", "-g", "25", "-keyint_min", "25", "-sc_threshold", "0",
        "-c:a", "aac", "-b:a", "192k", str(source),
    ])
    MergeRenderer(Settings(data_dir=tmp_path, minimum_free_bytes=0)).render(
        [source], output, tmp_path / "work", lambda _: None,
        cut_ranges=[[CutRange(start=time + 0.1, end=time + 0.7) for time in (2, 6, 10)]], export_mode="fast",
    )

    def pcm(path):
        # Binary output is read separately because run_command decodes text.
        wav = tmp_path / f"{path.stem}.wav"
        run_command(["ffmpeg", "-y", "-i", str(path), "-vn", "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)])
        with wave.open(str(wav)) as stream:
            return np.frombuffer(stream.readframes(stream.getnframes()), dtype=np.int16).astype(float)

    original, actual = pcm(source), pcm(output)
    for source_start, output_start in ((0, 0), (3, 2), (7, 5), (11, 8)):
        width, radius = 4800, 2400
        reference_at, output_at = int((source_start + 0.4) * 48000), int((output_start + 0.4) * 48000)
        reference = original[reference_at:reference_at + width]
        candidate = actual[output_at - radius:output_at + width + radius]
        fft_size = 1 << (len(candidate) + width - 1).bit_length()
        correlation = np.fft.irfft(np.fft.rfft(candidate, fft_size) * np.fft.rfft(reference[::-1], fft_size), fft_size)
        best = int(np.argmax(correlation[width - 1:len(candidate)]))
        assert abs(best - radius) / 48000 < 0.002
        matched = candidate[best:best + width]
        assert np.dot(matched, reference) / (np.linalg.norm(matched) * np.linalg.norm(reference)) > 0.8
