from __future__ import annotations

import math
import re
import unicodedata
import uuid
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from app.models import CutRange, SubtitleCue, SubtitleWord
from app.timeline import TimelineSegment

DISPLAY_BRIDGE_SECONDS = 0.75
MIN_ASS_EVENT_SECONDS = 0.08
MIN_CUT_RANGE_SECONDS = 0.05
AUTO_CUT_MIN_SPEECH_GAP_SECONDS = 1.2
AUTO_CUT_SPEECH_PADDING_SECONDS = 0.2
AUTO_CUT_MIN_RESULT_SECONDS = 0.35
DEFAULT_MAX_CUE_WORDS = 7
DEFAULT_MAX_CUE_UNITS = 42.0
ASS_MAX_LINE_UNITS = 24.0
DEFAULT_ASS_SUBTITLE_MARGIN_V = 420
MIN_ASS_SUBTITLE_MARGIN_V = 220
MAX_ASS_SUBTITLE_MARGIN_V = 560
DEFAULT_ASS_SUBTITLE_FONT_FAMILY = "Arial"
ASS_SUBTITLE_FONT_FAMILIES = {
    "Arial",
    "Arial Black",
    "Impact",
    "Segoe UI",
    "Tahoma",
    "Verdana",
}
SENTENCE_END_RE = re.compile(r"[.!?…]+[\"'”’)\]]*$")


def build_subtitle_cues(
    segments: list[dict[str, Any]],
    clip_start: float,
    clip_end: float,
    max_words: int = DEFAULT_MAX_CUE_WORDS,
    max_units: float = DEFAULT_MAX_CUE_UNITS,
) -> list[SubtitleCue]:
    words = [
        SubtitleWord(
            text=str(word["text"]),
            start=max(clip_start, float(word["start"])),
            end=min(clip_end, float(word["end"])),
        )
        for segment in segments
        for word in segment["words"]
        if word["end"] > clip_start and word["start"] < clip_end
    ]
    return group_subtitle_words(words, max_words=max_words, max_units=max_units)


def text_units(text: str) -> float:
    total = 0.0
    for char in text:
        if char.isspace():
            total += 0.35
        elif char in ".,:;!?…'\"-–—()[]":
            total += 0.45
        elif char.casefold() in {"i", "ı", "l", "j", "t", "f", "r"}:
            total += 0.62
        elif char.casefold() in {"m", "w", "ğ", "ş"}:
            total += 1.18
        else:
            total += 1.0
    return total


def words_text(words: list[SubtitleWord]) -> str:
    return " ".join(word.text.strip() for word in words if word.text.strip())


def is_sentence_end(text: str) -> bool:
    return bool(SENTENCE_END_RE.search(text.strip()))


def make_subtitle_cue(words: list[SubtitleWord]) -> SubtitleCue:
    return SubtitleCue(
        id=str(uuid.uuid4()),
        start=words[0].start,
        end=words[-1].end,
        text=words_text(words),
        words=words,
    )


def should_start_new_cue(
    current: list[SubtitleWord],
    candidate_word: SubtitleWord,
    max_words: int,
    max_units: float,
) -> bool:
    if not current:
        return False
    candidate = [*current, candidate_word]
    if len(candidate) > max_words:
        return True
    return text_units(words_text(candidate)) > max_units


def group_subtitle_words(
    words: list[SubtitleWord],
    max_words: int = DEFAULT_MAX_CUE_WORDS,
    max_units: float = DEFAULT_MAX_CUE_UNITS,
) -> list[SubtitleCue]:
    cues: list[SubtitleCue] = []
    current: list[SubtitleWord] = []

    def flush() -> None:
        nonlocal current
        if current:
            cues.append(make_subtitle_cue(current))
            current = []

    for word in sorted(words, key=lambda value: (value.start, value.end)):
        if should_start_new_cue(current, word, max_words, max_units):
            flush()
        current.append(word)
        if is_sentence_end(word.text):
            flush()
    flush()
    return cues


