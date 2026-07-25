from pathlib import Path
import json

from fastapi.testclient import TestClient

from app.config import Settings
from app.api import format_bytes, required_upload_space
from app.main import create_app
from app.models import ClipCandidate, SubtitleCue, SubtitleWord


def test_disk_requirement_keeps_reserve_instead_of_tripling_upload() -> None:
    gigabyte = 1024**3

    assert required_upload_space(4 * gigabyte, 2 * gigabyte) == 6 * gigabyte
    assert format_bytes(6 * gigabyte) == "6.0 GB"


def test_upload_job_lifecycle_without_worker(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        frontend_dist=tmp_path / "missing-frontend",
        minimum_free_bytes=0,
    )
    app = create_app(settings, start_worker=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/jobs",
            files={"video": ("sample.mp4", b"not-yet-processed", "video/mp4")},
        )
        assert response.status_code == 202
        job = response.json()
        assert job["status"] == "queued"

        detail = client.get(f"/api/jobs/{job['id']}")
        assert detail.status_code == 200
        assert detail.json()["filename"] == "sample.mp4"

        source = client.get(f"/api/jobs/{job['id']}/source")
        assert source.status_code == 200
        assert source.content == b"not-yet-processed"

        blocked_delete = client.delete(f"/api/jobs/{job['id']}")
        assert blocked_delete.status_code == 409

        app.state.database.update_job(job["id"], status="failed", stage="test")
        deleted = client.delete(f"/api/jobs/{job['id']}")
        assert deleted.status_code == 204
        assert not (tmp_path / "data" / "jobs" / job["id"]).exists()


def test_completed_export_exposes_metadata_download(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        frontend_dist=tmp_path / "missing-frontend",
        minimum_free_bytes=0,
    )
    app = create_app(settings, start_worker=False)
    job_dir = settings.data_dir / "jobs" / "job"
    export_dir = job_dir / "exports" / "short-1-export"
    export_dir.mkdir(parents=True)
    source = job_dir / "video.mp4"
    source.write_bytes(b"video")
    output = export_dir / "short-1-export.mp4"
    output.write_bytes(b"mp4")
    metadata = export_dir / "youtube-seo.md"
    metadata.write_text("# YouTube SEO Paketi", encoding="utf-8")
    database = app.state.database
    database.create_job("job", "video.mp4", source)
    database.update_job("job", status="completed", stage="Hazır")
    database.replace_clips(
        "job",
        [
            ClipCandidate(
                id="clip",
                job_id="job",
                rank=1,
                title="Klip",
                start=0,
                end=10,
                score=80,
                reasons=[],
                subtitles=[],
            )
        ],
    )
    database.create_export("export", "job", "clip")
    database.update_export("export", status="completed", progress=1.0, path=str(output))

    with TestClient(app) as client:
        detail = client.get("/api/jobs/job")
        download = client.get("/api/exports/export/metadata")

    assert detail.status_code == 200
    assert detail.json()["exports"][0]["metadata_filename"] == "youtube-seo.md"
    assert download.status_code == 200
    assert b"YouTube SEO Paketi" in download.content


