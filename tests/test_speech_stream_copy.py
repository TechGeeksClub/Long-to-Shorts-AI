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
from app.media import MediaError, probe_media, run_command
from app.speech_cleanup import SpeechCleanupService
from app.speech_storage import speech_export_workspace


FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def video_packets(path: Path) -> list[dict]:
    result = run_command([
        "ffprobe", "-v", "error", "-select_streams", "V:0", "-show_packets",
        "-show_entries", "packet=data_hash", "-show_data_hash", "sha256", "-of", "json", str(path),
    ])
    return json.loads(result.stdout)["packets"]


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="FFmpeg is not installed")
@pytest.mark.parametrize("encoder,pixel_format,tag", [
    ("libx264", "yuv420p", "avc1"),
    ("libx265", "yuv420p10le", "hvc1"),
])
def test_mute_copies_video_packets_and_only_changes_selected_audio(
    tmp_path, monkeypatch, encoder, pixel_format, tag,
):
    source = tmp_path / "source.mp4"
    options = ["-x265-params", "pools=1:frame-threads=1:log-level=error"] if encoder == "libx265" else []
    run_command([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "3",
        "-c:v", encoder, "-preset", "ultrafast", "-pix_fmt", pixel_format, "-tag:v", tag,
        *options, "-c:a", "aac", str(source),
    ])
    output = tmp_path / "muted.mp4"
    script = tmp_path / "mute.ffgraph"
    script.write_text("[0:a:0]volume=0:enable='between(t,1,2)'[aout]", encoding="utf-8")
    service = SpeechCleanupService(Database(tmp_path / "app.db"), Settings(data_dir=tmp_path, minimum_free_bytes=0))
    calls = []
    ffmpeg = service._ffmpeg

    def capture(arguments, duration, progress):
        calls.append(arguments)
        ffmpeg(arguments, duration, progress)

    monkeypatch.setattr(service, "_ffmpeg", capture)
    progress = []
    service._render_mute(source, output, script, 3, progress.append)

    assert len(calls) == 1
    assert calls[0][calls[0].index("-c:v") + 1] == "copy"
    assert "+faststart" not in calls[0]
    assert video_packets(output) == video_packets(source)
    info = probe_media(output, "ffprobe")
    assert (info.width, info.height) == (320, 180)
    assert abs(info.duration - 3) < .1
    assert progress[-1] == 1

    audio = tmp_path / "audio.wav"
    run_command([
        "ffmpeg", "-v", "error", "-y", "-i", str(output), "-vn", "-ac", "1",
        "-ar", "16000", "-c:a", "pcm_s16le", str(audio),
    ])
    with wave.open(str(audio)) as stream:
        samples = np.frombuffer(stream.readframes(stream.getnframes()), dtype=np.int16).astype(float)
    for start, end, silent in [(.25, .75, False), (1.2, 1.8, True), (2.2, 2.8, False)]:
        region = samples[int(start * 16000):int(end * 16000)]
        rms = np.sqrt(np.mean(region ** 2))
        assert rms < 10 if silent else rms > 500


