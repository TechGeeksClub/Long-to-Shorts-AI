from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from app import agent_selection
from app.codex_runner import CodexError
from app.config import Settings


def make_segments(count: int = 6, *, spacing: float = 5.0) -> list[dict[str, Any]]:
    sentences = [
        "Bu motosiklet uzun yolda rahat.",
        "Sele yüksekliği sürüşü doğrudan etkiliyor.",
        "Sonuçta yolculuk boyunca yorulmadım gerçekten.",
        "Yakıt tüketimini ayrıca ölçmek istedim.",
        "Şehir içinde tüketim biraz yükseldi.",
        "Uzun yolda ise tasarruf sağladı.",
    ]
    segments = []
    for index in range(count):
        text = sentences[index % len(sentences)]
        words = [
            {
                "text": token,
                "start": index * spacing + token_index * .85,
                "end": index * spacing + token_index * .85 + .65,
            }
            for token_index, token in enumerate(text.split())
        ]
        segments.append({"start": words[0]["start"], "end": words[-1]["end"],
                         "text": text, "words": words})
    return segments


def topic_map() -> dict[str, Any]:
    return {
        "summary": "Konuşma konfor ve yakıt tüketimini iki ayrı bölümde ele alıyor.",
        "topics": [
            {"title": "Uzun yol konforu", "summary": "Sele ve oturuşun uzun yol yorgunluğuna etkisi.",
             "start_unit_id": 0, "end_unit_id": 2},
            {"title": "Gerçek yakıt tüketimi", "summary": "Şehir içi ve uzun yol tüketiminin karşılaştırılması.",
             "start_unit_id": 3, "end_unit_id": 5},
        ],
    }


def clip_plan() -> dict[str, Any]:
    return {"clips": [
        {"topic_index": 0, "title": "Uzun yolda gerçekten rahat mı?",
         "reason": "Konfor iddiasını sele deneyimi ve tamamlanmış sonuçla açıklıyor.",
         "start_unit_id": 0, "end_unit_id": 2},
        {"topic_index": 1, "title": "Şehir ve uzun yol tüketimi",
         "reason": "Ölçümün amacı, iki kullanım koşulu ve sonuç aynı kesitte bulunuyor.",
         "start_unit_id": 3, "end_unit_id": 5},
    ]}


class ScriptedCodex:
    def __init__(self, responses: list[dict[str, Any] | Exception]):
        self.responses = list(responses)
        self.prompts: list[str] = []
        self.schemas: list[dict[str, Any]] = []

    def __call__(self, *, prompt: str, schema: dict[str, Any], **_: Any):
        self.prompts.append(prompt)
        self.schemas.append(schema)
        assert self.responses, "The engine made an unexpected additional Codex request."
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return deepcopy(response), "test-codex"


def select(tmp_path: Path, *, settings: Settings | None = None, segments=None):
    return agent_selection.select_agent_clips(
        job_id="job", segments=segments if segments is not None else make_segments(),
        duration=30, settings=settings or Settings(data_dir=tmp_path),
        work_dir=tmp_path / "analysis",
    )


def test_transcript_units_keep_every_word_and_do_not_mutate_input() -> None:
    segments = make_segments()
    before = deepcopy(segments)
    units = agent_selection.build_transcript_units(segments)
    repeated = agent_selection.build_transcript_units(segments)
    assert [unit.id for unit in units] == list(range(6))
    assert [(unit.start_word_id, unit.end_word_id) for unit in units] == [
        (index * 5, index * 5 + 4) for index in range(6)
    ]
    assert [unit.text for unit in units] == [segment["text"] for segment in segments]
    assert [(unit.start, unit.end) for unit in units] == [
        (segment["words"][0]["start"], segment["words"][-1]["end"]) for segment in segments
    ]
    assert units == repeated
    assert segments == before


def test_long_transcript_units_include_the_final_topic_without_truncation() -> None:
    segments = make_segments(1200)
    segments[-1]["words"][-1]["text"] = "SON_KONU_KAPANIŞI."
    segments[-1]["text"] = " ".join(word["text"] for word in segments[-1]["words"])
    units = agent_selection.build_transcript_units(segments)
    assert [word for unit in units for word in unit.text.split()] == [
        word["text"] for segment in segments for word in segment["words"]
    ]
    assert units[-1].end_word_id == 5999
    assert units[-1].text.endswith("SON_KONU_KAPANIŞI.")


