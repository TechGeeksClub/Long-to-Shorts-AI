from datetime import UTC, datetime
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app.agent_selection import AgentSelectionResult
from app.codex_runner import CodexError
from app.config import Settings
from app.database import Database
from app.main import create_app
from app.media import MediaInfo
from app.models import (
    AnalysisReport,
    ClipCandidate,
    CutRange,
    EditSettings,
    HookRange,
    InsertRange,
    SubtitleCue,
    SubtitleWord,
    TopicSection,
)
from app.processor import Processor


def _segments() -> list[dict]:
    return [
        {
            "start": 10.0,
            "end": 12.0,
            "text": "Artçı konforunu konuşalım.",
            "words": [
                {"text": "Artçı", "start": 10.0, "end": 10.5},
                {"text": "konforunu", "start": 10.6, "end": 11.2},
                {"text": "konuşalım.", "start": 11.3, "end": 12.0},
            ],
        },
        {
            "start": 40.0,
            "end": 42.0,
            "text": "Uzun yolda rahat.",
            "words": [
                {"text": "Uzun", "start": 40.0, "end": 40.5},
                {"text": "yolda", "start": 40.6, "end": 41.2},
                {"text": "rahat.", "start": 41.3, "end": 42.0},
            ],
        },
        {
            "start": 72.0,
            "end": 74.0,
            "text": "Yakıt tüketimi düşük.",
            "words": [
                {"text": "Yakıt", "start": 72.0, "end": 72.5},
                {"text": "tüketimi", "start": 72.6, "end": 73.2},
                {"text": "düşük.", "start": 73.3, "end": 74.0},
            ],
        },
    ]


def _seed_job(tmp_path: Path, cached: bool = True):
    settings = Settings(data_dir=tmp_path / "data", frontend_dist=tmp_path / "no-frontend", transcript_correction_enabled=False)
    database = Database(settings.database_path)
    database.initialize()
    job_dir = settings.data_dir / "jobs" / "job"
    job_dir.mkdir(parents=True)
    source = job_dir / "source.mp4"
    source.write_bytes(b"source video must stay intact")
    transcript_path = job_dir / "transcript.json"
    database.create_job("job", "source.mp4", source)
    if cached:
        transcript_path.write_text(json.dumps({"language": "tr", "segments": _segments()}, ensure_ascii=False), encoding="utf-8")
        database.update_job(
            "job", status="ready", stage="Hazır", duration=120.0,
            width=1920, height=1080, has_audio=1, language="tr", transcript_path=str(transcript_path),
        )
    return database, settings, source, transcript_path


def _manual_clip() -> ClipCandidate:
    return ClipCandidate(
        id="manual", job_id="job", rank=7, title="Kendi seçtiğim bölüm", start=60, end=100,
        score=0, reasons=["Manuel aralık"], selection_method="manual", selected=False,
        framing_mode="balanced", cut_ranges=[CutRange(start=80, end=82)],
        insert_ranges=[InsertRange(source_start=5, source_end=7, insert_at=90)],
        subtitles=[SubtitleCue(id="edited", start=72, end=74, text="Düzenlediğim konuşma",
            words=[SubtitleWord(text="Düzenlediğim", start=72, end=72.8),
                   SubtitleWord(text="konuşma", start=72.8, end=74)])],
        edit_settings=EditSettings(
            style="dynamic", hook_text="Benim açılışım", hook_range=HookRange(source_start=12, source_end=14),
            intro_effect="fade_black", outro_effect="fade_white", fade_audio=True,
        ),
    )


