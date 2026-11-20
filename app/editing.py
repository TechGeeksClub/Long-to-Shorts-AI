from __future__ import annotations

import math
import re

from app.models import EditSettings, SubscriptionSettings, SubtitleCue
from app.timeline import MIN_TIMELINE_SEGMENT_SECONDS, TimelineSegment


HOOK_FILLER = {
    "bu", "şu", "o", "bir", "de", "da", "ve", "ama", "peki", "var", "yok",
    "mı", "mi", "mu", "mü", "ben", "sen", "biz", "bize", "ne", "ha", "niye",
}


def subscription_window(settings: SubscriptionSettings, output_duration: float) -> tuple[float, float]:
    """Keep the full message within the final output, including shortened clips."""
    duration = min(settings.duration, max(0, output_duration))
    start = min(settings.start, max(0, output_duration - duration))
    return start, start + duration


def validate_edit_source(settings: EditSettings, source_duration: float) -> None:
    if settings.hook_range and source_duration > 0:
        if settings.hook_range.source_end > source_duration:
            raise ValueError("Hook aralığı kaynak videonun süresini aşamaz.")


def build_edit_timeline(
    timeline: list[TimelineSegment], settings: EditSettings,
) -> list[TimelineSegment]:
    hook = settings.hook_range
    if hook is None:
        return timeline
    body: list[TimelineSegment] = []
    for segment in timeline:
        if (
            not settings.hook_remove_from_body or segment.kind != "base"
            or segment.source_end <= hook.source_start
            or segment.source_start >= hook.source_end
        ):
            body.append(segment)
            continue
        if segment.source_start < hook.source_start:
            body.append(TimelineSegment(segment.source_start, hook.source_start, "base"))
        if segment.source_end > hook.source_end:
            body.append(TimelineSegment(hook.source_end, segment.source_end, "base"))
    return [TimelineSegment(hook.source_start, hook.source_end, "hook"), *(
        segment for segment in body if segment.duration >= MIN_TIMELINE_SEGMENT_SECONDS
    )]


def _transition_name(effect: str, direction: str = "in") -> str:
    return {
        "fade_black": "fadeblack", "fade_white": "fadewhite",
        "zoom_blur": "zoomin", "slide": "smoothleft", "rgb_split": "dissolve",
        "diagonal": "diagbr", "dissolve": "dissolve", "smooth_wipe": "smoothleft",
        "circle_reveal": "circleopen" if direction == "in" else "circleclose",
        "pixelize": "pixelize",
    }[effect]


def _transition_accent(video_filter: str, effect: str, start: float, end: float) -> str:
    enabled = f"between(t,{start:.6f},{end:.6f})"
    if effect == "rgb_split":
        return video_filter + f",rgbashift=rh=10:bh=-10:rv=-2:bv=2:enable='{enabled}'"
    if effect == "zoom_blur":
        return video_filter + f",gblur=sigma=5:steps=2:enable='{enabled}'"
    return video_filter


def apply_edge_effects(
    video_filter: str, audio_filter: str, settings: EditSettings, duration: float,
) -> tuple[str, str]:
    for direction, effect, requested_duration in (
        ("in", settings.intro_effect, settings.intro_duration),
        ("out", settings.outro_effect, settings.outro_duration),
    ):
        if effect == "none":
            continue
        length = min(requested_duration, duration / 2)
        if length <= 0:
            continue
        start = 0.0 if direction == "in" else duration - length
        if effect in {"fade_white", "fade_black"}:
            color = "white" if effect == "fade_white" else "black"
            video_filter += f",fade=t={direction}:st={start:.6f}:d={length:.6f}:color={color}"
        else:
            # Derive the matte from the video so size, SAR and frame rate match
            # even in low-resolution previews. The overlapping matte is exactly
            # as long as the transition; the video's duration stays unchanged.
            prefix = f"edge_{direction}"
            first, second = (
                (f"{prefix}_matte", f"{prefix}_video") if direction == "in"
                else (f"{prefix}_video", f"{prefix}_matte")
            )
            video_filter += (
                f",fps=30,settb=AVTB,format=yuv444p,split=2[{prefix}_video][{prefix}_copy];"
                f"[{prefix}_copy]trim=duration={length:.6f},setpts=PTS-STARTPTS,"
                f"lutyuv=y=16:u=128:v=128[{prefix}_matte];"
                f"[{first}][{second}]xfade=transition={_transition_name(effect, direction)}:"
                f"duration={length:.6f}:offset={start:.6f},trim=duration={duration:.6f}"
            )
            video_filter = _transition_accent(video_filter, effect, start, start + length)
        if settings.fade_audio:
            audio_filter += f",afade=t={direction}:st={start:.6f}:d={length:.6f}"
    return video_filter, audio_filter


