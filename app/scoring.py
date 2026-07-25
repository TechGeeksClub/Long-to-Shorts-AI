from __future__ import annotations

import math
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.media import mean_rms
from app.models import ClipCandidate
from app.subtitles import build_subtitle_cues


WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
STOP_WORDS = {
    "acaba", "ama", "artık", "aslında", "aynı", "bana", "bazı", "ben", "bence",
    "beni", "benim", "bile", "bir", "biri", "birkaç", "biz", "bize", "bizim",
    "bu", "bunu", "bunun", "burada", "böyle", "çok", "da", "daha", "de", "diye",
    "en", "falan", "felan", "gibi", "hem", "her", "hiç", "için", "ile", "ise",
    "kadar", "ki", "kim", "mı", "mi", "mu", "mü", "nasıl", "ne", "neden", "niye",
    "o", "olan", "olarak", "oldu", "oluyor", "onu", "onun", "orada", "öyle",
    "sen", "seni", "senin", "şey", "şimdi", "şöyle", "tabii", "tamam", "var",
    "ve", "veya", "ya", "yani", "yok", "zaten",
}
FILLER_WORDS = {
    "abi", "aynen", "eee", "falan", "felan", "hani", "işte", "şey", "şimdi",
    "şöyle", "tabii", "ya", "yani",
}
TOPIC_PIVOTS = (
    "başka bir konu",
    "başka konu",
    "bu arada",
    "gelelim",
    "konumuza gelelim",
    "peki",
    "son olarak",
    "şimdi gelelim",
)
QUESTION_TERMS = {"kim", "ne", "neden", "niye", "nasıl", "nerede", "hangi"}
EXPLANATION_TERMS = {
    "çünkü", "demek", "dolayı", "nedeni", "sebebi", "şöyle", "yüzden",
}
CONCLUSION_TERMS = {
    "böylece", "demek", "kısaca", "sonuç", "sonuçta", "özetle", "yani",
}


@dataclass
class TopicBlock:
    segments: list[dict[str, Any]]
    start: float
    end: float
    tokens: set[str]


@dataclass
class Candidate:
    start: float
    end: float
    text: str
    words: list[dict[str, Any]]
    segments: list[dict[str, Any]]
    topic_tokens: set[str]
    candidate_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    score: float = 0.0
    content_score: float = 0.0
    integrity_score: float = 0.0
    reasons: list[str] = field(default_factory=list)


def _tokens(text: str, *, meaningful: bool = False) -> list[str]:
    tokens = [token.casefold() for token in WORD_RE.findall(text)]
    if meaningful:
        return [token for token in tokens if token not in STOP_WORDS and len(token) > 2]
    return tokens


def _similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / math.sqrt(len(left) * len(right))


def _segment_tokens(segments: list[dict[str, Any]]) -> set[str]:
    return set(_tokens(" ".join(segment["text"] for segment in segments), meaningful=True))


def _is_topic_pivot(text: str) -> bool:
    normalized = text.casefold().strip()
    return any(normalized.startswith(pivot) for pivot in TOPIC_PIVOTS)


def segment_topics(segments: list[dict[str, Any]]) -> list[TopicBlock]:
    """Split a transcript when pauses, discourse pivots, or vocabulary shifts indicate a new topic."""
    if not segments:
        return []

    boundaries = [0]
    current_start = 0
    for index in range(1, len(segments)):
        gap = float(segments[index]["start"]) - float(segments[index - 1]["end"])
        elapsed = float(segments[index]["start"]) - float(segments[current_start]["start"])
        previous = _segment_tokens(segments[max(current_start, index - 3) : index])
        following = _segment_tokens(segments[index : min(len(segments), index + 3)])
        vocabulary_shift = _similarity(previous, following) < 0.08
        pivot = _is_topic_pivot(segments[index]["text"])
        if (
            (gap >= 2.2 and elapsed >= 12)
            or (pivot and elapsed >= 18)
            or (vocabulary_shift and elapsed >= 28 and gap >= 0.65)
        ):
            boundaries.append(index)
            current_start = index
    boundaries.append(len(segments))

    blocks: list[TopicBlock] = []
    for left, right in zip(boundaries, boundaries[1:], strict=False):
        block_segments = segments[left:right]
        if not block_segments:
            continue
        blocks.append(
            TopicBlock(
                segments=block_segments,
                start=float(block_segments[0]["start"]),
                end=float(block_segments[-1]["end"]),
                tokens=_segment_tokens(block_segments),
            )
        )
    return blocks