def _agent_result() -> AgentSelectionResult:
    return AgentSelectionResult(
        clips=[ClipCandidate(
            id="stable-agent-id", job_id="job", rank=1, title="Artçı konforu ve uzun yol", start=10, end=42,
            score=0, reasons=["Artçı konforu sorusu açıklamasıyla birlikte tamamlanıyor."],
            selection_method="agent", topic_id="comfort", subtitles=[
                SubtitleCue(id="agent-cue", start=10, end=12, text="Artçı konforunu konuşalım.")],
        )],
        topics=[
            TopicSection(id="comfort", title="Artçı konforu", start=10, end=42,
                         summary="Artçının uzun yoldaki oturuş ve konfor deneyimi."),
            TopicSection(id="fuel", title="Yakıt tüketimi", start=72, end=74,
                         summary="Yakıt tüketimi üzerine kısa bir gözlem."),
        ],
        summary="Konuşmada artçı konforu ve yakıt tüketimi ele alınıyor.", model="mock-codex-model",
    )


def _report(result: AgentSelectionResult, summary: str | None = None) -> AnalysisReport:
    return AnalysisReport(
        model=result.model, summary=summary or result.summary, topics=result.topics,
        clip_ids=[clip.id for clip in result.clips], created_at=datetime(2026, 10, 31, tzinfo=UTC),
    )


def _seed_export(database: Database, source: Path, clip: ClipCandidate) -> tuple[Path, Path]:
    output_dir = source.parent / "exports" / "short-7-kept-export"
    output_dir.mkdir(parents=True)
    output = output_dir / "short-7-kept-export.mp4"
    output.write_bytes(b"already rendered video")
    metadata = output_dir / "youtube-seo.md"
    metadata.write_text("Kaydedilmiş SEO metni", encoding="utf-8")
    database.create_export("kept-export", "job", clip.id, edit_settings=clip.edit_settings, playback_rate=1.1)
    database.update_export("kept-export", status="completed", progress=1, path=str(output))
    return output, metadata


