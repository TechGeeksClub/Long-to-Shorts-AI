from __future__ import annotations

import json
import math
import subprocess
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np


class MediaError(RuntimeError):
    pass


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    width: int
    height: int
    has_audio: bool


MIN_PLAYBACK_RATE = 1.0
MAX_PLAYBACK_RATE = 1.5
MIN_BALANCED_VERTICAL_OFFSET = 0
MAX_BALANCED_VERTICAL_OFFSET = 300


def normalize_playback_rate(value: float) -> float:
    try:
        rate = float(value)
    except (TypeError, ValueError) as exc:
        raise MediaError("Video hızı geçersiz.") from exc
    if not math.isfinite(rate) or rate < MIN_PLAYBACK_RATE or rate > MAX_PLAYBACK_RATE:
        raise MediaError(
            f"Video hızı {MIN_PLAYBACK_RATE:.2f}x ile {MAX_PLAYBACK_RATE:.2f}x arasında olmalı."
        )
    return round(rate, 3)


def apply_playback_rate_to_video_filter(video_filter: str, playback_rate: float) -> str:
    rate = normalize_playback_rate(playback_rate)
    if abs(rate - 1.0) <= 0.001:
        return video_filter
    return f"{video_filter},setpts=PTS/{rate:.6f}"


def build_audio_filter(playback_rate: float) -> str:
    rate = normalize_playback_rate(playback_rate)
    if abs(rate - 1.0) <= 0.001:
        return "asetpts=PTS-STARTPTS"
    return f"asetpts=PTS-STARTPTS,atempo={rate:.6f}"


def normalize_balanced_vertical_offset(value: int | float) -> int:
    try:
        offset = int(value)
    except (TypeError, ValueError) as exc:
        raise MediaError("Dengeli kadraj kaydirma gecersiz.") from exc
    if offset < MIN_BALANCED_VERTICAL_OFFSET or offset > MAX_BALANCED_VERTICAL_OFFSET:
        raise MediaError(
            "Dengeli kadraj kaydirma "
            f"{MIN_BALANCED_VERTICAL_OFFSET}px ile {MAX_BALANCED_VERTICAL_OFFSET}px arasinda olmali."
        )
    return offset


def build_video_filter(
    *,
    framing_mode: str,
    face_tracking_enabled: bool,
    crop_width: int | None,
    crop_height: int | None,
    balanced_vertical_offset: int = 0,
) -> str:
    if framing_mode == "fill":
        if crop_width is None or crop_height is None:
            raise MediaError("Fill kadraj için kırpma boyutları bulunamadı.")
        if face_tracking_enabled:
            crop_filter = (
                "setpts=PTS-STARTPTS,"
                "sendcmd=f=crop.cmd,"
                f"crop@cropper=w={crop_width}:h={crop_height}:x=0:y=0"
            )
        else:
            crop_filter = (
                "setpts=PTS-STARTPTS,"
                f"crop=w={crop_width}:h={crop_height}:"
                "x=(in_w-out_w)/2:y=(in_h-out_h)/2"
            )
        return (
            f"{crop_filter},"
            "scale=1080:1920:flags=lanczos,"
            "ass=subtitles.ass"
        )
    if framing_mode == "fit":
        return (
            "setpts=PTS-STARTPTS,"
            "split=2[background][foreground];"
            "[background]"
            "scale=1080:1920:force_original_aspect_ratio=increase:flags=bilinear,"
            "crop=1080:1920,"
            "boxblur=24:2[blurred];"
            "[foreground]"
            "scale=1080:1920:force_original_aspect_ratio=decrease:flags=lanczos[video];"
            "[blurred][video]"
            "overlay=(W-w)/2:(H-h)/2,"
            "ass=subtitles.ass"
        )
    if framing_mode == "balanced":
        balanced_offset = normalize_balanced_vertical_offset(balanced_vertical_offset)
        overlay_y = "(H-h)/2" if balanced_offset == 0 else f"(H-h)/2-{balanced_offset}"
        return (
            "setpts=PTS-STARTPTS,"
            "split=2[background][foreground];"
            "[background]"
            "scale=1080:1920:force_original_aspect_ratio=increase:flags=bilinear,"
            "crop=1080:1920,"
            "boxblur=24:2[blurred];"
            "[foreground]"
            "scale=1080:1248:force_original_aspect_ratio=increase:flags=lanczos,"
            "crop=1080:1248[video];"
            "[blurred][video]"
            f"overlay=(W-w)/2:{overlay_y},"
            "ass=subtitles.ass"
        )
    raise MediaError(f"Bilinmeyen kadraj modu: {framing_mode}")