def best_line_break_index(words: list[SubtitleWord]) -> int | None:
    if len(words) < 2 or text_units(words_text(words)) <= ASS_MAX_LINE_UNITS:
        return None

    best: tuple[float, int] | None = None
    for index in range(1, len(words)):
        left = words[:index]
        right = words[index:]
        left_units = text_units(words_text(left))
        right_units = text_units(words_text(right))
        overflow = max(0.0, left_units - ASS_MAX_LINE_UNITS) + max(
            0.0, right_units - ASS_MAX_LINE_UNITS
        )
        balance = abs(left_units - right_units) * 0.08
        edge_penalty = 2.0 if index == 1 or index == len(words) - 1 else 0.0
        punctuation_bonus = -0.7 if left[-1].text.rstrip().endswith((",", ";", ":")) else 0.0
        score = overflow * 4 + balance + edge_penalty + punctuation_bonus
        if best is None or score < best[0]:
            best = (score, index)
    return best[1] if best else None


def ass_caption_text(words: list[SubtitleWord], active_index: int) -> str:
    break_index = best_line_break_index(words)
    fragments: list[str] = []
    for index, word in enumerate(words):
        if break_index is not None and index == break_index:
            fragments.append(r"\N")
        color = "&H0000FFFF&" if index == active_index else "&H00FFFFFF&"
        fragments.append(rf"{{\c{color}}}{escape_ass_text(word.text)}")
    return " ".join(fragments).replace(r" \N ", r"\N")


def normalize_cut_ranges(
    cut_ranges: list[CutRange],
    clip_start: float,
    clip_end: float,
) -> list[CutRange]:
    normalized: list[CutRange] = []
    for cut in sorted(cut_ranges, key=lambda value: value.start):
        start = max(clip_start, min(cut.start, clip_end))
        end = max(clip_start, min(cut.end, clip_end))
        if end - start < MIN_CUT_RANGE_SECONDS:
            continue
        if normalized and start <= normalized[-1].end:
            previous = normalized[-1]
            normalized[-1] = previous.model_copy(update={"end": max(previous.end, end)})
        else:
            normalized.append(CutRange(start=round(start, 3), end=round(end, 3)))
    return normalized


def cut_duration(cut_ranges: list[CutRange]) -> float:
    return sum(cut.end - cut.start for cut in cut_ranges)


def remaining_duration_after_cuts(
    clip_start: float,
    clip_end: float,
    cut_ranges: list[CutRange],
) -> float:
    return max(0.0, clip_end - clip_start - cut_duration(cut_ranges))


def kept_ranges_after_cuts(
    clip_start: float,
    clip_end: float,
    cut_ranges: list[CutRange],
) -> list[tuple[float, float]]:
    kept: list[tuple[float, float]] = []
    cursor = clip_start
    for cut in cut_ranges:
        if cut.start > cursor:
            kept.append((cursor, cut.start))
        cursor = max(cursor, cut.end)
    if cursor < clip_end:
        kept.append((cursor, clip_end))
    return [(start, end) for start, end in kept if end - start >= MIN_CUT_RANGE_SECONDS]


def is_time_cut(time: float, cut_ranges: list[CutRange]) -> bool:
    return any(cut.start <= time < cut.end for cut in cut_ranges)


def removed_duration_before(time: float, cut_ranges: list[CutRange]) -> float:
    removed = 0.0
    for cut in cut_ranges:
        if time <= cut.start:
            break
        removed += max(0.0, min(time, cut.end) - cut.start)
    return removed


def shift_time_after_cuts(
    time: float,
    clip_start: float,
    cut_ranges: list[CutRange],
) -> float:
    return clip_start + (time - clip_start - removed_duration_before(time, cut_ranges))


