import shutil
from pathlib import Path

import pytest

from app.media import (
    apply_playback_rate_to_video_filter,
    build_audio_filter,
    build_video_filter,
    probe_media,
    render_cut_source,
    render_video,
    run_command,
)
from app.models import SubtitleCue
from app.subtitles import normalize_cues, write_ass


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg is not installed",
)
def test_render_produces_vertical_h264_video(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    run_command(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(source),
        ]
    )
    work = tmp_path / "work"
    work.mkdir()
    (work / "crop.cmd").write_text(
        "0.000 cropper x 218, cropper y 0;\n",
        encoding="utf-8",
    )
    cue = SubtitleCue(id="1", start=0.2, end=1.6, text="Türkçe test", words=[])
    write_ass(normalize_cues([cue], 0, 2), 0, work / "subtitles.ass")
    output = tmp_path / "output.mp4"

    render_video(
        source=source,
        output=output,
        work_dir=work,
        start=0,
        end=2,
        crop_width=202,
        crop_height=360,
        framing_mode="fill",
        face_tracking_enabled=True,
        ffmpeg="ffmpeg",
    )
    info = probe_media(output, "ffprobe")
    assert (info.width, info.height) == (1080, 1920)
    assert 1.8 <= info.duration <= 2.2
    assert info.has_audio


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg is not installed",
)
def test_render_cut_source_removes_middle_segment(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    run_command(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "3",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(source),
        ]
    )
    output = tmp_path / "cut.mp4"

    duration = render_cut_source(
        source=source,
        output=output,
        kept_ranges=[(0.0, 1.0), (2.0, 3.0)],
        ffmpeg="ffmpeg",
    )

    info = probe_media(output, "ffprobe")
    assert duration == 2.0
    assert 1.8 <= info.duration <= 2.2
    assert info.has_audio


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg is not installed",
)
def test_render_without_tracking_preserves_full_frame(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    run_command(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(source),
        ]
    )
    work = tmp_path / "work"
    work.mkdir()
    cue = SubtitleCue(id="1", start=0.1, end=0.8, text="Tam görüntü", words=[])
    write_ass(normalize_cues([cue], 0, 1), 0, work / "subtitles.ass")
    output = tmp_path / "output.mp4"

    render_video(
        source=source,
        output=output,
        work_dir=work,
        start=0,
        end=1,
        crop_width=None,
        crop_height=None,
        framing_mode="fit",
        face_tracking_enabled=False,
        ffmpeg="ffmpeg",
    )

    info = probe_media(output, "ffprobe")
    assert (info.width, info.height) == (1080, 1920)
    assert info.has_audio


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg is not installed",
)
def test_fill_without_tracking_uses_fixed_center_crop(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    run_command(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(source),
        ]
    )
    work = tmp_path / "work"
    work.mkdir()
    cue = SubtitleCue(id="1", start=0.1, end=0.8, text="Fill test", words=[])
    write_ass(normalize_cues([cue], 0, 1), 0, work / "subtitles.ass")
    output = tmp_path / "output.mp4"

    render_video(
        source=source,
        output=output,
        work_dir=work,
        start=0,
        end=1,
        crop_width=202,
        crop_height=360,
        framing_mode="fill",
        face_tracking_enabled=False,
        ffmpeg="ffmpeg",
    )

    info = probe_media(output, "ffprobe")
    assert (info.width, info.height) == (1080, 1920)
    assert info.has_audio


def test_fill_without_tracking_centers_crop_instead_of_using_left_edge() -> None:
    video_filter = build_video_filter(
        framing_mode="fill",
        face_tracking_enabled=False,
        crop_width=850,
        crop_height=1512,
    )

    assert video_filter.startswith("setpts=PTS-STARTPTS,")
    assert "x=(in_w-out_w)/2" in video_filter
    assert "y=(in_h-out_h)/2" in video_filter
    assert "x=0:y=0" not in video_filter


def test_tracking_filter_resets_timestamps_before_crop_commands() -> None:
    video_filter = build_video_filter(
        framing_mode="fill",
        face_tracking_enabled=True,
        crop_width=850,
        crop_height=1512,
    )

    assert video_filter.startswith("setpts=PTS-STARTPTS,sendcmd=f=crop.cmd")


def test_balanced_mode_leaves_smaller_gaps_than_fit() -> None:
    video_filter = build_video_filter(
        framing_mode="balanced",
        face_tracking_enabled=False,
        crop_width=None,
        crop_height=None,
    )

    assert video_filter.startswith("setpts=PTS-STARTPTS,")
    assert "scale=1080:1248:force_original_aspect_ratio=increase" in video_filter
    assert "crop=1080:1248" in video_filter
    assert "overlay=(W-w)/2:(H-h)/2" in video_filter


def test_balanced_mode_can_shift_foreground_up() -> None:
    video_filter = build_video_filter(
        framing_mode="balanced",
        face_tracking_enabled=False,
        crop_width=None,
        crop_height=None,
        balanced_vertical_offset=120,
    )

    assert "overlay=(W-w)/2:(H-h)/2-120" in video_filter


def test_playback_rate_filters_speed_video_and_audio() -> None:
    video_filter = apply_playback_rate_to_video_filter("scale=1080:1920", 1.1)

    assert video_filter == "scale=1080:1920,setpts=PTS/1.100000"
    assert build_audio_filter(1.1) == "asetpts=PTS-STARTPTS,atempo=1.100000"
    assert build_audio_filter(1.0) == "asetpts=PTS-STARTPTS"


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg is not installed",
)
def test_render_playback_rate_speeds_output_duration(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    run_command(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(source),
        ]
    )
    work = tmp_path / "work"
    work.mkdir()
    cue = SubtitleCue(id="1", start=0.1, end=1.8, text="Hız testi", words=[])
    write_ass(normalize_cues([cue], 0, 2), 0, work / "subtitles.ass")
    output = tmp_path / "output.mp4"

    render_video(
        source=source,
        output=output,
        work_dir=work,
        start=0,
        end=2,
        crop_width=None,
        crop_height=None,
        framing_mode="fit",
        face_tracking_enabled=False,
        ffmpeg="ffmpeg",
        playback_rate=1.25,
    )

    info = probe_media(output, "ffprobe")
    assert (info.width, info.height) == (1080, 1920)
    assert 1.45 <= info.duration <= 1.75
    assert info.has_audio


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg is not installed",
)
def test_balanced_mode_renders_vertical_video(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    run_command(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(source),
        ]
    )
    work = tmp_path / "work"
    work.mkdir()
    cue = SubtitleCue(id="1", start=0.1, end=0.8, text="Dengeli test", words=[])
    write_ass(normalize_cues([cue], 0, 1), 0, work / "subtitles.ass")
    output = tmp_path / "output.mp4"

    render_video(
        source=source,
        output=output,
        work_dir=work,
        start=0,
        end=1,
        crop_width=None,
        crop_height=None,
        framing_mode="balanced",
        face_tracking_enabled=False,
        ffmpeg="ffmpeg",
    )

    info = probe_media(output, "ffprobe")
    assert (info.width, info.height) == (1080, 1920)
    assert info.has_audio
