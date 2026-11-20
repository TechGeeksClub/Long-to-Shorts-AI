from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app import youtube_import as service


VIDEO_ID = "AbcD_123-xy"
VIDEO_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


@pytest.mark.parametrize("url", [
    VIDEO_URL,
    f"http://youtube.com/watch?v={VIDEO_ID}&list=PLwhatever&t=20",
    f"https://m.youtube.com/shorts/{VIDEO_ID}",
    f"https://youtube.com/live/{VIDEO_ID}?feature=share",
    f"youtu.be/{VIDEO_ID}?si=tracking",
    f" https://youtu.be/{VIDEO_ID} ",
])
def test_normalizes_direct_video_links(url: str) -> None:
    assert service.normalize_youtube_url(url) == VIDEO_URL


@pytest.mark.parametrize("url", [
    "", "https://example.com/watch?v=AbcD_123-xy",
    "https://youtube.com.evil.test/watch?v=AbcD_123-xy",
    "https://youtube.com@127.0.0.1/watch?v=AbcD_123-xy",
    "https://name:secret@youtube.com/watch?v=AbcD_123-xy",
    "https://youtube.com:443/watch?v=AbcD_123-xy",
    "file:///video.mp4", "http://localhost/watch?v=AbcD_123-xy",
    "https://youtube.com/playlist?list=PL123", "https://youtube.com/@channel",
    "https://youtu.be/AbcD_123-xy/extra", "https://youtube.com/watch?v=short",
    "https://youtube.com/watch?v=AbcD_123-xy&v=otherId1234",
    "https://youtube.com/watch?v=AbcD_123-xy\nextra",
    "https://youtube.com\\@evil.test/watch?v=AbcD_123-xy",
])
def test_rejects_non_video_and_untrusted_urls(url: str) -> None:
    with pytest.raises(service.YoutubeImportError):
        service.normalize_youtube_url(url)


@pytest.mark.parametrize(("url", "expected"), [
    ("youtube.com/@ObanOtagi", "https://www.youtube.com/@ObanOtagi/videos"),
    ("https://m.youtube.com/@ObanOtagi/videos?view=0", "https://www.youtube.com/@ObanOtagi/videos"),
    ("https://youtube.com/channel/UCabcdefghijklmnopqrstuv", "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv/videos"),
    ("https://youtube.com/c/ObanOtagi/videos", "https://www.youtube.com/c/ObanOtagi/videos"),
    ("https://youtube.com/@ObanOtagi/streams", "https://www.youtube.com/@ObanOtagi/videos"),
    ("https://youtube.com/@ObanOtagi/featured", "https://www.youtube.com/@ObanOtagi/videos"),
    ("https://youtube.com/@ObanOtagi/shorts", "https://www.youtube.com/@ObanOtagi/videos"),
    ("https://youtube.com/user/ObanOtagi", "https://www.youtube.com/user/ObanOtagi/videos"),
    ("https://youtube.com/@ObanOtağı", "https://www.youtube.com/@ObanOta%C4%9F%C4%B1/videos"),
])
def test_normalizes_channel_links(url: str, expected: str) -> None:
    assert service.normalize_youtube_channel_url(url) == expected


@pytest.mark.parametrize("url", [
    VIDEO_URL, "https://youtu.be/AbcD_123-xy", "https://youtube.com/playlist?list=PL123",
    "https://youtube.com/@name%2Fmore",
    "https://youtube.com/channel/bad-id", "https://youtube.com/videos",
    "https://youtube.com/@..%2F..", "https://youtube.com/c/name/extra",
])
def test_rejects_non_channel_links(url: str) -> None:
    with pytest.raises(service.YoutubeImportError):
        service.normalize_youtube_channel_url(url)


class FakeDownloader:
    def __init__(self, info: dict, process=None):
        self.info = info
        self.process = process
        self.extract_calls = []
        self.options = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def extract_info(self, url, **kwargs):
        self.extract_calls.append((url, kwargs))
        return self.info

    def process_ie_result(self, info, download):
        assert download is True
        if self.process:
            return self.process(self, info)
        return info

    def output_path(self, extension="mp4"):
        return Path(self.options["outtmpl"].replace("%(ext)s", extension))


