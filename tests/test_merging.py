import hashlib
import json
import shutil
import threading
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from app.config import Settings
from app.media import MediaError, run_command
from app.merging import MergeRenderer, VideoInfo, probe_video


FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def test_probe_handles_rotation_anamorphic_frames_and_missing_audio(monkeypatch):
    payload = {"streams": [{"codec_type": "video", "width": 720, "height": 576,
        "duration": "4", "sample_aspect_ratio": "16:15", "avg_frame_rate": "30000/1001",
        "side_data_list": [{"rotation": -90}]}], "format": {"duration": "4.02"}}
    monkeypatch.setattr("app.merging.run_command", lambda _: SimpleNamespace(stdout=json.dumps(payload)))
    info = probe_video(Path("portrait.mov"), "ffprobe")
    assert info == VideoInfo(4, 576, 768, False, 30000 / 1001)


@pytest.mark.parametrize("payload", [
    {"streams": [{"codec_type": "audio"}], "format": {"duration": "1"}},
    {"streams": [{"codec_type": "video", "width": 0, "height": 20}], "format": {"duration": "1"}},
    {"streams": [{"codec_type": "video", "width": 20, "height": 20}], "format": {"duration": "nan"}},
    {"streams": [{"codec_type": "video", "width": 20, "height": 20}], "format": {"duration": "-1"}},
])
def test_probe_rejects_invalid_media(payload, monkeypatch):
    monkeypatch.setattr("app.merging.run_command", lambda _: SimpleNamespace(stdout=json.dumps(payload)))
    with pytest.raises(MediaError):
        probe_video(Path("bad.mp4"), "ffprobe")


def test_render_failure_preserves_output_and_sources_and_cleans_temporary_files(tmp_path, monkeypatch):
    sources = [tmp_path / "one.mp4", tmp_path / "two.mp4"]
    for source in sources:
        source.write_bytes(b"source video")
    output = tmp_path / "output.mp4"
    output.write_bytes(b"existing output")
    work_dir = tmp_path / "work"
    renderer = MergeRenderer(Settings(data_dir=tmp_path))
    monkeypatch.setattr("app.merging.probe_video", lambda *_: VideoInfo(1, 320, 180, True, 30))
    def fail(arguments, *_):
        Path(arguments[-1]).write_bytes(b"partial render")
        raise MediaError("FFmpeg failed")
    monkeypatch.setattr(renderer, "_ffmpeg", fail)
    with pytest.raises(MediaError, match="FFmpeg failed"):
        renderer.render(sources, output, work_dir, lambda _: None)
    assert output.read_bytes() == b"existing output"
    assert all(source.read_bytes() == b"source video" for source in sources)
    assert list(work_dir.iterdir()) == []
    assert not list(tmp_path.glob("*.partial.mp4"))


def test_render_rejects_source_overwrite_and_cancelled_work(tmp_path):
    renderer = MergeRenderer(Settings(data_dir=tmp_path))
    source = tmp_path / "source.mp4"
    with pytest.raises(MediaError, match="üzerine"):
        renderer.render([source, tmp_path / "two.mp4"], source, tmp_path / "work", lambda _: None)
    renderer.cancel()
    with pytest.raises(MediaError, match="durduruldu"):
        renderer.render([source, tmp_path / "two.mp4"], tmp_path / "output.mp4", tmp_path / "work", lambda _: None)


@pytest.fixture
def real_sources(tmp_path):
    first = tmp_path / "ilk video.mp4"
    second = tmp_path / "ikinci türkçe.mp4"
    run_command(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:size=320x180:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "1.2",
        "-c:v", "libx264", "-c:a", "aac", str(first)])
    run_command(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:size=180x320:rate=30",
        "-t", "0.9", "-c:v", "libx264", str(second)])
    return first, second


