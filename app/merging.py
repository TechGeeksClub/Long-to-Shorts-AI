from __future__ import annotations

import json
import logging
import math
import shutil
import subprocess
import tempfile
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Literal

from app.config import Settings
from app.media import MediaError, run_command
from app.models import CutRange


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _CopyInfo:
    video_id: int
    audio_id: int
    video_tag: str
    video_timescale: int
    video_format: tuple
    audio_format: tuple
    video_configuration: str
    frame_rate: float
    frame_count: int
    audio_duration: float
    zero_start: bool


@dataclass(frozen=True)
class VideoInfo:
    duration: float
    width: int
    height: int
    has_audio: bool
    fps: float
    copy_info: _CopyInfo | None = field(default=None, compare=False, repr=False)


def _positive_number(value: object) -> float | None:
    try:
        number = float(str(value))
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _ratio(value: object) -> float | None:
    try:
        return _positive_number(float(Fraction(str(value).replace(":", "/"))))
    except (ValueError, ZeroDivisionError):
        return None


def _mp4_copy_info(payload: dict, video: dict) -> _CopyInfo | None:
    """Describe an MP4's primary streams for exact matching without reading every packet."""
    container = payload.get("format", {})
    if "mp4" not in container.get("format_name", "").split(","):
        return None
    streams = payload["streams"]
    audio_tracks = [stream for stream in streams if stream.get("codec_type") == "audio"]
    video_tracks = [stream for stream in streams if stream.get("codec_type") == "video"
                    and not stream.get("disposition", {}).get("attached_pic")]
    if len(audio_tracks) != 1 or len(video_tracks) != 1:
        return None
    audio = audio_tracks[0]
    video_tag = video.get("codec_tag_string")
    if (video.get("codec_name"), video_tag) not in {("h264", "avc1"), ("hevc", "hvc1")}:
        return None
    if audio.get("codec_name") != "aac" or audio.get("codec_tag_string") != "mp4a":
        return None
    if not video.get("extradata_hash") or not audio.get("extradata_hash"):
        return None
    if video.get("sample_aspect_ratio") not in {None, "N/A", "1:1"}:
        return None
    if any("display matrix" in side.get("side_data_type", "").lower() or side.get("rotation")
           for side in video.get("side_data_list", [])) or video.get("tags", {}).get("rotate"):
        return None
    try:
        video_id = int(video["id"], 0)
        audio_id = int(audio["id"], 0)
        timescale = Fraction(video["time_base"])
        frame_count = int(video["nb_frames"])
        frame_rate = _ratio(video.get("r_frame_rate"))
        audio_duration = _positive_number(audio.get("duration"))
        zero_start = all(abs(float(stream.get("start_time", "nan"))) <= 0.001
                         for stream in (container, video, audio))
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    if (video_id <= 0 or audio_id <= 0 or video_id == audio_id
            or timescale.numerator != 1 or timescale.denominator > 2**31 - 1
            or frame_count <= 0 or frame_rate is None or audio_duration is None):
        return None
    # Explicit track IDs let the concat demuxer skip camera telemetry and cover art.
    selected_ids = {video["id"], audio["id"]}
    if sum(stream.get("id") in selected_ids for stream in streams) != 2:
        return None
    video_fields = ("codec_name", "codec_tag_string", "width", "height", "pix_fmt", "has_b_frames",
                    "field_order", "color_range", "color_space", "color_transfer", "color_primaries",
                    "chroma_location", "time_base")
    audio_fields = ("codec_name", "codec_tag_string", "sample_rate", "channels", "channel_layout",
                    "time_base", "extradata_hash")
    return _CopyInfo(video_id, audio_id, video_tag, timescale.denominator,
                     tuple(video.get(key) for key in video_fields),
                     tuple(audio.get(key) for key in audio_fields),
                     video["extradata_hash"],
                     frame_rate, frame_count, audio_duration, zero_start)