def fake_downloader(monkeypatch, info, process=None):
    downloader = FakeDownloader(info, process)

    def make(options):
        downloader.options = options
        return downloader

    monkeypatch.setattr(service, "_make_youtube_dl", make)
    return downloader


def test_lists_one_flat_channel_page_with_bounded_pagination(monkeypatch) -> None:
    entries = [
        {"id": VIDEO_ID, "title": "İlk video", "duration": 123.5, "upload_date": "20261012"},
        {"id": "Second1234_", "title": "İkinci video", "duration": None},
        {"id": "Another1234", "title": "Sonraki sayfa"},
    ]
    downloader = fake_downloader(monkeypatch, {"channel": "Oban Otağı", "entries": iter(entries)})
    result = service.list_youtube_channel_videos("https://youtube.com/@ObanOtagi", limit=2, offset=30)
    assert result.channel_title == "Oban Otağı"
    assert result.channel_url == "https://www.youtube.com/@ObanOtagi/videos"
    assert [video.title for video in result.videos] == ["İlk video", "İkinci video"]
    assert result.videos[0].url == VIDEO_URL
    assert result.videos[0].duration == 123.5
    assert result.videos[1].duration is None
    assert result.videos[0].upload_date == "20261012"
    assert result.videos[0].thumbnail_url == f"https://i.ytimg.com/vi/{VIDEO_ID}/hqdefault.jpg"
    assert result.has_more is True
    assert len(downloader.extract_calls) == 1
    assert downloader.options["extract_flat"] == "in_playlist"
    assert downloader.options["playliststart"] == 31
    assert downloader.options["playlistend"] == 33
    assert downloader.options["skip_download"] is True


def test_channel_page_hides_unavailable_and_live_entries(monkeypatch) -> None:
    entries = [
        None,
        {"id": VIDEO_ID, "title": "[Private video]"},
        {"id": VIDEO_ID, "title": "Live", "live_status": "is_live"},
        {"id": VIDEO_ID, "title": "Upcoming", "live_status": "is_upcoming"},
        {"id": VIDEO_ID, "title": "Members", "availability": "subscriber_only"},
        {"id": "not-an-id", "title": "Invalid"},
        {"id": VIDEO_ID, "title": "Yayın kaydı", "live_status": "was_live", "duration": float("nan")},
        {"id": VIDEO_ID, "title": "Duplicate"},
    ]
    fake_downloader(monkeypatch, {"uploader": "Channel", "entries": entries})
    result = service.list_youtube_channel_videos("youtube.com/@channel")
    assert [video.title for video in result.videos] == ["Yayın kaydı"]
    assert result.videos[0].duration is None
    assert result.has_more is False


def test_empty_channel_is_a_valid_result(monkeypatch) -> None:
    fake_downloader(monkeypatch, {"title": "Empty - Videos", "entries": []})
    result = service.list_youtube_channel_videos("youtube.com/@channel")
    assert result.videos == []
    assert result.has_more is False


@pytest.fixture
def download_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(service.shutil, "which", lambda _: "C:/tools/ffmpeg.exe")
    monkeypatch.setattr(service.shutil, "disk_usage", lambda _: SimpleNamespace(free=10_000_000))
    monkeypatch.setattr(service, "_verify_downloaded_source", lambda *_: None)
    return Settings(data_dir=tmp_path, minimum_free_bytes=1000), tmp_path / "job"


def video_info(**values):
    return {"id": VIDEO_ID, "title": "Kanalımdaki video", "live_status": "not_live", **values}


def successful_process(downloader, info):
    target = downloader.output_path()
    downloader.options["progress_hooks"][0]({
        "status": "downloading", "total_bytes": 100, "downloaded_bytes": 50,
    })
    target.write_bytes(b"video")
    downloader.options["progress_hooks"][0]({"status": "finished"})
    downloader.options["postprocessor_hooks"][0]({
        "status": "finished", "postprocessor": "MoveFiles", "info_dict": {"filepath": str(target)},
    })
    return {**info, "filepath": str(target)}


