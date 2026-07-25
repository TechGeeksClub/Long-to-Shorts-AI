from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

from app.config import Settings
from app.scoring import Candidate


logger = logging.getLogger(__name__)

PREFERRED_MODELS = ("qwen2.5:7b", "llama3.1:8b", "llama3.2:3b")
ASSESSMENT_BATCH_SIZE = 20
SYSTEM_PROMPT = """You evaluate transcript-based short video candidates.
Return only valid JSON. Never invent word IDs. Evaluate meaning, context independence,
the opening hook, and whether the idea has a complete payoff. Reasons must be concise."""


@dataclass(frozen=True)
class SemanticAssessment:
    candidate_id: str
    topic: str
    hook_score: float
    standalone_score: float
    context_score: float
    payoff_score: float
    start_word_id: int | None
    end_word_id: int | None
    needs_previous_sentence: bool
    ending_is_complete: bool
    unresolved_references: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class CriticAssessment:
    candidate_id: str
    approved: bool
    needs_previous_sentence: bool
    ending_is_complete: bool
    unresolved_references: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class SemanticResult:
    assessments: dict[str, SemanticAssessment]
    model: str | None = None
    error: str | None = None

    @property
    def used_llm(self) -> bool:
        return bool(self.assessments)


@dataclass(frozen=True)
class CriticResult:
    assessments: dict[str, CriticAssessment]
    error: str | None = None


def assess_candidates(
    *,
    candidates: list[Candidate],
    segments: list[dict[str, Any]],
    settings: Settings,
) -> SemanticResult:
    if not settings.semantic_clip_enabled:
        return SemanticResult({}, error="Semantic clip selection is disabled.")
    if not candidates:
        return SemanticResult({}, error="No candidates were available for semantic analysis.")

    try:
        model = select_installed_model(settings)
        if model is None:
            return SemanticResult(
                {},
                error=(
                    "No compatible Ollama model is installed. "
                    f"Install `{settings.semantic_clip_model}` to enable semantic selection."
                ),
            )
        assessments: dict[str, SemanticAssessment] = {}
        errors: list[str] = []
        for offset in range(0, len(candidates), ASSESSMENT_BATCH_SIZE):
            batch = candidates[offset : offset + ASSESSMENT_BATCH_SIZE]
            try:
                prompt = build_assessment_prompt(batch, segments)
                payload = extract_json_object(
                    ollama_generate(settings=settings, model=model, prompt=prompt)
                )
                assessments.update(parse_semantic_assessments(payload, batch))
            except (
                OSError,
                TimeoutError,
                ValueError,
                json.JSONDecodeError,
                urllib.error.URLError,
                urllib.error.HTTPError,
            ) as exc:
                errors.append(str(exc) or exc.__class__.__name__)
                logger.warning("Semantic assessment batch failed: %s", exc)
        if not assessments:
            detail = errors[0] if errors else "Ollama returned no valid assessments."
            return SemanticResult({}, model=model, error=detail)
        return SemanticResult(
            assessments,
            model=model,
            error="; ".join(dict.fromkeys(errors)) or None,
        )
    except (
        OSError,
        TimeoutError,
        ValueError,
        json.JSONDecodeError,
        urllib.error.URLError,
        urllib.error.HTTPError,
    ) as exc:
        logger.warning("Semantic clip analysis unavailable: %s", exc)
        return SemanticResult({}, error=str(exc) or exc.__class__.__name__)


def criticize_candidates(
    *,
    candidates: list[Candidate],
    segments: list[dict[str, Any]],
    settings: Settings,
    model: str | None,
) -> CriticResult:
    if not candidates or model is None:
        return CriticResult({})
    try:
        prompt = build_critic_prompt(candidates, segments)
        payload = extract_json_object(
            ollama_generate(settings=settings, model=model, prompt=prompt)
        )
        return CriticResult(parse_critic_assessments(payload, candidates))
    except (
        OSError,
        TimeoutError,
        ValueError,
        json.JSONDecodeError,
        urllib.error.URLError,
        urllib.error.HTTPError,
    ) as exc:
        logger.warning("Semantic clip critic unavailable: %s", exc)
        return CriticResult({}, error=str(exc) or exc.__class__.__name__)


