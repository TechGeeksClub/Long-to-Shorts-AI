export type JobStatus =
  | "queued"
  | "importing"
  | "analyzing"
  | "ready"
  | "exporting"
  | "completed"
  | "failed"
  | "interrupted";

export interface SubtitleWord {
  text: string;
  start: number;
  end: number;
}

export interface SubtitleCue {
  id: string;
  start: number;
  end: number;
  text: string;
  words: SubtitleWord[];
}

export interface CutRange {
  start: number;
  end: number;
}

export interface SpeechRegion {
  id: string;
  start: number;
  end: number;
  text: string;
  manual?: boolean;
}

export interface SpeechCleanupExport {
  id: string;
  status: "queued" | "rendering" | "completed" | "failed";
  progress: number;
  error: string | null;
  filename: string | null;
  duration: number;
  mode: "cut" | "mute";
  created_at: string;
}

export interface SpeechCleanupState {
  job_id: string;
  transcript_available: boolean;
  status: "idle" | "queued" | "detecting" | "ready" | "failed";
  progress: number;
  error: string | null;
  duration: number | null;
  regions: SpeechRegion[];
  removed_region_ids: string[];
  mute_ranges?: CutRange[];
  mute_padding_before?: number;
  mute_padding_after?: number;
  exports: SpeechCleanupExport[];
}

export interface InsertRange {
  source_start: number;
  source_end: number;
  insert_at: number;
}

export interface CropKeyframe {
  time: number;
  x: number;
  y: number;
  width: number;
  height: number;
}

export type EdgeEffect = "none" | "fade_black" | "fade_white" | "zoom_blur" | "slide" | "rgb_split" | "diagonal"
  | "dissolve" | "smooth_wipe" | "circle_reveal" | "pixelize";
export type VideoEffect = "none" | "cinematic" | "vivid" | "warm" | "cool" | "monochrome" | "vintage";

export interface YoutubeBrief {
  audience: string;
  focus: string;
  tone: "natural" | "curious" | "informative";
  thumbnail_style: "bold" | "editorial" | "neon";
  aspect: "portrait" | "landscape";
  accent: "lime" | "orange" | "cyan";
  frame_time: number | null;
  channel_name: string;
}
export interface YoutubePackage {
  brief: YoutubeBrief;
  title: string;
  description: string;
  thumbnail_text: string;
  options: { titles: string[]; descriptions: string[]; thumbnail_texts: string[] };
  status: "idle" | "queued" | "generating" | "ready" | "failed";
  error: string | null;
  revision: string;
}
export interface CorrectionReport {
  created_at: string;
  fingerprint: string;
  corrections: { word_id: number; original: string; replacement: string; reason: string; start: number; end: number }[];
}

export interface EditSettings {
  style: "off" | "clean" | "dynamic" | "cinematic";
  hook_text: string;
  hook_duration: number;
  punch_ins: boolean;
  hook_range: { source_start: number; source_end: number } | null;
  hook_remove_from_body: boolean;
  hook_transition: EdgeEffect;
  hook_transition_duration: number;
  intro_effect: EdgeEffect;
  outro_effect: EdgeEffect;
  intro_duration: number;
  outro_duration: number;
  fade_audio: boolean;
  video_effect: VideoEffect;
  effect_intensity: number;
  subscription: SubscriptionSettings;
}

export interface SubscriptionSettings {
  enabled: boolean;
  channel_name: string;
  show_tagline: boolean;
  use_photo: boolean;
  click_effect: boolean;
  position: "above_subtitles" | "top";
  text: string;
  start: number;
  duration: number;
  animation: "fade" | "slide";
}

export interface SubscriptionSuggestion {
  text: string;
  start: number;
  duration: number;
  reason: string;
}

export interface SubscriptionSuggestions {
  options: SubscriptionSuggestion[];
  output_duration: number;
}

export interface HookSuggestion {
  text: string;
  reason: string;
}

export interface ClipCandidate {
  id: string;
  job_id: string;
  rank: number;
  title: string;
  start: number;
  end: number;
  score: number;
  content_score: number;
  integrity_score: number;
  selection_method: "heuristic" | "hybrid" | "manual" | "agent";
  topic_id: string | null;
  youtube: YoutubePackage;
  reasons: string[];
  subtitles: SubtitleCue[];
  cut_ranges: CutRange[];
  insert_ranges: InsertRange[];
  crop_keyframes: CropKeyframe[];
  framing_mode: "fit" | "balanced" | "fill";
  face_tracking_enabled: boolean;
  selected: boolean;
  edit_settings: EditSettings;
}

