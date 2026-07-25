from app.models import CutRange, InsertRange, SubtitleCue, SubtitleWord
from app.subtitles import compose_subtitles_for_timeline
from app.timeline import build_timeline_segments, timeline_duration


def test_inserted_source_range_is_placed_before_target_timestamp() -> None:
    timeline = build_timeline_segments(
        clip_start=630.0,
        clip_end=690.0,
        cut_ranges=[],
        insert_ranges=[
            InsertRange(
                source_start=80.0,
                source_end=90.0,
                insert_at=644.0,
            )
        ],
    )

    assert [
        (segment.source_start, segment.source_end, segment.kind)
        for segment in timeline
    ] == [
        (630.0, 644.0, "base"),
        (80.0, 90.0, "insert"),
        (644.0, 690.0, "base"),
    ]
    assert timeline_duration(timeline) == 70.0


def test_insert_and_cut_ranges_share_one_output_timeline() -> None:
    timeline = build_timeline_segments(
        clip_start=10.0,
        clip_end=20.0,
        cut_ranges=[CutRange(start=14.0, end=16.0)],
        insert_ranges=[
            InsertRange(source_start=1.0, source_end=3.0, insert_at=12.0),
            InsertRange(source_start=5.0, source_end=6.0, insert_at=16.0),
        ],
    )

    assert [
        (segment.source_start, segment.source_end, segment.kind)
        for segment in timeline
    ] == [
        (10.0, 12.0, "base"),
        (1.0, 3.0, "insert"),
        (12.0, 14.0, "base"),
        (5.0, 6.0, "insert"),
        (16.0, 20.0, "base"),
    ]
    assert timeline_duration(timeline) == 11.0


def test_subtitles_follow_the_composed_timeline_order() -> None:
    base_cues = [
        SubtitleCue(
            id="base-one",
            start=631.0,
            end=632.0,
            text="Ana klip başlangıcı",
            words=[
                SubtitleWord(text="Ana", start=631.0, end=631.3),
                SubtitleWord(text="klip", start=631.4, end=631.7),
                SubtitleWord(text="başlangıcı", start=631.7, end=632.0),
            ],
        ),
        SubtitleCue(
            id="base-two",
            start=645.0,
            end=646.0,
            text="Ana klip devamı",
            words=[
                SubtitleWord(text="Ana", start=645.0, end=645.3),
                SubtitleWord(text="klip", start=645.4, end=645.7),
                SubtitleWord(text="devamı", start=645.7, end=646.0),
            ],
        ),
    ]
    transcript_segments = [
        {
            "start": 81.0,
            "end": 82.0,
            "text": "Eklenen bölüm",
            "words": [
                {"text": "Eklenen", "start": 81.0, "end": 81.4},
                {"text": "bölüm", "start": 81.5, "end": 82.0},
            ],
        }
    ]
    timeline = build_timeline_segments(
        clip_start=630.0,
        clip_end=690.0,
        cut_ranges=[],
        insert_ranges=[
            InsertRange(source_start=80.0, source_end=90.0, insert_at=644.0)
        ],
    )

    cues = compose_subtitles_for_timeline(
        base_cues=base_cues,
        transcript_segments=transcript_segments,
        timeline=timeline,
        output_start=630.0,
    )

    assert [cue.text for cue in cues] == [
        "Ana klip başlangıcı",
        "Eklenen bölüm",
        "Ana klip devamı",
    ]
    assert cues[0].start == 631.0
    assert cues[1].start == 645.0
    assert cues[2].start == 655.0