def test_export_request_persists_optional_llm_seo_flag(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        frontend_dist=tmp_path / "missing-frontend",
        minimum_free_bytes=0,
    )
    app = create_app(settings, start_worker=False)
    job_dir = settings.data_dir / "jobs" / "job"
    job_dir.mkdir(parents=True)
    source = job_dir / "video.mp4"
    source.write_bytes(b"video")
    database = app.state.database
    database.create_job("job", "video.mp4", source)
    database.update_job("job", status="ready", stage="Hazır")
    database.replace_clips(
        "job",
        [
            ClipCandidate(
                id="clip",
                job_id="job",
                rank=1,
                title="Klip",
                start=0,
                end=10,
                score=80,
                reasons=[],
                subtitles=[],
            )
        ],
    )

    with TestClient(app) as client:
        default_response = client.post(
            "/api/jobs/job/exports",
            json={"clip_ids": ["clip"]},
        )
        enabled_response = client.post(
            "/api/jobs/job/exports",
            json={
                "clip_ids": ["clip"],
                "llm_seo_enabled": True,
                "playback_rate": 1.1,
                "subtitle_margin_v": 260,
                "subtitle_font_family": "Impact",
                "balanced_vertical_offset": 120,
            },
        )

    assert default_response.status_code == 202
    assert enabled_response.status_code == 202
    default_export = database.get_export_record(default_response.json()["exports"][0]["id"])
    enabled_export = database.get_export_record(enabled_response.json()["exports"][0]["id"])
    assert default_export is not None
    assert enabled_export is not None
    assert default_export["llm_seo_enabled"] == 0
    assert enabled_export["llm_seo_enabled"] == 1
    assert default_export["playback_rate"] == 1.0
    assert enabled_export["playback_rate"] == 1.1
    assert default_export["subtitle_margin_v"] == 420
    assert enabled_export["subtitle_margin_v"] == 260
    assert default_export["subtitle_font_family"] == "Arial"
    assert enabled_export["subtitle_font_family"] == "Impact"
    assert default_export["balanced_vertical_offset"] == 0
    assert enabled_export["balanced_vertical_offset"] == 120


def test_rejects_unsupported_file_type(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        frontend_dist=tmp_path / "missing-frontend",
        minimum_free_bytes=0,
    )
    app = create_app(settings, start_worker=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/jobs",
            files={"video": ("notes.txt", b"hello", "text/plain")},
        )
    assert response.status_code == 415