def test_mute_rejects_insufficient_space_before_starting_ffmpeg(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video" * 100)
    service = SpeechCleanupService(Database(tmp_path / "app.db"), Settings(data_dir=tmp_path))
    monkeypatch.setattr(service, "_mute_video_copy_options", lambda _: ["-c:v", "copy"])
    monkeypatch.setattr("app.speech_cleanup.shutil.disk_usage", lambda _: SimpleNamespace(free=100))
    monkeypatch.setattr(service, "_ffmpeg", lambda *_: pytest.fail("No output should start on a full disk"))
    with pytest.raises(MediaError, match="boş alan"):
        service._render_mute(source, tmp_path / "out.mp4", tmp_path / "mute.ffgraph", 3, lambda _: None)


def test_mute_disk_failure_does_not_start_video_encoding(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    service = SpeechCleanupService(Database(tmp_path / "app.db"), Settings(data_dir=tmp_path, minimum_free_bytes=0))
    monkeypatch.setattr(service, "_mute_video_copy_options", lambda _: ["-c:v", "copy"])
    calls = []

    def full_disk(arguments, *_):
        calls.append(arguments)
        raise MediaError("No space left on device")

    monkeypatch.setattr(service, "_ffmpeg", full_disk)
    with pytest.raises(MediaError, match="No space left"):
        service._render_mute(source, tmp_path / "out.mp4", tmp_path / "mute.ffgraph", 3, lambda _: None)
    assert len(calls) == 1


def test_mute_rejected_copy_retries_with_compatible_encoding(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    service = SpeechCleanupService(Database(tmp_path / "app.db"), Settings(data_dir=tmp_path, minimum_free_bytes=0))
    monkeypatch.setattr(service, "_mute_video_copy_options", lambda _: ["-c:v", "copy"])
    output = tmp_path / "out.mp4"
    calls = []

    def render(arguments, *_):
        calls.append(arguments)
        if len(calls) == 1:
            output.write_bytes(b"partial")
            raise MediaError("Could not find tag for codec")
        assert not output.exists()
        output.write_bytes(b"encoded")

    monkeypatch.setattr(service, "_ffmpeg", render)
    service._render_mute(source, output, tmp_path / "mute.ffgraph", 3, lambda _: None)
    assert len(calls) == 2
    assert calls[1][calls[1].index("-c:v") + 1] == "libx264"
    assert output.read_bytes() == b"encoded"


def ready_speech(app, source: Path):
    database = app.state.database
    database.create_job("video", source.name, source)
    database.update_job("video", status="ready", duration=3)
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO speech_cleanup(job_id,status,duration,regions_json,removed_json) VALUES (?,?,?,?,?)",
            ("video", "ready", 3, json.dumps([{"id": "speech", "start": 1, "end": 2, "text": "Speech"}]),
             json.dumps(["speech"])),
        )


def test_external_speech_output_download_and_project_deletion(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data", speech_export_dir=tmp_path / "external", minimum_free_bytes=0)
    app = create_app(settings, start_worker=False)
    source = tmp_path / "original.mp4"
    source.write_bytes(b"source")
    ready_speech(app, source)

    def write_output(source_path, output, *_):
        assert source_path == source
        assert output.is_relative_to(settings.speech_export_dir)
        output.write_bytes(b"generated video")

    monkeypatch.setattr(app.state.speech_cleanup, "_render_mute", write_output)
    with TestClient(app) as client:
        export = client.post("/api/jobs/video/speech-cleanup/exports", json={"mode": "mute"}).json()
        app.state.speech_cleanup.render(export["id"])
        record = app.state.speech_cleanup.export_record("video", export["id"])
        output = Path(record["path"])
        assert output.is_file() and output.is_relative_to(settings.speech_export_dir)
        downloaded = client.get(f"/api/jobs/video/speech-cleanup/exports/{export['id']}/download")
        assert downloaded.status_code == 200 and downloaded.content == b"generated video"
        assert client.delete("/api/projects/video").status_code == 204
        assert output.read_bytes() == b"generated video"
        assert source.read_bytes() == b"source"


def test_recovery_cleans_only_interrupted_partial_files_in_both_locations(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", speech_export_dir=tmp_path / "external", minimum_free_bytes=0)
    app = create_app(settings, start_worker=False)
    source = tmp_path / "original.mp4"
    source.write_bytes(b"source")
    ready_speech(app, source)
    service = app.state.speech_cleanup
    export = service.queue_export("video", "mute", lambda _: None)
    service._update_export(export.id, status="rendering")
    folders = [speech_export_workspace(settings, "video", export.id, default_location=default)
               for default in (True, False)]
    for folder in folders:
        folder.mkdir(parents=True)
        for name in ("render.partial.mp4", "cut-0.mp4", "cut-custom.mp4", "konusma-temizleme-mute.mp4"):
            (folder / name).write_bytes(b"video")
    service.interrupt_active()
    assert service.export_record("video", export.id)["status"] == "failed"
    for folder in folders:
        assert not (folder / "render.partial.mp4").exists()
        assert not (folder / "cut-0.mp4").exists()
        assert (folder / "cut-custom.mp4").exists()
        assert (folder / "konusma-temizleme-mute.mp4").exists()
