import io
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from fastapi.testclient import TestClient

from app import subscriptions
from app.codex_runner import CodexError
from app.config import Settings
from app.editing import subscription_window
from app.main import create_app
from app.media import probe_media, render_video, run_command
from app.models import (
    ClipCandidate, CutRange, EditSettings, HookRange, InsertRange,
    SubscriptionSettings, SubtitleCue,
)
from app.processor import Processor
from app.subscriptions import generate_subscription_suggestions
from app.subscription_card import (
    AVATAR_PATH, AVATAR_CENTER, BUTTON_RECT, CANVAS_PADDING,
    CARD_HEIGHT, CARD_SCALE, CARD_WIDTH, CARD_X, card_y,
    render_card_frame, render_subscription_animation,
)
from app.subtitles import write_ass


def make_clip():
    return ClipCandidate(id="clip", job_id="job", rank=1, title="Uzun yol deneyimi",
        start=10, end=30, score=80, reasons=[], subtitles=[
            SubtitleCue(id="a", start=10, end=13, text="Uzun yolda sık sık mola verdim."),
            SubtitleCue(id="b", start=17, end=19, text="Böylece yolculuk daha rahat geçti."),
            SubtitleCue(id="c", start=27, end=29, text="Rotamı da önceden planladım."),
        ])


def seed(tmp_path):
    settings = Settings(data_dir=tmp_path, frontend_dist=tmp_path / "missing", ollama_seo_enabled=False)
    app = create_app(settings, start_worker=False)
    db = app.state.database
    source = tmp_path / "source.mp4"
    source.write_bytes(b"test")
    db.create_job("job", "source.mp4", source)
    db.update_job("job", status="ready", duration=60, width=640, height=360, stage="Hazır")
    db.create_clip(make_clip())
    return app, db, settings


def test_defaults_validation_persistence_and_queued_export_snapshot(tmp_path):
    app, db, _ = seed(tmp_path)
    # Old JSON is still readable without a database migration.
    with db.connect() as connection:
        connection.execute("UPDATE clips SET edit_settings_json = '{}' WHERE id = 'clip'")
    assert db.get_clip("clip").edit_settings.subscription.enabled is False
    payload = {"enabled": True, "channel_name": "  Oban Otağı  ", "text": "",
               "start": 8, "duration": 2.5, "animation": "slide", "position": "top",
               "use_photo": True, "click_effect": True}
    with TestClient(app) as client:
        saved = client.patch("/api/jobs/job/clips/clip", json={"edit_settings": {"subscription": payload}})
        assert saved.status_code == 200
        expected = saved.json()["edit_settings"]["subscription"]
        assert expected["channel_name"] == "Oban Otağı"
        assert expected["text"] == "" and expected["show_tagline"] is False
        for preview in (False, True):
            result = client.post("/api/jobs/job/exports", json={"clip_ids": ["clip"], "is_preview": preview})
            assert result.status_code == 202
            assert result.json()["exports"][0]["edit_settings"]["subscription"] == expected
        for invalid in ({"channel_name": " "}, {"channel_name": "x" * 51}, {"position": "bottom"}, {"start": -1}, {"duration": 0}, {"duration": 5.1},
                        {"animation": "invalid"}, {"text": "x" * 91}, {"start": "NaN"}):
            response = client.patch("/api/jobs/job/clips/clip", json={
                "edit_settings": {"subscription": {**payload, **invalid}}})
            assert response.status_code == 422
        client.patch("/api/jobs/job/clips/clip", json={"edit_settings": {"subscription": {"enabled": False}}})
    db.initialize()
    assert db.get_clip("clip").edit_settings.subscription.enabled is False
    assert all(export.edit_settings.subscription == SubscriptionSettings(**expected)
               for export in db.get_job("job").exports)


