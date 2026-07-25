from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


JobStatus = Literal[
    "queued",
    "analyzing",
    "ready",
    "exporting",
    "completed",
    "failed",
    "interrupted",
]
ExportStatus = Literal["queued", "rendering", "completed", "failed"]
FramingMode = Literal["fit", "balanced", "fill"]
SubtitleFontFamily = Literal["Arial", "Arial Black", "Impact", "Segoe UI", "Tahoma", "Verdana"]


class SubtitleWord(BaseModel):
    text: str
    start: float
    end: float


class SubtitleCue(BaseModel):
    id: str
    start: float
    end: float
    text: str
    words: list[SubtitleWord] = Field(default_factory=list)


class CutRange(BaseModel):
    start: float
    end: float


class CropKeyframe(BaseModel):
    time: float
    x: int
    y: int
    width: int
    height: int


class ClipCandidate(BaseModel):
    id: str
    job_id: str
    rank: int
    title: str
    start: float
    end: float
    score: float
    reasons: list[str]
    subtitles: list[SubtitleCue]
    cut_ranges: list[CutRange] = Field(default_factory=list)
    crop_keyframes: list[CropKeyframe] = Field(default_factory=list)
    framing_mode: FramingMode = "fit"
    face_tracking_enabled: bool = False
    selected: bool = True

    @property
    def duration(self) -> float:
        return self.end - self.start


class Export(BaseModel):
    id: str
    job_id: str
    clip_id: str
    status: ExportStatus
    progress: float
    llm_seo_enabled: bool = False
    playback_rate: float = 1.0
    subtitle_margin_v: int = 420
    subtitle_font_family: SubtitleFontFamily = "Arial"
    balanced_vertical_offset: int = 0
    filename: str | None = None
    metadata_filename: str | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class JobSummary(BaseModel):
    id: str
    filename: str
    status: JobStatus
    progress: float
    stage: str
    error: str | None = None
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    language: str | None = None
    created_at: datetime
    updated_at: datetime


class JobDetail(JobSummary):
    clips: list[ClipCandidate] = Field(default_factory=list)
    exports: list[Export] = Field(default_factory=list)


class ClipUpdate(BaseModel):
    start: float | None = None
    end: float | None = None
    selected: bool | None = None
    framing_mode: FramingMode | None = None
    face_tracking_enabled: bool | None = None
    subtitles: list[SubtitleCue] | None = None
    cut_ranges: list[CutRange] | None = None
    reset_subtitles: bool = False
    auto_cut_silence: bool = False


class ManualClipCreate(BaseModel):
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)


class ExportRequest(BaseModel):
    clip_ids: list[str] = Field(min_length=1)
    llm_seo_enabled: bool = False
    playback_rate: float = Field(default=1.0, ge=1.0, le=1.5)
    subtitle_margin_v: int = Field(default=420, ge=220, le=560)
    subtitle_font_family: SubtitleFontFamily = "Arial"
    balanced_vertical_offset: int = Field(default=0, ge=0, le=300)


class ExportResponse(BaseModel):
    exports: list[Export]