def test_downloads_a_single_video_and_returns_actual_source(monkeypatch, download_environment) -> None:
    settings, job_dir = download_environment
    downloader = fake_downloader(monkeypatch, video_info(), successful_process)
    progress = []
    result = service.download_youtube_video(VIDEO_URL, job_dir, settings, lambda value, stage: progress.append((value, stage)))
    assert result.source_path == job_dir / "youtube-source.mp4"
    assert result.title == "Kanalımdaki video"
    assert result.video_id == VIDEO_ID
    assert result.url == VIDEO_URL
    assert result.source_path.read_bytes() == b"video"
    assert downloader.extract_calls == [(VIDEO_URL, {"download": False, "ie_key": "Youtube"})]
    assert downloader.options["noplaylist"] is True
    assert downloader.options["cookiesfrombrowser"] is None
    assert downloader.options["cookiefile"] is None
    assert "node" in downloader.options["js_runtimes"]
    assert downloader.options["ffmpeg_location"] == "C:/tools/ffmpeg.exe"
    assert downloader.options["skip_unavailable_fragments"] is False
    assert list(job_dir.iterdir()) == [result.source_path]
    assert [value for value, _ in progress] == sorted(value for value, _ in progress)
    assert progress[-1][0] == 1


@pytest.mark.parametrize("live_status", ["is_live", "is_upcoming", "post_live"])
def test_rejects_live_videos_before_download(monkeypatch, download_environment, live_status) -> None:
    settings, job_dir = download_environment
    calls = []
    fake_downloader(monkeypatch, video_info(live_status=live_status), lambda *_: calls.append(True))
    with pytest.raises(service.YoutubeImportError, match="Canlı"):
        service.download_youtube_video(VIDEO_URL, job_dir, settings)
    assert not calls


def test_disk_reserve_is_checked_before_metadata(monkeypatch, download_environment) -> None:
    settings, job_dir = download_environment
    monkeypatch.setattr(service.shutil, "disk_usage", lambda _: SimpleNamespace(free=999))
    downloader = fake_downloader(monkeypatch, video_info())
    with pytest.raises(service.YoutubeImportError, match="disk alanı"):
        service.download_youtube_video(VIDEO_URL, job_dir, settings)
    assert downloader.extract_calls == []


def test_download_rejects_insufficient_space_for_streams_and_merge(monkeypatch, download_environment) -> None:
    settings, job_dir = download_environment
    monkeypatch.setattr(service.shutil, "disk_usage", lambda _: SimpleNamespace(free=3000))
    fake_downloader(monkeypatch, video_info(requested_formats=[{"filesize": 1200}, {"filesize": 400}]))
    with pytest.raises(service.YoutubeImportError, match="disk alanı"):
        service.download_youtube_video(VIDEO_URL, job_dir, settings)


def test_failed_download_cleans_its_partials_and_preserves_other_project_data(monkeypatch, download_environment) -> None:
    settings, job_dir = download_environment
    job_dir.mkdir()
    transcript = job_dir / "transcript.json"
    transcript.write_text("saved")

    def fail(downloader, _):
        downloader.output_path("mp4.part").write_bytes(b"partial")
        downloader.output_path("f137.mp4").write_bytes(b"stream")
        raise RuntimeError("connection timed out")

    fake_downloader(monkeypatch, video_info(), fail)
    with pytest.raises(service.YoutubeImportError, match="İnternet"):
        service.download_youtube_video(VIDEO_URL, job_dir, settings)
    assert transcript.read_text() == "saved"
    assert sorted(path.name for path in job_dir.iterdir()) == ["transcript.json"]


def test_running_download_stops_when_disk_reserve_is_reached(monkeypatch, download_environment) -> None:
    settings, job_dir = download_environment

    def fail(downloader, _):
        downloader.output_path("mp4.part").write_bytes(b"partial")
        monkeypatch.setattr(service.shutil, "disk_usage", lambda _: SimpleNamespace(free=900))
        downloader.options["progress_hooks"][0]({"status": "downloading", "downloaded_bytes": 4})

    fake_downloader(monkeypatch, video_info(), fail)
    with pytest.raises(service.YoutubeImportError, match="disk alanı"):
        service.download_youtube_video(VIDEO_URL, job_dir, settings)
    assert not list(job_dir.iterdir())


