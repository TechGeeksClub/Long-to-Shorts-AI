import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.database import Database
from app.editing import apply_edge_effects, build_edit_timeline
from app.main import create_app
from app.media import probe_media, render_cut_source, render_video, run_command
from app.models import ClipCandidate, CutRange, EditSettings, HookRange, InsertRange, SubtitleCue, SubtitleWord
from app.processor import Processor
from app.subtitles import compose_subtitles_for_timeline, write_ass
from app.timeline import TimelineSegment, build_timeline_segments, timeline_duration


@pytest.mark.parametrize("start,end", [(3, 2), (1, 1), (0, .2), (-1, 2), (0, 15.1), (0, float("inf"))])
def test_invalid_hook_ranges_are_rejected(start: float, end: float) -> None:
    with pytest.raises(ValidationError):
        HookRange(source_start=start, source_end=end)
    assert HookRange(source_start=.4, source_end=.7).source_end == .7


def test_hook_moves_to_front_with_cuts_and_inserts_preserved() -> None:
    base = build_timeline_segments(clip_start=10, clip_end=20,
        cut_ranges=[CutRange(start=14, end=16)],
        insert_ranges=[InsertRange(source_start=30, source_end=31, insert_at=12)])
    edit = EditSettings(hook_range=HookRange(source_start=12, source_end=17))
    timeline = build_edit_timeline(base, edit)
    assert timeline == [TimelineSegment(12, 17, "hook"), TimelineSegment(10, 12, "base"),
                        TimelineSegment(30, 31, "insert"), TimelineSegment(17, 20, "base")]
    copy = build_edit_timeline(base, edit.model_copy(update={"hook_remove_from_body": False}))
    assert copy == [TimelineSegment(12, 17, "hook"), *base]
    assert timeline_duration(copy) == timeline_duration(base) + 5
    outside = EditSettings(hook_range=HookRange(source_start=40, source_end=42))
    assert build_edit_timeline(base, outside) == [TimelineSegment(40, 42, "hook"), *base]


def test_hook_subtitles_preserve_edits_and_include_words_outside_main_clip() -> None:
    base_cue = SubtitleCue(id="edit", start=10.2, end=11, text="Düzeltilmiş söz",
        words=[SubtitleWord(text="Düzeltilmiş", start=10.2, end=10.6),
               SubtitleWord(text="söz", start=10.6, end=11)])
    segments = [{"start": 9, "end": 9.8, "text": "Önce", "words": [
        {"text": "Önce", "start": 9, "end": 9.8}]},
        {"start": 10.2, "end": 11, "text": "Ham transkript", "words": [
        {"text": "Ham", "start": 10.2, "end": 10.6}, {"text": "transkript", "start": 10.6, "end": 11}]}]
    cues = compose_subtitles_for_timeline(base_cues=[base_cue], transcript_segments=segments,
        timeline=[TimelineSegment(9, 12, "hook"), TimelineSegment(12, 20, "base")],
        output_start=10, base_start=10, base_end=20)
    assert [cue.text for cue in cues] == ["Önce", "Düzeltilmiş söz"]
    assert cues[0].start == 10
    assert cues[1].start == 11.2
    assert cues[1].words[-1].end == 12


def test_effect_timing_uses_output_seconds_and_caps_short_clips() -> None:
    edit = EditSettings(intro_effect="fade_black", outro_effect="fade_white",
                        intro_duration=2, outro_duration=2, fade_audio=True)
    video, audio = apply_edge_effects("video", "audio", edit, 1)
    assert "fade=t=in:st=0.000000:d=0.500000:color=black" in video
    assert "fade=t=out:st=0.500000:d=0.500000:color=white" in video
    assert "afade=t=out:st=0.500000:d=0.500000" in audio
    _, untouched = apply_edge_effects("video", "audio", edit.model_copy(update={"fade_audio": False}), 2)
    assert untouched == "audio"
    assert apply_edge_effects("video", "audio", EditSettings(), 2) == ("video", "audio")


