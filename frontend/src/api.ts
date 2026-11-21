import type {
  AnalysisStatus,
  ClipCandidate,
  ContentHistoryEntry,
  ContentHistoryItem,
  ContentOptions,
  ContentOptionsRequest,
  ContentPackage,
  ContentPackageRequest,
  CutRange,
  EditSettings,
  ExportItem,
  HookSuggestion,
  InsertRange,
  JobDetail,
  JobSummary,
  MergeTask,
  ProjectDetail,
  ProjectSummary,
  SpeechCleanupExport,
  SpeechCleanupState,
  SubtitleCue,
  SubscriptionSuggestions,
  YoutubePackage,
  YoutubeChannelVideos,
} from "./types";

export class ApiError extends Error {
  constructor(message: string, readonly code?: string) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = `İstek başarısız (${response.status})`;
    let code: string | undefined;
    try {
      const { detail } = (await response.json()) as { detail?: string | { msg: string }[] | { code: string; message: string } };
      if (typeof detail === "string") message = detail;
      else if (Array.isArray(detail)) message = detail.map((item) => item.msg).join(" ");
      else if (detail && typeof detail.message === "string") {
        message = detail.message;
        if (typeof detail.code === "string") code = detail.code;
      }
    } catch {
      // Keep the status-based message when the response has no JSON body.
    }
    throw new ApiError(message, code);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

async function requireCodexReady(): Promise<void> {
  let status: AnalysisStatus;
  try { status = await request<AnalysisStatus>("/api/analysis/status"); }
  catch {
    throw new Error("Codex bağlantısı doğrulanamadı. Uygulamayı güncel sürümle yeniden başlatıp tekrar dene.");
  }
  if (status.provider !== "codex" || typeof status.available !== "boolean" || typeof status.authenticated !== "boolean") {
    throw new Error("Bu uygulama sürümü Codex konu analizini henüz desteklemiyor. Güncel sürümle yeniden başlat.");
  }
  if (!status.available || !status.authenticated) {
    throw new Error(status.message || "Codex oturumunu açıp bağlantıyı kontrol ettikten sonra tekrar dene.");
  }
}

async function checkUploadSpace(files: File[]): Promise<void> {
  await request<{ ok: boolean }>("/api/uploads/check", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ size_bytes: files.reduce((total, file) => total + file.size, 0), file_count: files.length }),
  });
}

function uploadForm<T>(
  url: string, data: FormData, onProgress: (value: number) => void, errorMessage: string, networkMessage = errorMessage,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    };
    xhr.onerror = () => reject(new Error(networkMessage));
    xhr.onload = () => {
      try {
        const payload = JSON.parse(xhr.responseText);
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(payload as T);
        } else {
          const detail = payload.detail;
          reject(new Error(typeof detail === "string" ? detail
            : Array.isArray(detail) ? detail.map((item: { msg: string }) => item.msg).join(" ")
              : errorMessage));
        }
      } catch {
        reject(new Error(errorMessage));
      }
    };
    xhr.send(data);
  });
}

async function uploadProjectFiles(url: string, files: File[], onProgress: (value: number) => void): Promise<ProjectDetail> {
  await checkUploadSpace(files);
  const data = new FormData();
  files.forEach((file) => data.append("videos", file));
  return uploadForm<ProjectDetail>(
    url, data, onProgress, "Videolar yüklenemedi.", "Videolar yüklenemedi. Bağlantını kontrol edip yeniden dene.",
  );
}