def _candidate_from_segments(
    block_segments: list[dict[str, Any]],
    topic_tokens: set[str],
    duration: float,
) -> Candidate:
    start = max(0.0, float(block_segments[0]["start"]) - 0.15)
    end = min(duration, float(block_segments[-1]["end"]) + 0.2)
    return Candidate(
        start=start,
        end=end,
        text=" ".join(segment["text"] for segment in block_segments).strip(),
        words=[word for segment in block_segments for word in segment["words"]],
        segments=block_segments,
        topic_tokens=topic_tokens,
    )


def _build_topic_candidates(
    segments: list[dict[str, Any]],
    duration: float,
    min_seconds: float,
    max_seconds: float,
) -> list[Candidate]:
    topics = segment_topics(segments)
    candidates: list[Candidate] = []

    for topic_index, topic in enumerate(topics):
        topic_duration = topic.end - topic.start
        if min_seconds <= topic_duration <= max_seconds:
            candidates.append(_candidate_from_segments(topic.segments, topic.tokens, duration))
            continue

        if topic_duration > max_seconds:
            for start_index, first in enumerate(topic.segments):
                collected: list[dict[str, Any]] = []
                start = float(first["start"])
                for segment in topic.segments[start_index:]:
                    proposed_duration = float(segment["end"]) - start
                    if proposed_duration > max_seconds:
                        break
                    collected.append(segment)
                    if proposed_duration >= min_seconds:
                        candidates.append(
                            _candidate_from_segments(collected, topic.tokens, duration)
                        )
                        if proposed_duration >= max_seconds - 5:
                            break
            continue

        # A short topic is expanded with the closest neighboring topic so the clip
        # remains understandable while meeting the requested duration.
        combined = list(topic.segments)
        left = topic_index - 1
        right = topic_index + 1
        while (
            float(combined[-1]["end"]) - float(combined[0]["start"]) < min_seconds
            and (left >= 0 or right < len(topics))
        ):
            left_similarity = _similarity(topic.tokens, topics[left].tokens) if left >= 0 else -1
            right_similarity = (
                _similarity(topic.tokens, topics[right].tokens) if right < len(topics) else -1
            )
            if right_similarity >= left_similarity and right < len(topics):
                combined.extend(topics[right].segments)
                right += 1
            elif left >= 0:
                combined = topics[left].segments + combined
                left -= 1
            if float(combined[-1]["end"]) - float(combined[0]["start"]) > max_seconds:
                break
        combined_duration = float(combined[-1]["end"]) - float(combined[0]["start"])
        if min_seconds <= combined_duration <= max_seconds:
            candidates.append(
                _candidate_from_segments(combined, _segment_tokens(combined), duration)
            )

    if not candidates and segments:
        candidates.append(_candidate_from_segments(segments, _segment_tokens(segments), duration))
    return candidates


def _overlap_ratio(left: Candidate, right: Candidate) -> float:
    intersection = max(0.0, min(left.end, right.end) - max(left.start, right.start))
    return intersection / max(0.001, min(left.end - left.start, right.end - right.start))


def _candidate_tokens(candidate: Candidate) -> set[str]:
    return set(_tokens(candidate.text, meaningful=True))


def _candidate_similarity(left: Candidate, right: Candidate) -> float:
    return max(
        _similarity(left.topic_tokens, right.topic_tokens),
        _similarity(_candidate_tokens(left), _candidate_tokens(right)),
    )


def _center_gap(left: Candidate, right: Candidate) -> float:
    left_center = (left.start + left.end) / 2
    right_center = (right.start + right.end) / 2
    return abs(left_center - right_center)


def _nearest_center_gap(candidate: Candidate, selected: list[Candidate], duration: float) -> float:
    if not selected:
        return duration
    return min(_center_gap(candidate, existing) for existing in selected)


def _max_selected_similarity(candidate: Candidate, selected: list[Candidate]) -> float:
    if not selected:
        return 0.0
    return max(_candidate_similarity(candidate, existing) for existing in selected)


def _candidate_fits_diversity_pass(
    candidate: Candidate,
    selected: list[Candidate],
    *,
    duration: float,
    similarity_threshold: float,
    minimum_center_gap: float,
) -> bool:
    return all(
        _overlap_ratio(candidate, existing) <= 0.08
        and _candidate_similarity(candidate, existing) <= similarity_threshold
        and _center_gap(candidate, existing) >= minimum_center_gap
        for existing in selected
    ) and _nearest_center_gap(candidate, selected, duration) >= minimum_center_gap