export interface ExportItem {
  id: string;
  job_id: string;
  clip_id: string;
  status: "queued" | "rendering" | "completed" | "failed";
  progress: number;
  llm_seo_enabled: boolean;
  playback_rate: number;
  subtitle_margin_v: number;
  subtitle_font_family: string;
  balanced_vertical_offset: number;
  edit_settings: EditSettings;
  filename: string | null;
  is_preview: boolean;
  metadata_filename: string | null;
  thumbnail_filename: string | null;
  package_filename: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface JobSummary {
  id: string;
  filename: string;
  source_type?: "upload" | "youtube";
  source_url?: string | null;
  status: JobStatus;
  progress: number;
  stage: string;
  error: string | null;
  duration: number | null;
  width: number | null;
  height: number | null;
  language: string | null;
  created_at: string;
  updated_at: string;
}

export interface YoutubeChannelVideo {
  id: string;
  title: string;
  url: string;
  thumbnail_url: string | null;
  duration: number | null;
}

export interface YoutubeChannelVideos {
  channel_title: string;
  channel_url: string;
  videos: YoutubeChannelVideo[];
  has_more: boolean;
  next_offset: number | null;
}

export interface JobDetail extends JobSummary {
  transcript_available: boolean;
  clips: ClipCandidate[];
  exports: ExportItem[];
  analysis: TopicAnalysis | null;
  correction: CorrectionReport | null;
}

export interface ProjectSummary {
  id: string;
  name: string;
  active_video_id: string | null;
  video_count: number;
  created_at: string;
  updated_at: string;
}

export interface ProjectVideo extends JobSummary {
  kind: "source" | "merged";
}

export interface MergeTask {
  id: string;
  status: "queued" | "rendering" | "completed" | "failed";
  progress: number;
  error: string | null;
  video_ids: string[];
  cuts: Record<string, CutRange[]>;
  export_mode: "auto" | "fast" | "precise";
  output_directory: string | null;
  output_path: string | null;
  output_video_id: string | null;
  filename: string | null;
  created_at: string;
}

export interface ProjectDetail extends ProjectSummary {
  videos: ProjectVideo[];
  merges: MergeTask[];
}

export interface TopicSection {
  id: string;
  title: string;
  start: number;
  end: number;
  summary: string;
}

export interface TopicAnalysis {
  provider: "codex";
  model: string | null;
  summary: string;
  topics: TopicSection[];
  clip_ids: string[];
  created_at: string;
}

export interface AnalysisStatus {
  provider: "codex";
  available: boolean;
  authenticated: boolean;
  model: string | null;
  message: string;
}

export interface ContentOptionsRequest {
  brief: string;
  reference_url?: string;
  reference_transcript?: string;
  target_seconds?: number;
}

export interface ContentFlowBeat {
  title: string;
  story: string;
  visual: string;
  transition: string;
}

export interface ContentFlow {
  id: string;
  title: string;
  hook: string;
  approach: string;
  premise?: string;
  payoff?: string;
  tradeoff?: string;
  beats: (ContentFlowBeat | string)[];
}

export interface ContentOptions {
  reference_insights: string;
  target_seconds?: number;
  options: ContentFlow[];
}

export interface ContentHistoryItem {
  id: string;
  created_at: string;
  brief_preview: string;
  title: string;
  target_seconds: number;
}

export interface ContentHistoryEntry {
  id: string;
  brief: string;
  has_reference: boolean;
  data: ContentOptions;
}

export interface ContentPackageRequest {
  brief: string;
  reference_insights: string;
  option: ContentFlow;
  target_seconds?: number;
}

export interface ContentEditBeat {
  beat: string;
  spoken?: string;
  visual: string;
  edit: string;
  audio?: string;
  on_screen?: string;
  transition?: string;
}

export interface ContentPackage {
  script: string;
  target_seconds?: number;
  estimated_speech_seconds?: number;
  edit_plan: ContentEditBeat[];
  seo: { titles: string[]; description: string; keywords: string[] };
}