def apply_hook_transition(
    video_filter: str, audio_filter: str, settings: EditSettings, duration: float,
    boundary: float | None,
) -> tuple[str, str]:
    """Blend the hook into the body without removing speech or shifting cues."""
    if (settings.hook_transition == "none" or boundary is None
            or not math.isfinite(boundary) or not 0 < boundary < duration):
        return video_filter, audio_filter
    # A frame of cloned handles on either side is the minimum useful overlap.
    # Quantize the handles to our output frame rate to avoid fractional pad
    # frames changing the finished duration.
    half_frames = min(
        round(settings.hook_transition_duration * 30 / 2),
        math.floor(boundary * 30 / 2), math.floor((duration - boundary) * 30 / 2),
    )
    if half_frames < 1:
        return video_filter, audio_filter
    half = half_frames / 30
    length = half * 2
    start = boundary - half
    video_filter += (
        ",fps=30,settb=AVTB,format=yuv444p,split=2[hook_before][hook_after];"
        f"[hook_before]trim=end={boundary:.6f},setpts=PTS-STARTPTS,"
        f"tpad=stop_mode=clone:stop_duration={half:.6f}[hook_tail];"
        f"[hook_after]trim=start={boundary:.6f},setpts=PTS-STARTPTS,"
        f"tpad=start_mode=clone:start_duration={half:.6f}[hook_head];"
        f"[hook_tail][hook_head]xfade=transition={_transition_name(settings.hook_transition)}:"
        f"duration={length:.6f}:offset={start:.6f},trim=duration={duration:.6f},setpts=PTS-STARTPTS"
    )
    video_filter = _transition_accent(video_filter, settings.hook_transition, start, start + length)
    if settings.fade_audio:
        # Keep all speech audible; a restrained dip marks the chapter change.
        audio_filter += (
            f",volume='1-0.65*max(0,1-abs(t-{boundary:.6f})/{half:.6f})':eval=frame"
        )
    return video_filter, audio_filter


def apply_video_effect(video_filter: str, settings: EditSettings) -> str:
    """Grade only the footage, before text and subscription overlays."""
    intensity = settings.effect_intensity
    effect = settings.video_effect
    if effect == "none" or intensity == 0:
        return video_filter
    base, separator, suffix = video_filter.rpartition(",ass=subtitles.ass")
    if not separator:
        raise ValueError("Video efekti için altyazı filtresi bulunamadı.")
    if effect == "cinematic":
        grade = (
            f"eq=contrast={1 + .18 * intensity:.6f}:saturation={1 - .16 * intensity:.6f}:"
            f"brightness={-.015 * intensity:.6f},"
            f"colorbalance=bs={.035 * intensity:.6f}:rs={-.025 * intensity:.6f}:"
            f"rh={.035 * intensity:.6f}:bh={-.02 * intensity:.6f}"
        )
    elif effect == "vivid":
        grade = f"eq=saturation={1 + .45 * intensity:.6f}:contrast={1 + .12 * intensity:.6f}"
    elif effect in {"warm", "cool"}:
        sign = 1 if effect == "warm" else -1
        grade = (
            f"colorbalance=rs={.06 * intensity * sign:.6f}:"
            f"rm={.08 * intensity * sign:.6f}:bs={-.06 * intensity * sign:.6f}:"
            f"bm={-.08 * intensity * sign:.6f}"
        )
    elif effect == "monochrome":
        grade = f"hue=s={1 - intensity:.6f}"
    elif effect == "vintage":
        sepia = (.393, .769, .189, .349, .686, .168, .272, .534, .131)
        keys = ("rr", "rg", "rb", "gr", "gg", "gb", "br", "bg", "bb")
        channels = [
            f"{key}={((1-intensity) if index in {0, 4, 8} else 0) + value*intensity:.6f}"
            for index, (key, value) in enumerate(zip(keys, sepia))
        ]
        grade = "colorchannelmixer=" + ":".join(channels)
        grade += f",eq=contrast={1 - .1 * intensity:.6f}:brightness={.025 * intensity:.6f}"
    else:
        raise ValueError(f"Bilinmeyen video efekti: {effect}")
    return f"{base},{grade},ass=subtitles.ass{suffix}"