def apply_cut_ranges_to_subtitles(
    cues: list[SubtitleCue],
    clip_start: float,
    clip_end: float,
    cut_ranges: list[CutRange],
) -> list[SubtitleCue]:
    normalized_cuts = normalize_cut_ranges(cut_ranges, clip_start, clip_end)
    if not normalized_cuts:
        return normalize_cues(cues, clip_start, clip_end)

    shifted: list[SubtitleCue] = []
    for cue in normalize_cues(cues, clip_start, clip_end):
        words = cue_words(cue)
        if words:
            kept_words: list[SubtitleWord] = []
            for word in words:
                midpoint = (word.start + word.end) / 2
                if is_time_cut(midpoint, normalized_cuts):
                    continue
                start = shift_time_after_cuts(word.start, clip_start, normalized_cuts)
                end = max(start + 0.01, shift_time_after_cuts(word.end, clip_start, normalized_cuts))
                kept_words.append(word.model_copy(update={"start": start, "end": end}))
            if kept_words:
                shifted.append(
                    cue.model_copy(
                        update={
                            "start": kept_words[0].start,
                            "end": kept_words[-1].end,
                            "text": words_text(kept_words),
                            "words": kept_words,
                        }
                    )
                )
            continue

        midpoint = (cue.start + cue.end) / 2
        if is_time_cut(midpoint, normalized_cuts):
            continue
        start = shift_time_after_cuts(cue.start, clip_start, normalized_cuts)
        end = max(start + 0.01, shift_time_after_cuts(cue.end, clip_start, normalized_cuts))
        shifted.append(cue.model_copy(update={"start": start, "end": end}))
    return shifted


def compose_subtitles_for_timeline(
    *,
    base_cues: list[SubtitleCue],
    transcript_segments: list[dict[str, Any]],
    timeline: list[TimelineSegment],
    output_start: float,
) -> list[SubtitleCue]:
    composed: list[SubtitleCue] = []
    output_cursor = output_start
    for timeline_index, segment in enumerate(timeline):
        source_cues = (
            base_cues
            if segment.kind == "base"
            else build_subtitle_cues(
                transcript_segments,
                segment.source_start,
                segment.source_end,
            )
        )
        sliced = _slice_subtitles_to_range(
            source_cues,
            segment.source_start,
            segment.source_end,
        )
        offset = output_cursor - segment.source_start
        for cue_index, cue in enumerate(sliced):
            words = [
                word.model_copy(
                    update={
                        "start": word.start + offset,
                        "end": word.end + offset,
                    }
                )
                for word in cue.words
            ]
            composed.append(
                cue.model_copy(
                    update={
                        "id": f"{cue.id}-timeline-{timeline_index}-{cue_index}",
                        "start": cue.start + offset,
                        "end": cue.end + offset,
                        "words": words,
                    }
                )
            )
        output_cursor += segment.duration
    return normalize_cues(composed, output_start, output_cursor)


def _slice_subtitles_to_range(
    cues: list[SubtitleCue],
    source_start: float,
    source_end: float,
) -> list[SubtitleCue]:
    sliced: list[SubtitleCue] = []
    for cue in cues:
        if cue.end <= source_start or cue.start >= source_end:
            continue
        words = cue_words(cue) if cue.words else []
        if words:
            kept_words = [
                word.model_copy(
                    update={
                        "start": max(source_start, word.start),
                        "end": min(source_end, word.end),
                    }
                )
                for word in words
                if source_start <= (word.start + word.end) / 2 < source_end
            ]
            if not kept_words:
                continue
            text = cue.text if len(kept_words) == len(words) else words_text(kept_words)
            sliced.append(
                cue.model_copy(
                    update={
                        "start": kept_words[0].start,
                        "end": kept_words[-1].end,
                        "text": text,
                        "words": kept_words,
                    }
                )
            )
            continue
        sliced.append(
            cue.model_copy(
                update={
                    "start": max(source_start, cue.start),
                    "end": min(source_end, cue.end),
                }
            )
        )
    return sliced