def test_card_is_separate_from_captions_and_user_text_is_literal(tmp_path):
    path = tmp_path / "subtitles.ass"
    edit = EditSettings(subscription=SubscriptionSettings(enabled=True,
        text=r"{\pos(0,0)}", show_tagline=True, start=4, duration=2.5))
    write_ass(make_clip().subtitles, 10, path, edit_settings=edit,
              playback_rate=1.5, clip_duration=15, subtitle_margin_v=560)
    captions = path.read_text(encoding="utf-8-sig")
    assert "Dialogue: 0" in captions
    assert ",Subscribe," not in captions
    assert edit.subscription.text not in captions
    rendered = render_card_frame(edit.subscription, .12)
    assert rendered.mode == "RGBA" and rendered.getbbox() is not None


def content_bounds(image, region, *, include_check=False):
    pixels = np.asarray(image)
    left, top, width, height = region
    crop = pixels[top:top + height, left:left + width]
    mask = (crop[:, :, 0] > 220) & (crop[:, :, 1] > 220) & (crop[:, :, 2] > 220)
    if include_check:
        mask |= ((crop[:, :, 0] > 130) & (crop[:, :, 1] > 190) & (crop[:, :, 2] > 145))
    if crop.shape[2] == 4:
        mask &= crop[:, :, 3] > 220
    y, x = np.nonzero(mask)
    assert len(x) > 30
    return left + x.min(), top + y.min(), left + x.max(), top + y.max()


@pytest.mark.parametrize("progress", [.12, .90])
def test_button_icon_and_text_are_centered_in_both_states(progress):
    settings = SubscriptionSettings(enabled=True, click_effect=True)
    image = render_card_frame(settings, progress)
    left, top, width, height = BUTTON_RECT
    region = (CANVAS_PADDING + left + 5, CANVAS_PADDING + top + 5, width - 10, height - 10)
    x1, y1, x2, y2 = content_bounds(image, region, include_check=progress > .38)
    expected_x = CANVAS_PADDING + left + width / 2
    expected_y = CANVAS_PADDING + top + height / 2
    assert abs((x1 + x2) / 2 - expected_x) <= 3
    assert abs((y1 + y2) / 2 - expected_y) <= 3


def test_photo_endpoint_and_canonical_animated_preview(tmp_path):
    app, _, _ = seed(tmp_path)
    settings = SubscriptionSettings(enabled=True, use_photo=True, click_effect=True, duration=4.2)
    with TestClient(app) as client:
        response = client.get("/api/branding/subscription-avatar")
        assert response.status_code == 200 and response.headers["content-type"] == "image/jpeg"
        assert response.content == AVATAR_PATH.read_bytes()
        preview = client.get("/api/branding/subscription-preview", params={
            "channel_name": settings.channel_name, "use_photo": True, "click_effect": True,
            "show_tagline": False, "duration": settings.duration, "animation": "slide",
        })
        assert preview.status_code == 200
        assert preview.headers["content-type"] == "image/webp"
        image = Image.open(io.BytesIO(preview.content))
        assert image.is_animated and image.n_frames > 10
        assert image.size == render_card_frame(settings, .12).size
        image.seek(image.n_frames - 1)
        assert image.convert("RGBA").getchannel("A").getbbox() is not None


def test_photo_fallback_and_disabled_click_effect_are_consistent(tmp_path, monkeypatch):
    settings = SubscriptionSettings(enabled=True, use_photo=False, click_effect=False)
    fallback = render_card_frame(settings, .12)
    assert np.array_equal(np.asarray(fallback), np.asarray(render_card_frame(settings, .90)))
    photo = render_card_frame(settings.model_copy(update={"use_photo": True}), .12)
    assert not np.array_equal(np.asarray(photo), np.asarray(fallback))
    monkeypatch.setattr("app.subscription_card.AVATAR_PATH", tmp_path / "missing.jpg")
    missing = render_card_frame(settings.model_copy(update={"use_photo": True}), .12)
    assert np.array_equal(np.asarray(missing), np.asarray(fallback))


def test_card_is_centered_and_clear_of_captions_in_both_positions():
    settings = SubscriptionSettings(enabled=True)
    assert CARD_X == pytest.approx((1080 - CARD_WIDTH * CARD_SCALE) / 2)
    assert CARD_X >= 48
    for margin in (220, 420, 560):
        top = card_y(settings, margin)
        bottom = top + CARD_HEIGHT * CARD_SCALE
        assert top >= 288 and bottom <= 1248
        assert bottom <= 1920 - margin - 260
    assert card_y(settings.model_copy(update={"position": "top"}), 420) == 336