def probe_video(path: Path, ffprobe: str) -> VideoInfo:
    """Read display geometry and timing without requiring an audio stream."""
    try:
        response = run_command([
            ffprobe, "-v", "error", "-show_streams", "-show_format", "-show_data_hash", "sha256", "-of", "json", str(path),
        ])
        payload = json.loads(response.stdout)
        streams = payload.get("streams") or []
        video = next((stream for stream in streams
                      if stream.get("codec_type") == "video"
                      and not stream.get("disposition", {}).get("attached_pic")), None)
        if video is None:
            raise MediaError("Dosyada birleştirilebilecek video akışı bulunamadı.")
        duration = _positive_number(video.get("duration"))
        if duration is None:
            duration = _positive_number(payload.get("format", {}).get("duration"))
        if duration is None:
            raise MediaError("Video süresi geçersiz veya belirlenemedi.")
        width, height = int(video.get("width") or 0), int(video.get("height") or 0)
        if width <= 0 or height <= 0:
            raise MediaError("Video boyutları geçersiz.")
        # Square-pixel output should retain the displayed aspect ratio, including
        # anamorphic sources and portrait clips stored with a display matrix.
        width = max(1, round(width * (_ratio(video.get("sample_aspect_ratio")) or 1)))
        rotation = next((side.get("rotation") for side in video.get("side_data_list", [])
                         if side.get("rotation") is not None), video.get("tags", {}).get("rotate", 0))
        angle = float(rotation or 0)
        if math.isfinite(angle) and math.isclose(abs(angle) % 180, 90, abs_tol=0.1):
            width, height = height, width
        fps = _ratio(video.get("avg_frame_rate")) or _ratio(video.get("r_frame_rate")) or 30
        return VideoInfo(duration, width, height,
                         any(stream.get("codec_type") == "audio" for stream in streams), fps,
                         _mp4_copy_info(payload, video))
    except MediaError:
        raise
    except (OSError, TypeError, ValueError, KeyError) as exc:
        raise MediaError(f"Video bilgileri okunamadı: {exc}") from exc