def subtitle_words_from_segments(
    segments: list[dict[str, Any]],
    clip_start: float,
    clip_end: float,
) -> list[SubtitleWord]:
    return [
        SubtitleWord(
            text=word["text"],
            start=max(clip_start, float(word["start"])),
            end=min(clip_end, float(word["end"])),
        )
        for segment in segments
        for word in segment["words"]
        if word["end"] > clip_start and word["start"] < clip_end
    ]


def build_speech_gap_cut_ranges(
    segments: list[dict[str, Any]],
    clip_start: float,
    clip_end: float,
    min_gap_seconds: float = AUTO_CUT_MIN_SPEECH_GAP_SECONDS,
    padding_seconds: float = AUTO_CUT_SPEECH_PADDING_SECONDS,
) -> list[CutRange]:
    words = sorted(subtitle_words_from_segments(segments, clip_start, clip_end), key=lambda word: word.start)
    if not words:
        return []

    cut_ranges: list[CutRange] = []

    def add_gap(gap_start: float, gap_end: float) -> None:
        gap_start = max(clip_start, min(gap_start, clip_end))
        gap_end = max(clip_start, min(gap_end, clip_end))
        if gap_end - gap_start < min_gap_seconds:
            return
        cut_start = gap_start + padding_seconds
        cut_end = gap_end - padding_seconds
        if cut_end - cut_start >= AUTO_CUT_MIN_RESULT_SECONDS:
            cut_ranges.append(CutRange(start=round(cut_start, 3), end=round(cut_end, 3)))

    add_gap(clip_start, words[0].start)
    for left, right in zip(words, words[1:], strict=False):
        add_gap(left.end, right.start)
    add_gap(words[-1].end, clip_end)
    return normalize_cut_ranges(cut_ranges, clip_start, clip_end)


def split_subtitle_text(text: str) -> list[str]:
    return [token for token in re.split(r"\s+", text.strip()) if token]


def comparable_token(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text.casefold())
    without_marks = "".join(char for char in normalized if not unicodedata.combining(char))
    transliterated = without_marks.translate(
        str.maketrans({"ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u"})
    )
    return re.sub(r"[^\w]+", "", transliterated, flags=re.UNICODE)


def find_transcript_sequence(
    tokens: list[str],
    transcript_words: list[SubtitleWord],
    start_index: int,
) -> tuple[int, int] | None:
    comparable_tokens = [comparable_token(token) for token in tokens]
    if not comparable_tokens or any(not token for token in comparable_tokens):
        return None
    comparable_words = [comparable_token(word.text) for word in transcript_words]
    length = len(comparable_tokens)

    def search(first_index: int) -> tuple[int, int] | None:
        for index in range(first_index, len(comparable_words) - length + 1):
            if comparable_words[index : index + length] == comparable_tokens:
                return index, index + length
        return None

    return search(start_index) or search(0)


def find_nearest_transcript_sequence(
    tokens: list[str],
    transcript_words: list[SubtitleWord],
    start_index: int,
) -> tuple[int, int] | None:
    exact = find_transcript_sequence(tokens, transcript_words, start_index)
    if exact is not None:
        return exact

    comparable_tokens = [comparable_token(token) for token in tokens]
    if not comparable_tokens or any(not token for token in comparable_tokens):
        return None
    comparable_words = [comparable_token(word.text) for word in transcript_words]
    token_count = len(comparable_tokens)
    length_delta = max(2, math.ceil(token_count * 0.35))
    min_length = max(1, token_count - length_delta)
    max_length = min(len(comparable_words), token_count + length_delta)
    if not comparable_words or min_length > max_length:
        return None

    best: tuple[float, int, int] | None = None
    for index in range(len(comparable_words)):
        if index < start_index - length_delta:
            continue
        for window_length in range(min_length, max_length + 1):
            after_last = index + window_length
            if after_last > len(comparable_words):
                continue
            window = comparable_words[index:after_last]
            ratio = SequenceMatcher(None, comparable_tokens, window).ratio()
            cursor_penalty = 0.02 if index < start_index else 0.0
            length_penalty = abs(window_length - token_count) * 0.015
            score = ratio - cursor_penalty - length_penalty
            if best is None or score > best[0]:
                best = (score, index, after_last)

    if best is None or best[0] < 0.62:
        return None
    return best[1], best[2]


