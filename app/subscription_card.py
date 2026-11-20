from __future__ import annotations

import io
import math
import os
import subprocess
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from app.editing import subscription_window
from app.models import SubscriptionSettings


CARD_WIDTH, CARD_HEIGHT = 864, 176
CARD_SCALE = .88
CARD_X = (1080 - CARD_WIDTH * CARD_SCALE) / 2
CANVAS_PADDING = 28
CANVAS_SIZE = (CARD_WIDTH + CANVAS_PADDING * 2, CARD_HEIGHT + CANVAS_PADDING * 2)
BUTTON_RECT = (488, 50, 248, 76)
AVATAR_CENTER, AVATAR_RADIUS = (76, 88), 48
BELL_CENTER = (796, 88)
AVATAR_PATH = Path(__file__).resolve().parent / "assets" / "oban-otagi-profile.jpg"
SUPERSAMPLE = 2
FPS = 30


def card_y(settings: SubscriptionSettings, subtitle_margin_v: int) -> float:
    if settings.position == "top":
        return 336
    return min(1216, 1920 - subtitle_margin_v - 260) - CARD_HEIGHT * CARD_SCALE


def has_profile_photo(settings: SubscriptionSettings) -> bool:
    return settings.use_photo and AVATAR_PATH.is_file()


@lru_cache(maxsize=64)
def card_font(size: int, bold: bool = True):
    fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for path in (
        fonts / ("segoeuib.ttf" if bold else "segoeui.ttf"),
        fonts / ("arialbd.ttf" if bold else "arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu") / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
    ):
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def fit_label(text: str, size: int, width: int, bold: bool = True) -> str:
    font = card_font(size * SUPERSAMPLE, bold)
    if font.getlength(text) <= width * SUPERSAMPLE:
        return text
    while text and font.getlength(text + "…") > width * SUPERSAMPLE:
        text = text[:-1]
    return text.rstrip() + "…"


def _point(x: float, y: float) -> tuple[float, float]:
    return ((CANVAS_PADDING + x) * SUPERSAMPLE, (CANVAS_PADDING + y) * SUPERSAMPLE)


def _box(x: float, y: float, width: float, height: float) -> tuple[float, float, float, float]:
    return (*_point(x, y), *_point(x + width, y + height))


def _ink_text(draw: ImageDraw.ImageDraw, text: str, x: float, center_y: float,
              size: int, color: str, bold: bool = True) -> float:
    """Position visible letter bounds, rather than font ascender/descender boxes."""
    font = card_font(size * SUPERSAMPLE, bold)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    px, py = _point(x, center_y)
    draw.text((px - left, py - (top + bottom) / 2), text, font=font, fill=color)
    return (right - left) / SUPERSAMPLE


def _poly(draw: ImageDraw.ImageDraw, points: list[tuple[float, float]], fill: str) -> None:
    draw.polygon([_point(x, y) for x, y in points], fill=fill)


def _line(draw: ImageDraw.ImageDraw, points: list[tuple[float, float]], color: str,
          width: float = 2.5) -> None:
    draw.line([_point(x, y) for x, y in points], fill=color,
              width=max(1, round(width * SUPERSAMPLE)), joint="curve")