def _diverse_selection_value(
    candidate: Candidate,
    selected: list[Candidate],
    *,
    duration: float,
    target_gap: float,
    use_gap_bonus: bool = True,
) -> float:
    similarity_penalty = _max_selected_similarity(candidate, selected) * 35
    gap = _nearest_center_gap(candidate, selected, duration)
    gap_bonus = min(1.0, gap / max(1.0, target_gap)) * 4 if use_gap_bonus else 0.0
    return candidate.score - similarity_penalty + gap_bonus


def _select_diverse_candidates(
    candidates: list[Candidate],
    *,
    count: int,
    duration: float,
    min_seconds: float,
    max_seconds: float,
) -> list[Candidate]:
    ranked = sorted(candidates, key=lambda value: value.score, reverse=True)
    selected: list[Candidate] = []
    target_gap = max(max_seconds * 1.5, min(max_seconds * 4, duration / max(1, count) * 0.75))
    diversity_passes = (
        (0.42, target_gap),
        (0.50, target_gap * 0.66),
        (0.62, target_gap * 0.40),
        (0.78, min_seconds * 0.65),
        (1.01, 0.0),
    )

    for similarity_threshold, minimum_center_gap in diversity_passes:
        made_progress = True
        while len(selected) < count and made_progress:
            made_progress = False
            best: Candidate | None = None
            best_value = -math.inf
            for candidate in ranked:
                if candidate in selected or not _candidate_fits_diversity_pass(
                    candidate,
                    selected,
                    duration=duration,
                    similarity_threshold=similarity_threshold,
                    minimum_center_gap=minimum_center_gap,
                ):
                    continue
                value = _diverse_selection_value(
                    candidate,
                    selected,
                    duration=duration,
                    target_gap=target_gap,
                    use_gap_bonus=minimum_center_gap > 0,
                )
                if value > best_value:
                    best = candidate
                    best_value = value
            if best is not None:
                selected.append(best)
                made_progress = True
    return sorted(selected, key=lambda value: value.score, reverse=True)