export const api = {
  analysisStatus: () => request<AnalysisStatus>("/api/analysis/status"),
  listContentHistory: () => request<ContentHistoryItem[]>("/api/content-studio/history"),
  getContentHistoryEntry: (id: string) => request<ContentHistoryEntry>(`/api/content-studio/history/${id}`),
  suggestContentOptions: (payload: ContentOptionsRequest) =>
    request<ContentOptions>("/api/content-studio/options", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    }),
  suggestContentOptionsFromVideo: async (
    brief: string, video: File, onProgress: (value: number) => void, targetSeconds?: number,
  ) => {
    await checkUploadSpace([video]);
    const data = new FormData();
    data.append("brief", brief);
    data.append("video", video);
    if (targetSeconds !== undefined) data.append("target_seconds", String(targetSeconds));
    return uploadForm<ContentOptions>("/api/content-studio/options/video", data, onProgress, "Örnek video yüklenemedi.");
  },
  generateContentPackage: (payload: ContentPackageRequest) =>
    request<ContentPackage>("/api/content-studio/generate", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    }),
  listJobs: () => request<JobSummary[]>("/api/jobs"),
  getJob: (jobId: string) => request<JobDetail>(`/api/jobs/${jobId}`),
  listProjects: () => request<ProjectSummary[]>("/api/projects"),
  getProject: (projectId: string) => request<ProjectDetail>(`/api/projects/${projectId}`),
  pickLocalVideos: () => request<{ paths: string[] }>("/api/videos/pick-local", { method: "POST" }),
  createProject: (files: File[], onProgress: (value: number) => void) =>
    uploadProjectFiles("/api/projects", files, onProgress),
  createProjectFromPaths: (paths: string[]) =>
    request<ProjectDetail>("/api/projects/from-paths", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ paths }),
    }),
  addProjectVideos: (projectId: string, files: File[], onProgress: (value: number) => void) =>
    uploadProjectFiles(`/api/projects/${projectId}/videos`, files, onProgress),
  addProjectVideosFromPaths: (projectId: string, paths: string[]) =>
    request<ProjectDetail>(`/api/projects/${projectId}/videos/from-paths`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ paths }),
    }),
  selectProjectVideo: (projectId: string, videoId: string) =>
    request<ProjectDetail>(`/api/projects/${projectId}/active-video`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ video_id: videoId }),
    }),
  mergeVideos: (
    projectId: string, videoIds: string[], cuts: Record<string, CutRange[]> = {},
    exportMode: "auto" | "fast" | "precise" = "auto",
    outputDirectory: string | null = null,
  ) =>
    request<MergeTask>(`/api/projects/${projectId}/merges`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ video_ids: videoIds, cuts, export_mode: exportMode, output_directory: outputDirectory }),
    }),
  pickMergeExportDirectory: () =>
    request<{ directory: string | null }>("/api/projects/pick-export-directory", { method: "POST" }),
  revealMergeExport: (projectId: string, mergeId: string) =>
    request<void>(`/api/projects/${projectId}/merges/${mergeId}/reveal`, { method: "POST" }),
  deleteProject: (projectId: string) =>
    request<void>(`/api/projects/${projectId}`, { method: "DELETE" }),
  getSpeechCleanup: (jobId: string) =>
    request<SpeechCleanupState>(`/api/jobs/${jobId}/speech-cleanup`),
  detectSpeech: (jobId: string) =>
    request<SpeechCleanupState>(`/api/jobs/${jobId}/speech-cleanup/detect`, { method: "POST" }),
  saveSpeechSelection: (jobId: string, removedRegionIds: string[]) =>
    request<SpeechCleanupState>(`/api/jobs/${jobId}/speech-cleanup/selection`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ removed_region_ids: removedRegionIds }),
    }),
  addManualSpeechRegion: (jobId: string, start: number, end: number, text: string) =>
    request<SpeechCleanupState>(`/api/jobs/${jobId}/speech-cleanup/manual-regions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ start, end, text }),
    }),
  deleteManualSpeechRegion: (jobId: string, regionId: string) =>
    request<SpeechCleanupState>(`/api/jobs/${jobId}/speech-cleanup/manual-regions/${encodeURIComponent(regionId)}`, {
      method: "DELETE",
    }),
  exportSpeechCleanup: (jobId: string, mode: "cut" | "mute") =>
    request<SpeechCleanupExport>(`/api/jobs/${jobId}/speech-cleanup/exports`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    }),
  revealSpeechExport: (jobId: string, exportId: string) =>
    request<void>(`/api/jobs/${jobId}/speech-cleanup/exports/${exportId}/reveal`, { method: "POST" }),
  browseYoutubeChannel: (url: string, offset = 0, signal?: AbortSignal) =>
    request<YoutubeChannelVideos>("/api/youtube/channel/videos", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, offset }), signal,
    }),
  importYoutube: async (url: string) => {
    await requireCodexReady();
    return request<JobSummary>("/api/jobs/youtube", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
  },
  suggestHooks: (jobId: string, clipId: string) =>
    request<HookSuggestion[]>(`/api/jobs/${jobId}/clips/${clipId}/hooks`),
  suggestSubscriptions: (clip: ClipCandidate, editSettings: EditSettings, playbackRate: number, signal?: AbortSignal) =>
    request<SubscriptionSuggestions>(`/api/jobs/${clip.job_id}/clips/${clip.id}/subscription-suggestions`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ edit_settings: { ...editSettings, subscription: undefined }, playback_rate: playbackRate }), signal,
    }),
  reanalyze: async (jobId: string) => {
    await requireCodexReady();
    return request<JobDetail>(`/api/jobs/${jobId}/reanalyze`, { method: "POST" });
  },
  correctTranscript: (jobId: string) => request<JobDetail>(`/api/jobs/${jobId}/transcript/correct`, { method: "POST" }),
  revertCorrection: (jobId: string, wordId: number) => request<JobDetail>(`/api/jobs/${jobId}/transcript/corrections/${wordId}/revert`, { method: "POST" }),
  saveYoutube: (clip: ClipCandidate, youtube: Pick<YoutubePackage, "brief" | "title" | "description" | "thumbnail_text">) =>
    request<ClipCandidate>(`/api/jobs/${clip.job_id}/clips/${clip.id}/youtube`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(youtube),
    }),
  generateYoutube: (clip: ClipCandidate) => request<ClipCandidate>(`/api/jobs/${clip.job_id}/clips/${clip.id}/youtube/generate`, { method: "POST" }),
  createClip: (jobId: string, start: number, end: number, topicId?: string) =>
    request<ClipCandidate>(`/api/jobs/${jobId}/clips`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ start, end, topic_id: topicId }),
    }),
  upload: async (file: File, onProgress: (value: number) => void) => {
    await requireCodexReady();
    await checkUploadSpace([file]);
    return new Promise<JobDetail>((resolve, reject) => {
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
    });
  },
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
      insert_ranges?: InsertRange[];
      reset_subtitles?: boolean;
      auto_cut_silence?: boolean;
      edit_settings?: EditSettings;
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
    isPreview = false,
  ) =>
    request<{ exports: ExportItem[] }>(`/api/jobs/${jobId}/exports`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        clip_ids: clipIds,
        llm_seo_enabled: llmSeoEnabled,
        playback_rate: playbackRate,
        subtitle_margin_v: subtitleMarginV,
        balanced_vertical_offset: balancedVerticalOffset,
        subtitle_font_family: subtitleFontFamily,
        is_preview: isPreview,
      }),
    }),
  deleteJob: (jobId: string) =>
    request<void>(`/api/jobs/${jobId}`, { method: "DELETE" }),
};