@lru_cache(maxsize=24)
def _base_frame(channel_name: str, tagline: str, use_photo: bool, photo_version: tuple,
                subscribed: bool) -> Image.Image:
    image = Image.new("RGBA", tuple(value * SUPERSAMPLE for value in CANVAS_SIZE))
    shadow = Image.new("RGBA", image.size)
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle(_box(0, 8, CARD_WIDTH, CARD_HEIGHT),
        radius=32 * SUPERSAMPLE, fill=(0, 0, 0, 105))
    image.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(8 * SUPERSAMPLE)))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(_box(0, 0, CARD_WIDTH, CARD_HEIGHT), radius=32 * SUPERSAMPLE,
        fill="#11141A", outline="#343B46", width=SUPERSAMPLE)
    draw.ellipse(_box(28, 40, 96, 96), fill="#EF5361")
    draw.ellipse(_box(31, 43, 90, 90), fill="#252C36")
    if use_photo:
        try:
            with Image.open(AVATAR_PATH) as original:
                original = ImageOps.exif_transpose(original).convert("RGB")
                width, height = original.size
                crop = original.crop((round(width * .12), round(height * .07),
                                      round(width * .88), round(height * .83)))
                photo = ImageOps.fit(crop, (90 * SUPERSAMPLE, 90 * SUPERSAMPLE),
                                     method=Image.Resampling.LANCZOS).convert("RGBA")
            mask = Image.new("L", photo.size)
            ImageDraw.Draw(mask).ellipse((0, 0, photo.width - 1, photo.height - 1), fill=255)
            photo.putalpha(mask)
            image.alpha_composite(photo, tuple(round(value) for value in _point(31, 43)))
        except (OSError, ValueError):
            use_photo = False
    if not use_photo:
        initials = "".join(word[0] for word in channel_name.split()[:2]).upper()
        font = card_font(30 * SUPERSAMPLE)
        ink = draw.textbbox((0, 0), initials, font=font)
        _ink_text(draw, initials, 76 - (ink[2] - ink[0]) / SUPERSAMPLE / 2, 88, 30, "#FFFFFF")

    name_size = 42
    while name_size > 30 and card_font(name_size * SUPERSAMPLE).getlength(channel_name) > 308 * SUPERSAMPLE:
        name_size -= 1
    _ink_text(draw, fit_label(channel_name, name_size, 308), 150, 72, name_size, "#F7F8FB")
    tagline_size = 23
    while tagline_size > 21 and card_font(tagline_size * SUPERSAMPLE, False).getlength(tagline) > 308 * SUPERSAMPLE:
        tagline_size -= 1
    _ink_text(draw, fit_label(tagline, tagline_size, 308, False), 150, 113, tagline_size, "#A6AFBE", False)

    x, y, width, height = BUTTON_RECT
    draw.rounded_rectangle(_box(x, y, width, height), radius=23 * SUPERSAMPLE,
        fill="#2B333E" if subscribed else "#F34453")
    label, size = ("Abone Olundu", 27) if subscribed else ("Abone Ol", 32)
    font = card_font(size * SUPERSAMPLE)
    ink = draw.textbbox((0, 0), label, font=font)
    text_width = (ink[2] - ink[0]) / SUPERSAMPLE
    icon_width, gap = 20, 12
    group_x = x + (width - icon_width - gap - text_width) / 2
    center_y = y + height / 2
    if subscribed:
        _line(draw, [(group_x + 1, center_y), (group_x + 7, center_y + 6),
                     (group_x + 20, center_y - 7)], "#A8E6BE", 3.5)
    else:
        _poly(draw, [(group_x, center_y - 11), (group_x, center_y + 11),
                     (group_x + 20, center_y)], "#FFFFFF")
    _ink_text(draw, label, group_x + icon_width + gap, center_y, size, "#FFFFFF")
    return image


def _bell(image: Image.Image, angle: float, active: bool) -> None:
    draw = ImageDraw.Draw(image)
    background = "#3A3022" if active else "#242B35"
    foreground = "#FFD27D" if active else "#D5DCE6"
    draw.ellipse(_box(760, 52, 72, 72), fill=background)
    icon = Image.new("RGBA", (72 * SUPERSAMPLE, 72 * SUPERSAMPLE))
    pen = ImageDraw.Draw(icon)
    def local(points):
        return [(x * SUPERSAMPLE, y * SUPERSAMPLE) for x, y in points]
    pen.arc(tuple(value * SUPERSAMPLE for value in (25, 18, 47, 42)),
        180, 360, fill=foreground, width=round(2.8 * SUPERSAMPLE))
    pen.line(local([(25, 30), (25, 41), (20, 47), (52, 47), (47, 41), (47, 30)]),
        fill=foreground, width=round(2.8 * SUPERSAMPLE), joint="curve")
    pen.arc(tuple(value * SUPERSAMPLE for value in (31, 47, 41, 56)),
        0, 180, fill=foreground, width=round(2.8 * SUPERSAMPLE))
    if active:
        pen.arc(tuple(value * SUPERSAMPLE for value in (16, 14, 56, 57)),
            194, 234, fill=foreground, width=round(2.3 * SUPERSAMPLE))
        pen.arc(tuple(value * SUPERSAMPLE for value in (16, 14, 56, 57)),
            306, 346, fill=foreground, width=round(2.3 * SUPERSAMPLE))
    if angle:
        icon = icon.rotate(angle, resample=Image.Resampling.BICUBIC)
    image.alpha_composite(icon, tuple(round(value) for value in _point(760, 52)))


