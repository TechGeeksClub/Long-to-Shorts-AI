import json
import shutil
import zipfile
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import transcript_correction, youtube
from app.codex_runner import CodexError
from app.config import Settings
from app.database import Database
from app.main import create_app
from app.media import probe_media, render_video, run_command
from app.models import (AnalysisReport, ClipCandidate, EditSettings, SubtitleCue, SubtitleWord,
                        TopicSection, YoutubeBrief, YoutubePackage)
from app.processor import Processor
from app.subtitles import write_ass
from app.transcript_correction import correct_saved_cues, correct_transcript


def transcript():
    text = "Aslında ne kıstayım ne uzunum bence."
    words = [{"text": word, "start": index * .5, "end": index * .5 + .4}
             for index, word in enumerate(text.split())]
    return [{"start": 0, "end": 3, "text": text, "words": words}]


def correction():
    return {"corrections": [{"word_id": 2, "original": "kıstayım", "replacement": "kısayım", "reason": "Boy bağlamında duyum hatası."}]}


def test_correction_preserves_original_timestamps_and_edited_captions(tmp_path, monkeypatch):
    original = transcript()
    snapshot = deepcopy(original)
    monkeypatch.setattr(transcript_correction, "run_codex_json", lambda **_: (correction(), None))
    result, report = correct_transcript(original, Settings(), tmp_path)
    assert original == snapshot
    assert result[0]["text"] == "Aslında ne kısayım ne uzunum bence."
    assert [(w["start"], w["end"]) for w in result[0]["words"]] == [(w["start"], w["end"]) for w in original[0]["words"]]
    cue = SubtitleCue(id="cue", start=0, end=3, text=original[0]["text"], words=[SubtitleWord(**w) for w in original[0]["words"]])
    updated = correct_saved_cues([cue], report.corrections)[0]
    assert "kısayım" in updated.text
    assert updated.words[2].start == 1
    user_edit = cue.model_copy(update={"text": "Benim kendi düzelttiğim metin."})
    assert correct_saved_cues([user_edit], report.corrections)[0] == user_edit


@pytest.mark.parametrize("patch", [{"word_id": 999}, {"original": "yanlış"}, {"replacement": "başka iki"}, {"replacement": "{\\evil}"}])
def test_invalid_agent_corrections_never_mutate_original(tmp_path, monkeypatch, patch):
    data = correction()
    data["corrections"][0].update(patch)
    source = transcript()
    monkeypatch.setattr(transcript_correction, "run_codex_json", lambda **_: (data, None))
    with pytest.raises(CodexError):
        correct_transcript(source, Settings(), tmp_path)
    assert source == transcript()


def seed(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, frontend_dist=tmp_path / "missing"), start_worker=False)
    db = app.state.database
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake")
    db.create_job("job", "source.mp4", source)
    path = tmp_path / "transcript.json"
    path.write_text(json.dumps({"language": "tr", "segments": transcript()}, ensure_ascii=False), encoding="utf-8")
    db.update_job("job", status="ready", stage="Hazır", duration=60, transcript_path=str(path))
    cue = SubtitleCue(id="cue", start=0, end=3, text=transcript()[0]["text"], words=[SubtitleWord(**w) for w in transcript()[0]["words"]])
    clip = ClipCandidate(id="clip", job_id="job", rank=1, start=0, end=10, title="Boy ve konfor", score=0, reasons=[], subtitles=[cue])
    db.create_clip(clip)
    return app, db, path


def test_correction_backups_and_updates_saved_clip_once(tmp_path, monkeypatch):
    app, db, path = seed(tmp_path)
    calls = []
    monkeypatch.setattr(transcript_correction, "run_codex_json", lambda **kwargs: (calls.append(kwargs) or correction(), None))
    processor = Processor(db, app.state.settings)
    processor.correct_subtitles("job")
    assert "kısayım" in db.get_clip("clip").subtitles[0].text
    assert "kıstayım" in path.with_name("transcript.original.json").read_text(encoding="utf-8")
    assert db.get_job("job").correction.corrections[0].replacement == "kısayım"
    processor.correct_subtitles("job")
    assert len(calls) == 1
    with TestClient(app) as client:
        response = client.post("/api/jobs/job/transcript/corrections/2/revert")
        assert response.status_code == 200
        assert response.json()["correction"]["corrections"] == []
        assert "kıstayım" in db.get_clip("clip").subtitles[0].text
        assert client.post("/api/jobs/job/transcript/corrections/2/revert").status_code == 404


