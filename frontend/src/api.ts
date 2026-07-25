import type { ClipCandidate, CutRange, JobDetail, JobSummary, SubtitleCue } from "./types";

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = `İstek başarısız (${response.status})`;
    try {
      const payload = (await response.json()) as { detail?: string };
      if (payload.detail) message = payload.detail;
    } catch {
      // Keep the status-based message when the response has no JSON body.
    }
    throw new Error(message);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  listJobs: () => request<JobSummary[]>("/api/jobs"),
  getJob: (jobId: string) => request<JobDetail>(`/api/jobs/${jobId}`),
  reanalyze: (jobId: string) =>
    request<JobDetail>(`/api/jobs/${jobId}/reanalyze`, { method: "POST" }),
  createClip: (jobId: string, start: number, end: number) =>
    request<ClipCandidate>(`/api/jobs/${jobId}/clips`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ start, end }),
    }),
  upload: (file: File, onProgress: (value: number) => void) =>
    new Promise<JobDetail>((resolve, reject) => {
      const data = new FormData();
      data.append("video", file);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/jobs");
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) onProgress(event.loaded / event.total);
      };
      xhr.onerror = () => reject(new Error("Video yüklenemedi."));
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(JSON.parse(xhr.responseText) as JobDetail);
          return;
        }
        try {
          const payload = JSON.parse(xhr.responseText) as { detail?: string };
          reject(new Error(payload.detail ?? "Video yüklenemedi."));
        } catch {
          reject(new Error("Video yüklenemedi."));
        }
      };
      xhr.send(data);
    }),
  updateClip: (
    jobId: string,
    clipId: string,
    payload: {
      start?: number;
      end?: number;
      selected?: boolean;
      framing_mode?: "fit" | "balanced" | "fill";
      face_tracking_enabled?: boolean;
      subtitles?: SubtitleCue[];
      cut_ranges?: CutRange[];
      reset_subtitles?: boolean;
      auto_cut_silence?: boolean;
    },
  ) =>
    request<ClipCandidate>(`/api/jobs/${jobId}/clips/${clipId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  exportClips: (
    jobId: string,
    clipIds: string[],
    llmSeoEnabled = false,
    playbackRate = 1,
    subtitleMarginV = 420,
    balancedVerticalOffset = 0,
    subtitleFontFamily = "Arial",
  ) =>
    request(`/api/jobs/${jobId}/exports`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        clip_ids: clipIds,
        llm_seo_enabled: llmSeoEnabled,
        playback_rate: playbackRate,
        subtitle_margin_v: subtitleMarginV,
        balanced_vertical_offset: balancedVerticalOffset,
        subtitle_font_family: subtitleFontFamily,
      }),
    }),
  deleteJob: (jobId: string) =>
    request<void>(`/api/jobs/${jobId}`, { method: "DELETE" }),
};