def _ease(value: float) -> float:
    value = min(1, max(0, value))
    return value * value * (3 - 2 * value)


def _cursor(image: Image.Image, x: float, y: float, opacity: float) -> None:
    cursor = Image.new("RGBA", (38 * SUPERSAMPLE, 48 * SUPERSAMPLE))
    draw = ImageDraw.Draw(cursor)
    points = [(3, 3), (3, 31), (10, 25), (16, 39), (22, 36), (16, 23), (28, 23)]
    points = [(x * SUPERSAMPLE, y * SUPERSAMPLE) for x, y in points]
    draw.polygon(points, fill=(255, 255, 255, round(opacity * 255)))
    draw.line([*points, points[0]], fill=(15, 19, 25, round(opacity * 255)),
        width=round(1.5 * SUPERSAMPLE), joint="curve")
    image.alpha_composite(cursor, tuple(round(value) for value in _point(x - 3, y - 3)))


def render_card_frame(settings: SubscriptionSettings, progress: float) -> Image.Image:
    """One canonical drawing for the editor and the exported video."""
    progress = min(1, max(0, progress))
    photo = has_profile_photo(settings)
    version = (str(AVATAR_PATH), AVATAR_PATH.stat().st_mtime_ns) if photo else ()
    subscribed = settings.click_effect and progress >= .38
    bell_active = settings.click_effect and progress >= .68
    tagline = settings.text if settings.show_tagline and settings.text else "YouTube kanalı"
    image = _base_frame(settings.channel_name, tagline, photo, version, subscribed).copy()
    ring_phase = (progress - .68) / .14
    angle = math.sin(ring_phase * math.pi * 5) * 15 * (1 - ring_phase) if 0 <= ring_phase <= 1 else 0
    _bell(image, angle, bell_active)
    if settings.click_effect:
        overlay = Image.new("RGBA", image.size)
        draw = ImageDraw.Draw(overlay)
        for moment, center in ((.38, (690, 110)), (.68, BELL_CENTER)):
            pulse = (progress - moment) / .09
            if 0 <= pulse <= 1:
                radius = 8 + 23 * _ease(pulse)
                draw.ellipse(_box(center[0] - radius, center[1] - radius, radius * 2, radius * 2),
                    outline=(255, 255, 255, round(140 * (1 - pulse))), width=2 * SUPERSAMPLE)
        image.alpha_composite(overlay)
        if .18 <= progress <= .84:
            if progress < .38:
                fraction = _ease((progress - .18) / .20)
                x, y = 725 - 35 * fraction, 154 - 44 * fraction
            elif progress < .44:
                x, y = 690, 110
            else:
                fraction = _ease((progress - .44) / .24)
                x, y = 690 + (796 - 690) * fraction, 110 + (88 - 110) * fraction
            opacity = min(1, max(0, (.84 - progress) / .06))
            _cursor(image, x, y, opacity)
    duration = max(.001, settings.duration)
    fade = min(.18, duration / 4) / duration
    alpha = min(1, progress / fade, (1 - progress) / fade)
    if alpha < 1:
        image.putalpha(image.getchannel("A").point(lambda value: round(value * max(0, alpha))))
    if settings.animation == "slide":
        entry = min(.28, duration / 8) / duration
        offset = round(18 * SUPERSAMPLE * (1 - _ease(progress / entry)))
        if offset:
            shifted = Image.new("RGBA", image.size)
            shifted.alpha_composite(image, (0, offset))
            image = shifted
    return image.resize(CANVAS_SIZE, Image.Resampling.LANCZOS)


