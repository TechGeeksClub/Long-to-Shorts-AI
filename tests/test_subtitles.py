from pathlib import Path

from app.models import CutRange, SubtitleCue, SubtitleWord
from app.subtitles import (
    DEFAULT_ASS_SUBTITLE_MARGIN_V,
    apply_cut_ranges_to_subtitles,
    build_speech_gap_cut_ranges,
    build_subtitle_cues,
    escape_ass_text,
    normalize_cues,
    retime_cues_from_transcript,
    write_ass,
)


def dialogue_times(content: str) -> list[tuple[str, str]]:
    times = []
    for line in content.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line.split(",", 9)
        times.append((parts[1], parts[2]))
    return times


def make_segment_words(texts: list[str]) -> list[dict[str, float | str]]:
    return [
        {"text": text, "start": index * 0.45, "end": index * 0.45 + 0.35}
        for index, text in enumerate(texts)
    ]


def test_build_subtitles_respects_sentence_boundaries() -> None:
    words = make_segment_words(
        [
            "bir",
            "de.",
            "Sen",
            "ne",
            "yapıyorsun",
            "yani?",
            "Yağmur",
            "yağdığını",
            "ben",
            "bilmiyordum.",
            "Bu",
            "yolu",
            "bilmekle",
            "ilgisi",
            "yok",
            "ki.",
        ]
    )

    cues = build_subtitle_cues(
        [{"start": 0.0, "end": 8.0, "text": "", "words": words}],
        0.0,
        8.0,
    )

    assert [cue.text for cue in cues] == [
        "bir de.",
        "Sen ne yapıyorsun yani?",
        "Yağmur yağdığını ben bilmiyordum.",
        "Bu yolu bilmekle ilgisi yok ki.",
    ]


def test_build_subtitles_splits_long_sentence_by_screen_width() -> None:
    words = make_segment_words(
        [
            "geçen",
            "şeyi",
            "gördüm,",
            "bir",
            "tane",
            "yabancı",
            "virajı",
            "7-5'de",
            "giriyor",
            "şu",
            "virajlara.",
        ]
    )

    cues = build_subtitle_cues(
        [{"start": 0.0, "end": 8.0, "text": "", "words": words}],
        0.0,
        8.0,
    )

    assert [cue.text for cue in cues] == [
        "geçen şeyi gördüm, bir tane yabancı virajı",
        "7-5'de giriyor şu virajlara.",
    ]


def test_changed_text_is_retimed_evenly() -> None:
    cue = SubtitleCue(
        id="cue",
        start=10.0,
        end=12.0,
        text="Türkçe altyazı düzgün çalışır",
        words=[],
    )
    normalized = normalize_cues([cue], 10.0, 15.0)[0]

    assert [word.text for word in normalized.words] == [
        "Türkçe",
        "altyazı",
        "düzgün",
        "çalışır",
    ]
    assert normalized.words[0].start == 10.0
    assert normalized.words[-1].end == 12.0


def test_cut_ranges_remove_words_and_shift_subtitles() -> None:
    cue = SubtitleCue(
        id="cue",
        start=0.0,
        end=4.0,
        text="bir iki üç dört",
        words=[
            SubtitleWord(text="bir", start=0.0, end=0.4),
            SubtitleWord(text="iki", start=1.0, end=1.4),
            SubtitleWord(text="üç", start=2.0, end=2.4),
            SubtitleWord(text="dört", start=3.0, end=3.4),
        ],
    )

    shifted = apply_cut_ranges_to_subtitles(
        [cue],
        0.0,
        4.0,
        [CutRange(start=1.0, end=2.0)],
    )

    assert len(shifted) == 1
    assert shifted[0].text == "bir üç dört"
    assert [(word.text, word.start, word.end) for word in shifted[0].words] == [
        ("bir", 0.0, 0.4),
        ("üç", 1.0, 1.4),
        ("dört", 2.0, 2.4),
    ]


def test_build_speech_gap_cut_ranges_uses_transcript_words_not_noise() -> None:
    segments = [
        {
            "start": 0.0,
            "end": 6.0,
            "text": "motor sesi merhaba devam",
            "words": [
                {"text": "merhaba", "start": 1.0, "end": 1.4},
                {"text": "devam", "start": 5.0, "end": 5.4},
            ],
        },
        {
            "start": 2.0,
            "end": 4.0,
            "text": "[noise]",
            "words": [],
        },
    ]

    cuts = build_speech_gap_cut_ranges(segments, 0.0, 6.0)

    assert cuts == [
        CutRange(start=1.6, end=4.8),
    ]