def _forbid_media_reprocessing(monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("A cached transcript with duration metadata must not reprocess media or transcribe again.")
    for name in ("probe_media", "extract_audio", "transcribe"):
        monkeypatch.setattr(f"app.processor.{name}", forbidden)


def test_cached_reanalysis_preserves_manual_edits_and_export_files(tmp_path: Path, monkeypatch) -> None:
    database, settings, source, transcript = _seed_job(tmp_path)
    manual = _manual_clip()
    database.create_clip(manual)
    output, metadata = _seed_export(database, source, manual)
    manual_before = database.get_clip(manual.id).model_dump()
    export_before = dict(database.get_export_record("kept-export"))
    _forbid_media_reprocessing(monkeypatch)
    calls = []

    def choose(**kwargs):
        calls.append(kwargs)
        assert kwargs["duration"] == 120
        assert all("id" in word and "sentence_id" in word
                   for segment in kwargs["segments"] for word in segment["words"])
        kwargs["progress_callback"]("Codex konuları değerlendiriyor", .5)
        return _agent_result()

    monkeypatch.setattr("app.processor.select_agent_clips", choose)
    Processor(database, settings).analyze("job")

    job = database.get_job("job")
    assert len(calls) == 1
    assert job.status == "ready" and job.error is None
    assert job.analysis.provider == "codex"
    assert job.analysis.clip_ids == ["stable-agent-id"]
    assert [topic.id for topic in job.analysis.topics] == ["comfort", "fuel"]
    assert database.get_clip(manual.id).model_dump() == manual_before
    assert dict(database.get_export_record("kept-export")) == export_before
    assert database.get_clip("stable-agent-id").rank == 8
    assert output.read_bytes() == b"already rendered video"
    assert metadata.read_text(encoding="utf-8") == "Kaydedilmiş SEO metni"
    assert source.read_bytes() == b"source video must stay intact"
    assert transcript.exists() and not (source.parent / "audio.wav").exists()


def test_repeated_agent_ids_keep_user_edits_and_do_not_duplicate(tmp_path: Path, monkeypatch) -> None:
    database, settings, source, _ = _seed_job(tmp_path)
    result = _agent_result()
    monkeypatch.setattr("app.processor.select_agent_clips", lambda **_: result)
    _forbid_media_reprocessing(monkeypatch)
    processor = Processor(database, settings)
    processor.analyze("job")
    database.update_clip(
        "stable-agent-id", title="Benim düzelttiğim başlık", start=9.5, end=44,
        selected=False, framing_mode="balanced", cut_ranges=[CutRange(start=20, end=21)],
        subtitles=[SubtitleCue(id="user-cue", start=10, end=11, text="Benim düzelttiğim sözler")],
        edit_settings=EditSettings(style="cinematic", hook_text="Benim hook yazım"),
    )
    edited = database.get_clip("stable-agent-id")
    before = edited.model_dump()
    output, _ = _seed_export(database, source, edited)
    result.clips[0].title = "Modelin yeni fakat tercih edilmeyen başlığı"
    processor.analyze("job")
    processor.analyze("job")

    job = database.get_job("job")
    assert job.status == "ready"
    assert [clip.id for clip in job.clips] == ["stable-agent-id"]
    assert database.get_clip("stable-agent-id").model_dump() == before
    assert job.analysis.clip_ids == ["stable-agent-id"]
    assert database.get_export_record("kept-export")["status"] == "completed"
    assert output.exists()


def test_codex_failure_keeps_previous_analysis_clips_exports_and_transcript(tmp_path: Path, monkeypatch) -> None:
    database, settings, source, transcript = _seed_job(tmp_path)
    manual = _manual_clip()
    database.create_clip(manual)
    output, metadata = _seed_export(database, source, manual)
    result = _agent_result()
    database.merge_agent_analysis("job", result.clips, _report(result))
    before = database.get_job("job")
    before_exports = dict(database.get_export_record("kept-export"))
    _forbid_media_reprocessing(monkeypatch)

    def failed(**kwargs):
        raise CodexError("Örnek Codex bağlantı hatası")

    monkeypatch.setattr("app.processor.select_agent_clips", failed)
    Processor(database, settings).analyze("job")
    after = database.get_job("job")
    assert after.status == "failed"
    assert "Örnek Codex bağlantı hatası" in after.error
    assert after.analysis == before.analysis
    assert after.clips == before.clips
    assert after.exports == before.exports
    assert dict(database.get_export_record("kept-export")) == before_exports
    assert output.exists() and metadata.exists()
    saved_transcript = json.loads(transcript.read_text(encoding="utf-8"))
    assert saved_transcript["segments"][0]["text"] == _segments()[0]["text"]
    assert Path(database.get_job_record("job")["transcript_path"]) == transcript


def test_new_upload_saves_transcript_before_codex_failure_and_reuses_it_on_retry(tmp_path: Path, monkeypatch) -> None:
    database, settings, source, transcript = _seed_job(tmp_path, cached=False)
    counts = {"probe": 0, "extract": 0, "transcribe": 0, "agent": 0}
    monkeypatch.setattr(Settings, "validate_binaries", lambda _: [])

    def probe(*args):
        counts["probe"] += 1
        return MediaInfo(duration=120, width=1920, height=1080, has_audio=True)

    def extract(input_path, output_path, ffmpeg):
        counts["extract"] += 1
        assert input_path == source
        output_path.write_bytes(b"mock audio")

    def transcribe(audio_path, model, progress):
        counts["transcribe"] += 1
        assert audio_path.exists()
        progress(.5)
        return _segments(), "tr"

    def choose(**kwargs):
        counts["agent"] += 1
        assert transcript.exists()
        if counts["agent"] == 1:
            raise CodexError("Codex geçici olarak kullanılamıyor")
        return _agent_result()

    monkeypatch.setattr("app.processor.probe_media", probe)
    monkeypatch.setattr("app.processor.extract_audio", extract)
    monkeypatch.setattr("app.processor.transcribe", transcribe)
    monkeypatch.setattr("app.processor.select_agent_clips", choose)
    processor = Processor(database, settings)
    processor.analyze("job")
    failed_job = database.get_job("job")
    assert failed_job.status == "failed" and failed_job.clips == []
    assert failed_job.analysis is None and failed_job.language == "tr"
    assert failed_job.duration == 120 and failed_job.width == 1920
    assert transcript.exists()
    assert json.loads(transcript.read_text(encoding="utf-8"))["language"] == "tr"
    assert not (source.parent / "audio.wav").exists()
    processor.analyze("job")
    assert database.get_job("job").status == "ready"
    assert database.get_job("job").analysis.clip_ids == ["stable-agent-id"]
    assert counts == {"probe": 1, "extract": 1, "transcribe": 1, "agent": 2}


def test_invalid_analysis_merge_rolls_back_without_changing_saved_work(tmp_path: Path) -> None:
    database, _, source, _ = _seed_job(tmp_path)
    manual = _manual_clip()
    database.create_clip(manual)
    output, metadata = _seed_export(database, source, manual)
    before = database.get_job("job")
    result = _agent_result()
    invalid_report = _report(result).model_copy(update={"clip_ids": ["not-a-saved-clip"]})
    with pytest.raises(ValueError):
        database.merge_agent_analysis("job", result.clips, invalid_report)
    after = database.get_job("job")
    assert after == before
    assert database.get_clip("stable-agent-id") is None
    assert output.exists() and metadata.exists()


def test_legacy_database_migration_adds_null_analysis_and_topic_without_data_loss(tmp_path: Path) -> None:
    database, _, source, _ = _seed_job(tmp_path)
    manual = _manual_clip()
    database.create_clip(manual)
    output, metadata = _seed_export(database, source, manual)
    manual_before = database.get_clip(manual.id)
    export_before = dict(database.get_export_record("kept-export"))
    with database.connect() as connection:
        connection.execute("ALTER TABLE jobs DROP COLUMN analysis_json")
        connection.execute("ALTER TABLE clips DROP COLUMN topic_id")
    database.initialize()
    database.initialize()
    job = database.get_job("job")
    assert job.analysis is None
    assert job.clips[0].topic_id is None
    assert job.clips[0] == manual_before
    assert dict(database.get_export_record("kept-export")) == export_before
    assert output.exists() and metadata.exists()


@pytest.mark.parametrize("available,authenticated", [(True, True), (True, False), (False, False)])
def test_analysis_status_endpoint_reports_mocked_codex_state_without_inference(tmp_path: Path, monkeypatch, available: bool, authenticated: bool) -> None:
    settings = Settings(data_dir=tmp_path / "data", frontend_dist=tmp_path / "missing")
    expected = {"provider": "codex", "available": available, "authenticated": authenticated,
                "model": None, "message": "Codex hazır" if authenticated else "Codex bağlantısını kontrol et"}
    calls = []

    def status(app_settings):
        calls.append(app_settings)
        return expected

    monkeypatch.setattr("app.api.codex_status", status)
    app = create_app(settings, start_worker=False)
    with TestClient(app) as client:
        response = client.get("/api/analysis/status")
    assert response.status_code == 200
    assert response.json() == expected
    assert calls == [settings]


def test_reanalysis_endpoint_queues_without_removing_existing_work(tmp_path: Path, monkeypatch) -> None:
    database, settings, source, _ = _seed_job(tmp_path)
    manual = _manual_clip()
    database.create_clip(manual)
    output, metadata = _seed_export(database, source, manual)
    result = _agent_result()
    database.merge_agent_analysis("job", result.clips, _report(result))
    before = database.get_job("job")
    app = create_app(settings, start_worker=False)
    queued = []
    monkeypatch.setattr(app.state.work_queue, "enqueue_analysis", queued.append)
    monkeypatch.setattr("app.api.codex_status", lambda _: {
        "provider": "codex", "available": True, "authenticated": True, "model": None, "message": "Hazır"})
    with TestClient(app) as client:
        response = client.post("/api/jobs/job/reanalyze")
    assert response.status_code == 202
    after = database.get_job("job")
    assert after.status == "queued" and queued == ["job"]
    assert after.clips == before.clips
    assert after.exports == before.exports
    assert after.analysis == before.analysis
    assert output.exists() and metadata.exists()