def test_agent_selects_complete_topic_clips_with_source_derived_times(tmp_path: Path, monkeypatch) -> None:
    runner = ScriptedCodex([topic_map(), clip_plan()])
    monkeypatch.setattr(agent_selection, "run_codex_json", runner)
    result = select(tmp_path)
    source = make_segments()
    assert result.model == "test-codex"
    assert result.summary == topic_map()["summary"]
    assert [topic.title for topic in result.topics] == [item["title"] for item in topic_map()["topics"]]
    assert len(result.clips) == 2
    for index, clip in enumerate(result.clips):
        first, last = index * 3, index * 3 + 2
        assert clip.selection_method == "agent"
        assert clip.score == 0
        assert clip.topic_id == result.topics[index].id
        assert clip.title == clip_plan()["clips"][index]["title"]
        assert any(clip_plan()["clips"][index]["reason"] in reason for reason in clip.reasons)
        assert source[first]["start"] - .15 - .001 <= clip.start <= source[first]["start"]
        assert source[last]["end"] <= clip.end <= source[last]["end"] + .15 + .001
        subtitle_words = [word.text for cue in clip.subtitles for word in cue.words]
        assert subtitle_words == [word["text"] for segment in source[first:last + 1]
                                  for word in segment["words"]]
    assert len(runner.prompts) == 2
    assert not runner.responses
    for segment in source:
        assert any(segment["text"] in prompt for prompt in runner.prompts)


def test_identical_plans_create_stable_clip_and_topic_ids(tmp_path: Path, monkeypatch) -> None:
    runner = ScriptedCodex([topic_map(), clip_plan(), topic_map(), clip_plan()])
    monkeypatch.setattr(agent_selection, "run_codex_json", runner)
    first = select(tmp_path)
    second = select(tmp_path)
    assert [clip.id for clip in first.clips] == [clip.id for clip in second.clips]
    assert [topic.id for topic in first.topics] == [topic.id for topic in second.topics]


@pytest.mark.parametrize("invalid_topics", [
    [{"title": "Eksik", "summary": "Son cümleler unutulmuş.", "start_unit_id": 0, "end_unit_id": 2}],
    [{"title": "İlk", "summary": "İlk bölüm.", "start_unit_id": 0, "end_unit_id": 1},
     {"title": "Son", "summary": "Arada eksik cümle var.", "start_unit_id": 3, "end_unit_id": 5}],
    [{"title": "İlk", "summary": "İlk bölüm.", "start_unit_id": 0, "end_unit_id": 3},
     {"title": "Son", "summary": "Aynı cümle iki kez kapsanıyor.", "start_unit_id": 3, "end_unit_id": 5}],
    [{"title": "Uydurma", "summary": "Olmayan cümleye uzanıyor.", "start_unit_id": 0, "end_unit_id": 99}],
])
def test_invalid_topic_coverage_is_rejected_without_fabricated_clips(
    tmp_path: Path, monkeypatch, invalid_topics: list[dict[str, Any]],
) -> None:
    invalid = {"summary": "Geçersiz bölümleme.", "topics": invalid_topics}
    runner = ScriptedCodex([invalid, invalid])
    monkeypatch.setattr(agent_selection, "run_codex_json", runner)
    with pytest.raises(CodexError):
        select(tmp_path)
    assert len(runner.prompts) == 2


@pytest.mark.parametrize("bad_start,bad_end", [(0, 99), (1, 4), (4, 2)])
def test_unknown_cross_topic_or_reversed_clip_bounds_are_rejected(
    tmp_path: Path, monkeypatch, bad_start: int, bad_end: int,
) -> None:
    bad = clip_plan()
    bad["clips"][0].update(start_unit_id=bad_start, end_unit_id=bad_end)
    runner = ScriptedCodex([topic_map(), bad, bad])
    monkeypatch.setattr(agent_selection, "run_codex_json", runner)
    with pytest.raises(CodexError):
        select(tmp_path)
    assert len(runner.prompts) == 3


def test_invalid_plan_is_corrected_once_with_agent_feedback(tmp_path: Path, monkeypatch) -> None:
    bad = clip_plan()
    bad["clips"][0]["end_unit_id"] = 99
    runner = ScriptedCodex([topic_map(), bad, clip_plan()])
    monkeypatch.setattr(agent_selection, "run_codex_json", runner)
    result = select(tmp_path)
    assert len(result.clips) == 2
    assert len(runner.prompts) == 3
    assert runner.prompts[-1] != runner.prompts[-2]
    assert "failed validation" in runner.prompts[-1]
    assert "unknown" in runner.prompts[-1].casefold()


def chunked_script(*, first_chunk_has_clip: bool = True) -> list[dict[str, Any]]:
    topics = topic_map()
    choices = clip_plan()["clips"]
    return [
        {"summary": "İlk bölümde uzun yol konforu konuşuluyor.", "topics": [topics["topics"][0]]},
        {"summary": "Son bölümde tüketim karşılaştırılıyor.", "topics": [topics["topics"][1]]},
        topics,
        {"clips": [choices[0]] if first_chunk_has_clip else []},
        {"clips": [choices[1]]},
        {"selected_indices": [1, 0] if first_chunk_has_clip else [0]},
    ]