def _topic_coherence(candidate: Candidate) -> float:
    if len(candidate.segments) < 3:
        return 1.0
    third = max(1, len(candidate.segments) // 3)
    chunks = [
        _segment_tokens(candidate.segments[:third]),
        _segment_tokens(candidate.segments[third : third * 2]),
        _segment_tokens(candidate.segments[third * 2 :]),
    ]
    similarities = [
        _similarity(left, right)
        for left, right in zip(chunks, chunks[1:], strict=False)
        if left and right
    ]
    return min(1.0, (sum(similarities) / len(similarities)) * 2.5) if similarities else 0.5


def _title_for(candidate: Candidate, rank: int) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", candidate.text)
    best = max(
        sentences,
        key=lambda sentence: (
            len(set(_tokens(sentence, meaningful=True))),
            -len(_tokens(sentence)),
        ),
        default="",
    ).strip()
    words = best.split()[:10]
    title = " ".join(words) or f"Klip {rank}"
    if len(best.split()) > 10:
        title += "…"
    return title


def score_candidate_pool(
    *,
    segments: list[dict[str, Any]],
    duration: float,
    rms_envelope: np.ndarray,
    rms_bucket_seconds: float,
    min_seconds: float = 30.0,
    max_seconds: float = 60.0,
) -> list[Candidate]:
    raw = _build_topic_candidates(segments, duration, min_seconds, max_seconds)
    if not raw:
        return []

    document_frequency: Counter[str] = Counter()
    token_sets: list[set[str]] = []
    for candidate in raw:
        token_set = set(_tokens(candidate.text, meaningful=True))
        token_sets.append(token_set)
        document_frequency.update(token_set)

    max_density = max(
        len(candidate.words) / max(1.0, candidate.end - candidate.start) for candidate in raw
    )
    for index, candidate in enumerate(raw):
        candidate_duration = candidate.end - candidate.start
        all_tokens = _tokens(candidate.text)
        meaningful_tokens = _tokens(candidate.text, meaningful=True)
        density = (
            len(candidate.words)
            / max(1.0, candidate_duration)
            / max(0.01, max_density)
        )
        coherence = _topic_coherence(candidate)
        filler_ratio = sum(token in FILLER_WORDS for token in all_tokens) / max(1, len(all_tokens))
        opening_tokens = set(_tokens(" ".join(candidate.text.split()[:35])))
        question = "?" in candidate.text or bool(opening_tokens & QUESTION_TERMS)
        token_set = set(all_tokens)
        explanation = bool(token_set & EXPLANATION_TERMS)
        conclusion = bool(token_set & CONCLUSION_TERMS)
        narrative = min(
            1.0,
            (0.45 if question else 0)
            + (0.35 if explanation else 0)
            + (0.2 if conclusion else 0),
        )
        lexical = len(token_sets[index]) / max(1, len(meaningful_tokens))
        rarity = (
            sum(
                math.log((len(raw) + 1) / (document_frequency[token] + 1)) + 1
                for token in token_sets[index]
            )
            / max(1, len(token_sets[index]))
            / 2
        )
        energy = mean_rms(
            rms_envelope,
            rms_bucket_seconds,
            candidate.start,
            candidate.end,
        )
        gaps = [
            right["start"] - left["end"]
            for left, right in zip(candidate.words, candidate.words[1:], strict=False)
        ]
        silence_penalty = min(
            0.4,
            sum(gap for gap in gaps if gap > 1.2) / max(1.0, candidate_duration),
        )
        clean_start = (
            1.0
            if candidate.words and candidate.words[0]["start"] - candidate.start < 1.0
            else 0.4
        )
        clean_end = 1.0 if candidate.text.rstrip().endswith((".", "!", "?", "…")) else 0.65
        boundary = (clean_start + clean_end) / 2

        score = (
            coherence * 27
            + narrative * 20
            + density * 13
            + lexical * 8
            + min(1.0, rarity) * 8
            + energy * 9
            + boundary * 15
            - filler_ratio * 40
            - silence_penalty * 25
        )
        content_score = (
            narrative * 28
            + density * 18
            + lexical * 12
            + min(1.0, rarity) * 12
            + energy * 15
            + coherence * 15
            - filler_ratio * 30
        )
        integrity_score = (
            coherence * 45
            + boundary * 40
            + (1 - min(1.0, silence_penalty / 0.4)) * 15
        )
        reasons: list[str] = []
        if coherence >= 0.62:
            reasons.append("Konu bütünlüğü")
        if question and explanation:
            reasons.append("Soru-cevap akışı")
        elif explanation:
            reasons.append("Açıklayıcı anlatım")
        if conclusion:
            reasons.append("Sonuç içeren bölüm")
        if boundary >= 0.9:
            reasons.append("Temiz başlangıç ve bitiş")
        if energy >= 0.6 and len(reasons) < 3:
            reasons.append("Canlı anlatım")
        if not reasons:
            reasons.append("Tutarlı konu akışı")
        candidate.score = round(max(0.0, min(100.0, score)), 1)
        candidate.content_score = round(max(0.0, min(100.0, content_score)), 1)
        candidate.integrity_score = round(max(0.0, min(100.0, integrity_score)), 1)
        candidate.reasons = reasons[:3]
    return raw


def select_candidate_pool(
    candidates: list[Candidate],
    *,
    count: int,
    duration: float,
    min_seconds: float,
    max_seconds: float,
) -> list[Candidate]:
    return _select_diverse_candidates(
        candidates,
        count=count,
        duration=duration,
        min_seconds=min_seconds,
        max_seconds=max_seconds,
    )


def candidates_to_clips(
    *,
    job_id: str,
    candidates: list[Candidate],
    segments: list[dict[str, Any]],
    selection_method: str = "heuristic",
) -> list[ClipCandidate]:
    result: list[ClipCandidate] = []
    for rank, candidate in enumerate(candidates, start=1):
        result.append(
            ClipCandidate(
                id=str(uuid.uuid4()),
                job_id=job_id,
                rank=rank,
                title=_title_for(candidate, rank),
                start=round(candidate.start, 3),
                end=round(candidate.end, 3),
                score=candidate.score,
                reasons=candidate.reasons,
                subtitles=build_subtitle_cues(segments, candidate.start, candidate.end),
                content_score=candidate.content_score,
                integrity_score=candidate.integrity_score,
                selection_method=selection_method,
                selected=True,
            )
        )
    return result


def score_candidates(
    *,
    job_id: str,
    segments: list[dict[str, Any]],
    duration: float,
    rms_envelope: np.ndarray,
    rms_bucket_seconds: float,
    min_seconds: float = 30.0,
    max_seconds: float = 60.0,
    count: int = 10,
) -> list[ClipCandidate]:
    raw = score_candidate_pool(
        segments=segments,
        duration=duration,
        rms_envelope=rms_envelope,
        rms_bucket_seconds=rms_bucket_seconds,
        min_seconds=min_seconds,
        max_seconds=max_seconds,
    )
    selected = select_candidate_pool(
        raw,
        count=count,
        duration=duration,
        min_seconds=min_seconds,
        max_seconds=max_seconds,
    )
    return candidates_to_clips(
        job_id=job_id,
        candidates=selected,
        segments=segments,
    )