def test_changed_text_with_same_word_count_preserves_word_timing() -> None:
    cue = SubtitleCue(
        id="cue",
        start=10.0,
        end=12.0,
        text="Merhaba dünya!",
        words=[
            SubtitleWord(text="merhaba", start=10.0, end=10.7),
            SubtitleWord(text="dunya", start=10.8, end=11.4),
        ],
    )

    normalized = normalize_cues([cue], 10.0, 12.0)[0]

    assert [word.text for word in normalized.words] == ["Merhaba", "dünya!"]
    assert [(word.start, word.end) for word in normalized.words] == [
        (10.0, 10.7),
        (10.8, 11.4),
    ]


def test_retime_from_transcript_preserves_edited_text_and_grouping() -> None:
    segments = [
        {
            "start": 10.0,
            "end": 13.0,
            "text": "merhaba dunya nasilsin",
            "words": [
                {"text": "merhaba", "start": 10.0, "end": 10.5},
                {"text": "dunya", "start": 10.6, "end": 11.0},
                {"text": "nasilsin", "start": 11.2, "end": 12.0},
            ],
        }
    ]
    edited = SubtitleCue(
        id="edited",
        start=20.0,
        end=24.0,
        text="Merhaba dünya nasılsın?",
        words=[
            SubtitleWord(text="Merhaba", start=20.0, end=21.0),
            SubtitleWord(text="dünya", start=21.0, end=22.0),
            SubtitleWord(text="nasılsın?", start=22.0, end=24.0),
        ],
    )

    retimed = retime_cues_from_transcript([edited], segments, 10.0, 20.0)

    assert len(retimed) == 1
    assert retimed[0].id == "edited"
    assert retimed[0].text == "Merhaba dünya nasılsın?"
    assert [word.text for word in retimed[0].words] == ["Merhaba", "dünya", "nasılsın?"]
    assert [(word.start, word.end) for word in retimed[0].words] == [
        (10.0, 10.5),
        (10.6, 11.0),
        (11.2, 12.0),
    ]


def test_manual_subtitle_timing_is_not_retimed_from_transcript() -> None:
    segments = [
        {
            "start": 10.0,
            "end": 11.0,
            "text": "merhaba",
            "words": [{"text": "merhaba", "start": 10.0, "end": 10.5}],
        }
    ]
    cue = SubtitleCue(
        id="manual-cue",
        start=12.0,
        end=13.0,
        text="merhaba",
        words=[SubtitleWord(text="merhaba", start=12.0, end=13.0)],
    )

    retimed = retime_cues_from_transcript([cue], segments, 10.0, 15.0)

    assert retimed[0].start == 12.0
    assert retimed[0].end == 13.0
    assert retimed[0].words[0].start == 12.0


def test_retime_from_transcript_handles_small_text_differences() -> None:
    segments = [
        {
            "start": 10.0,
            "end": 13.0,
            "text": "merhaba arkadaşlar bugün konuya başlıyoruz",
            "words": [
                {"text": "merhaba", "start": 10.0, "end": 10.4},
                {"text": "arkadaşlar", "start": 10.5, "end": 10.9},
                {"text": "bugün", "start": 11.0, "end": 11.4},
                {"text": "konuya", "start": 11.5, "end": 11.8},
                {"text": "başlıyoruz", "start": 11.9, "end": 12.4},
            ],
        }
    ]
    edited = SubtitleCue(
        id="edited",
        start=20.0,
        end=24.0,
        text="Merhaba bugün konuya başlıyoruz",
        words=[],
    )

    retimed = retime_cues_from_transcript([edited], segments, 10.0, 20.0)

    assert retimed[0].text == "Merhaba bugün konuya başlıyoruz"
    assert [word.text for word in retimed[0].words] == [
        "Merhaba",
        "bugün",
        "konuya",
        "başlıyoruz",
    ]
    assert retimed[0].words[0].start == 10.0
    assert retimed[0].words[-1].end == 12.4