def test_cancellation_stops_download_and_cleans_partials(monkeypatch, download_environment) -> None:
    settings, job_dir = download_environment
    event = threading.Event()

    def cancel(downloader, _):
        downloader.output_path("mp4.part").write_bytes(b"partial")
        event.set()
        downloader.options["progress_hooks"][0]({"status": "downloading"})

    fake_downloader(monkeypatch, video_info(), cancel)
    with pytest.raises(service.YoutubeImportError, match="durduruldu"):
        service.download_youtube_video(VIDEO_URL, job_dir, settings, cancel_event=event)
    assert not list(job_dir.iterdir())


def test_existing_source_is_preserved(monkeypatch, download_environment) -> None:
    settings, job_dir = download_environment
    job_dir.mkdir()
    source = job_dir / "youtube-source.mp4"
    source.write_bytes(b"original")
    downloader = fake_downloader(monkeypatch, video_info(), successful_process)
    with pytest.raises(service.YoutubeImportError, match="zaten var"):
        service.download_youtube_video(VIDEO_URL, job_dir, settings)
    assert source.read_bytes() == b"original"
    assert downloader.extract_calls == []


def test_interrupted_separate_stream_does_not_block_import_retry(monkeypatch, download_environment):
    settings, job_dir = download_environment
    job_dir.mkdir()
    (job_dir / "youtube-source.f137.mp4").write_bytes(b"completed-video-stream")
    (job_dir / "youtube-source.f140.m4a.part").write_bytes(b"incomplete-audio-stream")
    fake_downloader(monkeypatch, video_info(), successful_process)
    result = service.download_youtube_video(VIDEO_URL, job_dir, settings)
    assert result.source_path == job_dir / "youtube-source.mp4"
    assert result.source_path.read_bytes() == b"video"
    assert (job_dir / "youtube-source.f137.mp4").read_bytes() == b"completed-video-stream"
    assert (job_dir / "youtube-source.f140.m4a.part").read_bytes() == b"incomplete-audio-stream"


def test_progress_updates_are_throttled_but_completion_is_reported(monkeypatch, download_environment) -> None:
    settings, job_dir = download_environment
    monkeypatch.setattr(service.time, "monotonic", lambda: 1.0)

    def process(downloader, info):
        hook = downloader.options["progress_hooks"][0]
        for downloaded in range(1, 1000):
            hook({"status": "downloading", "downloaded_bytes": downloaded, "total_bytes": 1000})
        return successful_process(downloader, info)

    fake_downloader(monkeypatch, video_info(), process)
    progress = []
    service.download_youtube_video(VIDEO_URL, job_dir, settings, lambda value, stage: progress.append((value, stage)))
    assert len(progress) <= 6
    assert progress[-1][0] == 1


def test_missing_final_source_is_an_error(monkeypatch, download_environment) -> None:
    settings, job_dir = download_environment
    fake_downloader(monkeypatch, video_info())
    with pytest.raises(service.YoutubeImportError, match="dosyası oluşmadı"):
        service.download_youtube_video(VIDEO_URL, job_dir, settings)


def test_individual_stream_reported_by_downloader_is_not_a_complete_source(monkeypatch, download_environment):
    settings, job_dir = download_environment

    def process(downloader, info):
        stream = downloader.output_path("f137.mp4")
        stream.write_bytes(b"video-only stream")
        return {**info, "filepath": str(stream)}

    monkeypatch.setattr(service, "_verify_downloaded_source", lambda *_: pytest.fail("Validated an individual stream"))
    fake_downloader(monkeypatch, video_info(), process)
    with pytest.raises(service.YoutubeImportError):
        service.download_youtube_video(VIDEO_URL, job_dir, settings)
    assert not list(job_dir.iterdir())


def test_private_channel_fetch_errors_are_actionable(monkeypatch) -> None:
    def unavailable(_):
        raise RuntimeError("Sign in to confirm you are not a bot")

    monkeypatch.setattr(service, "_make_youtube_dl", unavailable)
    with pytest.raises(service.YoutubeImportError, match="YouTube Studio"):
        service.list_youtube_channel_videos("youtube.com/@channel")


