import {
  type DragEvent,
  type FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { api } from "./api";
import { EDIT_STYLE_LABELS, VIDEO_EFFECT_LABELS, MotovlogEditor } from "./MotovlogEditor";
import { TopicAnalysisPanel } from "./TopicAnalysisPanel";
import { TranscriptCorrection } from "./TranscriptCorrection";
import { YoutubeStudio } from "./YoutubeStudio";
import { YoutubeImport } from "./YoutubeImport";
import { SpeechCleanup } from "./SpeechCleanup";
import { ProjectVideos } from "./ProjectVideos";
import { LocalVideoPaths } from "./LocalVideoPaths";
import { VideoMerge } from "./VideoMerge";
import type {
  ClipCandidate,
  CutRange,
  InsertRange,
  JobDetail,
  ProjectDetail,
  ProjectSummary,
  SubtitleCue,
  SubtitleWord,
  TopicSection,
} from "./types";

const ACTIVE_STATUSES = new Set(["queued", "importing", "analyzing", "exporting"]);
const MIN_MANUAL_CLIP_SECONDS = 1;
const END_TIME_TOLERANCE_SECONDS = 1;
const SENTENCE_END_PATTERN = /[.!?…]$/;
const SENTENCE_END_TOKEN_PATTERN = /[.!?…]+["'”’)\]]*$/;
const MAX_SUBTITLE_GROUP_WORDS = 7;
const MAX_SUBTITLE_GROUP_UNITS = 42;
const PLAYBACK_RATES = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 2];
const EXPORT_PLAYBACK_RATES = [1, 1.05, 1.1, 1.15, 1.2, 1.25, 1.3, 1.4, 1.5];
const BALANCED_VERTICAL_OFFSET_OPTIONS = [
  { value: 0, label: "0px merkez" },
  { value: 40, label: "40px" },
  { value: 60, label: "60px" },
  { value: 80, label: "80px" },
  { value: 120, label: "120px" },
  { value: 160, label: "160px" },
  { value: 200, label: "200px" },
  { value: 240, label: "240px" },
  { value: 300, label: "300px en yukari" },
];
const SUBTITLE_MARGIN_OPTIONS = [
  { value: 260, label: "260px eski" },
  { value: 320, label: "320px" },
  { value: 380, label: "380px" },
  { value: 420, label: "420px önerilen" },
  { value: 480, label: "480px" },
  { value: 540, label: "540px en yüksek" },
];
const SUBTITLE_FONT_OPTIONS = ["Arial", "Arial Black", "Impact", "Segoe UI", "Tahoma", "Verdana"];
const PREVIEW_SEEK_STEP_SECONDS = 2;

interface CutRangeEdit {
  id: string;
  start: string;
  end: string;
}

interface InsertRangeEdit {
  id: string;
  sourceStart: string;
  sourceEnd: string;
  insertAt: string;
}

interface SubtitleTimeEdit {
  start: string;
  end: string;
}

function formatTime(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return "--:--";
  const whole = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(whole / 60);
  const remainder = whole % 60;
  return `${minutes}:${remainder.toString().padStart(2, "0")}`;
}

function formatEditorTime(seconds: number): string {
  const safe = Math.max(0, seconds);
  const minutes = Math.floor(safe / 60);
  const remainder = safe - minutes * 60;
  return `${minutes}:${remainder.toFixed(2).padStart(5, "0")}`;
}

function formatPreviewTime(seconds: number): string {
  const safe = Math.max(0, seconds);
  const minutes = Math.floor(safe / 60);
  const remainder = safe - minutes * 60;
  return `${minutes}:${remainder.toFixed(2).padStart(5, "0")}`;
}

function formatRate(rate: number): string {
  return `${rate.toFixed(rate === 1 ? 0 : 2)}x`;
}

function formatClipRelativeTime(value: number): string {
  return formatEditorTime(Math.max(0, value));
}

function parseEditorTime(value: string): number | null {
  const normalized = value.trim().replace(",", ".");
  if (!normalized) return null;
  if (!normalized.includes(":")) {
    const seconds = Number(normalized);
    return Number.isFinite(seconds) && seconds >= 0 ? seconds : null;
  }
  const parts = normalized.split(":");
  if (parts.length !== 2) return null;
  const minutes = Number(parts[0]);
  const seconds = Number(parts[1]);
  if (
    !Number.isInteger(minutes)
    || minutes < 0
    || !Number.isFinite(seconds)
    || seconds < 0
    || seconds >= 60
  ) {
    return null;
  }
  return minutes * 60 + seconds;
}

function normalizeEditorBounds(
  parsedStart: number,
  parsedEnd: number,
  sourceDuration: number | null,
): { parsedStart: number; parsedEnd: number } {
  let nextEnd = parsedEnd;
  if (sourceDuration !== null && Number.isFinite(sourceDuration)) {
    if (parsedStart >= sourceDuration) {
      throw new Error(`Başlangıç video süresini aşamaz. Video sonu: ${formatEditorTime(sourceDuration)}`);
    }
    if (nextEnd > sourceDuration) {
      if (nextEnd - sourceDuration <= END_TIME_TOLERANCE_SECONDS) {
        nextEnd = sourceDuration;
      } else {
        throw new Error(`Bitiş video süresini aşamaz. Video sonu: ${formatEditorTime(sourceDuration)}`);
      }
    }
  }
  if (nextEnd <= parsedStart) {
    throw new Error("Bitiş zamanı başlangıçtan sonra olmalıdır.");
  }
  if (nextEnd - parsedStart < MIN_MANUAL_CLIP_SECONDS) {
    throw new Error("Klip süresi en az 1 saniye olmalıdır.");
  }
  return { parsedStart, parsedEnd: nextEnd };
}

function cutsToEdits(clip: ClipCandidate): CutRangeEdit[] {
  return (clip.cut_ranges ?? []).map((cut, index) => ({
    id: `cut-${index}-${cut.start.toFixed(3)}`,
    start: formatClipRelativeTime(cut.start - clip.start),
    end: formatClipRelativeTime(cut.end - clip.start),
  }));
}

function insertsToEdits(clip: ClipCandidate): InsertRangeEdit[] {
  return (clip.insert_ranges ?? []).map((insert, index) => ({
    id: `insert-${index}-${insert.insert_at.toFixed(3)}`,
    sourceStart: formatEditorTime(insert.source_start),
    sourceEnd: formatEditorTime(insert.source_end),
    insertAt: formatEditorTime(insert.insert_at),
  }));
}

function cuesToTimeEdits(cues: SubtitleCue[], clipStart: number): Record<string, SubtitleTimeEdit> {
  return Object.fromEntries(
    cues.map((cue) => [
      cue.id,
      {
        start: formatClipRelativeTime(cue.start - clipStart),
        end: formatClipRelativeTime(cue.end - clipStart),
      },
    ]),
  );
}

function sameCutRanges(left: CutRange[], right: CutRange[]): boolean {
  if (left.length !== right.length) return false;
  return left.every((cut, index) => (
    Math.abs(cut.start - right[index].start) <= 0.001
    && Math.abs(cut.end - right[index].end) <= 0.001
  ));
}

function sameInsertRanges(left: InsertRange[], right: InsertRange[]): boolean {
  if (left.length !== right.length) return false;
  return left.every((insert, index) => (
    Math.abs(insert.source_start - right[index].source_start) <= 0.001
    && Math.abs(insert.source_end - right[index].source_end) <= 0.001
    && Math.abs(insert.insert_at - right[index].insert_at) <= 0.001
  ));
}

function effectiveClipDuration(clip: ClipCandidate): number {
  const baseDuration = clip.end - clip.start;
  const removedDuration = (clip.cut_ranges ?? []).reduce(
    (total, cut) => total + cut.end - cut.start,
    0,
  );
  const insertedDuration = (clip.insert_ranges ?? []).reduce(
    (total, insert) => total + insert.source_end - insert.source_start,
    0,
  );
  const hook = clip.edit_settings.hook_range;
  let hookAddition = 0;
  if (hook) {
    hookAddition = hook.source_end - hook.source_start;
    if (clip.edit_settings.hook_remove_from_body) {
      const overlap = Math.max(0, Math.min(clip.end, hook.source_end) - Math.max(clip.start, hook.source_start));
      const alreadyCut = (clip.cut_ranges ?? []).reduce((total, cut) => total + Math.max(0,
        Math.min(cut.end, hook.source_end) - Math.max(cut.start, hook.source_start)), 0);
      hookAddition -= Math.max(0, overlap - alreadyCut);
    }
  }
  return Math.max(0, baseDuration - removedDuration + insertedDuration + hookAddition);
}

function countWords(text: string): number {
  return text.trim().split(/\s+/).filter(Boolean).length;
}

function splitSubtitleText(text: string): string[] {
  return text.trim().split(/\s+/).filter(Boolean);
}

function subtitleTextUnits(text: string): number {
  return [...text].reduce((total, char) => {
    const lower = char.toLocaleLowerCase("tr-TR");
    if (/\s/.test(char)) return total + 0.35;
    if (`.,:;!?…'"-–—()[]`.includes(char)) return total + 0.45;
    if (["i", "ı", "l", "j", "t", "f", "r"].includes(lower)) return total + 0.62;
    if (["m", "w", "ğ", "ş"].includes(lower)) return total + 1.18;
    return total + 1;
  }, 0);
}

function subtitleWordsText(words: SubtitleWord[]): string {
  return words.map((word) => word.text.trim()).filter(Boolean).join(" ");
}

function isSentenceEndToken(text: string): boolean {
  return SENTENCE_END_TOKEN_PATTERN.test(text.trim());
}

function wordsForGrouping(cue: SubtitleCue): SubtitleWord[] | null {
  if (!cue.words.length) return null;
  const tokens = splitSubtitleText(cue.text);
  if (tokens.length === cue.words.length) {
    return cue.words.map((word, index) => ({ ...word, text: tokens[index] }));
  }
  if (subtitleWordsText(cue.words).trim() === cue.text.trim()) {
    return cue.words.map((word) => ({ ...word }));
  }
  return null;
}

function makeSubtitleCueFromWords(words: SubtitleWord[], index: number): SubtitleCue {
  return {
    id: `group-${index}-${words[0].start.toFixed(3)}`,
    start: words[0].start,
    end: words[words.length - 1].end,
    text: subtitleWordsText(words),
    words,
  };
}

function shouldStartNewSubtitleGroup(
  current: SubtitleWord[],
  word: SubtitleWord,
): boolean {
  if (!current.length) return false;
  const candidate = [...current, word];
  return (
    candidate.length > MAX_SUBTITLE_GROUP_WORDS
    || subtitleTextUnits(subtitleWordsText(candidate)) > MAX_SUBTITLE_GROUP_UNITS
  );
}

function regroupSubtitleWords(words: SubtitleWord[]): SubtitleCue[] {
  const grouped: SubtitleCue[] = [];
  let current: SubtitleWord[] = [];

  const flush = () => {
    if (!current.length) return;
    grouped.push(makeSubtitleCueFromWords(current, grouped.length));
    current = [];
  };

  for (const word of [...words].sort((left, right) => left.start - right.start)) {
    if (shouldStartNewSubtitleGroup(current, word)) {
      flush();
    }
    current.push({ ...word });
    if (isSentenceEndToken(word.text)) {
      flush();
    }
  }
  flush();
  return grouped;
}

function mergeSubtitleCues(group: SubtitleCue[]): SubtitleCue {
  const first = group[0];
  const last = group[group.length - 1];
  const words = group
    .flatMap((cue) => cue.words)
    .sort((left, right) => left.start - right.start);
  return {
    ...first,
    end: last.end,
    text: group.map((cue) => cue.text.trim()).filter(Boolean).join(" "),
    words,
  };
}

function cloneSubtitleCues(cues: SubtitleCue[]): SubtitleCue[] {
  return cues.map((cue) => ({
    ...cue,
    words: cue.words.map((word) => ({ ...word })),
  }));
}

function regroupSubtitleCuesBySentence(cues: SubtitleCue[]): SubtitleCue[] {
  const sorted = [...cues].sort((left, right) => left.start - right.start);
  const groupedWords: SubtitleWord[] = [];
  for (const cue of sorted) {
    const words = wordsForGrouping(cue);
    if (!words) {
      groupedWords.length = 0;
      break;
    }
    groupedWords.push(...words);
  }
  if (groupedWords.length) {
    return regroupSubtitleWords(groupedWords);
  }

  const grouped: SubtitleCue[] = [];
  let current: SubtitleCue[] = [];
  let currentWords = 0;

  const flush = () => {
    if (!current.length) return;
    grouped.push(mergeSubtitleCues(current));
    current = [];
    currentWords = 0;
  };

  for (const cue of sorted) {
    current.push(cue);
    currentWords += countWords(cue.text);
    const text = cue.text.trim();
    if (SENTENCE_END_PATTERN.test(text) || currentWords >= MAX_SUBTITLE_GROUP_WORDS) {
      flush();
    }
  }
  flush();
  return grouped;
}

function manualCueId(id: string): string {
  return id.startsWith("manual-") ? id : `manual-${id}`;
}

function retimeCueToBounds(
  cue: SubtitleCue,
  start: number,
  end: number,
  manualTiming: boolean,
): SubtitleCue {
  const tokens = splitSubtitleText(cue.text);
  if (!tokens.length) {
    return {
      ...cue,
      id: manualTiming ? manualCueId(cue.id) : cue.id,
      start,
      end,
      words: [],
    };
  }
  const duration = Math.max(0.01, end - start);
  const words = !manualTiming && cue.words.length === tokens.length
    ? cue.words.map((word, index) => {
        const wordStart = Math.max(start, Math.min(word.start, end));
        const wordEnd = Math.max(wordStart + 0.01, Math.min(word.end, end));
        return { text: tokens[index], start: wordStart, end: wordEnd };
      })
    : tokens.map((token, index) => ({
        text: token,
        start: start + (duration * index) / tokens.length,
        end: start + (duration * (index + 1)) / tokens.length,
      }));
  return {
    ...cue,
    id: manualTiming ? manualCueId(cue.id) : cue.id,
    start,
    end,
    words,
  };
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat("tr-TR", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function Icon({ name }: { name: "upload" | "spark" | "film" | "download" | "trash" }) {
  const paths = {
    upload: "M12 16V4m0 0L7 9m5-5 5 5M5 15v4h14v-4",
    spark: "M12 3l1.6 5.4L19 10l-5.4 1.6L12 17l-1.6-5.4L5 10l5.4-1.6L12 3z",
    film: "M4 5h16v14H4V5zm4 0v14m8-14v14M4 9h4m8 0h4M4 15h4m8 0h4",
    download: "M12 4v11m0 0l-4-4m4 4 4-4M5 19h14",
    trash: "M5 7h14m-9 4v5m4-5v5M8 7l1-3h6l1 3m1 0-1 13H8L7 7",
  };
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d={paths[name]} />
    </svg>
  );
}

function UploadPanel({
  busy,
  addingPaths,
  progress,
  onFiles,
  onPaths,
  importing,
  onYoutube,
}: {
  busy: boolean;
  addingPaths: boolean;
  progress: number;
  onFiles: (files: File[]) => void;
  onPaths: (paths: string[]) => Promise<boolean>;
  importing: boolean;
  onYoutube: (url: string) => Promise<void>;
}) {
  const [dragging, setDragging] = useState(false);
  const [source, setSource] = useState<"paths" | "upload" | "youtube">("paths");
  const input = useRef<HTMLInputElement>(null);
  const accept = (files: FileList | null) => {
    const selected = Array.from(files ?? []);
    if (selected.length && !busy && !importing) onFiles(selected);
  };
  const drop = (event: DragEvent) => {
    event.preventDefault();
    setDragging(false);
    accept(event.dataTransfer.files);
  };
  return (
    <section className="empty-state">
      <div className="eyebrow">YEREL AI VİDEO STÜDYOSU</div>
      <h1>Videolarını ekle.<br /><span>Birlikte veya ayrı işle.</span></h1>
      <p>
        Aynı projeye bir veya birden fazla video ekle. Videolarını birleştir,
        seçtiğin videodaki konuşmaları sustur veya LongToShorts ile kısa klipler oluştur.
      </p>
      <div className="source-chooser" role="group" aria-label="Video kaynağı">
        <button type="button" aria-pressed={source === "paths"} disabled={busy || importing} onClick={() => setSource("paths")}>Yerel dosyalar</button>
        <button type="button" aria-pressed={source === "upload"} disabled={busy || importing} onClick={() => setSource("upload")}>Kopyalayarak yükle</button>
        <button type="button" aria-pressed={source === "youtube"} disabled={busy || importing} onClick={() => setSource("youtube")}>YouTube’dan al</button>
      </div>
      {source === "paths" && <LocalVideoPaths disabled={busy || importing} pending={addingPaths} onPaths={onPaths} />}
      {source === "youtube" && <YoutubeImport busy={importing} onImport={onYoutube} />}
      {source === "upload" && <button
        className={`drop-zone ${dragging ? "dragging" : ""}`}
        type="button"
        disabled={busy}
        onClick={() => input.current?.click()}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={drop}
      >
        <input
          ref={input}
          type="file"
          multiple
          accept=".mp4,.mov,.mkv,.m4v,.avi,.webm"
          onChange={(event) => {
            accept(event.currentTarget.files);
            event.currentTarget.value = "";
          }}
        />
        <span className="upload-icon"><Icon name="upload" /></span>
        <strong>{busy ? "Videolar yükleniyor" : "Videolarını buraya bırak"}</strong>
        <span>{busy ? `%${Math.round(progress * 100)}${progress >= 1 ? " · Videolar doğrulanıyor" : ""}` : "veya bilgisayarından birden fazla dosya seç"}</span>
        {busy && <i className="upload-progress"><b style={{ width: `${progress * 100}%` }} /></i>}
      </button>}
      <div className="feature-row">
        <span><b>01</b> Bir projede birden fazla video</span>
        <span><b>02</b> Ayrı sekmede video birleştirme</span>
        <span><b>03</b> Seçilen videoya özel araçlar</span>
      </div>
      <TopicAnalysisPanel />
    </section>
  );
}

function JobProgress({ job, compact = false, onRetry, retrying = false }: {
  job: JobDetail;
  compact?: boolean;
  onRetry?: () => void;
  retrying?: boolean;
}) {
  const failed = job.status === "failed" || job.status === "interrupted";
  const exporting = job.status === "exporting";
  const importing = job.status === "importing" || (job.status === "queued" && job.source_type === "youtube");
  const progress = exporting
    ? Math.max(0, ...job.exports.filter((item) => item.status === "rendering" || item.status === "queued").map((item) => item.progress))
    : job.progress;
  return (
    <section className={`processing-card ${failed ? "failed" : ""} ${compact ? "compact" : ""}`}>
      <div className="processing-orbit">
        <span>{failed ? "!" : Math.round(progress * 100)}</span>
      </div>
      <div>
        <div className="eyebrow">{failed ? "İŞLEM DURDU" : importing ? "YOUTUBE VİDEOSU ALINIYOR" : exporting ? "VİDEO HAZIRLANIYOR" : "KONU ANALİZİ DEVAM EDİYOR"}</div>
        <h2>{job.stage}</h2>
        <p>{job.error ?? (importing ? "Video bu bilgisayara indiriliyor. Ardından altyazı ve Codex konu analizi otomatik başlayacak." : exporting ? "Kurgu, altyazılar ve efektler videoya işleniyor." : "Konuşma metni hazırlanır; Codex konuları ve anlamlı kesit sınırlarını belirler.")}</p>
        {failed && onRetry && <button type="button" className="secondary-button" disabled={retrying} onClick={onRetry}>
          {retrying ? "Başlatılıyor" : job.source_type === "youtube" ? "İşlemi yeniden dene" : "Codex analizini yeniden dene"}
        </button>}
        {!failed && (
          <div className="main-progress">
            <i style={{ width: `${progress * 100}%` }} />
          </div>
        )}
      </div>
    </section>
  );
}

function ClipList({
  clips,
  activeId,
  onActivate,
  onToggle,
}: {
  clips: ClipCandidate[];
  activeId: string | null;
  onActivate: (clip: ClipCandidate) => void;
  onToggle: (clip: ClipCandidate, selected: boolean) => void;
}) {
  return (
    <div className="clip-list">
      {clips.map((clip) => {
        const manual = clip.selection_method === "manual" || clip.reasons.includes("Manuel aralık");
        const agent = clip.selection_method === "agent";
        return (
          <article
            key={clip.id}
            className={`clip-card ${activeId === clip.id ? "active" : ""}`}
            onClick={() => onActivate(clip)}
          >
            <label className="check" onClick={(event) => event.stopPropagation()}>
              <input
                type="checkbox"
                checked={clip.selected}
                onChange={(event) => onToggle(clip, event.target.checked)}
              />
              <span />
            </label>
            <div className="rank">{String(clip.rank).padStart(2, "0")}</div>
            <div className="clip-copy">
              <h3>{clip.title}</h3>
              <div className="clip-meta">
                <span>{formatEditorTime(clip.start)} → {formatEditorTime(clip.end)}</span>
                <span>{Math.round(effectiveClipDuration(clip))} sn çıktı</span>
              </div>
              <div className="reason-row">
                {clip.reasons.map((reason) => <span key={reason}>{reason}</span>)}
              </div>
            </div>
            <div
              className={`score ${manual ? "manual" : ""} ${agent ? "agent" : ""}`}
              title={
                manual
                  ? "Manuel klip"
                  : agent ? "Codex konuşmayı okuyarak bu kesiti önerdi."
                  : "Önceki otomatik seçim sisteminden kayıtlı kesit"
              }
            >
              <b>{manual ? "M" : agent ? "AI" : "K"}</b>
              <span>{manual ? "MANUEL" : agent ? "CODEX" : "KAYITLI"}</span>
            </div>
          </article>
        );
      })}
    </div>
  );
}

function ManualClipCreator({
  duration,
  onCreate,
}: {
  duration: number | null;
  onCreate: (start: number, end: number) => Promise<void>;
}) {
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [creating, setCreating] = useState(false);
  const [message, setMessage] = useState("");

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setMessage("");
    const parsedStart = parseEditorTime(start);
    const parsedEnd = parseEditorTime(end);
    if (parsedStart === null || parsedEnd === null) {
      setMessage("Zamanları dakika:saniye biçiminde girin. Örnek: 12:30");
      return;
    }
    try {
      const normalized = normalizeEditorBounds(parsedStart, parsedEnd, duration);
      setCreating(true);
      await onCreate(normalized.parsedStart, normalized.parsedEnd);
      setStart("");
      setEnd("");
      setMessage("Manuel klip oluşturuldu ve listenin sonuna eklendi.");
    } catch (requestError) {
      setMessage(requestError instanceof Error ? requestError.message : "Manuel klip oluşturulamadı.");
    } finally {
      setCreating(false);
    }
  };

  return (
    <form className="manual-clip-creator" onSubmit={submit}>
      <div className="manual-clip-copy">
        <span>MANUEL BÖLÜM</span>
        <strong>İstediğin zaman aralığından klip oluştur</strong>
        <small>Altyazı, kadraj ve dışa aktarma ayarları diğer kliplerle aynı hazırlanır.</small>
      </div>
      <label>
        Başlangıç (dk:sn)
        <input
          value={start}
          inputMode="decimal"
          placeholder="12:30"
          onChange={(event) => setStart(event.target.value)}
        />
      </label>
      <label>
        Bitiş (dk:sn)
        <input
          value={end}
          inputMode="decimal"
          placeholder="13:15"
          onChange={(event) => setEnd(event.target.value)}
        />
      </label>
      <button className="primary-button" type="submit" disabled={creating}>
        <Icon name="spark" />
        {creating ? "Oluşturuluyor" : "Klibi oluştur"}
      </button>
      {message && (
        <p className={`manual-clip-message ${message.startsWith("Manuel klip oluşturuldu") ? "success" : "error"}`}>
          {message}
        </p>
      )}
    </form>
  );
}

function Editor({
  job,
  clip,
  previewTime,
  onSaved,
  onPreview,
  exportPlaybackRate,
}: {
  job: JobDetail;
  clip: ClipCandidate;
  previewTime: number;
  onSaved: (clip: ClipCandidate) => void;
  onPreview: (clip: ClipCandidate) => Promise<void>;
  exportPlaybackRate: number;
}) {
  const [start, setStart] = useState(formatEditorTime(clip.start));
  const [end, setEnd] = useState(formatEditorTime(clip.end));
  const [cues, setCues] = useState<SubtitleCue[]>(clip.subtitles);
  const [cueTimeEdits, setCueTimeEdits] = useState<Record<string, SubtitleTimeEdit>>(() =>
    cuesToTimeEdits(clip.subtitles, clip.start),
  );
  const [cutEdits, setCutEdits] = useState<CutRangeEdit[]>(() => cutsToEdits(clip));
  const [insertEdits, setInsertEdits] = useState<InsertRangeEdit[]>(() =>
    insertsToEdits(clip),
  );
  const [initialCues, setInitialCues] = useState<SubtitleCue[]>(() =>
    cloneSubtitleCues(clip.subtitles),
  );
  const [previousCues, setPreviousCues] = useState<SubtitleCue[] | null>(null);
  const [framingMode, setFramingMode] = useState<"fit" | "balanced" | "fill">(
    clip.framing_mode,
  );
  const [faceTracking, setFaceTracking] = useState(clip.face_tracking_enabled);
  const [saving, setSaving] = useState(false);
  const [framingSaving, setFramingSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [messageTone, setMessageTone] = useState<"info" | "success" | "error">("info");

  useEffect(() => {
    setStart(formatEditorTime(clip.start));
    setEnd(formatEditorTime(clip.end));
    setCues(clip.subtitles);
    setCueTimeEdits(cuesToTimeEdits(clip.subtitles, clip.start));
    setCutEdits(cutsToEdits(clip));
    setInsertEdits(insertsToEdits(clip));
    setPreviousCues(null);
    setFramingMode(clip.framing_mode);
    setFaceTracking(clip.face_tracking_enabled);
    setMessage("");
    setMessageTone("info");
  }, [clip.id]);

  useEffect(() => {
    setInitialCues(cloneSubtitleCues(clip.subtitles));
  }, [clip.id]);

  const updateCue = (cueId: string, text: string) => {
    setCues((current) =>
      current.map((cue) => (cue.id === cueId ? { ...cue, text } : cue)),
    );
  };
  const updateCueTime = (cueId: string, field: "start" | "end", value: string) => {
    setCueTimeEdits((current) => ({
      ...current,
      [cueId]: {
        start: current[cueId]?.start ?? "0:00.00",
        end: current[cueId]?.end ?? "0:01.00",
        [field]: value,
      },
    }));
  };
  const replaceCues = (nextCues: SubtitleCue[], baseStart = clip.start) => {
    setCues(nextCues);
    setCueTimeEdits(cuesToTimeEdits(nextCues, baseStart));
  };
  const addCue = () => {
    setPreviousCues(cloneSubtitleCues(cues));
    const safePreviewTime = Number.isFinite(previewTime) ? previewTime : clip.start;
    const nextStart = Math.min(
      Math.max(clip.start, safePreviewTime),
      Math.max(clip.start, clip.end - 0.2),
    );
    const nextEnd = Math.min(clip.end, nextStart + 1.5);
    const cue: SubtitleCue = {
      id: `manual-new-${Date.now()}`,
      start: Number(nextStart.toFixed(3)),
      end: Number(Math.max(nextStart + 0.01, nextEnd).toFixed(3)),
      text: "Yeni altyazi",
      words: [],
    };
    replaceCues(
      [...cues, retimeCueToBounds(cue, cue.start, cue.end, true)]
        .sort((left, right) => left.start - right.start),
    );
    setMessage("Yeni altyazi onizleme zamanina eklendi. Zamanini ve metnini duzenleyip kaydedin.");
    setMessageTone("info");
  };
  const removeCue = (cueId: string) => {
    if (cues.length <= 1) {
      setMessage("En az bir altyazi satiri kalmali.");
      setMessageTone("error");
      return;
    }
    setPreviousCues(cloneSubtitleCues(cues));
    replaceCues(cues.filter((cue) => cue.id !== cueId));
    setMessage("Altyazi satiri silindi. Kaydetmeyi unutmayin.");
    setMessageTone("info");
  };
  const updateCut = (cutId: string, field: "start" | "end", value: string) => {
    setCutEdits((current) =>
      current.map((cut) => (cut.id === cutId ? { ...cut, [field]: value } : cut)),
    );
  };
  const addCut = () => {
    setCutEdits((current) => [
      ...current,
      {
        id: `cut-new-${Date.now()}`,
        start: "0:00.00",
        end: "0:01.00",
      },
    ]);
  };
  const removeCut = (cutId: string) => {
    setCutEdits((current) => current.filter((cut) => cut.id !== cutId));
  };
  const updateInsert = (
    insertId: string,
    field: "sourceStart" | "sourceEnd" | "insertAt",
    value: string,
  ) => {
    setInsertEdits((current) =>
      current.map((insert) => (
        insert.id === insertId ? { ...insert, [field]: value } : insert
      )),
    );
  };
  const addInsert = () => {
    setInsertEdits((current) => [
      ...current,
      {
        id: `insert-new-${Date.now()}`,
        sourceStart: "1:20.00",
        sourceEnd: "1:30.00",
        insertAt: formatEditorTime(clip.start),
      },
    ]);
  };
  const removeInsert = (insertId: string) => {
    setInsertEdits((current) => current.filter((insert) => insert.id !== insertId));
  };
  const updateFraming = async (
    nextMode: "fit" | "balanced" | "fill",
    nextFaceTracking: boolean,
  ) => {
    const normalizedFaceTracking = nextMode === "fill" && nextFaceTracking;
    const previousMode = framingMode;
    const previousFaceTracking = faceTracking;
    setFramingMode(nextMode);
    setFaceTracking(normalizedFaceTracking);
    onSaved({
      ...clip,
      framing_mode: nextMode,
      face_tracking_enabled: normalizedFaceTracking,
      crop_keyframes: [],
    });
    setFramingSaving(true);
    setMessage("");
    setMessageTone("info");
    try {
      const updated = await api.updateClip(job.id, clip.id, {
        framing_mode: nextMode,
        face_tracking_enabled: normalizedFaceTracking,
      });
      onSaved(updated);
      setMessage("Kadraj kaydedildi.");
      setMessageTone("success");
    } catch (error) {
      setFramingMode(previousMode);
      setFaceTracking(previousFaceTracking);
      onSaved(clip);
      setMessage(error instanceof Error ? error.message : "Kadraj kaydedilemedi.");
      setMessageTone("error");
    } finally {
      setFramingSaving(false);
    }
  };
  const mergeCueWithNext = (cueId: string) => {
    const index = cues.findIndex((cue) => cue.id === cueId);
    if (index < 0 || index >= cues.length - 1) return;
    setPreviousCues(cloneSubtitleCues(cues));
    replaceCues([
      ...cues.slice(0, index),
      mergeSubtitleCues([cues[index], cues[index + 1]]),
      ...cues.slice(index + 2),
    ]);
    setMessage("Satırlar zamanları korunarak birleştirildi. Kaydetmeyi unutmayın.");
    setMessageTone("info");
  };
  const regroupBySentence = () => {
    setPreviousCues(cloneSubtitleCues(cues));
    replaceCues(regroupSubtitleCuesBySentence(cues));
    setMessage("Altyazılar cümle sonlarına göre gruplandı. Kaydetmeyi unutmayın.");
    setMessageTone("info");
  };
  const restorePreviousCues = () => {
    if (!previousCues) return;
    replaceCues(cloneSubtitleCues(previousCues));
    setPreviousCues(null);
    setMessage("Altyazılar bir önceki düzenine döndürüldü. Kaydetmeyi unutmayın.");
    setMessageTone("info");
  };
  const restoreInitialCues = () => {
    setPreviousCues(cloneSubtitleCues(cues));
    replaceCues(cloneSubtitleCues(initialCues));
    setMessage("Altyazılar en baştaki düzenine döndürüldü. Kaydetmeyi unutmayın.");
    setMessageTone("info");
  };
  const parsedBounds = () => {
    const parsedStart = parseEditorTime(start);
    const parsedEnd = parseEditorTime(end);
    if (parsedStart === null || parsedEnd === null) {
      throw new Error("Süreyi dakika:saniye biçiminde girin. Örnek: 10:29.50");
    }
    return normalizeEditorBounds(parsedStart, parsedEnd, job.duration);
  };
  const parsedCutRanges = (parsedStart: number, parsedEnd: number): CutRange[] => {
    const clipDuration = parsedEnd - parsedStart;
    const parsed = cutEdits.map((cut, index) => {
      const relativeStart = parseEditorTime(cut.start);
      const relativeEnd = parseEditorTime(cut.end);
      if (relativeStart === null || relativeEnd === null) {
        throw new Error("Kesilecek kısımları dakika:saniye biçiminde girin. Örnek: 0:08.50");
      }
      if (relativeStart < 0 || relativeEnd > clipDuration || relativeEnd <= relativeStart) {
        throw new Error(`${index + 1}. kesilecek kısım klip süresi içinde ve başlangıçtan sonra olmalıdır.`);
      }
      return {
        start: Number((parsedStart + relativeStart).toFixed(3)),
        end: Number((parsedStart + relativeEnd).toFixed(3)),
      };
    }).sort((left, right) => left.start - right.start);
    for (let index = 1; index < parsed.length; index += 1) {
      if (parsed[index].start < parsed[index - 1].end) {
        throw new Error("Kesilecek kısımlar birbiriyle çakışmamalıdır.");
      }
    }
    const removed = parsed.reduce((total, cut) => total + cut.end - cut.start, 0);
    if (clipDuration - removed < MIN_MANUAL_CLIP_SECONDS) {
      throw new Error("Kesitler çıkarıldıktan sonra klip süresi en az 1 saniye kalmalıdır.");
    }
    return parsed;
  };
  const parsedSubtitleCues = (parsedStart: number, parsedEnd: number): SubtitleCue[] => {
    const clipDuration = parsedEnd - parsedStart;
    const parsed = cues.map((cue, index) => {
      const edit = cueTimeEdits[cue.id] ?? {
        start: formatClipRelativeTime(cue.start - clip.start),
        end: formatClipRelativeTime(cue.end - clip.start),
      };
      const relativeStart = parseEditorTime(edit.start);
      const relativeEnd = parseEditorTime(edit.end);
      if (relativeStart === null || relativeEnd === null) {
        throw new Error("Altyazi zamanlarini dakika:saniye biciminde girin. Ornek: 0:12.30");
      }
      if (relativeStart < 0 || relativeEnd > clipDuration || relativeEnd <= relativeStart) {
        throw new Error(`${index + 1}. altyazi zamani klip suresi icinde ve bitis baslangictan sonra olmali.`);
      }
      const text = cue.text.trim();
      if (!text) {
        throw new Error(`${index + 1}. altyazi metni bos olamaz.`);
      }
      const absoluteStart = Number((parsedStart + relativeStart).toFixed(3));
      const absoluteEnd = Number((parsedStart + relativeEnd).toFixed(3));
      const timingChanged =
        Math.abs(absoluteStart - cue.start) > 0.001
        || Math.abs(absoluteEnd - cue.end) > 0.001
        || cue.id.startsWith("manual-");
      return retimeCueToBounds({ ...cue, text }, absoluteStart, absoluteEnd, timingChanged);
    }).sort((left, right) => left.start - right.start);
    return parsed;
  };
  const parsedInsertRanges = (
    parsedStart: number,
    parsedEnd: number,
    cutRanges: CutRange[],
  ): InsertRange[] => {
    const sourceDuration = job.duration ?? 0;
    if (insertEdits.length > 20) {
      throw new Error("Bir klibe en fazla 20 parça eklenebilir.");
    }
    return insertEdits.map((insert, index) => {
      const sourceStart = parseEditorTime(insert.sourceStart);
      const sourceEnd = parseEditorTime(insert.sourceEnd);
      const insertAt = parseEditorTime(insert.insertAt);
      if (sourceStart === null || sourceEnd === null || insertAt === null) {
        throw new Error(
          "Eklenecek parça zamanlarını dakika:saniye biçiminde girin. Örnek: 1:20.00",
        );
      }
      if (
        sourceEnd <= sourceStart
        || sourceEnd - sourceStart < 0.1
        || sourceEnd > sourceDuration
      ) {
        throw new Error(
          `${index + 1}. eklenecek kaynak aralığı video süresi içinde olmalıdır.`,
        );
      }
      if (insertAt < parsedStart || insertAt > parsedEnd) {
        throw new Error(
          `${index + 1}. yerleştirme noktası hedef klibin başlangıç ve bitişi arasında olmalıdır.`,
        );
      }
      if (cutRanges.some((cut) => cut.start < insertAt && insertAt < cut.end)) {
        throw new Error(
          `${index + 1}. yerleştirme noktası kesilecek bir aralığın içinde olamaz.`,
        );
      }
      return {
        source_start: Number(sourceStart.toFixed(3)),
        source_end: Number(sourceEnd.toFixed(3)),
        insert_at: Number(insertAt.toFixed(3)),
      };
    }).sort((left, right) => left.insert_at - right.insert_at);
  };
  const save = async () => {
    setSaving(true);
    setMessage("");
    setMessageTone("info");
    try {
      const { parsedStart, parsedEnd } = parsedBounds();
      const nextCutRanges = parsedCutRanges(parsedStart, parsedEnd);
      const nextInsertRanges = parsedInsertRanges(
        parsedStart,
        parsedEnd,
        nextCutRanges,
      );
      const nextCues = parsedSubtitleCues(parsedStart, parsedEnd);
      const boundsChanged =
        Math.abs(parsedStart - clip.start) > 0.001
        || Math.abs(parsedEnd - clip.end) > 0.001;
      const cutsChanged = !sameCutRanges(nextCutRanges, clip.cut_ranges ?? []);
      const insertsChanged = !sameInsertRanges(
        nextInsertRanges,
        clip.insert_ranges ?? [],
      );
      if (false) {
        throw new Error(
          "Kelime sayısı değişen altyazı satırı zaman kaydırır. Cümle için satırları birleştirin ya da Zamanları transkriptten düzelt'i kullanın.",
        );
      }
      const updated = await api.updateClip(job.id, clip.id, {
        start: parsedStart,
        end: parsedEnd,
        framing_mode: framingMode,
        face_tracking_enabled: framingMode === "fill" && faceTracking,
        cut_ranges: nextCutRanges,
        insert_ranges: nextInsertRanges,
        subtitles: boundsChanged || cutsChanged ? undefined : nextCues,
      });
      onSaved(updated);
      replaceCues(updated.subtitles, updated.start);
      setCutEdits(cutsToEdits(updated));
      setInsertEdits(insertsToEdits(updated));
      setStart(formatEditorTime(updated.start));
      setEnd(formatEditorTime(updated.end));
      setMessage(
        boundsChanged || cutsChanged || insertsChanged
          ? "Süre, kesit, eklenen parçalar ve altyazı zaman çizelgesi güncellendi."
          : "Değişiklikler kaydedildi.",
      );
      setMessageTone("success");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Kaydedilemedi.");
      setMessageTone("error");
    } finally {
      setSaving(false);
    }
  };
  const resetSubtitlesFromTranscript = async () => {
    setSaving(true);
    setMessage("");
    setMessageTone("info");
    try {
      const { parsedStart, parsedEnd } = parsedBounds();
      const nextCutRanges = parsedCutRanges(parsedStart, parsedEnd);
      const nextInsertRanges = parsedInsertRanges(
        parsedStart,
        parsedEnd,
        nextCutRanges,
      );
      const nextCues = parsedSubtitleCues(parsedStart, parsedEnd);
      const updated = await api.updateClip(job.id, clip.id, {
        start: parsedStart,
        end: parsedEnd,
        framing_mode: framingMode,
        face_tracking_enabled: framingMode === "fill" && faceTracking,
        cut_ranges: nextCutRanges,
        insert_ranges: nextInsertRanges,
        subtitles: nextCues,
        reset_subtitles: true,
      });
      onSaved(updated);
      replaceCues(updated.subtitles, updated.start);
      setCutEdits(cutsToEdits(updated));
      setInsertEdits(insertsToEdits(updated));
      setInitialCues(cloneSubtitleCues(updated.subtitles));
      setPreviousCues(null);
      setStart(formatEditorTime(updated.start));
      setEnd(formatEditorTime(updated.end));
      setMessage("Altyazı metinleri korunarak zamanlar transkriptten yeniden düzeltildi.");
      setMessageTone("success");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Altyazı zamanları düzeltilemedi.");
      setMessageTone("error");
    } finally {
      setSaving(false);
    }
  };
  const autoCutSilence = async () => {
    setSaving(true);
    setMessage("");
    setMessageTone("info");
    try {
      const { parsedStart, parsedEnd } = parsedBounds();
      const nextCutRanges = parsedCutRanges(parsedStart, parsedEnd);
      const nextInsertRanges = parsedInsertRanges(
        parsedStart,
        parsedEnd,
        nextCutRanges,
      );
      const updated = await api.updateClip(job.id, clip.id, {
        start: parsedStart,
        end: parsedEnd,
        framing_mode: framingMode,
        face_tracking_enabled: framingMode === "fill" && faceTracking,
        cut_ranges: nextCutRanges,
        insert_ranges: nextInsertRanges,
        auto_cut_silence: true,
      });
      onSaved(updated);
      replaceCues(updated.subtitles, updated.start);
      setCutEdits(cutsToEdits(updated));
      setInsertEdits(insertsToEdits(updated));
      setStart(formatEditorTime(updated.start));
      setEnd(formatEditorTime(updated.end));
      setMessage(
        `${updated.cut_ranges.length} kesit hazirlandi. Gurultu degil, transkriptteki insan konusmasi baz alindi.`,
      );
      setMessageTone("success");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Konusmasiz kisimlar bulunamadi.");
      setMessageTone("error");
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="editor" id="clip-editor">
      <div className="section-title">
        <div>
          <span>DÜZENLE</span>
          <h2>Klip {clip.rank}</h2>
        </div>
        <div className="editor-actions">
          <button className="secondary-button" onClick={save} disabled={saving}>
            {saving ? "Kaydediliyor" : "Değişiklikleri kaydet"}
          </button>
          {message && <div className={`inline-message ${messageTone}`}>{message}</div>}
        </div>
      </div>
      <MotovlogEditor key={clip.id} clip={clip} onSaved={onSaved}
        playbackRate={exportPlaybackRate}
        sourceDuration={job.duration} previewTime={previewTime} onPreview={onPreview}
        preview={job.exports.find((item) => item.clip_id === clip.id && item.is_preview)} />
      <div className="time-editor">
        <label>
          Başlangıç (dk:sn)
          <input
            value={start}
            inputMode="decimal"
            placeholder="10:29.50"
            onChange={(event) => setStart(event.target.value)}
          />
        </label>
        <div className="time-line"><i /></div>
        <label>
          Bitiş (dk:sn)
          <input
            value={end}
            inputMode="decimal"
            placeholder="11:27.50"
            onChange={(event) => setEnd(event.target.value)}
          />
        </label>
      </div>
      <div className="framing-section">
        <div className="framing-heading">
          <strong>Kadraj biçimi</strong>
          <span>Her klip için ayrı seçilir</span>
        </div>
        <div className="framing-options">
          <label className={`framing-option ${framingMode === "fit" ? "enabled" : ""}`}>
            <input
              type="radio"
              name={`framing-${clip.id}`}
              checked={framingMode === "fit"}
              disabled={framingSaving}
              onChange={() => void updateFraming("fit", false)}
            />
            <span className="focus-check" />
            <span className="focus-copy">
              <strong>Fit</strong>
              <small>Tüm görüntüyü korur, boş alanları bulanık doldurur.</small>
            </span>
          </label>
          <label
            className={`framing-option ${framingMode === "balanced" ? "enabled" : ""}`}
          >
            <input
              type="radio"
              name={`framing-${clip.id}`}
              checked={framingMode === "balanced"}
              disabled={framingSaving}
              onChange={() => void updateFraming("balanced", false)}
            />
            <span className="focus-check" />
            <span className="focus-copy">
              <strong>Dengeli</strong>
              <small>Daha büyük gösterir, üstte ve altta biraz boşluk bırakır.</small>
            </span>
          </label>
          <label className={`framing-option ${framingMode === "fill" ? "enabled" : ""}`}>
            <input
              type="radio"
              name={`framing-${clip.id}`}
              checked={framingMode === "fill"}
              disabled={framingSaving}
              onChange={() => void updateFraming("fill", faceTracking)}
            />
            <span className="focus-check" />
            <span className="focus-copy">
              <strong>Fill</strong>
              <small>9:16 ekranı tamamen doldurur, kenarları kırpar.</small>
            </span>
          </label>
        </div>
      </div>
      <label
        className={`focus-option ${faceTracking ? "enabled" : ""} ${framingMode !== "fill" ? "disabled" : ""}`}
      >
        <input
          type="checkbox"
          checked={faceTracking}
          disabled={framingMode !== "fill" || framingSaving}
          onChange={(event) => void updateFraming("fill", event.target.checked)}
        />
        <span className="focus-check" />
        <span className="focus-copy">
          <strong>Yüzü takip et</strong>
          <small>
            {framingMode !== "fill"
              ? "Yalnızca Fill kadraj biçiminde kullanılabilir."
              : faceTracking
              ? "Açık: kadraj algılanan yüzü takip eder."
              : "Kapalı: Fill kadraj sabit merkezden kırpılır."}
          </small>
        </span>
        <b>{framingSaving ? "KAYIT" : framingMode === "fill" && faceTracking ? "AÇIK" : "KAPALI"}</b>
      </label>
      <div className="subtitle-heading">
        <div>
          <h3>Altyazı metni</h3>
          <p>Kayıtlı metin dışa aktarırken sese göre tekrar senkronize edilir; cümle bölündüyse satırları birleştirerek kaymayı azaltın.</p>
        </div>
        <div className="subtitle-tools">
          <button
            type="button"
            className="tiny-button"
            onClick={regroupBySentence}
            disabled={cues.length < 2}
          >
            Cümleye göre grupla
          </button>
          <button
            type="button"
            className="tiny-button"
            onClick={restorePreviousCues}
            disabled={!previousCues}
          >
            Eski haline dön
          </button>
          <button
            type="button"
            className="tiny-button"
            onClick={restoreInitialCues}
          >
            En baştaki haline dön
          </button>
          <button
            type="button"
            className="tiny-button"
            onClick={resetSubtitlesFromTranscript}
            disabled={saving}
          >
            Zamanları transkriptten düzelt
          </button>
          <button
            type="button"
            className="tiny-button"
            onClick={addCue}
            disabled={saving}
          >
            Onizleme zamanina ekle
          </button>
          <span>{cues.length} grup</span>
        </div>
      </div>
      <div className="subtitle-list">
        {cues.map((cue, index) => (
          <div key={cue.id} className="subtitle-row">
            <label className="subtitle-time">
              Baslangic
              <input
                value={cueTimeEdits[cue.id]?.start ?? formatClipRelativeTime(cue.start - clip.start)}
                inputMode="decimal"
                onChange={(event) => updateCueTime(cue.id, "start", event.target.value)}
              />
            </label>
            <label className="subtitle-time">
              Bitis
              <input
                value={cueTimeEdits[cue.id]?.end ?? formatClipRelativeTime(cue.end - clip.start)}
                inputMode="decimal"
                onChange={(event) => updateCueTime(cue.id, "end", event.target.value)}
              />
            </label>
            <textarea
              rows={1}
              value={cue.text}
              onChange={(event) => updateCue(cue.id, event.target.value)}
            />
            <button
              type="button"
              className="tiny-button subtitle-merge"
              onClick={() => mergeCueWithNext(cue.id)}
              disabled={index >= cues.length - 1}
            >
              Sonrakiyle birleştir
            </button>
            <button
              type="button"
              className="tiny-button subtitle-delete"
              onClick={() => removeCue(cue.id)}
              disabled={cues.length <= 1}
            >
              Sil
            </button>
          </div>
        ))}
      </div>
      <div className="cut-heading">
        <div>
          <h3>Kesilecek kısımlar</h3>
          <p>Klip içindeki gereksiz aralıkları girin; kaydedince video ve altyazı zamanları buna göre kısalır.</p>
        </div>
        <div className="cut-actions">
          <button type="button" className="tiny-button" onClick={autoCutSilence} disabled={saving}>
            Konusmasiz yerleri bul
          </button>
          <button type="button" className="tiny-button" onClick={addCut} disabled={saving}>
            Kesit ekle
          </button>
        </div>
      </div>
      <div className="cut-list">
        {cutEdits.length === 0 ? (
          <div className="cut-empty">Henüz çıkarılacak aralık yok.</div>
        ) : cutEdits.map((cut, index) => (
          <div key={cut.id} className="cut-row">
            <span>{index + 1}</span>
            <label>
              Başlangıç
              <input
                value={cut.start}
                inputMode="decimal"
                placeholder="0:08.50"
                onChange={(event) => updateCut(cut.id, "start", event.target.value)}
              />
            </label>
            <label>
              Bitiş
              <input
                value={cut.end}
                inputMode="decimal"
                placeholder="0:12.00"
                onChange={(event) => updateCut(cut.id, "end", event.target.value)}
              />
            </label>
            <button
              type="button"
              className="tiny-button"
              onClick={() => removeCut(cut.id)}
            >
              Sil
            </button>
          </div>
        ))}
      </div>
      <div className="cut-heading insert-heading">
        <div>
          <h3>Başka yerden parça ekle</h3>
          <p>
            Kaynak videodan alınacak mutlak zaman aralığını ve hedef klipte hangi
            zamanın önüne yerleştirileceğini girin. Birleştirilmiş hali dışa
            aktarılan videoda görünür.
          </p>
        </div>
        <div className="cut-actions">
          <button type="button" className="tiny-button" onClick={addInsert} disabled={saving}>
            Parça ekle
          </button>
        </div>
      </div>
      <div className="cut-list">
        {insertEdits.length === 0 ? (
          <div className="cut-empty">Henüz başka bir zamandan eklenen parça yok.</div>
        ) : insertEdits.map((insert, index) => (
          <div key={insert.id} className="insert-row">
            <span>{index + 1}</span>
            <label>
              Kaynak başlangıç
              <input
                value={insert.sourceStart}
                inputMode="decimal"
                placeholder="1:20.00"
                onChange={(event) =>
                  updateInsert(insert.id, "sourceStart", event.target.value)}
              />
            </label>
            <label>
              Kaynak bitiş
              <input
                value={insert.sourceEnd}
                inputMode="decimal"
                placeholder="1:30.00"
                onChange={(event) =>
                  updateInsert(insert.id, "sourceEnd", event.target.value)}
              />
            </label>
            <label>
              Yerleştirme noktası
              <input
                value={insert.insertAt}
                inputMode="decimal"
                placeholder="10:44.00"
                onChange={(event) =>
                  updateInsert(insert.id, "insertAt", event.target.value)}
              />
            </label>
            <button
              type="button"
              className="tiny-button"
              onClick={() => removeInsert(insert.id)}
            >
              Sil
            </button>
          </div>
        ))}
      </div>
    </section>
  );
}

function Studio({
  job,
  onRefresh,
}: {
  job: JobDetail;
  onRefresh: () => Promise<void>;
}) {
  const initialClip = job.clips.find((clip) => clip.id === job.analysis?.clip_ids[0]) ?? job.clips[0];
  const [activeId, setActiveId] = useState(initialClip?.id ?? null);
  const [localClips, setLocalClips] = useState(job.clips);
  const [exporting, setExporting] = useState(false);
  const [llmSeoEnabled, setLlmSeoEnabled] = useState(false);
  const [exportPlaybackRate, setExportPlaybackRate] = useState(1);
  const [subtitleMarginV, setSubtitleMarginV] = useState(420);
  const [balancedVerticalOffset, setBalancedVerticalOffset] = useState(0);
  const [subtitleFontFamily, setSubtitleFontFamily] = useState("Arial");
  const [reanalyzing, setReanalyzing] = useState(false);
  const [error, setError] = useState("");
  const [previewTime, setPreviewTime] = useState(initialClip?.start ?? 0);
  const [playbackRate, setPlaybackRate] = useState(1);
  const video = useRef<HTMLVideoElement>(null);

  useEffect(() => setLocalClips(job.clips), [job.clips]);
  useEffect(() => {
    if (!localClips.some((clip) => clip.id === activeId)) {
      setActiveId(localClips[0]?.id ?? null);
    }
  }, [activeId, localClips]);
  const active = localClips.find((clip) => clip.id === activeId) ?? localClips[0];
  const clipElapsed = active ? Math.max(0, Math.min(previewTime - active.start, active.end - active.start)) : 0;
  const clipDuration = active ? Math.max(0, active.end - active.start) : 0;
  const analysisBusy = ACTIVE_STATUSES.has(job.status);
  const currentSuggestionIds = new Set(job.analysis?.clip_ids ?? []);
  const currentSuggestions = localClips.filter((clip) => currentSuggestionIds.has(clip.id));
  const savedClips = localClips.filter((clip) => !currentSuggestionIds.has(clip.id));

  useEffect(() => {
    const start = active?.start ?? 0;
    setPreviewTime(start);
    const element = video.current;
    if (!element || !active) return;

    const seekToClipStart = () => {
      try {
        element.currentTime = active.start;
        setPreviewTime(active.start);
      } catch {
        // The browser can reject seeking before metadata is ready; loadedmetadata retries it.
      }
    };

    if (element.readyState >= 1) {
      seekToClipStart();
      return;
    }
    element.addEventListener("loadedmetadata", seekToClipStart, { once: true });
    return () => element.removeEventListener("loadedmetadata", seekToClipStart);
  }, [active?.id, active?.start, active?.end]);

  useEffect(() => {
    if (video.current) {
      video.current.playbackRate = playbackRate;
    }
  }, [playbackRate, active?.id]);

  const activate = (clip: ClipCandidate) => {
    setActiveId(clip.id);
    setPreviewTime(clip.start);
    if (video.current) {
      video.current.currentTime = clip.start;
      video.current.playbackRate = playbackRate;
      void video.current.play().catch(() => {
        // Some browsers block programmatic playback; the user can still press play.
      });
    }
  };
  const seekPreview = (deltaSeconds: number) => {
    const element = video.current;
    if (!element || !active) return;
    const nextTime = Math.max(
      active.start,
      Math.min(active.end, element.currentTime + deltaSeconds),
    );
    element.currentTime = nextTime;
    setPreviewTime(nextTime);
  };
  const replaceClip = (updated: ClipCandidate) => {
    setLocalClips((clips) => clips.map((clip) => (clip.id === updated.id ? updated : clip)));
    if (updated.id === activeId) {
      setPreviewTime(updated.start);
    }
  };
  const toggle = async (clip: ClipCandidate, selected: boolean) => {
    replaceClip({ ...clip, selected });
    try {
      replaceClip(await api.updateClip(job.id, clip.id, { selected }));
    } catch (requestError) {
      replaceClip(clip);
      setError(requestError instanceof Error ? requestError.message : "Seçim kaydedilemedi.");
    }
  };
  const createManualClip = async (start: number, end: number) => {
    const created = await api.createClip(job.id, start, end);
    setLocalClips((clips) => [...clips, created]);
    setActiveId(created.id);
    setPreviewTime(created.start);
  };
  const createTopicClip = async (topic: TopicSection) => {
    const created = await api.createClip(job.id, topic.start, topic.end, topic.id);
    setLocalClips((clips) => [...clips, created]);
    setActiveId(created.id); setPreviewTime(created.start);
    await onRefresh();
    window.setTimeout(() => document.getElementById("clip-editor")?.scrollIntoView({ behavior: "smooth" }), 100);
  };
  const exportClip = async (clip: ClipCandidate) => {
    await api.exportClips(job.id, [clip.id], llmSeoEnabled, exportPlaybackRate, subtitleMarginV,
      balancedVerticalOffset, subtitleFontFamily);
    await onRefresh();
  };
  const exportSelected = async () => {
    const clipIds = localClips.filter((clip) => clip.selected).map((clip) => clip.id);
    if (!clipIds.length) {
      setError("Dışa aktarmak için en az bir klip seçin.");
      return;
    }
    setExporting(true);
    setError("");
    try {
      await api.exportClips(
        job.id,
        clipIds,
        llmSeoEnabled,
        exportPlaybackRate,
        subtitleMarginV,
        balancedVerticalOffset,
        subtitleFontFamily,
      );
      await onRefresh();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Dışa aktarma başlatılamadı.");
    } finally {
      setExporting(false);
    }
  };
  const previewClip = async (clip: ClipCandidate) => {
    await api.exportClips(job.id, [clip.id], false, exportPlaybackRate, subtitleMarginV,
      balancedVerticalOffset, subtitleFontFamily, true);
    await onRefresh();
  };
  const reanalyze = async () => {
    if (analysisBusy || reanalyzing) return;
    setReanalyzing(true);
    setError("");
    try {
      await api.reanalyze(job.id);
      await onRefresh();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Konu analizi başlatılamadı.");
    } finally {
      setReanalyzing(false);
    }
  };
  const completedExports = job.exports.filter((item) => item.status === "completed");

  if (!active) return <div className="studio">
    <TopicAnalysisPanel analysis={job.analysis} clips={localClips} onCreate={createTopicClip} busy={analysisBusy} />
    <TranscriptCorrection job={job} onRefresh={onRefresh} />
    {job.error && <div className="error-banner">{job.error}</div>}
    {analysisBusy && <JobProgress job={job} compact />}
    <div className="empty-suggestions">
      <h2>Henüz kesit yok</h2>
      <p>Konuşmanın konu haritasını inceleyebilir, yeni kesitler oluşturabilir veya kendin bir bölüm ekleyebilirsin.</p>
      <button type="button" className="secondary-button" onClick={reanalyze} disabled={analysisBusy || reanalyzing}>
        {reanalyzing ? "Başlatılıyor" : "Codex ile konuları çıkar"}
      </button>
    </div>
    <ManualClipCreator duration={job.duration} onCreate={createManualClip} />
  </div>;
  return (
    <div className="studio">
      <section className="studio-header">
        <div>
          <div className="eyebrow">{analysisBusy ? "İŞLEM DEVAM EDİYOR" : job.analysis ? "CODEX KONU ANALİZİ" : "KAYITLI VİDEO"} · {job.language?.toUpperCase()}</div>
          <h1>{job.analysis ? <>Konulara göre <span>{currentSuggestions.length} kesit.</span></> : <>Kayıtlı <span>{localClips.length} kesit.</span></>}</h1>
          <p>{job.filename} · {formatTime(job.duration)} · {job.width}×{job.height}</p>
        </div>
        <div className="header-actions">
          <label className="export-speed-control">
            Çıktı hızı
            <select
              value={exportPlaybackRate}
              onChange={(event) => setExportPlaybackRate(Number(event.target.value))}
            >
              {EXPORT_PLAYBACK_RATES.map((rate) => (
                <option key={rate} value={rate}>
                  {formatRate(rate)}
                </option>
              ))}
            </select>
          </label>
          <label className="export-subtitle-position-control">
            Altyazı yeri
            <select
              value={subtitleMarginV}
              onChange={(event) => setSubtitleMarginV(Number(event.target.value))}
            >
              {SUBTITLE_MARGIN_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            <small>Değer büyüdükçe yukarı çıkar.</small>
          </label>
          <label className="export-font-control">
            Altyazi fontu
            <select
              value={subtitleFontFamily}
              onChange={(event) => setSubtitleFontFamily(event.target.value)}
            >
              {SUBTITLE_FONT_OPTIONS.map((font) => (
                <option key={font} value={font}>
                  {font}
                </option>
              ))}
            </select>
          </label>
          <label className="export-balanced-offset-control">
            Dengeli kadraj
            <select
              value={balancedVerticalOffset}
              onChange={(event) => setBalancedVerticalOffset(Number(event.target.value))}
            >
              {BALANCED_VERTICAL_OFFSET_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            <small>Sadece Dengeli modda videoyu yukari tasir.</small>
          </label>
          <label className={`seo-toggle ${llmSeoEnabled ? "enabled" : ""}`}>
            <input
              type="checkbox"
              checked={llmSeoEnabled}
              onChange={(event) => setLlmSeoEnabled(event.target.checked)}
            />
            <span className="focus-check" />
            <span>
              <strong>Profesyonel LLM SEO</strong>
              <small>Kapalı: hızlı yerel SEO. Açık: Ollama ile daha kaliteli MD.</small>
            </span>
            <b>{llmSeoEnabled ? "AÇIK" : "KAPALI"}</b>
          </label>
          <button className="secondary-button" onClick={reanalyze} disabled={reanalyzing || analysisBusy}>
            {reanalyzing ? "Başlatılıyor" : "Codex ile konuları çıkar"}
          </button>
          <button className="primary-button" onClick={exportSelected} disabled={exporting}>
            <Icon name="spark" />
            {exporting ? "Hazırlanıyor" : "Seçilenleri dışa aktar"}
          </button>
        </div>
      </section>

      <TopicAnalysisPanel analysis={job.analysis} clips={localClips} onCreate={createTopicClip} busy={analysisBusy} onActivate={(clip) => {
        activate(clip);
        document.getElementById("clip-editor")?.scrollIntoView({ behavior: "smooth", block: "start" });
      }} />
      <TranscriptCorrection job={job} onRefresh={onRefresh} />
      {analysisBusy && <JobProgress job={job} compact />}
      {job.error && <div className="error-banner" role="alert"><strong>{job.stage}</strong><p>{job.error}</p></div>}
      {error && <div className="error-banner">{error}</div>}

      <div className="workspace">
        <aside className="preview-column">
          <div className="phone">
            <video
              key={`${active.id}-${active.start}-${active.end}`}
              ref={video}
              src={`/api/jobs/${job.id}/source#t=${active.start},${active.end}`}
              controls
              playsInline
              onTimeUpdate={(event) => {
                const current = event.currentTarget.currentTime;
                setPreviewTime(current);
                if (current >= active.end) {
                  event.currentTarget.pause();
                  setPreviewTime(active.end);
                }
              }}
              onLoadedMetadata={(event) => {
                event.currentTarget.currentTime = active.start;
                event.currentTarget.playbackRate = playbackRate;
                setPreviewTime(active.start);
              }}
              onSeeked={(event) => setPreviewTime(event.currentTarget.currentTime)}
            />
            <div className="preview-label">9:16 ÖNİZLEME</div>
          </div>
          <div className="preview-controls">
            <button
              type="button"
              onClick={() => seekPreview(-PREVIEW_SEEK_STEP_SECONDS)}
            >
              -2 sn
            </button>
            <label className="speed-control">
              Hız
              <select
                value={playbackRate}
                onChange={(event) => setPlaybackRate(Number(event.target.value))}
              >
                {PLAYBACK_RATES.map((rate) => (
                  <option key={rate} value={rate}>
                    {rate}x
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              onClick={() => seekPreview(PREVIEW_SEEK_STEP_SECONDS)}
            >
              +2 sn
            </button>
          </div>
          <div className="preview-time">
            <strong>{formatPreviewTime(previewTime)}</strong>
            <span>Kaynak zaman</span>
            <em>
              Klip {formatPreviewTime(clipElapsed)} / {formatPreviewTime(clipDuration)}
            </em>
          </div>
          <div className="preview-caption">
            <span>Kaynak önizlemesi</span>
            <p>
              {active.framing_mode === "fit"
                ? "Fit: tam görüntü ve bulanık arka plan kullanılır."
                : active.framing_mode === "balanced"
                  ? "Dengeli: görüntü büyütülür, üstte ve altta biraz boşluk bırakılır."
                : active.face_tracking_enabled
                  ? "Fill: 9:16 kadraj yüzü takip ederek ekranı doldurur."
                  : "Fill: sabit merkez kadrajı 9:16 ekranı doldurur."}
            </p>
          </div>
        </aside>

        <main className="editing-column">
          {!!currentSuggestions.length && <>
            <div className="section-title">
              <div><span>CODEX ÖNERİLERİ</span><h2>Konulara göre kesitler</h2></div>
              <small>{currentSuggestions.filter((clip) => clip.selected).length} seçili</small>
            </div>
            <ClipList clips={currentSuggestions} activeId={active.id} onActivate={activate} onToggle={toggle} />
          </>}
          {!!savedClips.length && <details className="saved-clips" open={!currentSuggestions.length || savedClips.some((clip) => clip.id === active.id)}>
            <summary>Kayıtlı ve manuel kesitlerin <span>{savedClips.length} kesit · {savedClips.filter((clip) => clip.selected).length} seçili</span></summary>
            <p>Önceki kesitlerin düzenleme ve çıktı kayıtlarıyla birlikte burada durur.</p>
            <ClipList clips={savedClips} activeId={active.id} onActivate={activate} onToggle={toggle} />
          </details>}
          <ManualClipCreator duration={job.duration} onCreate={createManualClip} />
          <Editor job={job} clip={active} previewTime={previewTime} onSaved={replaceClip} onPreview={previewClip}
            exportPlaybackRate={exportPlaybackRate} />
          <YoutubeStudio key={active.id} clip={active} sourceDuration={job.duration} previewTime={previewTime}
            onSaved={replaceClip} onRefresh={onRefresh} onExport={exportClip} busy={analysisBusy} />

          {!!job.exports.length && (
            <section className="exports">
              <div className="section-title">
                <div><span>ÇIKTILAR</span><h2>Dışa aktarmalar</h2></div>
                <small>{completedExports.length}/{job.exports.length} hazır</small>
              </div>
              {job.exports.map((item) => (
                <div className="export-row" key={item.id}>
                  <span className={`export-status ${item.status}`} />
                  <div>
                    <strong>
                      Klip {localClips.find((clip) => clip.id === item.clip_id)?.rank ?? "?"}
                      {item.is_preview && <span className="export-seo-pill">ÖNİZLEME</span>}
                    </strong>
                    <p>
                      {item.error ?? (item.status === "completed" ? item.filename : `%${Math.round(item.progress * 100)} işleniyor`)}
                      {Math.abs(item.playback_rate - 1) > 0.001 && (
                        <span className="export-speed-pill">{formatRate(item.playback_rate)}</span>
                      )}
                      <span className="export-subtitle-pill">{item.subtitle_margin_v}px</span>
                      <span className="export-font-pill">{item.subtitle_font_family}</span>
                      {item.balanced_vertical_offset > 0 && (
                        <span className="export-balanced-pill">Dengeli +{item.balanced_vertical_offset}px</span>
                      )}
                      {item.llm_seo_enabled && <span className="export-seo-pill">LLM SEO</span>}
                      {item.edit_settings.style !== "off" && (
                        <span className="export-seo-pill">{EDIT_STYLE_LABELS[item.edit_settings.style]} kurgu</span>
                      )}
                      {item.edit_settings.hook_range && <span className="export-seo-pill">HOOK</span>}
                      {(item.edit_settings.intro_effect !== "none" || item.edit_settings.outro_effect !== "none"
                        || (item.edit_settings.hook_range && item.edit_settings.hook_transition !== "none"))
                        && <span className="export-seo-pill">GEÇİŞ EFEKTİ</span>}
                      {item.edit_settings.video_effect && item.edit_settings.video_effect !== "none"
                        && item.edit_settings.effect_intensity > 0 && <span className="export-seo-pill">
                          {VIDEO_EFFECT_LABELS[item.edit_settings.video_effect]} · %{Math.round(item.edit_settings.effect_intensity * 100)}
                        </span>}
                    </p>
                    {item.status === "completed" && (
                      <details className="export-watch">
                        <summary>Videoyu izle</summary>
                        <video controls playsInline preload="none" src={`/api/exports/${item.id}/download`} />
                      </details>
                    )}
                  </div>
                  {item.status === "completed" && (
                    <div className="export-links">
                      <a href={`/api/exports/${item.id}/download`}>
                        <Icon name="download" /> MP4
                      </a>
                      {item.metadata_filename && (
                        <a href={`/api/exports/${item.id}/metadata`}>
                          SEO MD
                        </a>
                      )}
                      {item.thumbnail_filename && <a href={`/api/exports/${item.id}/thumbnail`}>KAPAK</a>}
                      {item.package_filename && <a href={`/api/exports/${item.id}/package`}>YOUTUBE ZIP</a>}
                    </div>
                  )}
                </div>
              ))}
            </section>
          )}
        </main>
      </div>
    </div>
  );
}

function projectSnapshot(next: ProjectDetail, previous: ProjectDetail | null): ProjectDetail {
  if (previous?.id !== next.id) return next;
  const known = new Set(next.merges.map((task) => task.id));
  const pending = previous.merges.filter((task) => !known.has(task.id)
    && (task.status === "queued" || task.status === "rendering"));
  return pending.length ? { ...next, merges: [...pending, ...next.merges] } : next;
}

type VideoIntake = { kind: "files"; files: File[] } | { kind: "paths"; paths: string[] };

export default function App() {
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [activeProject, setActiveProject] = useState<ProjectDetail | null>(null);
  const [activeJob, setActiveJob] = useState<JobDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [videoIntake, setVideoIntake] = useState<VideoIntake["kind"] | null>(null);
  const uploading = videoIntake !== null;
  const [uploadProgress, setUploadProgress] = useState(0);
  const [importingYoutube, setImportingYoutube] = useState(false);
  const [error, setError] = useState("");
  const [retryingAnalysis, setRetryingAnalysis] = useState(false);
  const [switchingVideo, setSwitchingVideo] = useState(false);
  const [projectTool, setProjectTool] = useState<"shorts" | "speech" | "merge">("shorts");
  const projectTabs = useRef<HTMLDivElement>(null);
  const navigation = useRef(0);
  const projectListRequest = useRef(0);
  const currentProjectId = useRef<string | null>(null);
  currentProjectId.current = activeProject?.id ?? null;

  useEffect(() => {
    if (projectTool !== "shorts") {
      document.querySelectorAll<HTMLVideoElement>("#project-shorts-panel video").forEach((video) => video.pause());
    }
  }, [projectTool]);

  const refreshProjects = useCallback(async () => {
    const version = ++projectListRequest.current;
    const nextProjects = await api.listProjects();
    if (projectListRequest.current === version) setProjects(nextProjects);
    return nextProjects;
  }, []);

  const openProject = useCallback(async (projectId: string) => {
    const version = ++navigation.current;
    setSwitchingVideo(true);
    try {
      const project = await api.getProject(projectId);
      const job = project.active_video_id ? await api.getJob(project.active_video_id) : null;
      if (navigation.current === version) {
        navigation.current += 1;
        setActiveProject((previous) => projectSnapshot(project, previous));
        setActiveJob(job);
        setSwitchingVideo(false);
      }
    } finally {
      if (navigation.current === version) setSwitchingVideo(false);
    }
  }, []);

  const refreshActive = useCallback(async () => {
    if (!activeProject || currentProjectId.current !== activeProject.id) return;
    const version = navigation.current;
    const project = await api.getProject(activeProject.id);
    const job = project.active_video_id ? await api.getJob(project.active_video_id) : null;
    if (navigation.current !== version || currentProjectId.current !== activeProject.id) return;
    setActiveProject((previous) => projectSnapshot(project, previous));
    setActiveJob(job);
    await refreshProjects();
  }, [activeProject?.id, refreshProjects]);

  useEffect(() => {
    let cancelled = false;
    void refreshProjects()
      .then(async (items) => {
        if (!cancelled && items[0]) await openProject(items[0].id);
      })
      .catch((reason) => setError(reason instanceof Error ? reason.message : "Uygulama açılamadı."))
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [refreshProjects, openProject]);

  useEffect(() => {
    if (!activeProject || (!activeProject.videos.some((item) => ACTIVE_STATUSES.has(item.status))
      && !ACTIVE_STATUSES.has(activeJob?.status ?? "")
      && !activeProject.merges.some((item) => item.status === "queued" || item.status === "rendering")
      && !activeJob?.exports.some((item) => item.status === "queued" || item.status === "rendering"))) {
      return;
    }
    let inFlight = false;
    let cancelled = false;
    const timer = window.setInterval(() => {
      if (inFlight) return;
      inFlight = true;
      void refreshActive()
        .catch((reason) => { if (!cancelled) setError(reason instanceof Error ? reason.message : "Proje yenilenemedi."); })
        .finally(() => { inFlight = false; });
    }, 1500);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [activeProject, activeJob?.status, activeJob?.exports, refreshActive]);

  const addVideos = async (input: VideoIntake) => {
    const count = input.kind === "paths" ? input.paths.length : input.files.length;
    if (!count || uploading || importingYoutube || switchingVideo) return false;
    const projectId = activeProject?.id;
    const version = ++navigation.current;
    setVideoIntake(input.kind);
    setUploadProgress(0);
    setError("");
    try {
      let project: ProjectDetail;
      if (input.kind === "paths") {
        project = projectId
          ? await api.addProjectVideosFromPaths(projectId, input.paths)
          : await api.createProjectFromPaths(input.paths);
      } else {
        project = projectId
          ? await api.addProjectVideos(projectId, input.files, setUploadProgress)
          : await api.createProject(input.files, setUploadProgress);
      }
      const job = !projectId && project.active_video_id ? await api.getJob(project.active_video_id) : null;
      if (navigation.current === version) {
        navigation.current += 1;
        setActiveProject((previous) => projectSnapshot(project, previous));
        if (!projectId) {
          setActiveJob(job);
          setProjectTool(count > 1 ? "merge" : "shorts");
        }
      }
      await refreshProjects().catch((reason) => setError(reason instanceof Error ? reason.message : "Proje listesi yenilenemedi."));
      return true;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Videolar projeye eklenemedi.");
      return false;
    } finally {
      setVideoIntake(null);
    }
  };

  const selectVideo = async (videoId: string) => {
    if (videoId === activeJob?.id) return;
    if (!activeProject || switchingVideo || uploading) throw new Error("Video değiştirmek için devam eden yüklemenin tamamlanmasını bekle.");
    const version = ++navigation.current;
    setSwitchingVideo(true);
    setError("");
    try {
      const job = await api.getJob(videoId);
      const project = await api.selectProjectVideo(activeProject.id, videoId);
      if (navigation.current === version) {
        navigation.current += 1;
        setActiveProject((previous) => projectSnapshot(project, previous));
        setActiveJob(job);
        setSwitchingVideo(false);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Video seçilemedi.");
      throw reason;
    } finally { if (navigation.current === version) setSwitchingVideo(false); }
  };

  const importYoutube = async (url: string) => {
    if (importingYoutube || uploading) return;
    setImportingYoutube(true);
    setError("");
    try {
      const job = await api.importYoutube(url);
      await openProject(job.id);
      await refreshProjects().catch((reason) => setError(reason instanceof Error ? reason.message : "Proje listesi yenilenemedi."));
    } finally { setImportingYoutube(false); }
  };
  const chooseProject = async (project: ProjectSummary) => {
    if (uploading || importingYoutube) return;
    setError("");
    try {
      await openProject(project.id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Proje açılamadı.");
    }
  };
  const retryAnalysis = async () => {
    if (!activeJob || ACTIVE_STATUSES.has(activeJob.status) || retryingAnalysis) return;
    const version = navigation.current;
    setRetryingAnalysis(true);
    setError("");
    try {
      const job = await api.reanalyze(activeJob.id);
      if (navigation.current === version) setActiveJob(job);
      await refreshProjects();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Codex analizi başlatılamadı.");
    } finally { setRetryingAnalysis(false); }
  };
  const removeProject = async (project: ProjectSummary, event: React.MouseEvent | React.KeyboardEvent) => {
    event.stopPropagation();
    if (uploading || importingYoutube) return;
    if (!window.confirm(`"${project.name}" projesi ve bu projeye ait dosyalar silinsin mi?`)) return;
    try {
      await api.deleteProject(project.id);
      const remaining = await refreshProjects();
      if (activeProject?.id === project.id) {
        if (remaining[0]) await openProject(remaining[0].id);
        else { navigation.current += 1; setActiveProject(null); setActiveJob(null); }
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Proje silinemedi.");
    }
  };
  const selectedCount = useMemo(
    () => activeJob?.clips.filter((clip) => clip.selected).length ?? 0,
    [activeJob?.clips],
  );
  const newProject = () => {
    navigation.current += 1;
    setActiveProject(null);
    setActiveJob(null);
    setSwitchingVideo(false);
    setError("");
  };

  if (loading) return <div className="splash"><span>SHORTS</span><b>STUDIO</b></div>;
  return (
    <div className="app-shell">
      <header className="topbar">
        <button className="brand" disabled={uploading || importingYoutube} onClick={newProject}>
          <span><Icon name="film" /></span>
          <strong>SHORTS<em>STUDIO</em></strong>
        </button>
        <div className="local-badge"><i /> YEREL VİDEO İŞLEME</div>
        <button className="new-button" disabled={uploading || importingYoutube} onClick={newProject}>
          <span>+</span> Yeni proje
        </button>
      </header>
      <div className="body">
        <aside className="history">
          <div className="history-title">PROJELER <span>{projects.length}</span></div>
          <div className="history-list">
            {projects.map((project) => (
              <button
                type="button"
                key={project.id}
                className={activeProject?.id === project.id ? "active" : ""}
                disabled={uploading || importingYoutube}
                onClick={() => void chooseProject(project)}
              >
                <i className={`job-dot ${activeProject?.id === project.id ? activeJob?.status ?? "ready" : ""}`} />
                <span><strong>{project.name}</strong><small>{project.video_count} video · {formatDate(project.created_at)}</small></span>
                {!(activeProject?.id === project.id && (activeProject.videos.some((video) => ACTIVE_STATUSES.has(video.status))
                  || activeProject.merges.some((merge) => merge.status === "queued" || merge.status === "rendering"))) && (
                  <b
                    className="delete-button"
                    role="button"
                    tabIndex={0}
                    aria-label={`${project.name} projesini sil`}
                    onClick={(event) => void removeProject(project, event)}
                    onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); void removeProject(project, event); } }}
                  ><Icon name="trash" /></b>
                )}
              </button>
            ))}
          </div>
          <div className="privacy-note">
            <i>●</i>
            <p><strong>Videoların yerelde kalır.</strong> Konuşma metni, konu analizi için Codex üzerinden OpenAI’ya gönderilir.</p>
          </div>
        </aside>
        <div className="content">
          {error && <div className="error-banner global" role="alert">{error}</div>}
          {activeProject && <ProjectVideos key={activeProject.id} project={activeProject} busy={uploading}
            addingPaths={videoIntake === "paths"} switching={switchingVideo} progress={uploadProgress}
            onSelect={(videoId) => { void selectVideo(videoId).catch(() => {}); }}
            onFiles={(files) => void addVideos({ kind: "files", files })}
            onPaths={(paths) => addVideos({ kind: "paths", paths })} />}
          {activeJob?.source_url && <div className="source-origin">
            <span>YouTube kaynağı</span>
            <a href={activeJob.source_url} target="_blank" rel="noopener noreferrer">Kaynak videoyu YouTube’da aç ↗</a>
          </div>}
          {activeProject && <div className="project-tools" ref={projectTabs} role="tablist" aria-label="Proje araçları"
            onKeyDown={(event) => {
              if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
              event.preventDefault();
              const tools = ["shorts", "speech", "merge"] as const;
              const offset = event.key === "ArrowLeft" ? -1 : 1;
              const next = event.key === "Home" ? tools[0] : event.key === "End" ? tools[2]
                : tools[(tools.indexOf(projectTool) + offset + tools.length) % tools.length];
              setProjectTool(next);
              projectTabs.current?.querySelector<HTMLButtonElement>(`#project-${next}-tab`)?.focus();
            }}>
            <button type="button" id="project-shorts-tab" role="tab" aria-selected={projectTool === "shorts"}
              aria-controls="project-shorts-panel" tabIndex={projectTool === "shorts" ? 0 : -1} onClick={() => setProjectTool("shorts")}>
              <Icon name="spark" /><span><strong>LongToShorts</strong><small>Konular, klipler ve dikey video</small></span>
            </button>
            <button type="button" id="project-speech-tab" role="tab" aria-selected={projectTool === "speech"}
              aria-controls="project-speech-panel" tabIndex={projectTool === "speech" ? 0 : -1} onClick={() => setProjectTool("speech")}>
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 10v4m4-8v12m4-15v18m4-15v12m4-8v4M3 21 21 3" /></svg>
              <span><strong>Konuşma Temizleme</strong><small>Tüm videodaki konuşma aralıkları</small></span>
            </button>
            <button type="button" id="project-merge-tab" role="tab" aria-selected={projectTool === "merge"}
              aria-controls="project-merge-panel" tabIndex={projectTool === "merge" ? 0 : -1} onClick={() => setProjectTool("merge")}>
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h5v5H4zM15 5h5v5h-5zM8 16h8v5H8zM6.5 10v3H12m5.5-3v3H12v3" /></svg>
              <span><strong>Video Birleştirme</strong><small>Videoları kes, sırala ve birleştir</small></span>
            </button>
          </div>}
          {!activeProject ? (
            <UploadPanel busy={uploading} addingPaths={videoIntake === "paths"} progress={uploadProgress}
              onFiles={(files) => void addVideos({ kind: "files", files })}
              onPaths={(paths) => addVideos({ kind: "paths", paths })}
              importing={importingYoutube} onYoutube={importYoutube} />
          ) : <>
            <div id="project-shorts-panel" role="tabpanel" aria-labelledby="project-shorts-tab" hidden={projectTool !== "shorts"}>
              {!activeJob ? <p className="speech-empty">İşlem yapmak için projeye video ekle.</p> : ACTIVE_STATUSES.has(activeJob.status) && !activeJob.clips.length ? (
                <JobProgress job={activeJob} />
              ) : (activeJob.status === "failed" || activeJob.status === "interrupted") && !activeJob.clips.length ? (
                <>
                  <JobProgress job={activeJob} onRetry={() => void retryAnalysis()} retrying={retryingAnalysis} />
                  <TopicAnalysisPanel analysis={activeJob.analysis} />
                </>
              ) : (
                <Studio key={activeJob.id} job={activeJob} onRefresh={refreshActive} />
              )}
            </div>
            <div id="project-speech-panel" role="tabpanel" aria-labelledby="project-speech-tab" hidden={projectTool !== "speech"}>
              {projectTool === "speech" && activeJob && <SpeechCleanup key={activeJob.id} job={activeJob} />}
            </div>
            <div id="project-merge-panel" role="tabpanel" aria-labelledby="project-merge-tab" hidden={projectTool !== "merge"}>
              {projectTool === "merge" && <VideoMerge key={activeProject.id} project={activeProject}
                onRefresh={refreshActive} onSelectVideo={selectVideo}
                onMergeQueued={(task) => setActiveProject((previous) => previous?.id === activeProject.id
                  ? { ...previous, merges: [task, ...previous.merges.filter((item) => item.id !== task.id)] } : previous)} />}
            </div>
          </>}
        </div>
      </div>
      {selectedCount > 0 && activeJob && projectTool === "shorts" && <div className="selection-toast">{selectedCount} klip seçili</div>}
    </div>
  );
}
