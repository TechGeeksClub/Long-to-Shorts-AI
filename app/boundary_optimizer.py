from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.scoring import Candidate
from app.semantic_analyzer import CriticAssessment, SemanticAssessment
from app.transcript_units import flatten_words, sentence_word_groups, words_by_id


START_PADDING_SECONDS = 0.15
END_PADDING_SECONDS = 0.28
NATURAL_PAUSE_SECONDS = 0.3


def optimize_candidate_boundaries(
    *,
    candidate: Candidate,
    segments: list[dict[str, Any]],
    duration: float,
    min_seconds: float,
    max_seconds: float,
    semantic: SemanticAssessment | None = None,
    critic: CriticAssessment | None = None,
) -> Candidate:
    all_words = flatten_words(segments)
    word_lookup = words_by_id(segments)
    if not all_words or not word_lookup:
        return candidate

    candidate_ids = [
        int(word["id"])
        for word in candidate.words
        if isinstance(word.get("id"), int)
    ]
    if not candidate_ids:
        return candidate

    start_id = semantic.start_word_id if semantic else None
    end_id = semantic.end_word_id if semantic else None
    if (
        start_id not in word_lookup
        or float(word_lookup[start_id].get("start") or 0) < candidate.start - 30
        or float(word_lookup[start_id].get("start") or 0) > candidate.end + 30
    ):
        start_id = min(candidate_ids)
    if (
        end_id not in word_lookup
        or float(word_lookup[end_id].get("end") or 0) < candidate.start - 30
        or float(word_lookup[end_id].get("end") or 0) > candidate.end + 30
    ):
        end_id = max(candidate_ids)
    if start_id > end_id:
        start_id, end_id = min(candidate_ids), max(candidate_ids)

    sentence_groups = sentence_word_groups(segments)
    needs_previous = bool(
        (semantic and (semantic.needs_previous_sentence or semantic.unresolved_references))
        or (critic and (critic.needs_previous_sentence or critic.unresolved_references))
    )
    ending_is_complete = bool(
        (semantic is None or semantic.ending_is_complete)
        and (critic is None or critic.ending_is_complete)
    )

    start_word = word_lookup[start_id]
    end_word = word_lookup[end_id]
    if needs_previous:
        sentence_id = start_word.get("sentence_id")
        if isinstance(sentence_id, int) and sentence_id - 1 in sentence_groups:
            start_word = sentence_groups[sentence_id - 1][0]
            start_id = int(start_word["id"])
    if not ending_is_complete:
        sentence_id = end_word.get("sentence_id")
        if isinstance(sentence_id, int) and sentence_id + 1 in sentence_groups:
            end_word = sentence_groups[sentence_id + 1][-1]
            end_id = int(end_word["id"])

    index_by_id = {
        int(word["id"]): index
        for index, word in enumerate(all_words)
        if isinstance(word.get("id"), int)
    }
    first_index = index_by_id[start_id]
    last_index = index_by_id[end_id]
    start = _snap_start(all_words, first_index)
    end = _snap_end(all_words, last_index, duration)

    if end - start < min_seconds:
        start, end = _expand_to_minimum(
            start=start,
            end=end,
            original_start=candidate.start,
            original_end=candidate.end,
            duration=duration,
            min_seconds=min_seconds,
        )
    if end - start > max_seconds:
        start, end = candidate.start, candidate.end

    selected_words = [
        word
        for word in all_words
        if float(word.get("end") or 0) >= start and float(word.get("start") or 0) <= end
    ]
    selected_segments = [
        segment
        for segment in segments
        if float(segment.get("end") or 0) >= start
        and float(segment.get("start") or 0) <= end
    ]
    if not selected_words or not selected_segments:
        return candidate

    text = " ".join(
        str(segment.get("text") or "").strip()
        for segment in selected_segments
        if str(segment.get("text") or "").strip()
    )
    return replace(
        candidate,
        start=round(max(0.0, start), 3),
        end=round(min(duration, end), 3),
        text=text or candidate.text,
        words=selected_words,
        segments=selected_segments,
    )


def _snap_start(words: list[dict[str, Any]], index: int) -> float:
    word_start = float(words[index].get("start") or 0)
    if index == 0:
        return max(0.0, word_start - START_PADDING_SECONDS)
    previous_end = float(words[index - 1].get("end") or word_start)
    gap = max(0.0, word_start - previous_end)
    padding = min(START_PADDING_SECONDS, gap * 0.45) if gap >= NATURAL_PAUSE_SECONDS else 0.1
    return max(0.0, word_start - padding)


def _snap_end(words: list[dict[str, Any]], index: int, duration: float) -> float:
    word_end = float(words[index].get("end") or 0)
    if index + 1 >= len(words):
        return min(duration, word_end + END_PADDING_SECONDS)
    next_start = float(words[index + 1].get("start") or word_end)
    gap = max(0.0, next_start - word_end)
    padding = min(END_PADDING_SECONDS, gap * 0.45) if gap >= NATURAL_PAUSE_SECONDS else 0.2
    return min(duration, word_end + padding)


def _expand_to_minimum(
    *,
    start: float,
    end: float,
    original_start: float,
    original_end: float,
    duration: float,
    min_seconds: float,
) -> tuple[float, float]:
    start = min(start, original_start)
    end = max(end, original_end)
    missing = max(0.0, min_seconds - (end - start))
    start = max(0.0, start - missing / 2)
    end = min(duration, end + missing / 2)
    missing = max(0.0, min_seconds - (end - start))
    if missing:
        if start <= 0:
            end = min(duration, end + missing)
        else:
            start = max(0.0, start - missing)
    return start, end