def test_forbidden_download_retries_hls_and_removes_failed_stream(monkeypatch, download_environment):
    settings, job_dir = download_environment
    selected_formats = []
    failed_streams = []

    def process(downloader, info):
        selected_formats.append(downloader.options["format"])
        if len(selected_formats) == 1:
            failed_stream = downloader.output_path("f137.mp4")
            failed_stream.write_bytes(b"failed-stream")
            failed_streams.append(failed_stream)
            raise RuntimeError("HTTP Error 403: Forbidden")
        assert not failed_streams[0].exists()
        return successful_process(downloader, info)

    fake_downloader(monkeypatch, video_info(), process)
    result = service.download_youtube_video(VIDEO_URL, job_dir, settings)
    assert result.source_path.read_bytes() == b"video"
    assert len(selected_formats) == 2
    assert selected_formats[1] == service.HLS_FORMAT


@pytest.mark.parametrize("fallback_is_valid", [True, False])
def test_corrupt_download_retries_and_publishes_only_a_verified_source(
    monkeypatch, download_environment, fallback_is_valid,
):
    settings, job_dir = download_environment
    attempts = []
    checked = []

    def process(downloader, info):
        attempts.append(downloader.options["format"])
        target = downloader.output_path()
        target.write_bytes(b"valid" if len(attempts) == 2 and fallback_is_valid else b"corrupt")
        return {**info, "filepath": str(target)}

    def verify(source, _ffmpeg, _ffprobe, expected_duration, check_cancelled):
        check_cancelled()
        assert expected_duration == 120
        assert not (job_dir / "youtube-source.mp4").exists()
        assert all(not previous.exists() for previous in checked)
        checked.append(source)
        if source.read_bytes() == b"corrupt":
            raise service._InvalidYoutubeSource("Video bozuk")

    monkeypatch.setattr(service, "_verify_downloaded_source", verify)
    fake_downloader(monkeypatch, video_info(duration=120), process)
    if fallback_is_valid:
        result = service.download_youtube_video(VIDEO_URL, job_dir, settings)
        assert result.source_path.read_bytes() == b"valid"
        assert list(job_dir.iterdir()) == [result.source_path]
    else:
        with pytest.raises(service.YoutubeImportError, match="Video bozuk"):
            service.download_youtube_video(VIDEO_URL, job_dir, settings)
        assert not list(job_dir.iterdir())
    assert len(attempts) == len(checked) == 2
    assert attempts[1] == service.HLS_FORMAT
    assert all(not path.exists() for path in checked)


@pytest.mark.parametrize(("message", "attempts", "expected_error"), [
    ("HTTP Error 403: Forbidden", 2, "403"),
    ("connection timed out", 1, "İnternet"),
])
def test_download_error_fallback_is_bounded(
    monkeypatch, download_environment, message, attempts, expected_error,
):
    settings, job_dir = download_environment
    calls = []

    def fail(downloader, _):
        calls.append(downloader.options["format"])
        downloader.output_path("mp4.part").write_bytes(b"partial")
        raise RuntimeError(message)

    fake_downloader(monkeypatch, video_info(), fail)
    with pytest.raises(service.YoutubeImportError, match=expected_error):
        service.download_youtube_video(VIDEO_URL, job_dir, settings)
    assert len(calls) == attempts
    assert not list(job_dir.iterdir())


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg and FFprobe required")
def test_verify_download_detects_a_truncated_mp4(tmp_path):
    source = tmp_path / "source.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=64x64:r=12:d=2",
         "-f", "lavfi", "-i", "sine=duration=2", "-c:v", "libx264", "-c:a", "aac",
         "-movflags", "+faststart", str(source)],
        check=True, capture_output=True,
    )
    service._verify_downloaded_source(source, "ffmpeg", "ffprobe", 2.0, lambda: None)

    # Keep the MP4 metadata intact while cutting off part of its media data.
    with source.open("r+b") as stream:
        stream.truncate(source.stat().st_size * 3 // 4)
    with pytest.raises(service.YoutubeImportError):
        service._verify_downloaded_source(source, "ffmpeg", "ffprobe", 2.0, lambda: None)