def test_chunked_transcript_is_fully_read_then_reviewed_by_global_editor(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(agent_selection, "_chunks", lambda units, limit: [units[:3], units[3:]])
    runner = ScriptedCodex(chunked_script())
    monkeypatch.setattr(agent_selection, "run_codex_json", runner)
    result = select(tmp_path)
    assert len(result.topics) == 2
    assert [clip.title for clip in result.clips] == [item["title"] for item in reversed(clip_plan()["clips"])]
    assert len(runner.prompts) == 6
    assert all(segment["text"] in runner.prompts[index // 3]
               for index, segment in enumerate(make_segments()))
    assert "Sonuçta yolculuk boyunca yorulmadım gerçekten." not in runner.prompts[1]
    assert "Uzun yolda ise tasarruf sağladı." in runner.prompts[1]
    assert not runner.responses


def test_filler_only_chunk_is_allowed_to_offer_no_clip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(agent_selection, "_chunks", lambda units, limit: [units[:3], units[3:]])
    runner = ScriptedCodex(chunked_script(first_chunk_has_clip=False))
    monkeypatch.setattr(agent_selection, "run_codex_json", runner)
    result = select(tmp_path)
    assert len(result.topics) == 2
    assert len(result.clips) == 1
    assert result.clips[0].topic_id == result.topics[1].id
    assert result.clips[0].title == clip_plan()["clips"][1]["title"]
    assert len(runner.prompts) == 6


def test_no_independent_clips_still_returns_the_complete_topic_map(tmp_path: Path, monkeypatch) -> None:
    runner = ScriptedCodex([topic_map(), {"clips": []}])
    monkeypatch.setattr(agent_selection, "run_codex_json", runner)
    result = select(tmp_path)
    assert result.clips == []
    assert len(result.topics) == 2
    assert result.summary == topic_map()["summary"]
    assert len(runner.prompts) == 2


def test_whole_short_speech_is_accepted_despite_trailing_silence(tmp_path: Path, monkeypatch) -> None:
    segments = make_segments(1)
    runner = ScriptedCodex([
        {"summary": "Kısa bir konfor deneyimi.", "topics": [{"title": "Konfor deneyimi",
         "summary": "Konuşmacının tek ve tamamlanmış görüşü.", "start_unit_id": 0, "end_unit_id": 0}]},
        {"clips": [{"topic_index": 0, "title": "Uzun yol konforu", "reason": "Tek bir tamamlanmış görüş içeriyor.",
                    "start_unit_id": 0, "end_unit_id": 0}]},
    ])
    monkeypatch.setattr(agent_selection, "run_codex_json", runner)
    result = agent_selection.select_agent_clips(job_id="short", segments=segments, duration=5,
        settings=Settings(data_dir=tmp_path, agent_min_clip_seconds=8), work_dir=tmp_path / "analysis")
    assert len(result.clips) == 1
    assert result.clips[0].start == 0
    assert segments[0]["end"] <= result.clips[0].end <= 5
    assert [word.text for cue in result.clips[0].subtitles for word in cue.words] == segments[0]["text"].split()


def test_long_clip_is_rejected_instead_of_cutting_its_conclusion(tmp_path: Path, monkeypatch) -> None:
    runner = ScriptedCodex([topic_map(), clip_plan(), clip_plan()])
    monkeypatch.setattr(agent_selection, "run_codex_json", runner)
    settings = Settings(data_dir=tmp_path, agent_min_clip_seconds=8, agent_max_clip_seconds=10)
    with pytest.raises(CodexError):
        select(tmp_path, settings=settings)


def test_codex_failure_does_not_fall_back_to_heuristic_scores(tmp_path: Path, monkeypatch) -> None:
    def forbidden_score(*args, **kwargs):
        raise AssertionError("The score-based selector must never replace a failed agent run.")

    monkeypatch.setattr("app.scoring.score_candidate_pool", forbidden_score)
    monkeypatch.setattr("app.hybrid_selection.select_hybrid_clips", forbidden_score)
    runner = ScriptedCodex([CodexError("Codex oturumu kullanılamıyor.")])
    monkeypatch.setattr(agent_selection, "run_codex_json", runner)
    with pytest.raises(CodexError, match="Codex"):
        select(tmp_path)
    assert len(runner.prompts) == 1


def test_empty_transcript_does_not_start_an_agent_request(tmp_path: Path, monkeypatch) -> None:
    runner = ScriptedCodex([])
    monkeypatch.setattr(agent_selection, "run_codex_json", runner)
    assert agent_selection.build_transcript_units([]) == []
    with pytest.raises(CodexError):
        select(tmp_path, segments=[])
    assert not runner.prompts
