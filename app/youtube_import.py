from __future__ import annotations

import json
import logging
import math
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

from app.config import Settings
from app.media import MediaError, run_command


logger = logging.getLogger(__name__)
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}\Z")
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
SHORT_HOSTS = {"youtu.be", "www.youtu.be"}
SOURCE_PREFIX = "youtube-source"
SOURCE_EXTENSIONS = (".mp4", ".mkv", ".webm", ".mov", ".m4v")
HLS_FORMAT = (
    "bestvideo[protocol^=m3u8][vcodec^=avc1]+bestaudio[protocol^=m3u8]/"
    "best[protocol^=m3u8][vcodec^=avc1]/"
    "bestvideo[protocol^=m3u8]+bestaudio[protocol^=m3u8]/best[protocol^=m3u8]"
)


class YoutubeImportError(RuntimeError):
    pass


class _InvalidYoutubeSource(YoutubeImportError):
    pass


@dataclass(frozen=True)
class YoutubeDownload:
    source_path: Path
    title: str
    url: str
    video_id: str


@dataclass(frozen=True)
class YoutubeChannelVideo:
    id: str
    title: str
    url: str
    thumbnail_url: str | None
    duration: float | None
    upload_date: str | None


@dataclass(frozen=True)
class YoutubeChannelVideos:
    channel_title: str
    channel_url: str
    videos: list[YoutubeChannelVideo]
    has_more: bool


def _parse_url(value: str):
    if not isinstance(value, str):
        raise YoutubeImportError("Geçerli bir YouTube bağlantısı girin.")
    value = value.strip()
    if not value or len(value) > 2048 or any(character.isspace() for character in value):
        raise YoutubeImportError("Geçerli bir YouTube bağlantısı girin.")
    if any(ord(character) < 32 for character in value) or "\\" in value:
        raise YoutubeImportError("Geçerli bir YouTube bağlantısı girin.")
    if "://" not in value:
        value = f"https://{value}"
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"https", "http"}
            or parsed.hostname not in YOUTUBE_HOSTS | SHORT_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
        ):
            raise ValueError("Not a supported YouTube URL")
    except ValueError as exc:
        raise YoutubeImportError("Yalnızca youtube.com veya youtu.be bağlantıları kullanılabilir.") from exc
    return parsed


def normalize_youtube_url(url: str) -> str:
    parsed = _parse_url(url)
    parts = parsed.path.strip("/").split("/")
    video_id = ""
    if parsed.hostname in SHORT_HOSTS and len(parts) == 1:
        video_id = parts[0]
    elif parsed.hostname in YOUTUBE_HOSTS:
        if parsed.path == "/watch":
            values = parse_qs(parsed.query).get("v", [])
            if len(values) == 1:
                video_id = values[0]
        elif len(parts) == 2 and parts[0] in {"shorts", "live", "embed"}:
            video_id = parts[1]
    if not VIDEO_ID.fullmatch(video_id):
        raise YoutubeImportError("Tek bir videonun YouTube bağlantısını girin; kanal veya oynatma listesi kullanılamaz.")
    return f"https://www.youtube.com/watch?v={video_id}"


def normalize_youtube_channel_url(url: str) -> str:
    parsed = _parse_url(url)
    if parsed.hostname not in YOUTUBE_HOSTS:
        raise YoutubeImportError("youtube.com üzerindeki kanal bağlantısını girin.")
    parts = parsed.path.strip("/").split("/")
    if parts[-1:] and parts[-1] in {"videos", "featured", "shorts", "streams"}:
        parts = parts[:-1]
    valid = False
    if len(parts) == 1 and parts[0].startswith("@"):
        handle = unquote(parts[0][1:])
        valid = 1 <= len(handle) <= 100 and bool(re.fullmatch(r"[\w.\-·]+", handle))
        parts = ["@" + quote(handle, safe="._-·")]
    elif len(parts) == 2:
        if parts[0] == "channel":
            valid = bool(CHANNEL_ID.fullmatch(parts[1]))
        elif parts[0] in {"c", "user"}:
            valid = bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", parts[1]))
    if not valid:
        raise YoutubeImportError("Kanalın @kullanıcıadı, /channel/, /c/ veya /user/ bağlantısını girin.")
    return f"https://www.youtube.com/{'/'.join(parts)}/videos"


class _QuietLogger:
    def debug(self, message: str) -> None:
        logger.debug("yt-dlp: %s", message)

    def warning(self, message: str) -> None:
        logger.debug("yt-dlp: %s", message)

    def error(self, message: str) -> None:
        logger.debug("yt-dlp: %s", message)