def select_installed_model(settings: Settings) -> str | None:
    endpoint = urljoin(settings.ollama_base_url.rstrip("/") + "/", "api/tags")
    request = urllib.request.Request(endpoint, method="GET")
    with urllib.request.urlopen(request, timeout=2) as response:
        body = json.loads(response.read().decode("utf-8"))

    installed = {
        str(model.get("name") or model.get("model") or "").strip()
        for model in body.get("models", [])
        if isinstance(model, dict)
    }
    candidates = dict.fromkeys((settings.semantic_clip_model, *PREFERRED_MODELS))
    return next((model for model in candidates if model in installed), None)


def ollama_generate(*, settings: Settings, model: str, prompt: str) -> str:
    endpoint = urljoin(settings.ollama_base_url.rstrip("/") + "/", "api/generate")
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(
            {
                "model": model,
                "prompt": prompt,
                "system": SYSTEM_PROMPT,
                "stream": False,
                "keep_alive": "15m",
                "format": "json",
                "options": {
                    "temperature": 0.15,
                    "top_p": 0.85,
                    "num_predict": 5000,
                },
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=settings.ollama_timeout_seconds) as response:
        body = json.loads(response.read().decode("utf-8"))
    generated = str(body.get("response") or "").strip()
    if not generated:
        raise ValueError("Ollama returned an empty response.")
    return generated


def build_assessment_prompt(
    candidates: list[Candidate],
    segments: list[dict[str, Any]],
) -> str:
    payload = [
        _candidate_prompt_payload(candidate, segments, context_seconds=20)
        for candidate in candidates
    ]
    return (
        "Evaluate every candidate below for a standalone short-form video. "
        "The transcript language may vary. Select start_word_id and end_word_id only "
        "from the candidate context. Expand to adjacent context when a reference, question, "
        "or conclusion requires it. Do not choose timestamps. hook_score measures opening "
        "strength, standalone_score measures whether the clip works by itself, context_score "
        "measures how complete and understandable its context is, and payoff_score measures "
        "whether it delivers a satisfying answer or conclusion.\n\n"
        "Return exactly this JSON shape:\n"
        '{"assessments":[{"candidate_id":"...","topic":"...",'
        '"hook_score":0,"standalone_score":0,"context_score":0,"payoff_score":0,'
        '"start_word_id":0,"end_word_id":0,"needs_previous_sentence":false,'
        '"ending_is_complete":true,"unresolved_references":[],"reason":"..."}]}\n\n'
        "All scores must be from 0 to 100. Include one result per candidate.\n\n"
        f"Candidates:\n{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
    )


def build_critic_prompt(
    candidates: list[Candidate],
    segments: list[dict[str, Any]],
) -> str:
    payload = [
        _candidate_prompt_payload(candidate, segments, context_seconds=12)
        for candidate in candidates
    ]
    return (
        "Act as a strict final clip critic. Check whether the first sentence depends on "
        "earlier context, whether pronouns or phrases such as 'as I said' are unresolved, "
        "and whether the final idea is complete. Reject clips that cannot stand alone.\n\n"
        "Return exactly this JSON shape:\n"
        '{"assessments":[{"candidate_id":"...","approved":true,'
        '"needs_previous_sentence":false,"ending_is_complete":true,'
        '"unresolved_references":[],"reason":"..."}]}\n\n'
        f"Candidates:\n{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
    )


def _candidate_prompt_payload(
    candidate: Candidate,
    segments: list[dict[str, Any]],
    *,
    context_seconds: float,
) -> dict[str, Any]:
    context_start = max(0.0, candidate.start - context_seconds)
    context_end = candidate.end + context_seconds
    context_segments = [
        segment
        for segment in segments
        if float(segment.get("end") or 0) >= context_start
        and float(segment.get("start") or 0) <= context_end
    ]
    context = []
    for segment in context_segments:
        words = segment.get("words") or []
        if not words:
            continue
        context.append(
            {
                "start_word_id": words[0].get("id"),
                "end_word_id": words[-1].get("id"),
                "sentence_id": words[0].get("sentence_id"),
                "speaker": words[0].get("speaker"),
                "text": segment.get("text") or " ".join(
                    str(word.get("text") or "") for word in words
                ),
                "words": [
                    {
                        "id": word.get("id"),
                        "text": word.get("text"),
                    }
                    for word in words
                ],
            }
        )
    candidate_word_ids = [
        int(word["id"])
        for word in candidate.words
        if isinstance(word.get("id"), int)
    ]
    return {
        "candidate_id": candidate.candidate_id,
        "heuristic_score": candidate.score,
        "candidate_start_word_id": min(candidate_word_ids) if candidate_word_ids else None,
        "candidate_end_word_id": max(candidate_word_ids) if candidate_word_ids else None,
        "candidate_text": candidate.text,
        "context": context,
    }


def extract_json_object(value: str) -> dict[str, Any]:
    cleaned = value.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```json").removeprefix("```").strip()
        cleaned = cleaned.removesuffix("```").strip()
    first = cleaned.find("{")
    last = cleaned.rfind("}")
    if first < 0 or last <= first:
        raise ValueError("No JSON object was found in the Ollama response.")
    payload = json.loads(cleaned[first : last + 1])
    if not isinstance(payload, dict):
        raise ValueError("The Ollama response was not a JSON object.")
    return payload


def parse_semantic_assessments(
    payload: dict[str, Any],
    candidates: list[Candidate],
) -> dict[str, SemanticAssessment]:
    known_ids = {candidate.candidate_id for candidate in candidates}
    results: dict[str, SemanticAssessment] = {}
    for item in payload.get("assessments") or []:
        if not isinstance(item, dict):
            continue
        candidate_id = str(item.get("candidate_id") or "")
        if candidate_id not in known_ids:
            continue
        results[candidate_id] = SemanticAssessment(
            candidate_id=candidate_id,
            topic=str(item.get("topic") or "").strip(),
            hook_score=_score(item.get("hook_score")),
            standalone_score=_score(item.get("standalone_score")),
            context_score=_score(item.get("context_score")),
            payoff_score=_score(item.get("payoff_score")),
            start_word_id=_integer_or_none(item.get("start_word_id")),
            end_word_id=_integer_or_none(item.get("end_word_id")),
            needs_previous_sentence=_boolean(item.get("needs_previous_sentence")),
            ending_is_complete=_boolean(item.get("ending_is_complete"), default=True),
            unresolved_references=tuple(_string_list(item.get("unresolved_references"))),
            reason=str(item.get("reason") or "").strip(),
        )
    return results


def parse_critic_assessments(
    payload: dict[str, Any],
    candidates: list[Candidate],
) -> dict[str, CriticAssessment]:
    known_ids = {candidate.candidate_id for candidate in candidates}
    results: dict[str, CriticAssessment] = {}
    for item in payload.get("assessments") or []:
        if not isinstance(item, dict):
            continue
        candidate_id = str(item.get("candidate_id") or "")
        if candidate_id not in known_ids:
            continue
        results[candidate_id] = CriticAssessment(
            candidate_id=candidate_id,
            approved=_boolean(item.get("approved")),
            needs_previous_sentence=_boolean(item.get("needs_previous_sentence")),
            ending_is_complete=_boolean(item.get("ending_is_complete"), default=True),
            unresolved_references=tuple(_string_list(item.get("unresolved_references"))),
            reason=str(item.get("reason") or "").strip(),
        )
    return results


def _score(value: Any) -> float:
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _integer_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _boolean(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
    if value is None:
        return default
    return bool(value)