def render_card_preview(settings: SubscriptionSettings, animated: bool = True) -> bytes:
    version = (str(AVATAR_PATH), AVATAR_PATH.stat().st_mtime_ns) if has_profile_photo(settings) else ()
    return _cached_preview(settings.model_dump_json(), animated, version)


@lru_cache(maxsize=8)
def _cached_preview(payload: str, animated: bool, photo_version: tuple) -> bytes:
    settings = SubscriptionSettings.model_validate_json(payload)
    destination = io.BytesIO()
    if not animated:
        frame = render_card_frame(settings.model_copy(update={"click_effect": False}), .5)
        frame.save(destination, format="WEBP", lossless=True)
    else:
        count = max(2, math.ceil(settings.duration * FPS))
        frames = [render_card_frame(settings, min(.9, index / (count - 1))) for index in range(count)]
        frames[0].save(destination, format="WEBP", save_all=True, append_images=frames[1:],
            duration=round(1000 / FPS), loop=1, quality=92, method=3, minimize_size=False)
    return destination.getvalue()


def render_subscription_animation(
    settings: SubscriptionSettings, output_duration: float, destination: Path, ffmpeg: str,
) -> tuple[float, float]:
    start, end = subscription_window(settings, output_duration)
    if end <= start:
        raise ValueError("Abonelik kartı için video süresi bulunamadı.")
    duration = end - start
    effective = settings.model_copy(update={"duration": duration})
    count = max(2, math.ceil(duration * FPS))
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
        "-pixel_format", "rgba", "-video_size", f"{CANVAS_SIZE[0]}x{CANVAS_SIZE[1]}",
        "-framerate", str(FPS), "-i", "pipe:0", "-an", "-c:v", "qtrle", "-pix_fmt", "argb", str(destination)]
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
        try:
            assert process.stdin is not None
            for index in range(count):
                process.stdin.write(render_card_frame(effective, index / (count - 1)).tobytes())
            process.stdin.close()
            process.stdin = None
        except (BrokenPipeError, OSError) as exc:
            diagnostic = process.communicate()[1].decode("utf-8", errors="replace")
            raise RuntimeError(f"Abonelik animasyonu oluşturulamadı: {diagnostic[-500:]}") from exc
        diagnostic = process.communicate()[1].decode("utf-8", errors="replace")
        if process.returncode != 0:
            raise RuntimeError(f"Abonelik animasyonu oluşturulamadı: {diagnostic[-500:]}")
    return start, end


def subscription_filter_graph(
    video_filter: str, settings: SubscriptionSettings, start: float, end: float,
    subtitle_margin_v: int, output_width: int,
) -> str:
    width = round(CANVAS_SIZE[0] * CARD_SCALE / 2) * 2
    height = round(CANVAS_SIZE[1] * CARD_SCALE / 2) * 2
    y = card_y(settings, subtitle_margin_v) - CANVAS_PADDING * height / CANVAS_SIZE[1]
    scaling = f",scale={output_width}:{round(output_width * 16 / 9 / 2) * 2}" if output_width != 1080 else ""
    return (
        f"[0:v]{video_filter}[base];"
        f"[1:v]scale={width}:{height}:flags=lanczos,format=rgba,setpts=PTS-STARTPTS+{start:.6f}/TB[card];"
        f"[base][card]overlay=x=(W-w)/2:y={y:.6f}:eof_action=pass:repeatlast=0:"
        f"enable='gte(t,{start:.6f})*lt(t,{end:.6f})'{scaling}[video]"
    )