def _make_youtube_dl(options: dict[str, Any]):
    try:
        from yt_dlp import YoutubeDL
    except ImportError as exc:
        raise YoutubeImportError("YouTube desteği kurulu değil. Uygulamayı start.ps1 ile yeniden başlatın.") from exc
    return YoutubeDL(options)


def _base_options() -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "logger": _QuietLogger(),
        "socket_timeout": 20,
        "retries": 2,
        "extractor_retries": 2,
        "fragment_retries": 2,
        "js_runtimes": {"deno": {}, "node": {}},
        "remote_components": set(),
        "cachedir": False,
        "usenetrc": False,
        "cookiefile": None,
        "cookiesfrombrowser": None,
        "ignoreerrors": False,
    }


def _public_error(exc: Exception) -> YoutubeImportError:
    message = str(exc).lower()
    if "403" in message or "forbidden" in message:
        return YoutubeImportError(
            "YouTube bu videonun indirilmesine erişim izni vermedi (403). "
            "Daha sonra tekrar deneyin veya kendi videonuzu YouTube Studio’dan indirip dosya olarak yükleyin."
        )
    if any(word in message for word in ("private video", "members-only", "login required", "sign in", "not a bot", "age-restricted")):
        return YoutubeImportError(
            "YouTube bu içerik için oturum açılmasını istiyor. Herkese açık bir video seçin "
            "veya YouTube Studio’dan indirdiğiniz dosyayı yükleyin."
        )
    if any(word in message for word in ("unavailable", "not available", "removed", "does not exist", "404")):
        return YoutubeImportError("YouTube içeriği bulunamadı veya şu anda erişilemiyor. Bağlantıyı kontrol edin.")
    if any(word in message for word in ("timed out", "timeout", "connection", "network", "resolve", "429")):
        return YoutubeImportError("YouTube bağlantısı kurulamadı. İnternet bağlantısını kontrol edip tekrar deneyin.")
    if any(word in message for word in ("javascript", "js runtime", "challenge", "requested format", "no video formats")):
        return YoutubeImportError(
            "YouTube video biçimleri alınamadı. Node.js 22 veya üzerinin kurulu olduğunu "
            "kontrol edin ve uygulamayı start.ps1 ile güncelleyin."
        )
    return YoutubeImportError("YouTube içeriği alınamadı. Bağlantıyı kontrol edip tekrar deneyin veya video dosyasını yükleyin.")