def test_ass_escapes_control_characters_and_preserves_turkish(tmp_path: Path) -> None:
    cue = SubtitleCue(
        id="cue",
        start=1.0,
        end=2.0,
        text=r"şimdi {başla} \ test",
        words=[],
    )
    destination = tmp_path / "subtitle.ass"
    write_ass(normalize_cues([cue], 0.0, 3.0), 0.0, destination)
    content = destination.read_text(encoding="utf-8-sig")

    assert "şimdi" in content
    assert r"\{başla\}" in content
    assert r"\\" in content
    assert escape_ass_text("a\nb") == r"a\Nb"


def test_ass_uses_higher_subtitle_safe_area(tmp_path: Path) -> None:
    cue = SubtitleCue(id="cue", start=0.0, end=1.0, text="Güvenli alan", words=[])
    destination = tmp_path / "subtitle.ass"
    write_ass(normalize_cues([cue], 0.0, 1.0), 0.0, destination)
    content = destination.read_text(encoding="utf-8-sig")

    assert f",70,70,{DEFAULT_ASS_SUBTITLE_MARGIN_V},1" in content


def test_ass_accepts_export_subtitle_position_option(tmp_path: Path) -> None:
    cue = SubtitleCue(id="cue", start=0.0, end=1.0, text="Eski konum", words=[])
    destination = tmp_path / "subtitle.ass"
    write_ass(normalize_cues([cue], 0.0, 1.0), 0.0, destination, subtitle_margin_v=260)
    content = destination.read_text(encoding="utf-8-sig")

    assert ",70,70,260,1" in content


def test_ass_accepts_export_subtitle_font_option(tmp_path: Path) -> None:
    cue = SubtitleCue(id="cue", start=0.0, end=1.0, text="Font testi", words=[])
    destination = tmp_path / "subtitle.ass"
    write_ass(
        normalize_cues([cue], 0.0, 1.0),
        0.0,
        destination,
        subtitle_font_family="Impact",
    )
    content = destination.read_text(encoding="utf-8-sig")

    assert "Style: Shorts,Impact,76" in content


def test_ass_keeps_subtitle_visible_between_word_timings(tmp_path: Path) -> None:
    cue = SubtitleCue(
        id="cue",
        start=0.0,
        end=1.4,
        text="Merhaba dünya",
        words=[
            SubtitleWord(text="Merhaba", start=0.0, end=0.4),
            SubtitleWord(text="dünya", start=1.0, end=1.4),
        ],
    )
    destination = tmp_path / "subtitle.ass"
    write_ass([cue], 0.0, destination)

    assert dialogue_times(destination.read_text(encoding="utf-8-sig")) == [
        ("0:00:00.00", "0:00:01.00"),
        ("0:00:01.00", "0:00:01.40"),
    ]


def test_ass_bridges_small_gaps_between_subtitle_groups(tmp_path: Path) -> None:
    first = SubtitleCue(
        id="first",
        start=0.0,
        end=0.4,
        text="Merhaba",
        words=[SubtitleWord(text="Merhaba", start=0.0, end=0.4)],
    )
    second = SubtitleCue(
        id="second",
        start=0.7,
        end=1.1,
        text="dünya",
        words=[SubtitleWord(text="dünya", start=0.7, end=1.1)],
    )
    destination = tmp_path / "subtitle.ass"
    write_ass([first, second], 0.0, destination)

    assert dialogue_times(destination.read_text(encoding="utf-8-sig")) == [
        ("0:00:00.00", "0:00:00.70"),
        ("0:00:00.70", "0:00:01.10"),
    ]


def test_ass_breaks_long_caption_into_two_lines(tmp_path: Path) -> None:
    cue = SubtitleCue(
        id="cue",
        start=0.0,
        end=2.0,
        text="geçen şeyi gördüm, bir tane yabancı",
        words=[
            SubtitleWord(text="geçen", start=0.0, end=0.2),
            SubtitleWord(text="şeyi", start=0.2, end=0.4),
            SubtitleWord(text="gördüm,", start=0.4, end=0.6),
            SubtitleWord(text="bir", start=0.6, end=0.8),
            SubtitleWord(text="tane", start=0.8, end=1.0),
            SubtitleWord(text="yabancı", start=1.0, end=1.2),
        ],
    )
    destination = tmp_path / "subtitle.ass"
    write_ass([cue], 0.0, destination)

    assert r"\N" in destination.read_text(encoding="utf-8-sig")
