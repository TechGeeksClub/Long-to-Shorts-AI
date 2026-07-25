from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any


SENTENCE_END = re.compile(r"[.!?…][\"')\]]*$")
SENTENCE_PAUSE_SECONDS = 0.8


def annotate_transcript_segments(
    segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return transcript segments with deterministic word and sentence identifiers."""
    annotated: list[dict[str, Any]] = []
    next_word_id = 0
    sentence_id = 0
    sentence_is_open = False
    previous_end: float | None = None

    for source_segment in segments:
        segment = dict(source_segment)
        source_words = list(source_segment.get("words") or [])
        segment_start = float(source_segment.get("start") or 0.0)
        gap = segment_start - previous_end if previous_end is not None else 0.0
        if sentence_is_open and gap >= SENTENCE_PAUSE_SECONDS:
            sentence_id += 1
            sentence_is_open = False

        words: list[dict[str, Any]] = []
        first_sentence_id = sentence_id
        for source_word in source_words:
            word = dict(source_word)
            word["id"] = next_word_id
            word["sentence_id"] = sentence_id
            word["speaker"] = source_word.get("speaker", source_segment.get("speaker"))
            words.append(word)
            next_word_id += 1
            sentence_is_open = True
            if is_sentence_end(str(word.get("text") or "")):
                sentence_id += 1
                sentence_is_open = False

        segment_text = str(source_segment.get("text") or "")
        if words and sentence_is_open and is_sentence_end(segment_text):
            sentence_id += 1
            sentence_is_open = False

        segment["words"] = words
        segment["sentence_id"] = first_sentence_id
        segment["speaker"] = source_segment.get("speaker")
        annotated.append(segment)
        previous_end = float(source_segment.get("end") or segment_start)

    return annotated


def flatten_words(segments: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [word for segment in segments for word in segment.get("words") or []]


def words_by_id(segments: Iterable[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {
        int(word["id"]): word
        for word in flatten_words(segments)
        if isinstance(word.get("id"), int)
    }


def is_sentence_end(text: str) -> bool:
    return bool(SENTENCE_END.search(text.strip()))


def sentence_word_groups(
    segments: Iterable[dict[str, Any]],
) -> dict[int, list[dict[str, Any]]]:
    groups: dict[int, list[dict[str, Any]]] = {}
    for word in flatten_words(segments):
        sentence_id = word.get("sentence_id")
        if isinstance(sentence_id, int):
            groups.setdefault(sentence_id, []).append(word)
    return groups