def test_hidden_old_messages_do_not_change_the_card():
    settings = SubscriptionSettings(enabled=True, text="Önceden kaydedilmiş uzun abonelik mesajı.")
    image = render_card_frame(settings, .12)
    empty_message = render_card_frame(settings.model_copy(update={"text": ""}), .12)
    assert np.array_equal(np.asarray(image), np.asarray(empty_message))
    with_message = render_card_frame(settings.model_copy(update={"show_tagline": True}), .12)
    assert not np.array_equal(np.asarray(image), np.asarray(with_message))


def test_disabled_overlay_and_short_clip_clamping(tmp_path):
    path = tmp_path / "subtitles.ass"
    write_ass([], 0, path, edit_settings=EditSettings(subscription=SubscriptionSettings(text="Görünmemeli")),
              clip_duration=5)
    assert ",Subscribe," not in path.read_text(encoding="utf-8-sig")
    subscribe = SubscriptionSettings(enabled=True, text="Abone ol", start=100, duration=5)
    assert subscription_window(subscribe, 1) == (0, 1)


def agent_reply(boundary):
    return {"options": [{"text": text, "after_cue_id": boundary, "reason": "Yol deneyimi aktarıldıktan sonra."}
        for text in ("Sürüş deneyimleri için abone ol.", "Sen de iki tekerdeysen aramıza katıl.",
                     "Yeni rotaları birlikte keşfetmek için abone ol.")]}


def test_suggestions_use_composed_cues_cuts_inserts_hook_and_output_speed(tmp_path, monkeypatch):
    clip = make_clip().model_copy(update={
        "cut_ranges": [CutRange(start=14, end=16)],
        "insert_ranges": [InsertRange(source_start=40, source_end=43, insert_at=20)],
    })
    edit = EditSettings(hook_range=HookRange(source_start=10, source_end=13))
    words = "Bu mola yeri çok güzeldi.".split()
    segments = [{"start": 40, "end": 43, "text": " ".join(words), "words": [
        {"text": word, "start": 40 + index * .5, "end": 40.5 + index * .5}
        for index, word in enumerate(words)
    ]}]
    received = {}

    def run(**kwargs):
        data = json.loads(kwargs["prompt"].split("\n", 1)[1])
        received.update(data)
        # Cue b now ends at 7 source seconds: hook 3 + body [13,14] 1 + [16,19] 3.
        return agent_reply("1"), None

    monkeypatch.setattr(subscriptions, "run_codex_json", run)
    result = generate_subscription_suggestions(clip, edit, 1.25, segments, Settings(), tmp_path)
    assert result.output_duration == 21 / 1.25
    assert all(option.start == 7 / 1.25 for option in result.options)
    assert any("mola yeri" in item["text"] for item in received["transcript"])
    assert all(0 <= option.start <= result.output_duration - option.duration for option in result.options)
    assert clip.edit_settings.subscription.enabled is False  # Suggesting never saves settings.


def test_invalid_model_boundaries_retry_and_fail_without_saving(tmp_path, monkeypatch):
    calls = []

    def run(**kwargs):
        calls.append(kwargs["prompt"])
        return agent_reply("invented-cue"), None

    monkeypatch.setattr(subscriptions, "run_codex_json", run)
    with pytest.raises(CodexError, match="geçerli abonelik"):
        generate_subscription_suggestions(make_clip(), EditSettings(), 1, [], Settings(), tmp_path)
    assert len(calls) == 2 and "Validation correction" in calls[-1]