def hook_suggestions(cues: list[SubtitleCue]) -> list[dict[str, str]]:
    """Offer excerpts, never invent claims or change the speaker's words."""
    text = " ".join(cue.text.strip() for cue in sorted(cues, key=lambda cue: cue.start))
    sentences = re.split(r"(?<=[.!?…])\s+", text)
    candidates: list[tuple[int, int, str, str]] = []
    seen: set[str] = set()
    for index, sentence in enumerate(sentences):
        sentence = sentence.strip()
        tokens = re.findall(r"[\w]+(?:[-'][\w]+)*", sentence.casefold())
        if (
            not 8 <= len(sentence) <= 90
            or len(tokens) < 4
            or sum(token not in HOOK_FILLER for token in tokens) < 2
        ):
            continue
        key = sentence.casefold()
        if key in seen:
            continue
        seen.add(key)
        score, reason = 0, "Konuşmadan kısa bir alıntı"
        if "?" in sentence:
            score, reason = 3, "Soruyla açılış"
        elif re.search(r"\b(ama|aslında|fakat|oysa|meğer)\b", key):
            score, reason = 2, "Karşıtlık içeren ifade"
        elif re.search(r"\d", sentence):
            score, reason = 1, "Somut detay içeren ifade"
        candidates.append((score, index, sentence, reason))
    candidates.sort(key=lambda value: (-value[0], value[1]))
    chosen: list[tuple[int, int, str, str]] = []
    for candidate in candidates:
        if candidate[3] not in {item[3] for item in chosen}:
            chosen.append(candidate)
        if len(chosen) == 3:
            break
    chosen.extend(candidate for candidate in candidates if candidate not in chosen)
    return [{"text": text, "reason": reason} for _, _, text, reason in chosen[:3]]


def punch_in_times(cues: list[SubtitleCue], clip_start: float) -> list[float]:
    """Space restrained accents at sentence boundaries on the composed timeline."""
    times = [0.0]
    previous_end = ""
    for cue in sorted(cues, key=lambda cue: cue.start):
        time = max(0.0, cue.start - clip_start)
        if time - times[-1] >= 7 and (
            re.search(r"[.!?…]$", previous_end)
            or re.search(r"\b(ama|aslında|fakat|neden)\b", cue.text.casefold())
        ):
            times.append(round(time, 3))
            if len(times) >= 5:
                break
        previous_end = cue.text.strip()
    return times


def apply_edit_video_filter(
    video_filter: str,
    settings: EditSettings,
    times: list[float],
    duration: float,
) -> str:
    if settings.style in {"off", "clean"} or not settings.punch_ins:
        return video_filter
    # Keep overlays after the camera movement so lettering stays a constant size.
    base, separator, _ = video_filter.rpartition(",ass=subtitles.ass")
    if not separator:
        raise ValueError("Kurgu için altyazı filtresi bulunamadı.")
    if settings.style == "cinematic":
        frames = max(1, round(duration * 30))
        zoom = f"1+0.035*min(on/{frames},1)"
    else:
        accents = [
            f"0.055*max(0,1-abs(on-{round((time + 0.6) * 30)})/18)"
            for time in times
        ]
        zoom = "1+" + "+".join(accents or ["0"])
    return (
        f"{base},fps=30,"
        f"zoompan=z='{zoom}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':"
        "d=1:s=1080x1920:fps=30,ass=subtitles.ass"
    )