def words_for_tokens_in_span(tokens: list[str], matched_words: list[SubtitleWord]) -> list[SubtitleWord]:
    if len(tokens) == len(matched_words):
        return [
            SubtitleWord(text=token, start=word.start, end=word.end)
            for token, word in zip(tokens, matched_words, strict=True)
        ]

    span_start = matched_words[0].start
    span_end = max(matched_words[-1].end, span_start + 0.01)
    step = (span_end - span_start) / len(tokens)
    return [
        SubtitleWord(
            text=token,
            start=span_start + index * step,
            end=span_start + (index + 1) * step,
        )
        for index, token in enumerate(tokens)
    ]


def retime_cues_from_transcript(
    cues: list[SubtitleCue],
    segments: list[dict[str, Any]],
    clip_start: float,
    clip_end: float,
) -> list[SubtitleCue]:
    transcript_words = subtitle_words_from_segments(segments, clip_start, clip_end)
    if not transcript_words:
        return normalize_cues(cues, clip_start, clip_end)

    retimed: list[SubtitleCue] = []
    cursor = 0
    for cue in sorted(cues, key=lambda value: value.start):
        if cue.id.startswith("manual-"):
            retimed.append(normalize_cues([cue], clip_start, clip_end)[0])
            continue
        tokens = split_subtitle_text(cue.text)
        match = find_nearest_transcript_sequence(tokens, transcript_words, cursor)
        if match is None:
            retimed.append(normalize_cues([cue], clip_start, clip_end)[0])
            continue

        first, after_last = match
        matched_words = transcript_words[first:after_last]
        words = words_for_tokens_in_span(tokens, matched_words)
        retimed.append(
            cue.model_copy(
                update={
                    "start": words[0].start,
                    "end": words[-1].end,
                    "words": words,
                }
            )
        )
        cursor = after_last
    return retimed


def retime_cue(cue: SubtitleCue) -> SubtitleCue:
    tokens = split_subtitle_text(cue.text)
    if not tokens:
        return cue.model_copy(update={"words": []})
    if cue.words and len(tokens) == len(cue.words):
        words = []
        for token, word in zip(tokens, cue.words, strict=True):
            start = max(cue.start, min(word.start, cue.end))
            end = max(start + 0.01, min(word.end, cue.end))
            words.append(word.model_copy(update={"text": token, "start": start, "end": end}))
        return cue.model_copy(update={"words": words})
    duration = max(0.01, cue.end - cue.start)
    step = duration / len(tokens)
    words = [
        SubtitleWord(
            text=token,
            start=cue.start + index * step,
            end=cue.start + (index + 1) * step,
        )
        for index, token in enumerate(tokens)
    ]
    return cue.model_copy(update={"words": words})


def normalize_cues(cues: list[SubtitleCue], clip_start: float, clip_end: float) -> list[SubtitleCue]:
    normalized: list[SubtitleCue] = []
    for cue in sorted(cues, key=lambda value: value.start):
        start = max(clip_start, min(cue.start, clip_end))
        end = max(start + 0.01, min(cue.end, clip_end))
        updated = cue.model_copy(update={"start": start, "end": end})
        current_text = " ".join(word.text for word in cue.words).strip()
        if current_text != cue.text.strip() or not cue.words:
            updated = retime_cue(updated)
        else:
            words = [
                word.model_copy(
                    update={
                        "start": max(start, min(word.start, end)),
                        "end": max(start, min(word.end, end)),
                    }
                )
                for word in cue.words
            ]
            updated = updated.model_copy(update={"words": words})
        normalized.append(updated)
    return normalized


def escape_ass_text(text: str) -> str:
    return (
        text.replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\n", r"\N")
    )


def ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    centiseconds = int(round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    whole_seconds, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{fraction:02d}"


def cue_words(cue: SubtitleCue) -> list[SubtitleWord]:
    return sorted(cue.words or retime_cue(cue).words, key=lambda word: (word.start, word.end))


def cue_display_start(cue: SubtitleCue, words: list[SubtitleWord], clip_start: float) -> float:
    word_start = words[0].start if words else cue.start
    return max(clip_start, min(cue.start, word_start))


def cue_display_end(
    cue: SubtitleCue,
    words: list[SubtitleWord],
    clip_start: float,
    next_cue: SubtitleCue | None,
) -> float:
    display_start = cue_display_start(cue, words, clip_start)
    display_end = max(cue.end, *(word.end for word in words), display_start + MIN_ASS_EVENT_SECONDS)
    if next_cue is None:
        return display_end

    next_words = cue_words(next_cue)
    next_start = cue_display_start(next_cue, next_words, clip_start)
    if next_start <= display_start:
        return display_end
    if next_start <= display_end or next_start - display_end <= DISPLAY_BRIDGE_SECONDS:
        return max(next_start, display_start + MIN_ASS_EVENT_SECONDS)
    return display_end


def normalize_subtitle_margin_v(value: int | float) -> int:
    try:
        margin = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Altyazı konumu geçersiz.") from exc
    if margin < MIN_ASS_SUBTITLE_MARGIN_V or margin > MAX_ASS_SUBTITLE_MARGIN_V:
        raise ValueError(
            f"Altyazı konumu {MIN_ASS_SUBTITLE_MARGIN_V}px ile {MAX_ASS_SUBTITLE_MARGIN_V}px arasında olmalı."
        )
    return margin


def normalize_subtitle_font_family(value: str | None) -> str:
    font_family = (value or DEFAULT_ASS_SUBTITLE_FONT_FAMILY).strip()
    if font_family not in ASS_SUBTITLE_FONT_FAMILIES:
        allowed = ", ".join(sorted(ASS_SUBTITLE_FONT_FAMILIES))
        raise ValueError(f"Altyazi fontu gecersiz. Secenekler: {allowed}.")
    return font_family


def write_ass(
    cues: list[SubtitleCue],
    clip_start: float,
    destination: Path,
    subtitle_margin_v: int = DEFAULT_ASS_SUBTITLE_MARGIN_V,
    subtitle_font_family: str = DEFAULT_ASS_SUBTITLE_FONT_FAMILY,
) -> None:
    margin_v = normalize_subtitle_margin_v(subtitle_margin_v)
    font_family = normalize_subtitle_font_family(subtitle_font_family)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Shorts,{font_family},76,&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,-1,0,0,0,100,100,0,0,1,7,2,2,70,70,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    sorted_cues = sorted(cues, key=lambda value: value.start)
    for cue_index, cue in enumerate(sorted_cues):
        words = cue_words(cue)
        if not words:
            continue
        display_start = cue_display_start(cue, words, clip_start)
        display_end = cue_display_end(
            cue,
            words,
            clip_start,
            sorted_cues[cue_index + 1] if cue_index + 1 < len(sorted_cues) else None,
        )
        cursor = display_start
        for active_index, active_word in enumerate(words):
            if active_index > 0:
                cursor = max(cursor, min(active_word.start, display_end))
            if cursor >= display_end:
                break
            if active_index + 1 < len(words):
                next_start = min(max(words[active_index + 1].start, cursor), display_end)
            else:
                next_start = display_end
            if next_start <= cursor:
                next_start = min(display_end, cursor + MIN_ASS_EVENT_SECONDS)
            if next_start <= cursor:
                continue

            start = ass_time(cursor - clip_start)
            end = ass_time(next_start - clip_start)
            events.append(
                f"Dialogue: 0,{start},{end},Shorts,,0,0,0,,"
                f"{ass_caption_text(words, active_index)}"
            )
            cursor = next_start
    destination.write_text(header + "\n".join(events) + "\n", encoding="utf-8-sig")
