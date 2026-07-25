from __future__ import annotations

from dataclasses import dataclass

from app.models import CutRange, InsertRange


MIN_TIMELINE_SEGMENT_SECONDS = 0.01


@dataclass(frozen=True)
class TimelineSegment:
    source_start: float
    source_end: float
    kind: str

    @property
    def duration(self) -> float:
        return self.source_end - self.source_start


def build_timeline_segments(
    *,
    clip_start: float,
    clip_end: float,
    cut_ranges: list[CutRange],
    insert_ranges: list[InsertRange],
) -> list[TimelineSegment]:
    base_ranges = _kept_base_ranges(clip_start, clip_end, cut_ranges)
    ordered_inserts = sorted(
        enumerate(insert_ranges),
        key=lambda item: (item[1].insert_at, item[0]),
    )
    consumed: set[int] = set()
    timeline: list[TimelineSegment] = []

    for base_start, base_end in base_ranges:
        cursor = base_start
        for index, insert in ordered_inserts:
            if index in consumed or not (base_start <= insert.insert_at <= base_end):
                continue
            if insert.insert_at > cursor:
                timeline.append(TimelineSegment(cursor, insert.insert_at, "base"))
            timeline.append(
                TimelineSegment(insert.source_start, insert.source_end, "insert")
            )
            cursor = max(cursor, insert.insert_at)
            consumed.add(index)
        if base_end > cursor:
            timeline.append(TimelineSegment(cursor, base_end, "base"))

    return [
        segment
        for segment in timeline
        if segment.duration >= MIN_TIMELINE_SEGMENT_SECONDS
    ]


def timeline_duration(segments: list[TimelineSegment]) -> float:
    return sum(segment.duration for segment in segments)


def _kept_base_ranges(
    clip_start: float,
    clip_end: float,
    cut_ranges: list[CutRange],
) -> list[tuple[float, float]]:
    kept: list[tuple[float, float]] = []
    cursor = clip_start
    for cut in cut_ranges:
        if cut.start > cursor:
            kept.append((cursor, cut.start))
        cursor = max(cursor, cut.end)
    if cursor < clip_end:
        kept.append((cursor, clip_end))
    return kept
