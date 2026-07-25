from __future__ import annotations

from pathlib import Path

import numpy as np

from app.boundary_optimizer import optimize_candidate_boundaries
from app.clip_validator import passes_deterministic_critic
from app.config import Settings
from app.hybrid_selection import select_hybrid_clips
from app.scoring import Candidate
from app.semantic_analyzer import (
    CriticAssessment,
    CriticResult,
    SemanticAssessment,
    SemanticResult,
)
from app.transcript_units import annotate_transcript_segments, flatten_words


def make_segments(count: int = 30) -> list[dict]:
    segments = []
    vocabulary = [
        ["Bu", "sorunun", "temel", "nedeni", "nedir?"],
        ["Çünkü", "doğru", "yöntem", "sonucu", "değiştirir."],
        ["Örneğin", "küçük", "bir", "adım", "yeterlidir."],
    ]
    for index in range(count):
        start = index * 4.0
        tokens = vocabulary[index % len(vocabulary)]
        words = [
            {
                "text": token,
                "start": start + offset * 0.55,
                "end": start + offset * 0.55 + 0.42,
                "probability": 0.95,
            }
            for offset, token in enumerate(tokens)
        ]
        segments.append(
            {
                "start": start,
                "end": start + 3.2,
                "text": " ".join(tokens),
                "words": words,
                "avg_logprob": -0.1,
                "no_speech_prob": 0.01,
            }
        )
    return annotate_transcript_segments(segments)


def test_transcript_annotation_adds_stable_word_and_sentence_ids() -> None:
    segments = annotate_transcript_segments(
        [
            {
                "start": 0.0,
                "end": 1.5,
                "text": "İlk cümle bitti.",
                "speaker": "A",
                "words": [
                    {"text": "İlk", "start": 0.0, "end": 0.4},
                    {"text": "cümle", "start": 0.5, "end": 0.9},
                    {"text": "bitti.", "start": 1.0, "end": 1.4},
                ],
            },
            {
                "start": 2.5,
                "end": 3.4,
                "text": "Yeni cümle.",
                "speaker": "B",
                "words": [
                    {"text": "Yeni", "start": 2.5, "end": 2.8},
                    {"text": "cümle.", "start": 2.9, "end": 3.3},
                ],
            },
        ]
    )

    words = flatten_words(segments)
    assert [word["id"] for word in words] == list(range(5))
    assert [word["sentence_id"] for word in words] == [0, 0, 0, 1, 1]
    assert [word["speaker"] for word in words] == ["A", "A", "A", "B", "B"]


def test_boundary_optimizer_uses_word_ids_and_expands_previous_sentence() -> None:
    segments = make_segments(12)
    words = flatten_words(segments)
    candidate_words = words[10:45]
    candidate = Candidate(
        start=float(candidate_words[0]["start"]) - 0.15,
        end=float(candidate_words[-1]["end"]) + 0.2,
        text=" ".join(word["text"] for word in candidate_words),
        words=candidate_words,
        segments=segments[2:9],
        topic_tokens={"yöntem", "sonuç"},
    )
    assessment = SemanticAssessment(
        candidate_id=candidate.candidate_id,
        topic="Yöntemin etkisi",
        hook_score=80,
        standalone_score=90,
        context_score=88,
        payoff_score=85,
        start_word_id=int(candidate_words[5]["id"]),
        end_word_id=int(candidate_words[-5]["id"]),
        needs_previous_sentence=True,
        ending_is_complete=False,
        unresolved_references=("çünkü",),
        reason="Soru ve yanıt birlikte anlaşılır.",
    )

    optimized = optimize_candidate_boundaries(
        candidate=candidate,
        segments=segments,
        duration=48.0,
        min_seconds=30.0,
        max_seconds=60.0,
        semantic=assessment,
    )

    assert optimized.start <= candidate.start
    assert optimized.end >= candidate.end
    assert optimized.words[0]["id"] < assessment.start_word_id
    assert optimized.words[-1]["id"] > assessment.end_word_id


def test_deterministic_critic_rejects_dependent_or_incomplete_edges() -> None:
    base = Candidate(
        start=0,
        end=35,
        text="Bu yöntem tek başına anlaşılır ve net bir sonuç verir.",
        words=[],
        segments=[],
        topic_tokens={"yöntem", "sonuç"},
    )

    assert passes_deterministic_critic(base)
    assert not passes_deterministic_critic(
        Candidate(
            start=0,
            end=35,
            text="Bu nedenle önceki açıklamaya dönmemiz gerekir.",
            words=[],
            segments=[],
            topic_tokens=set(),
        )
    )
    assert not passes_deterministic_critic(
        Candidate(
            start=0,
            end=35,
            text="Asıl önemli nokta ise",
            words=[],
            segments=[],
            topic_tokens=set(),
        )
    )


def test_hybrid_selection_falls_back_when_ollama_is_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    segments = make_segments()
    settings = Settings(
        data_dir=tmp_path,
        candidate_count=4,
        semantic_candidate_count=12,
    )
    monkeypatch.setattr(
        "app.hybrid_selection.assess_candidates",
        lambda **_: SemanticResult({}, error="offline"),
    )

    clips = select_hybrid_clips(
        job_id="job",
        segments=segments,
        duration=120.0,
        rms_envelope=np.ones(240, dtype=np.float32),
        rms_bucket_seconds=0.5,
        settings=settings,
    )

    assert 1 <= len(clips) <= 4
    assert all(clip.selection_method == "heuristic" for clip in clips)


def test_hybrid_selection_combines_semantic_and_integrity_scores(
    tmp_path: Path,
    monkeypatch,
) -> None:
    segments = make_segments()
    settings = Settings(
        data_dir=tmp_path,
        candidate_count=3,
        semantic_candidate_count=10,
    )

    def fake_assess(*, candidates, **_):
        assessments = {}
        for candidate in candidates:
            word_ids = [word["id"] for word in candidate.words]
            assessments[candidate.candidate_id] = SemanticAssessment(
                candidate_id=candidate.candidate_id,
                topic="Tamamlanmış açıklama",
                hook_score=86,
                standalone_score=92,
                context_score=90,
                payoff_score=88,
                start_word_id=min(word_ids),
                end_word_id=max(word_ids),
                needs_previous_sentence=False,
                ending_is_complete=True,
                unresolved_references=(),
                reason="Bağımsız bir soru, açıklama ve sonuç içeriyor.",
            )
        return SemanticResult(assessments, model="test-model")

    def fake_critic(*, candidates, **_):
        return CriticResult(
            {
                candidate.candidate_id: CriticAssessment(
                    candidate_id=candidate.candidate_id,
                    approved=True,
                    needs_previous_sentence=False,
                    ending_is_complete=True,
                    unresolved_references=(),
                    reason="Klip tek başına anlaşılır.",
                )
                for candidate in candidates
            }
        )

    monkeypatch.setattr("app.hybrid_selection.assess_candidates", fake_assess)
    monkeypatch.setattr("app.hybrid_selection.criticize_candidates", fake_critic)

    clips = select_hybrid_clips(
        job_id="job",
        segments=segments,
        duration=120.0,
        rms_envelope=np.ones(240, dtype=np.float32),
        rms_bucket_seconds=0.5,
        settings=settings,
    )

    assert 1 <= len(clips) <= 3
    assert all(clip.selection_method == "hybrid" for clip in clips)
    assert all(clip.content_score >= 80 for clip in clips)
    assert all(clip.integrity_score >= 80 for clip in clips)
    assert all(30 <= clip.end - clip.start <= 60 for clip in clips)
