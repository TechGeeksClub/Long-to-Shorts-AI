from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


JobStatus = Literal[
    "queued",
    "importing",
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
EditStyle = Literal["off", "clean", "dynamic", "cinematic"]
EdgeEffect = Literal[
    "none", "fade_black", "fade_white", "zoom_blur", "slide", "rgb_split", "diagonal",
    "dissolve", "smooth_wipe", "circle_reveal", "pixelize",
]
VideoEffect = Literal["none", "cinematic", "vivid", "warm", "cool", "monochrome", "vintage"]


class YoutubeBrief(BaseModel):
    audience: str = Field(default="Motosiklet ve motovlog izleyicileri", max_length=250)
    focus: str = Field(default="", max_length=600)
    tone: Literal["natural", "curious", "informative"] = "natural"
    thumbnail_style: Literal["bold", "editorial", "neon"] = "bold"
    aspect: Literal["portrait", "landscape"] = "portrait"
    accent: Literal["lime", "orange", "cyan"] = "lime"
    frame_time: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    channel_name: str = Field(default="", max_length=70)


class YoutubeOptions(BaseModel):
    titles: list[str] = Field(default_factory=list)
    descriptions: list[str] = Field(default_factory=list)
    thumbnail_texts: list[str] = Field(default_factory=list)


class YoutubePackage(BaseModel):
    brief: YoutubeBrief = Field(default_factory=YoutubeBrief)
    title: str = Field(default="", max_length=100)
    description: str = Field(default="", max_length=5000)
    thumbnail_text: str = Field(default="", max_length=90)
    options: YoutubeOptions = Field(default_factory=YoutubeOptions)
    status: Literal["idle", "queued", "generating", "ready", "failed"] = "idle"
    error: str | None = None
    revision: str = ""


class YoutubeUpdate(BaseModel):
    brief: YoutubeBrief
    title: str = Field(default="", max_length=100)
    description: str = Field(default="", max_length=5000)
    thumbnail_text: str = Field(default="", max_length=90)


class WordCorrection(BaseModel):
    word_id: int
    original: str
    replacement: str
    reason: str
    start: float
    end: float


class CorrectionReport(BaseModel):
    created_at: datetime
    corrections: list[WordCorrection]
    fingerprint: str


class HookRange(BaseModel):
    source_start: float = Field(ge=0, allow_inf_nan=False)
    source_end: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_duration(self) -> "HookRange":
        if not 0.3 <= round(self.source_end - self.source_start, 6) <= 15:
            raise ValueError("Hook süresi 0.3 ile 15 saniye arasında olmalı.")
        return self


class SubscriptionSettings(BaseModel):
    enabled: bool = False
    channel_name: str = Field(default="Oban Otağı", max_length=50)
    show_tagline: bool = False
    use_photo: bool = True
    click_effect: bool = True
    position: Literal["above_subtitles", "top"] = "above_subtitles"
    text: str = Field(default="", max_length=90)
    start: float = Field(default=8, ge=0, le=86400, allow_inf_nan=False)
    duration: float = Field(default=4.2, ge=1, le=5, allow_inf_nan=False)
    animation: Literal["fade", "slide"] = "fade"

    @model_validator(mode="after")
    def validate_identity(self) -> "SubscriptionSettings":
        self.channel_name = " ".join(self.channel_name.split())
        self.text = " ".join(self.text.split())
        if self.enabled and not self.channel_name:
            raise ValueError("Abonelik kartında görünecek kanal adını yaz.")
        return self


class EditSettings(BaseModel):
    style: EditStyle = "off"
    hook_text: str = Field(default="", max_length=90)
    hook_duration: float = Field(default=2.8, ge=1, le=5, allow_inf_nan=False)
    punch_ins: bool = True
    hook_range: HookRange | None = None
    hook_remove_from_body: bool = True
    hook_transition: EdgeEffect = "fade_white"
    hook_transition_duration: float = Field(default=0.5, ge=0.1, le=2, allow_inf_nan=False)
    intro_effect: EdgeEffect = "none"
    outro_effect: EdgeEffect = "none"
    intro_duration: float = Field(default=0.4, ge=0.1, le=2, allow_inf_nan=False)
    outro_duration: float = Field(default=0.6, ge=0.1, le=2, allow_inf_nan=False)
    fade_audio: bool = False
    video_effect: VideoEffect = "none"
    effect_intensity: float = Field(default=0.65, ge=0, le=1, allow_inf_nan=False)
    subscription: SubscriptionSettings = Field(default_factory=SubscriptionSettings)


class SubscriptionSuggestRequest(BaseModel):
    edit_settings: EditSettings | None = None
    playback_rate: float = Field(default=1, ge=1, le=1.5, allow_inf_nan=False)


class SubscriptionSuggestion(BaseModel):
    text: str = Field(min_length=1, max_length=90)
    start: float = Field(ge=0, allow_inf_nan=False)
    duration: float = Field(ge=1, le=5, allow_inf_nan=False)
    reason: str = Field(min_length=1, max_length=250)


class SubscriptionSuggestions(BaseModel):
    options: list[SubscriptionSuggestion] = Field(min_length=3, max_length=3)
    output_duration: float


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


class InsertRange(BaseModel):
    source_start: float = Field(ge=0, allow_inf_nan=False)
    source_end: float = Field(gt=0, allow_inf_nan=False)
    insert_at: float = Field(ge=0, allow_inf_nan=False)


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
    content_score: float = 0.0
    integrity_score: float = 0.0
    selection_method: Literal["heuristic", "hybrid", "manual", "agent"] = "heuristic"
    topic_id: str | None = None
    youtube: YoutubePackage = Field(default_factory=YoutubePackage)
    cut_ranges: list[CutRange] = Field(default_factory=list)
    insert_ranges: list[InsertRange] = Field(default_factory=list)
    crop_keyframes: list[CropKeyframe] = Field(default_factory=list)
    framing_mode: FramingMode = "fit"
    face_tracking_enabled: bool = False
    selected: bool = True
    edit_settings: EditSettings = Field(default_factory=EditSettings)

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
    edit_settings: EditSettings = Field(default_factory=EditSettings)
    is_preview: bool = False
    thumbnail_filename: str | None = None
    package_filename: str | None = None
    filename: str | None = None
    metadata_filename: str | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class JobSummary(BaseModel):
    id: str
    filename: str
    source_type: Literal["upload", "youtube"] = "upload"
    source_url: str | None = None
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


class TopicSection(BaseModel):
    id: str
    title: str
    start: float
    end: float
    summary: str


class AnalysisReport(BaseModel):
    provider: Literal["codex"] = "codex"
    model: str | None = None
    summary: str
    topics: list[TopicSection]
    clip_ids: list[str]
    created_at: datetime


class JobDetail(JobSummary):
    transcript_available: bool = False
    clips: list[ClipCandidate] = Field(default_factory=list)
    exports: list[Export] = Field(default_factory=list)
    analysis: AnalysisReport | None = None
    correction: CorrectionReport | None = None


class YoutubeSourceRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


class YoutubeChannelBrowseRequest(YoutubeSourceRequest):
    offset: int = Field(default=0, ge=0, le=10000)


class YoutubeChannelVideoSummary(BaseModel):
    id: str
    title: str
    url: str
    thumbnail_url: str | None = None
    duration: float | None = None
    upload_date: str | None = None


class YoutubeChannelBrowseResponse(BaseModel):
    channel_title: str
    channel_url: str
    videos: list[YoutubeChannelVideoSummary]
    has_more: bool = False
    next_offset: int | None = None


class ClipUpdate(BaseModel):
    start: float | None = None
    end: float | None = None
    selected: bool | None = None
    framing_mode: FramingMode | None = None
    face_tracking_enabled: bool | None = None
    subtitles: list[SubtitleCue] | None = None
    cut_ranges: list[CutRange] | None = None
    insert_ranges: list[InsertRange] | None = None
    reset_subtitles: bool = False
    auto_cut_silence: bool = False
    edit_settings: EditSettings | None = None


class ManualClipCreate(BaseModel):
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    topic_id: str | None = None


class ExportRequest(BaseModel):
    clip_ids: list[str] = Field(min_length=1)
    llm_seo_enabled: bool = False
    playback_rate: float = Field(default=1.0, ge=1.0, le=1.5)
    subtitle_margin_v: int = Field(default=420, ge=220, le=560)
    subtitle_font_family: SubtitleFontFamily = "Arial"
    balanced_vertical_offset: int = Field(default=0, ge=0, le=300)
    is_preview: bool = False


class ExportResponse(BaseModel):
    exports: list[Export]
