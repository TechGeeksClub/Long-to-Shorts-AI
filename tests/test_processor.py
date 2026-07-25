import json
from pathlib import Path

from app.config import Settings
from app.database import Database
from app.models import ClipCandidate, SubtitleCue, SubtitleWord
from app.processor import Processor


def test_export_syncs_edited_subtitles_from_transcript(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data_dir = tmp_path / "data"
    database = Database(data_dir / "app.db")
    database.initialize()
    settings = Settings(data_dir=data_dir, ollama_seo_enabled=False)
    processor = Processor(database, settings)

    job_dir = data_dir / "jobs" / "job"
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
                subtitles=[edited],
            )
        ],
    )
    database.create_export(
        "export",
        "job",
        "clip",
        playback_rate=1.1,
        subtitle_margin_v=260,
        subtitle_font_family="Impact",
        balanced_vertical_offset=120,
    )

    def fake_render_video(**kwargs):
        assert kwargs["playback_rate"] == 1.1
        assert kwargs["balanced_vertical_offset"] == 120
        subtitles = kwargs["work_dir"] / "subtitles.ass"
        assert subtitles.exists()
        subtitle_content = subtitles.read_text(encoding="utf-8-sig")
        assert "Style: Shorts,Impact,76" in subtitle_content
        assert ",70,70,260,1" in subtitle_content
        kwargs["output"].write_bytes(b"mp4")
        progress_callback = kwargs.get("progress_callback")
        if progress_callback:
            progress_callback(1.0)

    monkeypatch.setattr("app.processor.render_video", fake_render_video)

    processor.export("export")

    updated = database.get_clip("clip")
    export = database.get_export_record("export")
    assert updated is not None
    assert export is not None
    assert export["status"] == "completed"
    output = Path(export["path"])
    metadata = output.with_name("youtube-seo.md")
    assert output.parent.name == output.stem
    assert metadata.exists()
    metadata_content = metadata.read_text(encoding="utf-8")
    assert "## LLM SEO Önerileri" in metadata_content
    assert "## Yerel Yedek Başlık Seçenekleri" in metadata_content
    assert "## Yerel Yedek Açıklama Seçenekleri" in metadata_content
    assert "## Yerel Yedek Etiketler" in metadata_content
    words = updated.subtitles[0].words
    assert [word.text for word in words] == ["Merhaba", "dünya", "nasılsın?"]
    assert words[0].start == 10.0
    assert words[-1].end == 12.0