def test_hook_bounds_and_effects_are_persisted_for_preview(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path, frontend_dist=tmp_path / "missing"), start_worker=False)
    database = app.state.database
    database.create_job("job", "source.mp4", tmp_path / "source.mp4")
    database.update_job("job", status="ready", duration=60, stage="Hazır")
    database.create_clip(ClipCandidate(id="clip", job_id="job", rank=1, title="Test", start=10,
                                      end=30, score=80, reasons=[], subtitles=[]))
    with TestClient(app) as client:
        invalid = client.patch("/api/jobs/job/clips/clip", json={"edit_settings": {
            "hook_range": {"source_start": 59, "source_end": 61}}})
        assert invalid.status_code == 422
        assert "kaynak" in invalid.json()["detail"]
        payload = {"hook_range": {"source_start": 40, "source_end": 43},
                   "intro_effect": "fade_white", "outro_effect": "fade_black", "fade_audio": True}
        saved = client.patch("/api/jobs/job/clips/clip", json={"edit_settings": payload})
        assert saved.status_code == 200
        preview = client.post("/api/jobs/job/exports", json={"clip_ids": ["clip"], "is_preview": True})
        assert preview.status_code == 202
        item = preview.json()["exports"][0]
        assert item["is_preview"] is True
        assert item["edit_settings"] == EditSettings.model_validate(payload).model_dump()
    database.initialize()
    assert database.get_job("job").exports[0].is_preview is True
    assert database.get_clip("clip").edit_settings.hook_range.source_start == 40


@pytest.mark.parametrize("preview", [False, True])
def test_processor_composes_hook_with_subtitles_and_keeps_editor_timeline(tmp_path: Path, monkeypatch, preview: bool) -> None:
    settings = Settings(data_dir=tmp_path, ollama_seo_enabled=False)
    database = Database(settings.database_path)
    database.initialize()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"test")
    transcript = tmp_path / "transcript.json"
    transcript.write_text(json.dumps({"segments": [
        {"start": 10, "end": 11, "text": "Ana bölüm.", "words": [
            {"text": "Ana", "start": 10, "end": 10.5}, {"text": "bölüm.", "start": 10.5, "end": 11}]},
        {"start": 12, "end": 13, "text": "Hook burada.", "words": [
            {"text": "Hook", "start": 12, "end": 12.5}, {"text": "burada.", "start": 12.5, "end": 13}]}]}), encoding="utf-8")
    database.create_job("job", "source.mp4", source)
    database.update_job("job", status="ready", duration=30, width=640, height=360,
                        stage="Hazır", transcript_path=str(transcript))
    original = [SubtitleCue(id="a", start=10, end=11, text="Ana bölüm."),
                SubtitleCue(id="h", start=12, end=13, text="Hook burada.")]
    database.create_clip(ClipCandidate(id="clip", job_id="job", rank=1, title="Test", start=10,
                                      end=20, score=80, reasons=[], subtitles=original))
    edit = EditSettings(hook_range=HookRange(source_start=12, source_end=14),
                        intro_effect="fade_white", outro_effect="fade_black")
    database.create_export("export", "job", "clip", edit_settings=edit, is_preview=preview,
                           playback_rate=1.5)

    def compose(**kwargs):
        assert kwargs["kept_ranges"] == [(12, 14), (10, 12), (14, 20)]
        kwargs["output"].write_bytes(b"composed")
        return 10

    def render(**kwargs):
        assert kwargs["start"] == 0 and kwargs["end"] == 10
        assert kwargs["hook_boundary_seconds"] == 2
        assert kwargs["playback_rate"] == 1.5
        assert kwargs["edit_settings"] == edit
        assert kwargs["output_width"] == (540 if preview else 1080)
        ass = (kwargs["work_dir"] / "subtitles.ass").read_text(encoding="utf-8-sig")
        assert ass.index("Hook") < ass.index("Ana")
        assert "Dialogue: 0,0:00:00.00" in ass
        assert "Dialogue: 0,0:00:02.00" in ass
        kwargs["output"].write_bytes(b"mp4")

    monkeypatch.setattr("app.processor.render_cut_source", compose)
    monkeypatch.setattr("app.processor.render_video", render)
    monkeypatch.setattr("app.youtube.render_thumbnail", lambda *args, **kwargs: None)
    Processor(database, settings).export("export")
    record = database.get_export_record("export")
    assert record["status"] == "completed"
    assert database.get_clip("clip").subtitles == original
    assert Path(record["path"]).with_name("youtube-seo.md").exists() is not preview


