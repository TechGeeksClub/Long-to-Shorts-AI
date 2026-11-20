import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import Database
from app.editing import hook_suggestions, punch_in_times
from app.main import create_app
from app.media import probe_media, render_video, run_command
from app.models import ClipCandidate, EditSettings, SubtitleCue, SubtitleWord
from app.processor import Processor
from app.subtitles import normalize_cues, write_ass


def test_hook_suggestions_are_short_original_excerpts() -> None:
    texts = ["Bu yolda biraz ilerledim.", "Peki bu ses nereden geliyor?", "Ama lastikler beni şaşırttı."]
    cues = [SubtitleCue(id=str(i), start=i * 3, end=i * 3 + 2, text=t) for i, t in enumerate(texts)]
    suggestions = hook_suggestions(cues)
    assert suggestions[0]["text"] == texts[1]
    assert {item["text"] for item in suggestions} == set(texts)
    assert hook_suggestions([]) == []
    assert hook_suggestions([cues[0], cues[0]]) == [
        {"text": texts[0], "reason": "Konuşmadan kısa bir alıntı"}
    ]


def test_accents_follow_composed_subtitle_time_and_are_spaced() -> None:
    cues = [SubtitleCue(id=str(i), start=100 + t, end=101 + t, text="Bir cümle.")
            for i, t in enumerate([0, 3, 8, 9, 16])]
    assert punch_in_times(cues, 100) == [0.0, 8.0, 16.0]


def test_style_regroups_captions_without_losing_words_and_scales_hook_time(tmp_path: Path) -> None:
    words = [SubtitleWord(text=t, start=10 + i * .4, end=10.3 + i * .4)
             for i, t in enumerate("Bu motosiklet ile ilk kez uzun yola çıktım".split())]
    cue = SubtitleCue(id="cue", start=10, end=13.1, text=" ".join(w.text for w in words), words=words)
    destination = tmp_path / "subtitles.ass"
    write_ass([cue], 10, destination, edit_settings=EditSettings(
        style="dynamic", hook_text="İlk uzun yol deneyimim", hook_duration=2.8,
    ), playback_rate=1.25)
    content = destination.read_text(encoding="utf-8-sig")
    assert "Dialogue: 1,0:00:00.00,0:00:03.50,Hook" in content
    events = [line for line in content.splitlines() if line.startswith("Dialogue: 0")]
    assert len(events) == len(words)
    assert "uzun yola çıktım" not in events[0]
    assert all(any(word.text in event for event in events) for word in words)
    assert "&H0043FFD9&" in content


def test_disabled_style_does_not_render_hook_or_animation(tmp_path: Path) -> None:
    path = tmp_path / "plain.ass"
    write_ass([SubtitleCue(id="cue", start=0, end=1, text="Bir test cümlesi.")], 0, path,
              edit_settings=EditSettings(style="off", hook_text="Gizli açılış"))
    content = path.read_text(encoding="utf-8-sig")
    assert "Gizli açılış" not in content
    assert "\\fscx96" not in content


def test_processor_uses_saved_export_style_after_clip_settings_change(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(data_dir=tmp_path, ollama_seo_enabled=False)
    database = Database(settings.database_path)
    database.initialize()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"test")
    database.create_job("job", "source.mp4", source)
    database.update_job("job", status="ready", stage="Hazır", width=640, height=360, duration=10)
    clip = ClipCandidate(id="clip", job_id="job", rank=1, title="Test", start=0, end=10,
                         score=80, reasons=[], subtitles=[
                             SubtitleCue(id="cue", start=1, end=3, text="Bu yolda biraz ilerledim.")
                         ])
    database.create_clip(clip)
    edit = EditSettings(style="dynamic", hook_text="Bu yolda biraz ilerledim.")
    database.create_export("export", "job", "clip", edit_settings=edit)
    database.update_clip("clip", edit_settings=EditSettings(style="clean"))

    def fake_render(**kwargs):
        assert kwargs["edit_settings"] == edit
        assert kwargs["hook_boundary_seconds"] is None
        assert kwargs["punch_in_times"] == [0.0]
        content = (kwargs["work_dir"] / "subtitles.ass").read_text(encoding="utf-8-sig")
        assert "Style: Shorts,Arial,80" in content
        assert "Dialogue: 1" in content
        kwargs["output"].write_bytes(b"mp4")

    monkeypatch.setattr("app.processor.render_video", fake_render)
    monkeypatch.setattr("app.youtube.render_thumbnail", lambda *args, **kwargs: None)
    Processor(database, settings).export("export")
    assert database.get_export_record("export")["status"] == "completed"