def _duration(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def list_youtube_channel_videos(url: str, limit: int = 30, offset: int = 0) -> YoutubeChannelVideos:
    canonical_url = normalize_youtube_channel_url(url)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise YoutubeImportError("Listelenecek video sayısı 1 ile 50 arasında olmalıdır.")
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 10000:
        raise YoutubeImportError("Video listesi başlangıcı 0 ile 10000 arasında olmalıdır.")
    options = {
        **_base_options(),
        "extract_flat": "in_playlist",
        "skip_download": True,
        "playliststart": offset + 1,
        "playlistend": offset + limit + 1,
        "lazy_playlist": True,
    }
    try:
        with _make_youtube_dl(options) as downloader:
            info = downloader.extract_info(canonical_url, download=False)
            if not isinstance(info, dict) or "entries" not in info:
                raise YoutubeImportError("Bu bağlantıdan kanal videoları alınamadı.")
            # lazy_playlist may return an iterator. Bound it independently of yt-dlp's options.
            from itertools import islice

            entries = list(islice(info.get("entries") or [], limit + 1))
        videos: list[YoutubeChannelVideo] = []
        seen: set[str] = set()
        for entry in entries[:limit]:
            if not isinstance(entry, dict):
                continue
            video_id = str(entry.get("id") or "")
            if not VIDEO_ID.fullmatch(video_id) or video_id in seen:
                continue
            title = str(entry.get("title") or "").strip()
            if not title or title.lower() in {"[private video]", "[deleted video]", "private video", "deleted video"}:
                continue
            if entry.get("is_live") or entry.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
                continue
            if entry.get("availability") in {"private", "premium_only", "subscriber_only", "needs_auth"}:
                continue
            seen.add(video_id)
            upload_date = entry.get("upload_date")
            videos.append(YoutubeChannelVideo(
                id=video_id,
                title=title[:500],
                url=f"https://www.youtube.com/watch?v={video_id}",
                thumbnail_url=f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                duration=_duration(entry.get("duration")),
                upload_date=str(upload_date) if upload_date else None,
            ))
        channel_title = str(info.get("channel") or info.get("uploader") or info.get("title") or "YouTube kanalı")
        return YoutubeChannelVideos(channel_title[:500], canonical_url, videos, len(entries) > limit)
    except YoutubeImportError:
        raise
    except Exception as exc:
        logger.warning("YouTube channel listing failed: %s", type(exc).__name__)
        raise _public_error(exc) from exc


def _require_space(directory: Path, reserve_bytes: int, additional_bytes: int = 0) -> None:
    free = shutil.disk_usage(directory).free
    if free < max(0, reserve_bytes) + max(0, additional_bytes):
        raise YoutubeImportError("Videoyu indirmek ve işlemek için yeterli boş disk alanı yok. Yer açıp tekrar deneyin.")


def _verify_downloaded_source(
    source: Path,
    ffmpeg: str,
    ffprobe: str,
    expected_duration: float | None,
    check_cancelled: Callable[[], None],
) -> None:
    invalid_message = "İndirilen YouTube videosu eksik veya bozuk. İçe aktarmayı tekrar deneyin."
    try:
        probe = run_command([
            ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type,duration",
            "-of", "json", str(source),
        ])
        metadata = json.loads(probe.stdout)
    except OSError as exc:
        raise YoutubeImportError("İndirilen videoyu doğrulamak için FFprobe çalıştırılamadı.") from exc
    except (MediaError, ValueError) as exc:
        raise _InvalidYoutubeSource(invalid_message) from exc

    streams = metadata.get("streams", [])
    durations = []
    for kind in ("video", "audio"):
        stream = next((item for item in streams if item.get("codec_type") == kind), None)
        if stream is None:
            raise _InvalidYoutubeSource(invalid_message)
        durations.append(_duration(stream.get("duration")))
    durations.append(_duration(metadata.get("format", {}).get("duration")))
    # YouTube's reported duration is rounded, and track start/end times may differ slightly.
    if expected_duration and any(value and value + 2 < expected_duration for value in durations):
        raise _InvalidYoutubeSource(invalid_message)

    check_cancelled()
    command = [
        ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-xerror", "-err_detect", "explode",
        "-abort_on", "empty_output_stream", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0",
        "-f", "null", "-",
    ]
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        raise YoutubeImportError("İndirilen videoyu doğrulamak için FFmpeg çalıştırılamadı.") from exc
    with process:
        try:
            while True:
                check_cancelled()
                try:
                    _, errors = process.communicate(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
    if process.returncode or errors:
        logger.debug("YouTube source decode failed: %s", errors.strip())
        raise _InvalidYoutubeSource(invalid_message)


def download_youtube_video(
    url: str,
    job_dir: Path,
    settings: Settings,
    progress_callback: Callable[[float, str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> YoutubeDownload:
    canonical_url = normalize_youtube_url(url)
    video_id = parse_qs(urlsplit(canonical_url).query)["v"][0]
    ffmpeg = shutil.which(settings.ffmpeg)
    if ffmpeg is None and Path(settings.ffmpeg).is_file():
        ffmpeg = str(Path(settings.ffmpeg).resolve())
    if ffmpeg is None:
        raise YoutubeImportError("YouTube videosunu birleştirmek için FFmpeg bulunamadı.")
    job_dir = Path(job_dir).resolve()
    job_dir.mkdir(parents=True, exist_ok=True)
    final_names = tuple(f"{SOURCE_PREFIX}{extension}" for extension in SOURCE_EXTENSIONS)
    if any((job_dir / name).is_file() for name in final_names):
        raise YoutubeImportError("Bu projede kaynak video zaten var; mevcut dosya korunuyor.")

    def check_cancelled() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise YoutubeImportError("YouTube aktarımı uygulama kapatıldığı için durduruldu. Tekrar deneyebilirsiniz.")

    last_progress = 0.0
    last_report_time = 0.0
    last_report_stage: str | None = None

    def report(value: float, stage: str) -> None:
        nonlocal last_progress, last_report_time, last_report_stage
        last_progress = max(last_progress, min(1.0, max(0.0, value)))
        now = time.monotonic()
        if progress_callback and (stage != last_report_stage or value >= 1 or now - last_report_time >= 0.5):
            progress_callback(last_progress, stage)
            last_report_time = now
            last_report_stage = stage

    def progress_hook(data: dict[str, Any]) -> None:
        check_cancelled()
        _require_space(job_dir, settings.minimum_free_bytes)
        if data.get("status") == "downloading":
            total = _duration(data.get("total_bytes") or data.get("total_bytes_estimate"))
            downloaded = _duration(data.get("downloaded_bytes")) or 0
            if total:
                # Keep enough space for the remaining stream plus the final merged file.
                _require_space(job_dir, settings.minimum_free_bytes, int(max(0, total - downloaded) + total))
                fraction = min(1.0, downloaded / total)
                report(0.1 + fraction * 0.75, "YouTube videosu indiriliyor")
            else:
                report(0.1, "YouTube videosu indiriliyor")
        elif data.get("status") == "finished":
            report(0.86, "Video ve ses birleştiriliyor")

    def download_attempt(attempt_dir: Path, format_selector: str) -> tuple[Path, str]:
        def postprocessor_hook(data: dict[str, Any]) -> None:
            check_cancelled()
            merge_bytes = 0
            if data.get("status") == "started" and "merger" in str(data.get("postprocessor", "")).lower():
                merge_bytes = sum(path.stat().st_size for path in attempt_dir.iterdir() if path.is_file())
            _require_space(job_dir, settings.minimum_free_bytes, merge_bytes)
            report(0.94, "İndirilen video hazırlanıyor")

        options = {
            **_base_options(),
            "noplaylist": True,
            "format": format_selector,
            "merge_output_format": "mp4/mkv",
            "outtmpl": str(attempt_dir / f"{SOURCE_PREFIX}.%(ext)s"),
            "ffmpeg_location": ffmpeg,
            "progress_hooks": [progress_hook],
            "postprocessor_hooks": [postprocessor_hook],
            "continuedl": False,
            "overwrites": False,
            "concurrent_fragment_downloads": 1,
            "skip_unavailable_fragments": False,
            "hls_prefer_native": True,
        }
        with _make_youtube_dl(options) as downloader:
            info = downloader.extract_info(canonical_url, download=False, ie_key="Youtube")
            check_cancelled()
            if not isinstance(info, dict) or info.get("_type", "video") != "video" or str(info.get("id")) != video_id:
                raise YoutubeImportError("Bağlantıdan tek bir YouTube videosu alınamadı.")
            if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
                raise YoutubeImportError("Canlı veya henüz başlamamış yayınlar aktarılamaz. Yayın tamamlandıktan sonra tekrar deneyin.")
            title = str(info.get("title") or video_id).strip()[:500] or video_id
            formats = info.get("requested_formats") or info.get("requested_downloads") or [info]
            expected_bytes = sum(
                _duration(item.get("filesize") or item.get("filesize_approx")) or 0
                for item in formats if isinstance(item, dict)
            )
            _require_space(job_dir, settings.minimum_free_bytes, int(expected_bytes * 2))
            downloader.process_ie_result(info, download=True)

        check_cancelled()
        source = next((path for name in final_names if (path := attempt_dir / name).is_file()), None)
        if source is None or not source.stat().st_size:
            raise _InvalidYoutubeSource("YouTube indirmesi tamamlanamadı; video dosyası oluşmadı.")
        report(0.96, "İndirilen görüntü ve ses doğrulanıyor")
        _verify_downloaded_source(source, ffmpeg, settings.ffprobe, _duration(info.get("duration")), check_cancelled)
        return source, title

    primary_format = "bestvideo[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/best[ext=mp4]/bestvideo+bestaudio/best"
    try:
        check_cancelled()
        _require_space(job_dir, settings.minimum_free_bytes)
        report(0.02, "YouTube video bilgileri alınıyor")
        previous_error: Exception | None = None
        for format_selector in (primary_format, HLS_FORMAT):
            try:
                with tempfile.TemporaryDirectory(prefix=".youtube-import-", dir=job_dir) as temp_dir:
                    source, title = download_attempt(Path(temp_dir), format_selector)
                    check_cancelled()
                    _require_space(job_dir, settings.minimum_free_bytes)
                    if any((job_dir / name).is_file() for name in final_names):
                        raise YoutubeImportError("Bu projede kaynak video zaten var; mevcut dosya korunuyor.")
                    source = source.replace(job_dir / source.name)
                report(1.0, "YouTube videosu aktarıldı")
                return YoutubeDownload(source, title, canonical_url, video_id)
            except Exception as exc:
                if previous_error is not None:
                    if isinstance(previous_error, _InvalidYoutubeSource) and not isinstance(exc, YoutubeImportError):
                        raise previous_error from exc
                    raise
                if not isinstance(exc, _InvalidYoutubeSource) and (
                    isinstance(exc, YoutubeImportError) or "403" not in str(exc)
                ):
                    raise
                previous_error = exc
                check_cancelled()
                report(last_progress, "Alternatif YouTube video akışı deneniyor")
        raise AssertionError("YouTube download attempts exhausted")
    except Exception as exc:
        if isinstance(exc, YoutubeImportError):
            raise
        logger.warning("YouTube video import failed: %s", type(exc).__name__)
        raise _public_error(exc) from exc
