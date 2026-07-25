from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

import numpy as np

from app.boundary_optimizer import optimize_candidate_boundaries
from app.clip_validator import passes_deterministic_critic
from app.config import Settings
from app.models import ClipCandidate
from app.scoring import (
    Candidate,
    candidates_to_clips,
    score_candidate_pool,
    select_candidate_pool,
)
from app.semantic_analyzer import assess_candidates, criticize_candidates


logger = logging.getLogger(__name__)


def select_hybrid_clips(
    *,
    job_id: str,
    segments: list[dict[str, Any]],
    duration: float,
    rms_envelope: np.ndarray,
    rms_bucket_seconds: float,
    settings: Settings,
) -> list[ClipCandidate]:
    raw = score_candidate_pool(
        segments=segments,
        duration=duration,
        rms_envelope=rms_envelope,
        rms_bucket_seconds=rms_bucket_seconds,
        min_seconds=settings.min_clip_seconds,
        max_seconds=settings.max_clip_seconds,
    )
    if not raw:
        return []

    semantic_pool_size = max(
        settings.candidate_count,
        min(100, settings.semantic_candidate_count),
    )
    semantic_pool = sorted(raw, key=lambda candidate: candidate.score, reverse=True)[
        :semantic_pool_size
    ]
    semantic_result = assess_candidates(
        candidates=semantic_pool,
        segments=segments,
        settings=settings,
    )
    if not semantic_result.used_llm:
        logger.info(
            "Using heuristic clip selection fallback: %s",
            semantic_result.error or "semantic analysis unavailable",
        )
        selected = select_candidate_pool(
            raw,
            count=settings.candidate_count,
            duration=duration,
            min_seconds=settings.min_clip_seconds,
            max_seconds=settings.max_clip_seconds,
        )
        return candidates_to_clips(
            job_id=job_id,
            candidates=selected,
            segments=segments,
            selection_method="heuristic",
        )

    optimized: list[Candidate] = []
    for candidate in semantic_pool:
        assessment = semantic_result.assessments.get(candidate.candidate_id)
        if assessment is None:
            optimized.append(replace(candidate, score=round(candidate.score * 0.9, 1)))
            continue
        content_score = (
            assessment.hook_score * 0.40
            + assessment.payoff_score * 0.35
            + assessment.context_score * 0.25
        )
        integrity_score = (
            assessment.standalone_score * 0.50
            + assessment.context_score * 0.25
            + (100.0 if assessment.ending_is_complete else 20.0) * 0.25
        )
        hybrid_score = (
            candidate.score * 0.30
            + content_score * 0.35
            + integrity_score * 0.35
        )
        reasons = _semantic_reasons(assessment.reason, assessment.topic, integrity_score)
        scored = replace(
            candidate,
            score=round(max(0.0, min(100.0, hybrid_score)), 1),
            content_score=round(content_score, 1),
            integrity_score=round(integrity_score, 1),
            reasons=reasons,
        )
        optimized.append(
            optimize_candidate_boundaries(
                candidate=scored,
                segments=segments,
                duration=duration,
                min_seconds=settings.min_clip_seconds,
                max_seconds=settings.max_clip_seconds,
                semantic=assessment,
            )
        )

    critic_pool = select_candidate_pool(
        optimized,
        count=min(len(optimized), settings.candidate_count * 2),
        duration=duration,
        min_seconds=settings.min_clip_seconds,
        max_seconds=settings.max_clip_seconds,
    )
    critic_result = criticize_candidates(
        candidates=critic_pool,
        segments=segments,
        settings=settings,
        model=semantic_result.model,
    )
    validated: list[Candidate] = []
    for candidate in critic_pool:
        semantic = semantic_result.assessments.get(candidate.candidate_id)
        critic = critic_result.assessments.get(candidate.candidate_id)
        if critic is None:
            validated.append(candidate)
            continue

        adjusted = optimize_candidate_boundaries(
            candidate=candidate,
            segments=segments,
            duration=duration,
            min_seconds=settings.min_clip_seconds,
            max_seconds=settings.max_clip_seconds,
            semantic=semantic,
            critic=critic,
        )
        if critic.approved:
            validated.append(adjusted)
            continue
        if (
            critic.needs_previous_sentence
            or not critic.ending_is_complete
            or critic.unresolved_references
        ) and passes_deterministic_critic(adjusted):
            validated.append(
                replace(
                    adjusted,
                    score=round(max(0.0, adjusted.score - 6.0), 1),
                    integrity_score=round(max(0.0, adjusted.integrity_score - 8.0), 1),
                    reasons=_prepend_reason(adjusted.reasons, "Sınırları genişletildi"),
                )
            )

    if not validated:
        logger.info("The semantic critic rejected every clip; using the heuristic fallback.")
        validated = raw
        method = "heuristic"
    else:
        method = "hybrid"

    selected = select_candidate_pool(
        validated,
        count=settings.candidate_count,
        duration=duration,
        min_seconds=settings.min_clip_seconds,
        max_seconds=settings.max_clip_seconds,
    )
    return candidates_to_clips(
        job_id=job_id,
        candidates=selected,
        segments=segments,
        selection_method=method,
    )


def _semantic_reasons(reason: str, topic: str, integrity_score: float) -> list[str]:
    reasons: list[str] = []
    concise_reason = " ".join(reason.split())
    if concise_reason:
        reasons.append(concise_reason[:80])
    elif topic:
        reasons.append(topic[:80])
    if integrity_score >= 80:
        reasons.append("Bağımsız ve tamamlanmış anlatım")
    if topic and topic.casefold() not in {item.casefold() for item in reasons}:
        reasons.append(topic[:60])
    return reasons[:3] or ["Anlamsal değerlendirme"]


def _prepend_reason(reasons: list[str], reason: str) -> list[str]:
    return [reason, *[item for item in reasons if item != reason]][:3]