def test_changing_clip_bounds_rebuilds_subtitles_from_transcript(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        frontend_dist=tmp_path / "missing-frontend",
        minimum_free_bytes=0,
    )
    app = create_app(settings, start_worker=False)
    job_dir = settings.data_dir / "jobs" / "job"
    job_dir.mkdir(parents=True)
    source = job_dir / "video.mp4"
    source.write_bytes(b"video")
    transcript_path = job_dir / "transcript.json"
    transcript_path.write_text(
        json.dumps(
            {
                "language": "tr",
                "segments": [
                    {
                        "start": 1.0,
                        "end": 2.0,
                        "text": "Eski metin",
                        "words": [{"text": "Eski", "start": 1.0, "end": 1.4}],
                    },
                    {
                        "start": 35.0,
                        "end": 36.0,
                        "text": "Yeni konu",
                        "words": [
                            {"text": "Yeni", "start": 35.0, "end": 35.4},
                            {"text": "konu", "start": 35.5, "end": 36.0},
                        ],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    database = app.state.database
    database.create_job("job", "video.mp4", source)
    database.update_job(
        "job",
        status="ready",
        stage="Hazır",
        duration=120.0,
        width=1920,
        height=1080,
        transcript_path=str(transcript_path),
    )
    old_word = SubtitleWord(text="Eski", start=1.0, end=1.4)
    old_cue = SubtitleCue(id="old", start=1.0, end=1.4, text="Eski", words=[old_word])
    database.replace_clips(
        "job",
        [
            ClipCandidate(
                id="clip",
                job_id="job",
                rank=1,
                title="Klip",
                start=0,
                end=40,
                score=80,
                reasons=[],
                subtitles=[old_cue],
            )
        ],
    )

    with TestClient(app) as client:
        response = client.patch(
            "/api/jobs/job/clips/clip",
            json={
                "start": 30,
                "end": 70,
                "subtitles": [old_cue.model_dump(mode="json")],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["start"] == 30
    assert [word["text"] for cue in payload["subtitles"] for word in cue["words"]] == [
        "Yeni",
        "konu",
    ]


def test_manual_clip_bounds_are_not_limited_to_candidate_duration(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        frontend_dist=tmp_path / "missing-frontend",
        minimum_free_bytes=0,
    )
    app = create_app(settings, start_worker=False)
    job_dir = settings.data_dir / "jobs" / "job"
    job_dir.mkdir(parents=True)
    source = job_dir / "video.mp4"
    source.write_bytes(b"video")
    database = app.state.database
    database.create_job("job", "video.mp4", source)
    database.update_job(
        "job",
        status="ready",
        stage="Hazır",
        duration=120.0,
        width=1920,
        height=1080,
    )
    cue = SubtitleCue(id="cue", start=0, end=1, text="Merhaba", words=[])
    database.replace_clips(
        "job",
        [
            ClipCandidate(
                id="clip",
                job_id="job",
                rank=1,
                title="Klip",
                start=0,
                end=40,
                score=80,
                reasons=[],
                subtitles=[cue],
            )
        ],
    )

    with TestClient(app) as client:
        response = client.patch(
            "/api/jobs/job/clips/clip",
            json={"start": 10, "end": 20},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["start"] == 10
    assert payload["end"] == 20


def test_manual_clip_is_appended_with_transcript_subtitles(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        frontend_dist=tmp_path / "missing-frontend",
        minimum_free_bytes=0,
    )
    app = create_app(settings, start_worker=False)
    job_dir = settings.data_dir / "jobs" / "job"
    job_dir.mkdir(parents=True)
    source = job_dir / "video.mp4"
    source.write_bytes(b"video")
    transcript_path = job_dir / "transcript.json"
    transcript_path.write_text(
        json.dumps(
            {
                "language": "tr",
                "segments": [
                    {
                        "start": 65.0,
                        "end": 67.0,
                        "text": "Manuel bölüm altyazısı",
                        "words": [
                            {"text": "Manuel", "start": 65.0, "end": 65.5},
                            {"text": "bölüm", "start": 65.6, "end": 66.1},
                            {"text": "altyazısı", "start": 66.2, "end": 67.0},
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    database = app.state.database
    database.create_job("job", "video.mp4", source)
    database.update_job(
        "job",
        status="ready",
        stage="Hazır",
        duration=180.0,
        width=1920,
        height=1080,
        transcript_path=str(transcript_path),
    )
    database.replace_clips(
        "job",
        [
            ClipCandidate(
                id="suggested",
                job_id="job",
                rank=1,
                title="Önerilen klip",
                start=0,
                end=40,
                score=80,
                reasons=["Konu bütünlüğü"],
                subtitles=[],
            )
        ],
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/jobs/job/clips",
            json={"start": 60, "end": 75},
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["rank"] == 2
    assert payload["start"] == 60
    assert payload["end"] == 75
    assert payload["selected"] is True
    assert payload["reasons"] == ["Manuel aralık"]
    assert payload["title"] == "Manuel bölüm altyazısı"
    assert [
        word["text"]
        for cue in payload["subtitles"]
        for word in cue["words"]
    ] == ["Manuel", "bölüm", "altyazısı"]
    assert [clip.rank for clip in database.get_job("job").clips] == [1, 2]


def test_manual_clip_rejects_range_outside_video(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        frontend_dist=tmp_path / "missing-frontend",
        minimum_free_bytes=0,
    )
    app = create_app(settings, start_worker=False)
    job_dir = settings.data_dir / "jobs" / "job"
    job_dir.mkdir(parents=True)
    source = job_dir / "video.mp4"
    source.write_bytes(b"video")
    database = app.state.database
    database.create_job("job", "video.mp4", source)
    database.update_job("job", status="ready", stage="Hazır", duration=120.0)

    with TestClient(app) as client:
        response = client.post(
            "/api/jobs/job/clips",
            json={"start": 110, "end": 125},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "Klip zaman aralığı video süresinin dışında."


def test_balanced_framing_persists_and_disables_face_tracking(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        frontend_dist=tmp_path / "missing-frontend",
        minimum_free_bytes=0,
    )
    app = create_app(settings, start_worker=False)
    job_dir = settings.data_dir / "jobs" / "job"
    job_dir.mkdir(parents=True)
    source = job_dir / "video.mp4"
    source.write_bytes(b"video")
    database = app.state.database
    database.create_job("job", "video.mp4", source)
    database.update_job(
        "job",
        status="ready",
        stage="Hazir",
        duration=120.0,
        width=1920,
        height=1080,
    )
    database.replace_clips(
        "job",
        [
            ClipCandidate(
                id="clip",
                job_id="job",
                rank=1,
                title="Klip",
                start=0,
                end=40,
                score=80,
                reasons=[],
                subtitles=[],
                framing_mode="fill",
                face_tracking_enabled=True,
            )
        ],
    )

    with TestClient(app) as client:
        response = client.patch(
            "/api/jobs/job/clips/clip",
            json={"framing_mode": "balanced", "face_tracking_enabled": True},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["framing_mode"] == "balanced"
    assert payload["face_tracking_enabled"] is False


def test_clip_end_near_source_end_is_clamped(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        frontend_dist=tmp_path / "missing-frontend",
        minimum_free_bytes=0,
    )
    app = create_app(settings, start_worker=False)
    job_dir = settings.data_dir / "jobs" / "job"
    job_dir.mkdir(parents=True)
    source = job_dir / "video.mp4"
    source.write_bytes(b"video")
    database = app.state.database
    database.create_job("job", "video.mp4", source)
    database.update_job(
        "job",
        status="ready",
        stage="Hazır",
        duration=1499.52,
        width=1920,
        height=1080,
    )
    cue = SubtitleCue(id="cue", start=1447, end=1448, text="Merhaba", words=[])
    database.replace_clips(
        "job",
        [
            ClipCandidate(
                id="clip",
                job_id="job",
                rank=1,
                title="Klip",
                start=1447,
                end=1483.07,
                score=80,
                reasons=[],
                subtitles=[cue],
            )
        ],
    )

    with TestClient(app) as client:
        response = client.patch(
            "/api/jobs/job/clips/clip",
            json={"start": 1447, "end": 1500},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["start"] == 1447
    assert payload["end"] == 1499.52


def test_subtitles_can_be_reset_from_transcript_without_changing_bounds(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        frontend_dist=tmp_path / "missing-frontend",
        minimum_free_bytes=0,
    )
    app = create_app(settings, start_worker=False)
    job_dir = settings.data_dir / "jobs" / "job"
    job_dir.mkdir(parents=True)
    source = job_dir / "video.mp4"
    source.write_bytes(b"video")
    transcript_path = job_dir / "transcript.json"
    transcript_path.write_text(
        json.dumps(
            {
                "language": "tr",
                "segments": [
                    {
                        "start": 10.0,
                        "end": 12.0,
                        "text": "Doğru zaman",
                        "words": [
                            {"text": "Doğru", "start": 10.0, "end": 10.6},
                            {"text": "zaman", "start": 10.7, "end": 11.4},
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    database = app.state.database
    database.create_job("job", "video.mp4", source)
    database.update_job(
        "job",
        status="ready",
        stage="Hazır",
        duration=120.0,
        width=1920,
        height=1080,
        transcript_path=str(transcript_path),
    )
    bad_word = SubtitleWord(text="Yanlış", start=13.0, end=13.5)
    bad_cue = SubtitleCue(id="bad", start=13.0, end=13.5, text="Yanlış", words=[bad_word])
    database.replace_clips(
        "job",
        [
            ClipCandidate(
                id="clip",
                job_id="job",
                rank=1,
                title="Klip",
                start=10,
                end=20,
                score=80,
                reasons=[],
                subtitles=[bad_cue],
            )
        ],
    )

    with TestClient(app) as client:
        response = client.patch(
            "/api/jobs/job/clips/clip",
            json={"start": 10, "end": 20, "reset_subtitles": True},
        )

    assert response.status_code == 200
    words = [word for cue in response.json()["subtitles"] for word in cue["words"]]
    assert [word["text"] for word in words] == ["Doğru", "zaman"]
    assert words[0]["start"] == 10.0
    assert words[-1]["end"] == 11.4


def test_subtitle_reset_preserves_submitted_text_and_grouping(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        frontend_dist=tmp_path / "missing-frontend",
        minimum_free_bytes=0,
    )
    app = create_app(settings, start_worker=False)
    job_dir = settings.data_dir / "jobs" / "job"
    job_dir.mkdir(parents=True)
    source = job_dir / "video.mp4"
    source.write_bytes(b"video")
    transcript_path = job_dir / "transcript.json"
    transcript_path.write_text(
        json.dumps(
            {
                "language": "tr",
                "segments": [
                    {
                        "start": 10.0,
                        "end": 13.0,
                        "text": "merhaba dunya nasilsin",
                        "words": [
                            {"text": "merhaba", "start": 10.0, "end": 10.5},
                            {"text": "dunya", "start": 10.6, "end": 11.0},
                            {"text": "nasilsin", "start": 11.2, "end": 12.0},
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    database = app.state.database
    database.create_job("job", "video.mp4", source)
    database.update_job(
        "job",
        status="ready",
        stage="Hazır",
        duration=120.0,
        width=1920,
        height=1080,
        transcript_path=str(transcript_path),
    )
    original = SubtitleCue(
        id="original",
        start=10.0,
        end=10.5,
        text="merhaba",
        words=[SubtitleWord(text="merhaba", start=10.0, end=10.5)],
    )
    edited = SubtitleCue(
        id="edited",
        start=20.0,
        end=24.0,
        text="Merhaba dünya nasılsın?",
        words=[
            SubtitleWord(text="Merhaba", start=20.0, end=21.0),
            SubtitleWord(text="dünya", start=21.0, end=22.0),
            SubtitleWord(text="nasılsın?", start=22.0, end=24.0),
        ],
    )
    database.replace_clips(
        "job",
        [
            ClipCandidate(
                id="clip",
                job_id="job",
                rank=1,
                title="Klip",
                start=10,
                end=20,
                score=80,
                reasons=[],
                subtitles=[original],
            )
        ],
    )

    with TestClient(app) as client:
        response = client.patch(
            "/api/jobs/job/clips/clip",
            json={
                "start": 10,
                "end": 20,
                "reset_subtitles": True,
                "subtitles": [edited.model_dump(mode="json")],
            },
        )

    assert response.status_code == 200
    subtitles = response.json()["subtitles"]
    assert len(subtitles) == 1
    assert subtitles[0]["id"] == "edited"
    assert subtitles[0]["text"] == "Merhaba dünya nasılsın?"
    assert [word["text"] for word in subtitles[0]["words"]] == [
        "Merhaba",
        "dünya",
        "nasılsın?",
    ]
    assert subtitles[0]["words"][0]["start"] == 10.0
    assert subtitles[0]["words"][-1]["end"] == 12.0


def test_cut_ranges_rebuild_and_shift_subtitles(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        frontend_dist=tmp_path / "missing-frontend",
        minimum_free_bytes=0,
    )
    app = create_app(settings, start_worker=False)
    job_dir = settings.data_dir / "jobs" / "job"
    job_dir.mkdir(parents=True)
    source = job_dir / "video.mp4"
    source.write_bytes(b"video")
    transcript_path = job_dir / "transcript.json"
    transcript_path.write_text(
        json.dumps(
            {
                "language": "tr",
                "segments": [
                    {
                        "start": 10.0,
                        "end": 14.0,
                        "text": "bir iki üç dört",
                        "words": [
                            {"text": "bir", "start": 10.0, "end": 10.4},
                            {"text": "iki", "start": 11.0, "end": 11.4},
                            {"text": "üç", "start": 12.0, "end": 12.4},
                            {"text": "dört", "start": 13.0, "end": 13.4},
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    database = app.state.database
    database.create_job("job", "video.mp4", source)
    database.update_job(
        "job",
        status="ready",
        stage="Hazır",
        duration=120.0,
        width=1920,
        height=1080,
        transcript_path=str(transcript_path),
    )
    cue = SubtitleCue(id="cue", start=10.0, end=13.4, text="bir iki üç dört", words=[])
    database.replace_clips(
        "job",
        [
            ClipCandidate(
                id="clip",
                job_id="job",
                rank=1,
                title="Klip",
                start=10,
                end=20,
                score=80,
                reasons=[],
                subtitles=[cue],
            )
        ],
    )

    with TestClient(app) as client:
        response = client.patch(
            "/api/jobs/job/clips/clip",
            json={
                "start": 10,
                "end": 20,
                "cut_ranges": [{"start": 11.0, "end": 12.0}],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["cut_ranges"] == [{"start": 11.0, "end": 12.0}]
    words = [word for cue in payload["subtitles"] for word in cue["words"]]
    assert [word["text"] for word in words] == ["bir", "üç", "dört"]
    assert words[1]["start"] == 11.0
    assert words[2]["start"] == 12.0


def test_auto_cut_silence_uses_transcript_speech_gaps(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        frontend_dist=tmp_path / "missing-frontend",
        minimum_free_bytes=0,
    )
    app = create_app(settings, start_worker=False)
    job_dir = settings.data_dir / "jobs" / "job"
    job_dir.mkdir(parents=True)
    source = job_dir / "video.mp4"
    source.write_bytes(b"video")
    transcript_path = job_dir / "transcript.json"
    transcript_path.write_text(
        json.dumps(
            {
                "language": "tr",
                "segments": [
                    {
                        "start": 10.0,
                        "end": 15.0,
                        "text": "bir iki",
                        "words": [
                            {"text": "bir", "start": 10.0, "end": 10.5},
                            {"text": "iki", "start": 14.0, "end": 14.5},
                        ],
                    },
                    {
                        "start": 11.0,
                        "end": 13.0,
                        "text": "[noise]",
                        "words": [],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    database = app.state.database
    database.create_job("job", "video.mp4", source)
    database.update_job(
        "job",
        status="ready",
        stage="Hazir",
        duration=120.0,
        width=1920,
        height=1080,
        transcript_path=str(transcript_path),
    )
    cue = SubtitleCue(id="cue", start=10.0, end=14.5, text="bir iki", words=[])
    database.replace_clips(
        "job",
        [
            ClipCandidate(
                id="clip",
                job_id="job",
                rank=1,
                title="Klip",
                start=10,
                end=16,
                score=80,
                reasons=[],
                subtitles=[cue],
            )
        ],
    )

    with TestClient(app) as client:
        response = client.patch(
            "/api/jobs/job/clips/clip",
            json={"start": 10, "end": 16, "auto_cut_silence": True},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["cut_ranges"] == [
        {"start": 10.7, "end": 13.8},
        {"start": 14.7, "end": 15.8},
    ]
    words = [word for cue in payload["subtitles"] for word in cue["words"]]
    assert [word["text"] for word in words] == ["bir", "iki"]
    assert round(words[1]["start"], 3) == 10.9


def test_reanalysis_can_be_queued_for_completed_job(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        frontend_dist=tmp_path / "missing-frontend",
        minimum_free_bytes=0,
    )
    app = create_app(settings, start_worker=False)
    job_dir = settings.data_dir / "jobs" / "job"
    job_dir.mkdir(parents=True)
    source = job_dir / "video.mp4"
    source.write_bytes(b"video")
    app.state.database.create_job("job", "video.mp4", source)
    app.state.database.update_job("job", status="completed", stage="Hazır")

    with TestClient(app) as client:
        response = client.post("/api/jobs/job/reanalyze")

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