def test_suggestion_api_ownership_errors_and_unsaved_edit_settings(tmp_path, monkeypatch):
    app, db, _ = seed(tmp_path)
    monkeypatch.setattr(subscriptions, "run_codex_json", lambda **_: (agent_reply("1"), None))
    with TestClient(app) as client:
        route = "/api/jobs/job/clips/clip/subscription-suggestions"
        assert client.post(route.replace("jobs/job/", "jobs/other/"), json={}).status_code == 404
        result = client.post(route, json={"playback_rate": 1.5,
            "edit_settings": {"hook_range": {"source_start": 10, "source_end": 13}}})
        assert result.status_code == 200
        assert result.json()["output_duration"] == 20 / 1.5
        assert len(result.json()["options"]) == 3
        assert db.get_clip("clip").edit_settings.hook_range is None
        assert client.post(route, json={"playback_rate": 0}).status_code == 422
        assert client.post(route, json={"edit_settings": {"hook_range": {
            "source_start": 59, "source_end": 62}}}).status_code == 422
        db.update_clip("clip", subtitles=[])
        assert client.post(route, json={}).status_code == 422
        db.update_clip("clip", subtitles=make_clip().subtitles)

        def fail(**_):
            raise CodexError("Codex oturumu doğrulanamadı.")

        monkeypatch.setattr(subscriptions, "run_codex_json", fail)
        failed = client.post(route, json={})
        assert failed.status_code == 502 and "oturumu" in failed.json()["detail"]


@pytest.mark.parametrize("preview", [False, True])
def test_processor_renders_saved_subscription_after_clip_is_changed(tmp_path, monkeypatch, preview):
    _, db, settings = seed(tmp_path)
    edit = EditSettings(subscription=SubscriptionSettings(enabled=True, text="Sürüşler için abone ol.",
                                                        start=4, duration=2.5))
    db.create_export("export", "job", "clip", edit_settings=edit, playback_rate=1.5, is_preview=preview)
    db.update_clip("clip", edit_settings=EditSettings())

    def render(**kwargs):
        content = (kwargs["work_dir"] / "subtitles.ass").read_text(encoding="utf-8-sig")
        assert ",Subscribe," not in content
        assert kwargs["edit_settings"].subscription == edit.subscription
        assert kwargs["playback_rate"] == 1.5
        assert kwargs["output_width"] == (540 if preview else 1080)
        kwargs["output"].write_bytes(b"mp4")

    monkeypatch.setattr("app.processor.render_video", render)
    monkeypatch.setattr("app.youtube.render_thumbnail", lambda *args, **kwargs: None)
    Processor(db, settings).export("export")
    assert db.get_export_record("export")["status"] == "completed"