class MergeRenderer:
    """Normalize bounded inputs, concatenate, and atomically publish one MP4."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                self._process.terminate()

    def render(
        self, sources: list[Path], output: Path, work_dir: Path,
        progress_callback: Callable[[float], None],
        *, cut_ranges: list[list[CutRange]] | None = None,
        export_mode: Literal["auto", "fast", "precise"] = "auto",
    ) -> VideoInfo:
        if export_mode not in {"auto", "fast", "precise"}:
            raise MediaError("Geçersiz dışa aktarma modu.")
        if cut_ranges is not None and len(cut_ranges) != len(sources):
            raise MediaError("Kesilecek aralıklar kaynak videolarla eşleşmiyor.")
        has_cuts = bool(cut_ranges and any(cut_ranges))
        if not sources or (len(sources) < 2 and not has_cuts):
            raise MediaError("Birleştirmek için en az iki video seç.")
        if output.resolve() in {source.resolve() for source in sources}:
            raise MediaError("Birleştirme çıktısı kaynak videonun üzerine yazılamaz.")
        self._check_cancelled()
        infos = [probe_video(source, self.settings.ffprobe) for source in sources]
        kept_ranges = self._kept_ranges(infos, cut_ranges)
        spans = [(source, info, start, end)
                 for source, info, ranges in zip(sources, infos, kept_ranges)
                 for start, end in ranges]
        width, height = (max(2, math.ceil(size / 2) * 2) for size in (infos[0].width, infos[0].height))
        fps = Fraction(min(60, max(1, infos[0].fps))).limit_denominator(1001)
        fps_text = f"{fps.numerator}/{fps.denominator}"
        # Round cumulative boundaries, so many short sources do not each add a
        # separate half-frame of duration error to a long merged result.
        durations: list[float] = []
        accumulated = 0.0
        previous_frame = 0
        for _, _, start, end in spans:
            accumulated += end - start
            next_frame = max(previous_frame + 1, round(accumulated * float(fps)))
            durations.append(float(Fraction(next_frame - previous_frame, 1) / fps))
            previous_frame = next_frame
        total_duration = sum(durations)
        output.parent.mkdir(parents=True, exist_ok=True)
        work_dir = work_dir.resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix="merge-", dir=work_dir))
        partial = output.with_name(f".{output.stem}-{uuid.uuid4().hex}.partial.mp4")
        try:
            if has_cuts and export_mode != "precise":
                copied = self._render_fast_cuts(sources, infos, kept_ranges, scratch, partial, progress_callback)
            elif not has_cuts and export_mode != "precise":
                copied = self._try_stream_copy(sources, infos, scratch, partial, progress_callback)
                if copied is None and export_mode == "fast":
                    raise MediaError("Bu videolar yeniden kodlanmadan birleştirilemiyor. Hassas modu seç.")
            else:
                copied = None
            if copied is not None:
                self._check_cancelled()
                partial.replace(output)
                progress_callback(1)
                return copied
            completed = 0.0
            progress_callback(0)
            chunks: list[Path] = []
            for index, ((source, info, start, end), duration) in enumerate(zip(spans, durations)):
                self._check_cancelled()
                factor = min(width / info.width, height / info.height)
                fitted_width = max(2, min(width, round(info.width * factor / 2) * 2))
                fitted_height = max(2, min(height, round(info.height * factor / 2) * 2))
                video_filter = (
                    f"setpts=PTS-STARTPTS,fps={fps_text},scale={fitted_width}:{fitted_height},"
                    f"setsar=1,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
                    f"tpad=stop_mode=clone:stop_duration={duration:.9f},trim=duration={duration:.9f}"
                )
                audio_filter = (
                    "aresample=48000:async=1:first_pts=0,"
                    "aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=stereo,"
                    f"apad,atrim=duration={duration:.9f},asetpts=N/SR/TB"
                )
                arguments = (["-ss", f"{start:.9f}"] if start else []) + ["-i", str(source)]
                if not info.has_audio:
                    arguments.extend(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"])
                chunk = scratch / f"part-{index:05d}.mkv"
                # PCM avoids an AAC priming/padding interval at every join. AAC
                # is encoded once across the full continuous audio timeline.
                arguments.extend([
                    "-map", "0:V:0", "-map", "0:a:0" if info.has_audio else "1:a:0",
                    "-vf", video_filter, "-af", audio_filter,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-bf", "0",
                    "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
                    "-t", f"{duration:.9f}", "-map_metadata", "-1", str(chunk),
                ])
                self._ffmpeg(arguments, duration,
                             lambda value: progress_callback(0.9 * (completed + value * duration) / total_duration))
                completed += duration
                chunks.append(chunk)
            manifest = scratch / "concat.txt"
            manifest.write_text("".join(
                f"file '{chunk.name}'\nduration {duration:.9f}\n"
                for chunk, duration in zip(chunks, durations)
            ), encoding="utf-8")
            self._ffmpeg([
                "-f", "concat", "-safe", "1", "-i", str(manifest),
                "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
                "-af", f"aresample=48000:async=1:first_pts=0,apad,atrim=duration={total_duration:.9f}",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                "-t", f"{total_duration:.9f}", "-movflags", "+faststart", str(partial),
            ], total_duration, lambda value: progress_callback(0.9 + value * 0.09))
            self._check_cancelled()
            result = probe_video(partial, self.settings.ffprobe)
            progress_callback(1)
            partial.replace(output)
            return result
        finally:
            partial.unlink(missing_ok=True)
            if scratch.resolve().parent == work_dir and scratch.name.startswith("merge-"):
                shutil.rmtree(scratch, ignore_errors=True)

    @staticmethod
    def _kept_ranges(
        infos: list[VideoInfo], cut_ranges: list[list[CutRange]] | None,
    ) -> list[list[tuple[float, float]]]:
        result = []
        for index, info in enumerate(infos):
            cuts: list[tuple[float, float]] = []
            for cut in (cut_ranges[index] if cut_ranges is not None else []):
                if (not math.isfinite(cut.start) or not math.isfinite(cut.end)
                        or cut.start < 0 or cut.end > info.duration + 0.001
                        or cut.end <= cut.start):
                    raise MediaError("Kesilecek aralıklar video süresi içinde olmalıdır.")
                cuts.append((cut.start, min(cut.end, info.duration)))
            cuts.sort()
            kept = []
            cursor = 0.0
            for start, end in cuts:
                if start > cursor:
                    kept.append((cursor, start))
                cursor = max(cursor, end)
            if cursor < info.duration:
                kept.append((cursor, info.duration))
            if not kept or sum(end - start for start, end in kept) < 0.05 - 1e-9:
                raise MediaError("Kesimden sonra her videoda en az 0,05 saniye kalmalıdır.")
            result.append(kept)
        return result

    def _cut_streams(self, source: Path) -> tuple[dict, dict | None]:
        """Only accept MP4 streams whose compressed packets can be joined safely."""
        payload = json.loads(run_command([
            self.settings.ffprobe, "-v", "error", "-show_streams", "-show_format",
            "-show_data_hash", "sha256", "-of", "json", str(source),
        ]).stdout)
        videos = [stream for stream in payload.get("streams", [])
                  if stream.get("codec_type") == "video"
                  and not stream.get("disposition", {}).get("attached_pic")]
        audios = [stream for stream in payload.get("streams", []) if stream.get("codec_type") == "audio"]
        if ("mp4" not in payload.get("format", {}).get("format_name", "").split(",")
                or len(videos) != 1 or len(audios) > 1):
            raise MediaError("Hızlı kesim MP4/MOV içindeki tek H.264 veya HEVC video akışını destekler. Hassas modu seç.")
        video, audio = videos[0], audios[0] if audios else None
        if ((video.get("codec_name"), video.get("codec_tag_string"))
                not in {("h264", "avc1"), ("hevc", "hvc1"), ("hevc", "hev1")}
                or not video.get("extradata_hash")
                or video.get("sample_aspect_ratio") not in {None, "N/A", "1:1"}
                or any(side.get("rotation") for side in video.get("side_data_list", []))
                or video.get("tags", {}).get("rotate")
                or abs(float(video.get("start_time", "nan"))) > 0.001
                or not math.isfinite(float(video.get("start_time", "nan")))):
            raise MediaError("Bu video hızlı kesimle uyumlu değil. Hassas modu seç.")
        if audio and (audio.get("codec_name") != "aac" or not audio.get("extradata_hash")):
            raise MediaError("Hızlı kesim AAC sesini destekler. Bu video için Hassas modu seç.")
        try:
            if int(video["id"], 0) <= 0 or (audio and int(audio["id"], 0) <= 0):
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise MediaError("Akış kimlikleri hızlı kesimle uyumlu değil. Hassas modu seç.") from exc
        return video, audio

    def _keyframe_neighbours(self, source: Path, target: float, duration: float) -> tuple[tuple[float, float], tuple[float, float]]:
        """Seek near an edit; do not decode or scan the full long source."""
        for window in (2, 8, 30, 120):
            self._check_cancelled()
            payload = json.loads(run_command([
                self.settings.ffprobe, "-v", "error", "-select_streams", "V:0",
                "-read_intervals", f"{target:.9f}%+{window}", "-show_packets",
                "-show_entries", "packet=pts_time,dts_time,duration_time,flags", "-of", "json", str(source),
            ]).stdout)
            keys = [(float(packet["pts_time"]), float(packet["dts_time"]))
                    for packet in payload.get("packets", [])
                    if "K" in packet.get("flags", "")
                    and "pts_time" in packet and "dts_time" in packet]
            keys = [key for key in keys if all(math.isfinite(value) for value in key)]
            before = [key for key in keys if key[0] <= target + 1e-6]
            after = [key for key in keys if key[0] >= target - 1e-6]
            # End-of-file is a valid boundary even when the last GOP is shorter.
            packets = payload.get("packets", [])
            reached_end = not packets or max(
                float(packet.get("pts_time", 0)) + float(packet.get("duration_time", 0))
                for packet in packets
            ) >= duration - 0.001
            if before and (after or reached_end):
                return max(before), min(after) if after else (duration, duration)
        raise MediaError("Kesimin yakınında güvenli ana kare bulunamadı. Hassas modu seç.")

    def _require_closed_keyframe(self, source: Path, timestamp: float, stream: dict) -> None:
        """Open-GOP recovery frames can reference removed pictures; require IDR/BLA."""
        self._check_cancelled()
        payload = json.loads(run_command([
            self.settings.ffprobe, "-v", "error", "-select_streams", "V:0",
            "-read_intervals", f"{timestamp:.9f}%+#1", "-show_packets", "-show_data",
            "-show_entries", "packet=pts_time,flags,data", "-of", "json", str(source),
        ]).stdout)
        packets = payload.get("packets", [])
        if not packets or abs(float(packets[0].get("pts_time", "nan")) - timestamp) > 0.001:
            raise MediaError("Kesim ana karesine güvenli erişilemedi. Hassas modu seç.")
        hex_rows = []
        for row in packets[0].get("data", "").splitlines():
            if ":" in row:
                hex_rows.append(row.partition(":")[2].strip().split("  ", 1)[0].replace(" ", ""))
        try:
            packet = bytes.fromhex("".join(hex_rows))
            length_size = int(stream.get("nal_length_size") or 4)
            offset, closed = 0, False
            while offset + length_size < len(packet):
                length = int.from_bytes(packet[offset:offset + length_size], "big")
                offset += length_size
                if not length or offset + length > len(packet):
                    break
                nal_type = packet[offset] & 31 if stream["codec_name"] == "h264" else (packet[offset] >> 1) & 63
                is_closed = nal_type == 5 if stream["codec_name"] == "h264" else 16 <= nal_type <= 20
                if is_closed:
                    closed = True
                offset += length
        except (ValueError, TypeError):
            closed = False
        if not closed:
            raise MediaError("Bu kesim açık GOP ana karesine denk geliyor. Görüntü hatasını önlemek için Hassas modu seç.")

    def _render_fast_cuts(
        self, sources: list[Path], infos: list[VideoInfo], kept_ranges: list[list[tuple[float, float]]],
        scratch: Path, partial: Path, progress: Callable[[float], None],
    ) -> VideoInfo:
        streams = [self._cut_streams(source) for source in sources]
        reference_video, reference_audio = streams[0]
        video_fields = ("id", "codec_name", "codec_tag_string", "width", "height", "pix_fmt",
                        "time_base", "field_order", "color_range", "color_space", "color_transfer",
                        "color_primaries", "chroma_location", "extradata_hash")
        audio_fields = ("id", "codec_name", "sample_rate", "channels", "channel_layout", "extradata_hash")
        for video, audio in streams:
            if (tuple(video.get(key) for key in video_fields) != tuple(reference_video.get(key) for key in video_fields)
                    or (audio is None) != (reference_audio is None)
                    or (audio and tuple(audio.get(key) for key in audio_fields)
                        != tuple(reference_audio.get(key) for key in audio_fields))):
                raise MediaError("Seçilen videoların görüntü veya ses biçimleri hızlı kesim için aynı olmalıdır. Hassas modu seç.")
        progress(0)
        video_entries, audio_entries = [], []
        total = 0.0
        expected_bytes = 0
        for source, info, ranges, (video, audio) in zip(sources, infos, kept_ranges, streams):
            filename = source.resolve().as_posix()
            if any(char in filename for char in "\r\n\0"):
                raise MediaError("Kaynak dosya yolu hızlı kesimle uyumlu değil.")
            quoted = "'file:" + filename.replace("'", "'\\''") + "'"
            boundaries: dict[float, tuple[tuple[float, float], tuple[float, float]]] = {}
            validated: set[float] = set()
            local_duration = 0.0
            for start, end in ranges:
                # Removed intervals expand to keyframes: keep starts round UP,
                # keep ends round DOWN. No removed frame is exposed in output.
                if start == 0:
                    begin = (0.0, 0.0)
                else:
                    if start not in boundaries:
                        boundaries[start] = self._keyframe_neighbours(source, start, info.duration)
                    begin = boundaries[start][1]
                if end == info.duration:
                    finish = (info.duration, info.duration)
                else:
                    if end not in boundaries:
                        boundaries[end] = self._keyframe_neighbours(source, end, info.duration)
                    finish = boundaries[end][0]
                if finish[0] - begin[0] < 0.05 - 1e-9:
                    continue
                for key in (begin, finish):
                    if 0 < key[0] < info.duration and key[0] not in validated:
                        self._require_closed_keyframe(source, key[0], video)
                        validated.add(key[0])
                span_duration = finish[0] - begin[0]
                # outpoint compares DTS. Using the keyframe's PTS would leak
                # the next GOP's first pictures when the video has B frames.
                video_entries.append(f"file {quoted}\ninpoint {begin[0]:.9f}\noutpoint {finish[1]:.9f}\nduration {span_duration:.9f}\n")
                audio_entries.append(f"file {quoted}\ninpoint {begin[0]:.9f}\noutpoint {finish[0]:.9f}\nduration {span_duration:.9f}\n")
                local_duration += span_duration
            if not local_duration:
                raise MediaError("Ana karelere hizalama bu videonun tamamını kaldırıyor. Daha kısa aralık seç veya Hassas modu kullan.")
            total += local_duration
            expected_bytes += math.ceil(source.stat().st_size * local_duration / info.duration)
        if shutil.disk_usage(partial.parent).free < expected_bytes + math.ceil(total * 24000) + max(0, self.settings.minimum_free_bytes):
            raise MediaError("Kesilmiş video için çıktı diskinde yeterli boş alan yok.")
        video_manifest = scratch / "cut-video.ffconcat"
        video_manifest.write_text(
            f"ffconcat version 1.0\nstream\nexact_stream_id {int(reference_video['id'], 0)}\n" + "".join(video_entries),
            encoding="utf-8", newline="\n",
        )
        # Preserve each demuxer's common timeline. Otherwise FFmpeg shifts the
        # audio input by its negative AAC priming start and delays the sound.
        arguments = ["-copyts", "-f", "concat", "-safe", "0", "-auto_convert", "0",
                     "-protocol_whitelist", "file", "-i", str(video_manifest)]
        if reference_audio:
            audio_manifest = scratch / "cut-audio.ffconcat"
            audio_manifest.write_text(
                f"ffconcat version 1.0\nstream\nexact_stream_id {int(reference_audio['id'], 0)}\n" + "".join(audio_entries),
                encoding="utf-8", newline="\n",
            )
            arguments.extend(["-f", "concat", "-safe", "0", "-protocol_whitelist", "file",
                              "-segment_time_metadata", "1", "-i", str(audio_manifest)])
        arguments.extend(["-map", "0:0", "-c:v", "copy", "-tag:v", reference_video["codec_tag_string"]])
        if reference_audio:
            # Encode only sound once. Resampling removes AAC packet pre-roll
            # and fills short source gaps without accumulating drift at joins.
            arguments.extend(["-map", "1:0", "-af", f"aselect=concatdec_select,aresample=48000:async=1:min_hard_comp=0.001:first_pts=0,apad,atrim=duration={total:.9f}",
                              "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"])
        arguments.extend(["-t", f"{total:.9f}", "-map_metadata", "-1", "-map_chapters", "-1", "-write_tmcd", "0", str(partial)])
        self._ffmpeg(arguments, total, lambda value: progress(value * 0.99))
        self._check_cancelled()
        result = probe_video(partial, self.settings.ffprobe)
        if (abs(result.duration - total) > max(0.05, 2 / infos[0].fps)
                or result.width != infos[0].width or result.height != infos[0].height
                or result.has_audio != (reference_audio is not None)):
            raise MediaError("Hızlı kesim çıktısı doğrulanamadı. Hassas modu seç.")
        return result

    def _try_stream_copy(
        self, sources: list[Path], infos: list[VideoInfo], scratch: Path, partial: Path,
        progress: Callable[[float], None],
    ) -> VideoInfo | None:
        reference = infos[0].copy_info
        if reference is None or not 1 <= reference.frame_rate <= 60:
            return None
        durations = []
        frames = 0
        source_size = 0
        quoted_sources = []
        for source, info in zip(sources, infos):
            stream = info.copy_info
            if (stream is None or not stream.zero_start
                    or info.width % 2 or info.height % 2
                    or (stream.video_id, stream.audio_id) != (reference.video_id, reference.audio_id)
                    or stream.video_format != reference.video_format or stream.audio_format != reference.audio_format
                    or stream.video_configuration != reference.video_configuration
                    or stream.frame_rate != reference.frame_rate
                    or abs(info.duration - stream.audio_duration) > 0.15):
                return None
            # Use the longest selected stream for each join. Camera metadata
            # tracks can otherwise extend a concat segment past its picture and sound.
            durations.append(max(info.duration, stream.audio_duration))
            frames += stream.frame_count
            filename = source.resolve().as_posix()
            if any(char in filename for char in "\r\n\0"):
                return None
            quoted_sources.append("'file:" + filename.replace("'", "'\\''") + "'")
            source_size += source.stat().st_size

        reserve = max(0, self.settings.minimum_free_bytes)
        if shutil.disk_usage(partial.parent).free < source_size + reserve:
            raise MediaError("Birleştirilmiş video için çıktı diskinde yeterli boş alan yok.")

        manifest = scratch / "direct.ffconcat"
        manifest.write_text(
            f"ffconcat version 1.0\nstream\nexact_stream_id {reference.video_id}\n"
            f"stream\nexact_stream_id {reference.audio_id}\n" + "".join(
                f"file {source}\nduration {duration:.9f}\n"
                for source, duration in zip(quoted_sources, durations)
            ), encoding="utf-8", newline="\n",
        )
        try:
            progress(0)
            duration = sum(durations)
            self._ffmpeg([
                "-f", "concat", "-safe", "0", "-auto_convert", "0", "-protocol_whitelist", "file",
                "-i", str(manifest), "-map", "0:0", "-map", "0:1", "-c", "copy",
                "-tag:v", reference.video_tag, "-video_track_timescale", str(reference.video_timescale),
                "-map_metadata", "-1", "-map_chapters", "-1", "-write_tmcd", "0", str(partial),
            ], duration, lambda value: progress(value * 0.99))
            self._check_cancelled()
            result = probe_video(partial, self.settings.ffprobe)
            copied = result.copy_info
            expected_video_duration = sum(durations[:-1]) + infos[-1].duration
            # MP4 muxing can rewrite the configuration record while leaving the
            # HEVC packets and the parameters used by the decoder intact.
            if (copied is not None and copied.frame_count == frames
                    and copied.video_format == reference.video_format and copied.audio_format == reference.audio_format
                    and abs(result.duration - expected_video_duration) <= 0.15
                    and abs(copied.audio_duration - duration) <= 0.2):
                return result
            logger.warning("Video stream copy failed output verification; normalizing sources")
        except MediaError as exc:
            self._check_cancelled()
            if any(token in str(exc).casefold() for token in (
                "no space left", "not enough space", "disk full", "disk quota",
            )):
                raise MediaError("Birleştirme sırasında çıktı diskindeki boş alan doldu.") from exc
            logger.warning("Video stream copy failed; normalizing sources")
        partial.unlink(missing_ok=True)
        return None

    def _check_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise MediaError("Video birleştirme uygulama kapanırken durduruldu.")

    def _ffmpeg(
        self, arguments: list[str], duration: float, progress: Callable[[float], None],
    ) -> None:
        command = [self.settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                   "-progress", "pipe:1", "-nostats", *arguments]
        with self._lock:
            self._check_cancelled()
            try:
                process = subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError as exc:
                raise MediaError(f"FFmpeg başlatılamadı: {exc}") from exc
            self._process = process
        errors: list[str] = []
        try:
            assert process.stdout is not None
            for line in process.stdout:
                key, _, value = line.strip().partition("=")
                if key == "out_time_us":
                    try:
                        progress(min(0.99, max(0, float(value) / 1_000_000 / duration)))
                    except ValueError:
                        pass
                elif "=" not in line:
                    errors.append(line.strip())
                    errors = errors[-12:]
            result = process.wait()
            self._check_cancelled()
            if result != 0:
                raise MediaError(" ".join(errors) or "Video birleştirme işlemi başarısız.")
            progress(1)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if process.stdout is not None:
                process.stdout.close()
            with self._lock:
                self._process = None