def center_pixel(video: Path, time: float, image_path: Path):
    run_command(["ffmpeg", "-y", "-ss", str(time), "-i", str(video), "-frames:v", "1", str(image_path)])
    with Image.open(image_path) as frame:
        return frame.convert("RGB").getpixel((frame.width // 2, frame.height // 2))


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="FFmpeg is not installed")
@pytest.mark.parametrize("encoder", ["libx264", "libx265"])
def test_compatible_merge_copies_packets_and_ignores_extra_camera_tracks(tmp_path, monkeypatch, encoder):
    if encoder not in run_command(["ffmpeg", "-hide_banner", "-encoders"]).stdout:
        pytest.skip(f"{encoder} encoder is unavailable")
    options = ["-pix_fmt", "yuv420p"]
    if encoder == "libx265":
        options = ["-pix_fmt", "yuv420p10le", "-tag:v", "hvc1",
                   "-x265-params", "pools=none:frame-threads=1:log-level=error"]
    sources = [tmp_path / "İlk sahne.mp4", tmp_path / "İkinci'nin sahnesi.mp4"]
    for index, (source, color) in enumerate(zip(sources, ("red", "blue"))):
        run_command([
            "ffmpeg", "-y", "-f", "lavfi", "-i", f"color={color}:size=160x96:rate=50",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "1.1",
            "-c:v", encoder, "-preset", "veryfast", *options, "-c:a", "aac",
            *(["-timecode", "01:00:00:00"] if index == 0 else []), str(source),
        ])
    renderer = MergeRenderer(Settings(data_dir=tmp_path, minimum_free_bytes=0))
    commands = []
    run_ffmpeg = renderer._ffmpeg

    def record(arguments, *args):
        commands.append(arguments)
        return run_ffmpeg(arguments, *args)

    monkeypatch.setattr(renderer, "_ffmpeg", record)
    output = tmp_path / "combined.mp4"
    info = renderer.render(sources, output, tmp_path / "work", lambda _: None)
    assert len(commands) == 1 and commands[0][commands[0].index("-c") + 1] == "copy"
    assert abs(info.duration - 2.2) <= 0.05

    def packets(path, track):
        result = run_command(["ffprobe", "-v", "error", "-select_streams", track, "-show_packets",
                              "-show_data_hash", "sha256", "-show_entries", "packet=data_hash", "-of", "json", str(path)])
        return [packet["data_hash"] for packet in json.loads(result.stdout)["packets"]]

    for track in ("v:0", "a:0"):
        assert packets(output, track) == packets(sources[0], track) + packets(sources[1], track)
    streams = json.loads(run_command(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)]).stdout)
    assert [stream["codec_type"] for stream in streams["streams"]] == ["video", "audio"]
    assert list((tmp_path / "work").iterdir()) == []


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="FFmpeg is not installed")
def test_copy_error_retries_normalization_without_publishing_the_failed_file(tmp_path, monkeypatch, real_sources):
    first, _ = real_sources
    second = tmp_path / "compatible.mp4"
    shutil.copyfile(first, second)
    output = tmp_path / "output.mp4"
    work_dir = tmp_path / "work"
    renderer = MergeRenderer(Settings(data_dir=tmp_path, minimum_free_bytes=0))
    run_ffmpeg = renderer._ffmpeg
    attempted_copy = False

    def fail_copy(arguments, *rest):
        nonlocal attempted_copy
        if "-auto_convert" in arguments:
            attempted_copy = True
            Path(arguments[-1]).write_bytes(b"discard incomplete copy")
            raise MediaError("stream copy failed")
        assert not output.exists()
        return run_ffmpeg(arguments, *rest)

    monkeypatch.setattr(renderer, "_ffmpeg", fail_copy)
    info = renderer.render([first, second], output, work_dir, lambda _: None)
    assert attempted_copy
    assert abs(info.duration - 2.4) <= 0.05
    assert list(work_dir.iterdir()) == []
    assert not list(tmp_path.glob("*.partial.mp4"))


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="FFmpeg is not installed")
def test_merge_normalizes_mixed_sources_preserves_order_and_inserts_silence(tmp_path, real_sources):
    sources = list(real_sources)
    original_hashes = [hashlib.sha256(source.read_bytes()).hexdigest() for source in sources]
    output = tmp_path / "merged.mp4"
    progress = []
    info = MergeRenderer(Settings(data_dir=tmp_path)).render(sources, output, tmp_path / "work", progress.append)
    assert (info.width, info.height) == (320, 180)
    assert info.fps == 25
    assert abs(info.duration - 2.1) <= 0.05
    assert info.has_audio
    assert progress[0] == 0 and progress[-1] == 1
    assert progress == sorted(progress)
    assert list((tmp_path / "work").iterdir()) == []
    assert original_hashes == [hashlib.sha256(source.read_bytes()).hexdigest() for source in sources]
    first_color = center_pixel(output, 0.3, tmp_path / "red.png")
    second_color = center_pixel(output, 1.6, tmp_path / "blue.png")
    assert first_color[0] > 200 and first_color[2] < 30
    assert second_color[2] > 200 and second_color[0] < 30
    with Image.open(tmp_path / "blue.png") as frame:
        assert max(frame.convert("RGB").getpixel((10, frame.height // 2))) < 15
    audio = tmp_path / "audio.wav"
    run_command(["ffmpeg", "-y", "-i", str(output), "-vn", "-ac", "1", "-ar", "48000",
                 "-c:a", "pcm_s16le", str(audio)])
    with wave.open(str(audio)) as stream:
        samples = np.frombuffer(stream.readframes(stream.getnframes()), dtype=np.int16).astype(float)
    assert np.sqrt(np.mean(samples[12000:36000] ** 2)) > 1000
    assert np.sqrt(np.mean(samples[70000:90000] ** 2)) < 15
    streams = json.loads(run_command(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)]).stdout)["streams"]
    audio_stream = next(stream for stream in streams if stream["codec_type"] == "audio")
    assert audio_stream["sample_rate"] == "48000" and audio_stream["channels"] == 2


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="FFmpeg is not installed")
def test_merge_reversed_order_uses_new_first_source_geometry(tmp_path, real_sources):
    first, second = real_sources
    output = tmp_path / "reversed.mp4"
    info = MergeRenderer(Settings(data_dir=tmp_path)).render([second, first], output, tmp_path / "work", lambda _: None)
    assert (info.width, info.height, info.fps) == (180, 320, 30)
    first_color = center_pixel(output, 0.3, tmp_path / "blue.png")
    second_color = center_pixel(output, 1.4, tmp_path / "red.png")
    assert first_color[2] > 200 and first_color[0] < 30
    assert second_color[0] > 200 and second_color[2] < 30


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="FFmpeg is not installed")
def test_merge_honors_phone_video_rotation(tmp_path, real_sources):
    first, second = real_sources
    rotated = tmp_path / "rotated.mp4"
    run_command(["ffmpeg", "-y", "-display_rotation", "90", "-i", str(first), "-c", "copy", str(rotated)])
    assert (probe_video(rotated, "ffprobe").width, probe_video(rotated, "ffprobe").height) == (180, 320)
    output = tmp_path / "portrait-merged.mp4"
    info = MergeRenderer(Settings(data_dir=tmp_path)).render([rotated, second], output, tmp_path / "work", lambda _: None)
    assert (info.width, info.height) == (180, 320)
    assert abs(info.duration - 2.1) <= 0.05
    color = center_pixel(output, 0.3, tmp_path / "rotated-red.png")
    assert color[0] > 200 and color[2] < 30


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="FFmpeg is not installed")
def test_cancel_terminates_active_ffmpeg(tmp_path):
    renderer = MergeRenderer(Settings(data_dir=tmp_path))
    errors = []
    started = threading.Event()
    def run():
        try:
            renderer._ffmpeg(["-re", "-f", "lavfi", "-i", "testsrc2=size=32x32:rate=10",
                              "-t", "60", "-f", "null", "-"], 60, lambda _: started.set())
        except MediaError as exc:
            errors.append(str(exc))
    worker = threading.Thread(target=run)
    worker.start()
    assert started.wait(timeout=5)
    renderer.cancel()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(errors) == 1 and "durduruldu" in errors[0]
    assert renderer._process is None
