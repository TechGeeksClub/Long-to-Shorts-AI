export type JobStatus =
  | "queued"
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
  selection_method: "heuristic" | "hybrid" | "manual";
  reasons: string[];
  subtitles: SubtitleCue[];
  cut_ranges: CutRange[];
  insert_ranges: InsertRange[];
  crop_keyframes: CropKeyframe[];
  framing_mode: "fit" | "balanced" | "fill";
  face_tracking_enabled: boolean;
  selected: boolean;
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
  filename: string | null;
  metadata_filename: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface JobSummary {
  id: string;
  filename: string;
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

export interface JobDetail extends JobSummary {
  clips: ClipCandidate[];
  exports: ExportItem[];
}
