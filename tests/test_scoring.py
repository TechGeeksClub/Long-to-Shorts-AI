import numpy as np

from app.scoring import score_candidates, segment_topics


def make_segments(count: int = 40) -> list[dict]:
    segments = []
    for index in range(count):
        start = index * 5.0
        words = [
            {
                "text": word,
                "start": start + offset * 0.6,
                "end": start + offset * 0.6 + 0.5,
                "probability": 0.95,
            }
            for offset, word in enumerate(
                ["Şimdi", "bu", "önemli", "konuyu", "nasıl", "çözeriz"]
            )
        ]
        segments.append(
            {
                "start": start,
                "end": start + 4.0,
                "text": "Şimdi bu önemli konuyu nasıl çözeriz?",
                "words": words,
                "avg_logprob": -0.1,
                "no_speech_prob": 0.01,
            }
        )
    return segments


def test_candidates_respect_duration_and_are_ranked() -> None:
    clips = score_candidates(
        job_id="job",
        segments=make_segments(80),
        duration=400,
        rms_envelope=np.ones(800, dtype=np.float32),
        rms_bucket_seconds=0.5,
    )

    assert len(clips) == 10
    assert [clip.rank for clip in clips] == list(range(1, 11))
    assert all(30 <= clip.end - clip.start <= 60 for clip in clips)
    assert all(clip.subtitles for clip in clips)


def test_short_transcript_still_returns_a_candidate() -> None:
    clips = score_candidates(
        job_id="job",
        segments=make_segments(2),
        duration=10,
        rms_envelope=np.ones(20, dtype=np.float32),
        rms_bucket_seconds=0.5,
    )
    assert len(clips) == 1
    assert clips[0].end <= 10


def test_topic_pivot_splits_transcript_into_subject_blocks() -> None:
    segments = [
        {
            "start": 0.0,
            "end": 15.0,
            "text": "Motor ekipmanları ve güvenli sürüş hakkında konuşuyoruz.",
            "words": [],
        },
        {
            "start": 15.4,
            "end": 30.0,
            "text": "Kask seçimi sürüş güvenliği için çok önemlidir.",
            "words": [],
        },
        {
            "start": 31.0,
            "end": 45.0,
            "text": "Şimdi gelelim Finike'de yaşamaya ve şehir imkanlarına.",
            "words": [],
        },
        {
            "start": 45.2,
            "end": 60.0,
            "text": "Finike sakin bir şehir ve havalimanına biraz uzaktır.",
            "words": [],
        },
    ]

    topics = segment_topics(segments)

    assert len(topics) == 2
    assert topics[0].end == 30.0
    assert topics[1].start == 31.0


def test_selected_clips_do_not_substantially_overlap() -> None:
    clips = score_candidates(
        job_id="job",
        segments=make_segments(),
        duration=200,
        rms_envelope=np.ones(400, dtype=np.float32),
        rms_bucket_seconds=0.5,
    )

    for index, clip in enumerate(clips):
        for other in clips[index + 1 :]:
            overlap = max(0.0, min(clip.end, other.end) - max(clip.start, other.start))
            shorter = min(clip.end - clip.start, other.end - other.start)
            assert overlap / shorter <= 0.08
