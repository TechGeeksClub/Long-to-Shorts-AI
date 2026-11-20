import shutil
import subprocess
from pathlib import Path
from typing import get_args

import numpy as np
import pytest

from app.editing import apply_edge_effects, apply_hook_transition, apply_video_effect
from app.media import probe_media, run_command
from app.models import EdgeEffect, EditSettings, VideoEffect
from app.subtitles import write_ass


FFMPEG_AVAILABLE = shutil.which("ffmpeg") and shutil.which("ffprobe")


def test_effects_preserve_subtitle_filter_order_and_zero_strength() -> None:
    original = "scale=160:90,ass=subtitles.ass"
    for effect in get_args(VideoEffect):
        assert apply_video_effect(original, EditSettings(video_effect=effect, effect_intensity=0)) == original
        graded = apply_video_effect(original, EditSettings(video_effect=effect))
        assert graded.endswith(",ass=subtitles.ass")
    with pytest.raises(ValueError):
        apply_video_effect("scale=160:90", EditSettings(video_effect="warm"))


def test_hook_transition_is_optional_and_clamps_short_clips() -> None:
    settings = EditSettings(hook_transition="smooth_wipe", hook_transition_duration=2)
    for boundary in (None, 0, 1, float("nan"), float("inf")):
        assert apply_hook_transition("video", "audio", settings, 1, boundary) == ("video", "audio")
    assert apply_hook_transition("video", "audio", settings, .04, .02) == ("video", "audio")
    video, audio = apply_hook_transition("video", "audio", settings, 1, .3)
    assert "xfade=transition=smoothleft" in video
    assert "tpad=stop_mode=clone" in video and "tpad=start_mode=clone" in video
    assert audio == "audio"
    _, faded_audio = apply_hook_transition("video", "audio", settings.model_copy(update={"fade_audio": True}), 1, .3)
    assert "volume=" in faded_audio


@pytest.fixture(scope="module")
def effect_source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not FFMPEG_AVAILABLE:
        pytest.skip("FFmpeg required")
    directory = tmp_path_factory.mktemp("video-effects")
    source = directory / "source.mp4"
    run_command(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                 "color=c=blue:s=160x90:r=30:d=2.4,drawbox=x=0:y=0:w=iw:h=ih:color=red:t=fill:enable='gte(t,1.2)',drawbox=x=0:y=0:w=40:h=ih:color=0x606060:t=fill",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=2.4:sample_rate=48000",
                 "-c:v", "libx264", "-c:a", "aac", str(source)])
    write_ass([], 0, directory / "subtitles.ass")
    return source


def _frame(path: Path, seconds: float) -> np.ndarray:
    result = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(seconds), "-i", str(path),
                             "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                            check=True, capture_output=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return np.frombuffer(result.stdout, dtype=np.uint8).reshape(90, 160, 3)


@pytest.mark.parametrize("effect", get_args(EdgeEffect))
def test_real_transitions_keep_video_audio_duration_and_hook_order(effect_source: Path, effect: str) -> None:
    settings = EditSettings(intro_effect=effect, outro_effect=effect, hook_transition=effect,
                            intro_duration=.3, outro_duration=.3, hook_transition_duration=.4,
                            fade_audio=True)
    video, audio = apply_hook_transition("setpts=PTS-STARTPTS", "asetpts=PTS-STARTPTS", settings, 2.4, 1.2)
    video, audio = apply_edge_effects(video, audio, settings, 2.4)
    output = effect_source.with_name(f"transition-{effect}.mp4")
    run_command(["ffmpeg", "-v", "error", "-y", "-i", str(effect_source), "-vf", video,
                 "-af", audio, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(output)])
    info = probe_media(output, "ffprobe")
    assert (info.width, info.height) == (160, 90)
    assert abs(info.duration - 2.4) < .08 and info.has_audio
    assert _frame(output, .6)[45, 80, 2] > 220
    assert _frame(output, 1.8)[45, 80, 0] > 220
    if effect == "fade_white":
        assert np.min(_frame(output, 1.1)[45, 80]) > 210
    if effect == "fade_black":
        assert np.max(_frame(output, 1.1)[45, 80]) < 30


@pytest.mark.parametrize("effect", get_args(VideoEffect))
def test_real_video_effects_render_and_change_footage(effect_source: Path, effect: str) -> None:
    settings = EditSettings(video_effect=effect, effect_intensity=1)
    output = effect_source.with_name(f"grade-{effect}.mp4")
    video = apply_video_effect("setpts=PTS-STARTPTS,ass=subtitles.ass", settings)
    run_command(["ffmpeg", "-v", "error", "-y", "-i", str(effect_source), "-vf", video,
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "copy", str(output)], cwd=effect_source.parent)
    info = probe_media(output, "ffprobe")
    assert abs(info.duration - 2.4) < .08 and info.has_audio
    point = (45, 80) if effect == "monochrome" else (45, 10)
    rendered = _frame(output, .6)[point].astype(float)
    original = _frame(effect_source, .6)[point].astype(float)
    if effect == "monochrome":
        assert np.max(rendered) - np.min(rendered) < 5
    elif effect in {"warm", "vintage", "cinematic"}:
        assert np.max(np.abs(rendered - original)) > 4