def run_command(command: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if process.returncode != 0:
        detail = process.stderr.strip().splitlines()
        message = detail[-1] if detail else "Bilinmeyen FFmpeg hatası"
        raise MediaError(message)
    return process


def probe_media(path: Path, ffprobe: str) -> MediaInfo:
    result = run_command(
        [
            ffprobe,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ]
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MediaError("Video bilgileri okunamadı.") from exc

    streams = payload.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if video is None:
        raise MediaError("Dosyada video akışı bulunamadı.")
    if audio is None:
        raise MediaError("Dosyada ses akışı bulunamadı; otomatik altyazı üretilemiyor.")

    duration_value = payload.get("format", {}).get("duration") or video.get("duration")
    try:
        duration = float(duration_value)
    except (TypeError, ValueError) as exc:
        raise MediaError("Video süresi belirlenemedi.") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise MediaError("Video süresi geçersiz.")

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    if width <= 0 or height <= 0:
        raise MediaError("Video çözünürlüğü belirlenemedi.")

    return MediaInfo(duration=duration, width=width, height=height, has_audio=True)


def extract_audio(source: Path, destination: Path, ffmpeg: str) -> None:
    run_command(
        [
            ffmpeg,
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ]
    )


def load_rms_envelope(wav_path: Path, bucket_seconds: float = 0.5) -> tuple[np.ndarray, float]:
    with wave.open(str(wav_path), "rb") as wav_file:
        sample_rate = wav_file.getframerate()
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        frames = wav_file.readframes(wav_file.getnframes())
    if sample_width != 2:
        return np.array([], dtype=np.float32), bucket_seconds
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    bucket_size = max(1, int(sample_rate * bucket_seconds))
    bucket_count = math.ceil(len(samples) / bucket_size)
    padded = np.pad(samples, (0, bucket_count * bucket_size - len(samples)))
    chunks = padded.reshape(bucket_count, bucket_size)
    rms = np.sqrt(np.mean(np.square(chunks), axis=1))
    if rms.size and rms.max() > 0:
        rms = rms / rms.max()
    return rms, bucket_seconds


def mean_rms(
    envelope: np.ndarray,
    bucket_seconds: float,
    start: float,
    end: float,
) -> float:
    if not envelope.size:
        return 0.5
    first = max(0, int(start / bucket_seconds))
    last = min(len(envelope), max(first + 1, math.ceil(end / bucket_seconds)))
    return float(envelope[first:last].mean()) if last > first else 0.0


def render_video(
    *,
    source: Path,
    output: Path,
    work_dir: Path,
    start: float,
    end: float,
    crop_width: int | None,
    crop_height: int | None,
    framing_mode: str,
    face_tracking_enabled: bool,
    ffmpeg: str,
    playback_rate: float = 1.0,
    balanced_vertical_offset: int = 0,
    progress_callback: Callable[[float], None] | None = None,
) -> None:
    duration = end - start
    normalized_playback_rate = normalize_playback_rate(playback_rate)
    output_duration = max(0.001, duration / normalized_playback_rate)
    video_filter = apply_playback_rate_to_video_filter(
        build_video_filter(
            framing_mode=framing_mode,
            face_tracking_enabled=face_tracking_enabled,
            crop_width=crop_width,
            crop_height=crop_height,
            balanced_vertical_offset=balanced_vertical_offset,
        ),
        normalized_playback_rate,
    )
    audio_filter = build_audio_filter(normalized_playback_rate)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(source),
        "-vf",
        video_filter,
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-af",
        audio_filter,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-r",
        "30",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        "-nostats",
        str(output),
    ]
    process = subprocess.Popen(
        command,
        cwd=work_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert process.stdout is not None
    error_lines: list[str] = []
    for line in process.stdout:
        key, _, value = line.strip().partition("=")
        if key in {"out_time_us", "out_time_ms"} and progress_callback:
            try:
                # FFmpeg currently reports both fields in microseconds.
                seconds = float(value) / 1_000_000
                progress_callback(min(0.99, max(0.0, seconds / output_duration)))
            except ValueError:
                pass
        elif key not in {"progress", "frame", "fps", "stream_0_0_q", "bitrate", "total_size",
                         "out_time", "dup_frames", "drop_frames", "speed"}:
            error_lines.append(line.strip())
    return_code = process.wait()
    if return_code != 0:
        raise MediaError(error_lines[-1] if error_lines else "Video dışa aktarılamadı.")
    if progress_callback:
        progress_callback(1.0)


def render_cut_source(
    *,
    source: Path,
    output: Path,
    kept_ranges: list[tuple[float, float]],
    ffmpeg: str,
    progress_callback: Callable[[float], None] | None = None,
) -> float:
    if not kept_ranges:
        raise MediaError("Kesitlerden sonra dışa aktarılacak video kalmadı.")

    filters: list[str] = []
    concat_inputs: list[str] = []
    for index, (start, end) in enumerate(kept_ranges):
        filters.append(
            f"[0:v]trim=start={start:.3f}:end={end:.3f},"
            f"setpts=PTS-STARTPTS[v{index}]"
        )
        filters.append(
            f"[0:a]atrim=start={start:.3f}:end={end:.3f},"
            f"asetpts=PTS-STARTPTS[a{index}]"
        )
        concat_inputs.append(f"[v{index}][a{index}]")
    filters.append(
        f"{''.join(concat_inputs)}concat=n={len(kept_ranges)}:v=1:a=1[vout][aout]"
    )
    filter_complex = ";".join(filters)
    duration = sum(end - start for start, end in kept_ranges)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-filter_complex",
        filter_complex,
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        "-nostats",
        str(output),
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert process.stdout is not None
    error_lines: list[str] = []
    for line in process.stdout:
        key, _, value = line.strip().partition("=")
        if key in {"out_time_us", "out_time_ms"} and progress_callback:
            try:
                seconds = float(value) / 1_000_000
                progress_callback(min(0.99, max(0.0, seconds / duration)))
            except ValueError:
                pass
        elif key not in {"progress", "frame", "fps", "stream_0_0_q", "bitrate", "total_size",
                         "out_time", "dup_frames", "drop_frames", "speed"}:
            error_lines.append(line.strip())
    return_code = process.wait()
    if return_code != 0:
        raise MediaError(error_lines[-1] if error_lines else "Kesilecek kısımlar çıkarılamadı.")
    if progress_callback:
        progress_callback(1.0)
    return duration