def test_every_topic_can_create_a_manual_clip_and_youtube_is_snapshotted(tmp_path, monkeypatch):
    app, db, _ = seed(tmp_path)
    report = AnalysisReport(summary="Test", topics=[TopicSection(id="topic", title="Önerilmeyen konu", summary="Tam konu", start=0, end=3)], clip_ids=[], created_at=datetime.now(UTC))
    db.update_job("job", analysis_json=report.model_dump_json())
    def thumbnail(source, destination, *args):
        destination.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (100, 100), "blue").save(destination)
    monkeypatch.setattr("app.api.render_thumbnail", thumbnail)
    with TestClient(app) as client:
        clip = client.post("/api/jobs/job/clips", json={"start": 0, "end": 3, "topic_id": "topic"})
        assert clip.status_code == 201
        assert clip.json()["title"] == "Önerilmeyen konu"
        assert clip.json()["topic_id"] == "topic"
        body = {"brief": YoutubeBrief(frame_time=1, thumbnail_style="neon").model_dump(),
                "title": "Seçtiğim başlık", "description": "Seçtiğim açıklama", "thumbnail_text": "BOY MESELESİ"}
        saved = client.patch("/api/jobs/job/clips/clip/youtube", json=body)
        assert saved.status_code == 200
        assert saved.json()["youtube"]["revision"]
        assert client.get("/api/jobs/job/clips/clip/youtube/thumbnail").status_code == 200
        assert client.get("/api/jobs/other/clips/clip/youtube/thumbnail").status_code == 404
        export = client.post("/api/jobs/job/exports", json={"clip_ids": ["clip"]})
        record = db.get_export_record(export.json()["exports"][0]["id"])
        assert json.loads(record["youtube_json"])["title"] == body["title"]
        assert client.post("/api/jobs/job/clips/clip/youtube/generate").status_code == 202
        assert client.post("/api/jobs/job/clips/clip/youtube/generate").status_code == 409


def test_youtube_options_grounded_in_transcript_and_brief(tmp_path, monkeypatch):
    app, db, _ = seed(tmp_path)
    payload = {"titles": [f"Başlık {i}" for i in range(4)], "descriptions": [f"Açıklama {i}" for i in range(3)],
               "thumbnail_texts": [f"Kapak {i}" for i in range(4)]}
    prompts = []
    monkeypatch.setattr(youtube, "run_codex_json", lambda **kwargs: (prompts.append(kwargs["prompt"]) or payload, None))
    options = youtube.generate_youtube_options(db.get_clip("clip"), app.state.settings, tmp_path)
    assert options.titles == payload["titles"]
    assert "kıstayım" in prompts[0] and "Motosiklet" in prompts[0]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
@pytest.mark.parametrize("effect", ["zoom_blur", "slide", "rgb_split", "diagonal"])
def test_modern_effects_render_real_video(tmp_path, effect):
    source = tmp_path / "source.mp4"
    run_command(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=1",
                 "-f", "lavfi", "-i", "sine=duration=1", "-c:v", "libx264", "-c:a", "aac", str(source)])
    write_ass([], 0, tmp_path / "subtitles.ass")
    output = tmp_path / "output.mp4"
    render_video(source=source, output=output, work_dir=tmp_path, start=0, end=1, crop_width=None, crop_height=None,
                 framing_mode="fit", face_tracking_enabled=False, ffmpeg="ffmpeg", output_width=540,
                 edit_settings=EditSettings(intro_effect=effect, outro_effect=effect, intro_duration=.3, outro_duration=.3))
    info = probe_media(output, "ffprobe")
    assert (info.width, info.height) == (540, 960)
    assert info.has_audio and .9 <= info.duration <= 1.1


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
@pytest.mark.parametrize("style,aspect,size", [("bold", "portrait", (1080,1920)), ("neon", "landscape", (1280,720)), ("editorial", "portrait", (1080,1920))])
def test_export_package_contains_real_thumbnail_and_selected_copy(tmp_path, style, aspect, size):
    source = tmp_path / "source.mp4"
    run_command(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=1", "-c:v", "libx264", str(source)])
    output = tmp_path / "exports" / "short.mp4"
    output.parent.mkdir()
    shutil.copy2(source, output)
    clip = ClipCandidate(id="clip", job_id="job", rank=1, start=0, end=1, title="Test", score=0, reasons=[], subtitles=[])
    package = YoutubePackage(brief=YoutubeBrief(frame_time=.2, aspect=aspect, thumbnail_style=style), title="Seçilen başlık",
                             description="Seçilen açıklama", thumbnail_text="KISA BOYLA FORZA?")
    youtube.write_youtube_package(source, output, clip, package, Settings())
    with Image.open(output.parent / "thumbnail.jpg") as image:
        assert image.size == size
        assert image.getbbox()
    assert (output.parent / "title.txt").read_text(encoding="utf-8") == "Seçilen başlık"
    with zipfile.ZipFile(output.parent / "youtube-package.zip") as archive:
        assert {"short.mp4", "thumbnail.jpg", "title.txt", "description.txt", "youtube.json"} <= set(archive.namelist())