def frame(path: Path, seconds: float, width: int, height: int):
    result = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(seconds), "-i", str(path),
        "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"], capture_output=True,
        check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return np.frombuffer(result.stdout, dtype=np.uint8).reshape(height, width, 3)


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
@pytest.mark.parametrize("animation,rate,width", [("fade", 1, 1080), ("slide", 1.5, 540)])
def test_real_video_card_appears_at_requested_output_time_and_keeps_audio(tmp_path, animation, rate, width):
    source = tmp_path / "source.mp4"
    run_command(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=320x180:r=30:d=3",
        "-f", "lavfi", "-i", "sine=duration=3", "-c:v", "libx264", "-c:a", "aac", str(source)])
    edit = EditSettings(subscription=SubscriptionSettings(enabled=True, text="Sürüşler için abone ol.",
        start=.35, duration=1, animation=animation))
    cues = [SubtitleCue(id="cue", start=0, end=3, text="Konuşmanın altyazısı burada devam eder.")]
    write_ass(cues, 0, tmp_path / "subtitles.ass", edit_settings=edit, playback_rate=rate, clip_duration=3)
    output = tmp_path / "output.mp4"
    render_video(source=source, output=output, work_dir=tmp_path, start=0, end=3,
        crop_width=None, crop_height=None, framing_mode="fit", face_tracking_enabled=False,
        ffmpeg="ffmpeg", playback_rate=rate, edit_settings=edit, output_width=width)
    info = probe_media(output, "ffprobe")
    assert (info.width, info.height) == (width, width * 16 // 9)
    assert info.has_audio and abs(info.duration - 3 / rate) < .15

    def count_card(seconds):
        pixels = frame(output, seconds, info.width, info.height)
        region = pixels[round(info.height * .44):round(info.height * .67)]
        return np.sum(np.all(region < 70, axis=2))

    assert count_card(.1) == 0
    assert count_card(.8) > width * 10
    assert count_card(1.65) == 0


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
def test_animation_short_clip_has_alpha_and_exact_clamped_duration(tmp_path):
    settings = SubscriptionSettings(enabled=True, start=100, duration=5, animation="slide")
    output = tmp_path / "short-card.mov"
    start, end = render_subscription_animation(settings, 1, output, "ffmpeg")
    assert (start, end) == (0, 1)
    result = run_command(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)])
    metadata = json.loads(result.stdout)
    video = next(stream for stream in metadata["streams"] if stream["codec_type"] == "video")
    assert video["pix_fmt"] in {"argb", "rgba", "bgra", "abgr"}
    assert abs(float(metadata["format"]["duration"]) - 1) < .05
    assert video["width"] == render_card_frame(settings, .5).width


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
@pytest.mark.parametrize("position,rate,width,margin", [("above_subtitles", 1.5, 540, 560), ("top", 1, 1080, 420)])
def test_photo_button_state_and_bell_animation_render_in_both_positions(tmp_path, position, rate, width, margin):
    source = tmp_path / "source.mp4"
    run_command(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=320x180:r=30:d=6",
        "-f", "lavfi", "-i", "sine=duration=6", "-c:v", "libx264", "-c:a", "aac", str(source)])
    subscribe = SubscriptionSettings(enabled=True, start=.3, duration=3, position=position, animation="slide")
    edit = EditSettings(style="dynamic", subscription=subscribe)
    write_ass([], 0, tmp_path / "subtitles.ass", edit_settings=edit, playback_rate=rate,
              clip_duration=6, subtitle_margin_v=margin)
    output = tmp_path / "output.mp4"
    render_video(source=source, output=output, work_dir=tmp_path, start=0, end=6, crop_width=None,
        crop_height=None, framing_mode="fit", face_tracking_enabled=False, ffmpeg="ffmpeg",
        edit_settings=edit, playback_rate=rate, output_width=width, subtitle_margin_v=margin)
    info = probe_media(output, "ffprobe")
    assert info.has_audio and abs(info.duration - 6 / rate) < .15
    initial = frame(output, .8, info.width, info.height)
    scale = width / 1080
    y = card_y(subscribe, margin)
    row = initial[round((y + CARD_HEIGHT * CARD_SCALE / 2) * scale)]
    dark = np.all(row < 80, axis=1)
    x = np.flatnonzero(dark)
    assert x.max() - x.min() > width * .60
    left_margin, right_margin = x.min(), width - 1 - x.max()
    assert abs(int(left_margin) - int(right_margin)) <= 2
    after_click = frame(output, 1.9, info.width, info.height)
    after_bell = frame(output, 2.8, info.width, info.height)
    scale = width / 1080
    y = card_y(subscribe, margin)
    photo_x = round((CARD_X + AVATAR_CENTER[0] * CARD_SCALE) * scale)
    photo_y = round((y + AVATAR_CENTER[1] * CARD_SCALE) * scale)
    patch = initial[photo_y - 8:photo_y + 8, photo_x - 8:photo_x + 8]
    assert patch.std(axis=(0, 1)).mean() > 8  # Actual photo detail, rather than a flat avatar circle.
    button_x = round((CARD_X + (BUTTON_RECT[0] + BUTTON_RECT[2] - 24) * CARD_SCALE) * scale)
    button_y = round((y + (BUTTON_RECT[1] + 16) * CARD_SCALE) * scale)
    assert initial[button_y, button_x, 0] > 170
    assert after_click[button_y, button_x, 0] < 110  # Red becomes the subscribed state.
    bell_left = round((CARD_X + 760 * CARD_SCALE) * scale)
    bell_top = round((y + 50 * CARD_SCALE) * scale)
    side = round(64 * CARD_SCALE * scale)
    bell = after_bell[bell_top:bell_top + side, bell_left:bell_left + side]
    amber = (bell[:, :, 0] > 150) & (bell[:, :, 1] > 110) & (bell[:, :, 2] < 150)
    assert amber.sum() > 6