def pixel(path: Path, seconds: float) -> np.ndarray:
    result = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(seconds), "-i", str(path),
        "-frames:v", "1", "-vf", "crop=2:2", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
        capture_output=True, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return np.frombuffer(result.stdout, dtype=np.uint8).reshape(-1, 3).mean(axis=0)


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
def test_real_hook_order_and_intro_outro_frames_with_audio(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    run_command(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
        "color=c=red:s=160x90:r=30:d=3,drawbox=x=0:y=0:w=iw:h=ih:color=green:t=fill:enable='gte(t,1)',drawbox=x=0:y=0:w=iw:h=ih:color=blue:t=fill:enable='gte(t,2)'",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
        "-c:v", "libx264", "-c:a", "aac", str(source)])
    composed = tmp_path / "composed.mp4"
    assert render_cut_source(source=source, output=composed, kept_ranges=[(2, 3), (0, 2)], ffmpeg="ffmpeg") == 3
    assert pixel(composed, .5)[2] > 220  # Hook (blue) is first.
    assert pixel(composed, 1.5)[0] > 220  # Main opening (red) follows.
    assert pixel(composed, 2.5)[1] > 90   # Main ending (green) remains.
    edit = EditSettings(intro_effect="fade_black", outro_effect="fade_white",
                        intro_duration=.4, outro_duration=.6, fade_audio=True)
    write_ass([], 0, tmp_path / "subtitles.ass")
    output = tmp_path / "preview.mp4"
    render_video(source=composed, output=output, work_dir=tmp_path, start=0, end=3,
        crop_width=None, crop_height=None, framing_mode="fit", face_tracking_enabled=False,
        ffmpeg="ffmpeg", edit_settings=edit, playback_rate=1.25, output_width=540)
    info = probe_media(output, "ffprobe")
    assert (info.width, info.height) == (540, 960)
    assert 2.3 <= info.duration <= 2.5 and info.has_audio
    assert max(pixel(output, 0)) < 10
    assert min(pixel(output, 2.33)) > 205
    audio = subprocess.run(["ffmpeg", "-v", "error", "-i", str(output), "-vn", "-ac", "1",
        "-ar", "8000", "-f", "s16le", "-"], capture_output=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    samples = np.frombuffer(audio, dtype=np.int16).astype(float)
    rms = lambda values: np.sqrt(np.mean(values ** 2))
    assert rms(samples[:640]) < rms(samples[8000:8640]) * .4
    assert rms(samples[18560:19200]) < rms(samples[8000:8640]) * .4


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
def test_preview_composition_can_downscale_source_without_changing_order(tmp_path: Path) -> None:
    source = tmp_path / "hd.mp4"
    run_command(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=1920x1080:r=30:d=1",
        "-f", "lavfi", "-i", "sine=duration=1", "-c:v", "libx264", "-c:a", "aac", str(source)])
    output = tmp_path / "preview-source.mp4"
    render_cut_source(source=source, output=output, kept_ranges=[(.5, 1), (0, .5)], ffmpeg="ffmpeg", max_width=960)
    info = probe_media(output, "ffprobe")
    assert (info.width, info.height) == (960, 540)
    assert .9 <= info.duration <= 1.1 and info.has_audio
    assert pixel(output, .25)[2] > 220