def test_settings_migrate_persist_validate_and_snapshot_for_exports(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, frontend_dist=tmp_path / "missing")
    app = create_app(settings, start_worker=False)
    database = app.state.database
    # Simulate a database from before this feature, including a populated clip.
    database.create_job("job", "video.mp4", tmp_path / "video.mp4")
    database.update_job("job", status="ready", duration=60, stage="Hazır")
    clip = ClipCandidate(id="clip", job_id="job", rank=1, title="Test", start=0, end=30,
                         score=80, reasons=[], subtitles=[])
    database.create_clip(clip)
    with database.connect() as connection:
        connection.execute("ALTER TABLE clips DROP COLUMN edit_settings_json")
        connection.execute("ALTER TABLE exports DROP COLUMN edit_settings_json")
    database.initialize()
    database.initialize()
    assert database.get_clip("clip").edit_settings.style == "off"
    with TestClient(app) as client:
        payload = {"style": "dynamic", "hook_text": "Yolda ne değişti?", "hook_duration": 2.8, "punch_ins": True}
        expected = EditSettings.model_validate(payload).model_dump()
        response = client.patch("/api/jobs/job/clips/clip", json={"edit_settings": payload})
        assert response.status_code == 200
        assert response.json()["edit_settings"] == expected
        exported = client.post("/api/jobs/job/exports", json={"clip_ids": ["clip"]})
        assert exported.status_code == 202
        export = exported.json()["exports"][0]
        assert export["edit_settings"] == expected
        client.patch("/api/jobs/job/clips/clip", json={"edit_settings": {"style": "clean"}})
        assert json.loads(database.get_export_record(export["id"])["edit_settings_json"]) == expected
        assert client.get("/api/jobs/other/clips/clip/hooks").status_code == 404
        assert client.get("/api/jobs/job/clips/clip/hooks").json() == []
        for invalid in ({"style": "invalid"}, {"hook_text": "x" * 91}, {"hook_duration": 0}):
            assert client.patch("/api/jobs/job/clips/clip", json={"edit_settings": invalid}).status_code == 422
    assert Database(settings.database_path).get_clip("clip").edit_settings.style == "clean"


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
@pytest.mark.parametrize("style,framing", [("clean", "fit"), ("dynamic", "balanced"), ("cinematic", "fill")])
def test_edited_video_renders_with_audio_and_correct_speed(tmp_path: Path, style: str, framing: str) -> None:
    source = tmp_path / "source.mp4"
    run_command(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=60",
                 "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "2",
                 "-c:v", "libx264", "-c:a", "aac", str(source)])
    edit = EditSettings(style=style, hook_text="Türkçe açılış: şaşırdım!", hook_duration=1)
    write_ass(normalize_cues([SubtitleCue(id="cue", start=0, end=2, text="Motor sesi korunur.")], 0, 2),
              0, tmp_path / "subtitles.ass", edit_settings=edit, playback_rate=1.25)
    output = tmp_path / "output.mp4"
    render_video(source=source, output=output, work_dir=tmp_path, start=0, end=2,
                 crop_width=202 if framing == "fill" else None,
                 crop_height=360 if framing == "fill" else None,
                 framing_mode=framing, face_tracking_enabled=False, ffmpeg="ffmpeg",
                 playback_rate=1.25, edit_settings=edit, punch_in_times=[0])
    info = probe_media(output, "ffprobe")
    assert (info.width, info.height) == (1080, 1920)
    assert 1.45 <= info.duration <= 1.8
    assert info.has_audio
