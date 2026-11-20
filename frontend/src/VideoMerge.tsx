import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import type { CutRange, MergeTask, ProjectDetail, ProjectVideo } from "./types";
import "./video-merge.css";

const MAX_MERGE_VIDEOS = 100;
type ExportMode = MergeTask["export_mode"];
type PendingAction = "merge" | "select" | "refresh" | "pick" | "reveal";

function savedOutputDirectory(projectId: string): string {
  try {
    return localStorage.getItem(`merge-output-directory:${projectId}`) ?? "";
  } catch {
    return "";
  }
}

function parseTime(value: string): number | null {
  const parts = value.trim().replace(",", ".").split(":");
  if (parts.length < 1 || parts.length > 3
    || parts.some((part, index) => !(index === parts.length - 1 ? /^\d+(?:\.\d{1,3})?$/ : /^\d+$/).test(part))) return null;
  const values = parts.map(Number);
  if (values.some((part, index) => !Number.isFinite(part) || (index > 0 && part >= 60))) return null;
  const seconds = values.reduce((total, part) => total * 60 + part, 0);
  return Number.isFinite(seconds) ? seconds : null;
}

function mergeCuts(cuts: CutRange[]): CutRange[] {
  const result: CutRange[] = [];
  for (const cut of [...cuts].sort((a, b) => a.start - b.start)) {
    const previous = result[result.length - 1];
    if (previous && cut.start <= previous.end) previous.end = Math.max(previous.end, cut.end);
    else result.push({ ...cut });
  }
  return result;
}

function removedDuration(cuts: CutRange[]): number {
  return cuts.reduce((total, cut) => total + cut.end - cut.start, 0);
}

function formatTime(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "Süre hazırlanıyor";
  const hundredths = Math.round(Math.max(0, value) * 100);
  const hours = Math.floor(hundredths / 360000);
  const minutes = Math.floor(hundredths / 6000) % 60;
  const seconds = Math.floor(hundredths / 100) % 60;
  const fraction = hundredths % 100;
  return `${hours ? `${hours}:` : ""}${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(fraction).padStart(2, "0")}`;
}

function canUse(video: ProjectVideo): boolean {
  return video.status !== "importing"
    && !(video.status === "queued" && video.source_type === "youtube")
    && (video.duration === null || video.duration > 0);
}

function initialSelection(project: ProjectDetail): string[] {
  return project.videos.filter((video) => video.kind !== "merged" && canUse(video)).map((video) => video.id);
}

function isActive(task: MergeTask): boolean {
  return task.status === "queued" || task.status === "rendering";
}

function SourceCutEditor({ video, cuts, disabled, open, onOpenChange, onChange, onPlay }: {
  video: ProjectVideo;
  cuts: CutRange[];
  disabled: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onChange: (cuts: CutRange[]) => void;
  onPlay: (player: HTMLVideoElement) => void;
}) {
  const player = useRef<HTMLVideoElement>(null);
  const [loadedDuration, setLoadedDuration] = useState<number | null>(null);
  const [playhead, setPlayhead] = useState(0);
  const [start, setStart] = useState("00:00.00");
  const [end, setEnd] = useState(formatTime(Math.min(10, video.duration ?? 10)));
  const [error, setError] = useState("");
  const [skipCuts, setSkipCuts] = useState(false);
  const duration = loadedDuration ?? video.duration;
  const parsedStart = parseTime(start);
  const parsedEnd = parseTime(end);

  useEffect(() => { if (!open) player.current?.pause(); }, [open]);

  const seek = (time: number) => {
    if (player.current) player.current.currentTime = time;
    setPlayhead(time);
  };
  const addCut = () => {
    if (parsedStart === null || parsedEnd === null) {
      setError("Zamanı saniye veya saat:dakika:saniye biçiminde yaz. Örnek: 01:12.500");
      return;
    }
    if (duration === null || parsedStart < 0 || parsedEnd > duration || parsedEnd <= parsedStart) {
      setError("Başlangıç bitişten önce, her iki zaman da video süresi içinde olmalı.");
      return;
    }
    if (parsedEnd - parsedStart < 0.05 - 1e-9) {
      setError("Çıkarılacak aralık en az 0,05 saniye olmalı.");
      return;
    }
    const next = mergeCuts([...cuts, { start: parsedStart, end: parsedEnd }]);
    if (duration - removedDuration(next) < 0.05 - 1e-9) {
      setError("Videoda en az 0,05 saniyelik bir bölüm bırak.");
      return;
    }
    onChange(next);
    setError("");
  };
  const updatePlayhead = (current: HTMLVideoElement) => {
    if (skipCuts && !current.paused && !current.seeking) {
      const cut = cuts.find((range) => current.currentTime >= range.start && current.currentTime < range.end);
      if (cut) {
        current.currentTime = cut.end;
        if (duration !== null && cut.end >= duration - 0.01) current.pause();
      }
    }
    setPlayhead(current.currentTime);
  };

  return <details className="merge-source-preview merge-cut-editor" open={open}
    onToggle={(event) => { if (event.currentTarget.open !== open) onOpenChange(event.currentTarget.open); }}>
    <summary>İzle ve kes {cuts.length > 0 && <span>{cuts.length} aralık · {formatTime(removedDuration(cuts))} çıkarılacak</span>}</summary>
    <video ref={player} controls playsInline preload="none" src={`/api/jobs/${video.id}/source`}
      onPlay={(event) => { onPlay(event.currentTarget); updatePlayhead(event.currentTarget); }}
      onTimeUpdate={(event) => updatePlayhead(event.currentTarget)}
      onLoadedMetadata={(event) => { const value = event.currentTarget.duration; if (Number.isFinite(value) && value > 0) setLoadedDuration(value); }}
      onError={() => setError("Video önizlemesi yüklenemedi. Kaynak dosyanın yerinde olduğunu kontrol et.")} />
    <div className="merge-cut-clock"><span>Oynatma konumu <b>{formatTime(playhead)}</b></span><span>{formatTime(duration)}</span></div>
    <div className="merge-cut-timeline" aria-label="Kırmızı alanlar çıkarılacak bölümlerdir">
      {duration !== null && duration > 0 && cuts.map((cut, index) => <span key={index} className="merge-cut-mark"
        style={{ left: `${cut.start / duration * 100}%`, width: `${(cut.end - cut.start) / duration * 100}%` }} />)}
      <input type="range" min={0} max={duration ?? 0} step={0.01} value={Math.min(playhead, duration ?? 0)} disabled={!duration}
        aria-label={`${video.filename} oynatma konumu`} onChange={(event) => seek(Number(event.target.value))} />
    </div>
    <p className="merge-cut-instruction">Çıkarılacak bölümün başını ve sonunu seç. Kırmızı aralıklar dışa aktarılırken videodan çıkarılır.</p>
    <div className="merge-cut-fields">
      <label>Başlangıç<input type="text" inputMode="decimal" placeholder="00:00.000" value={start} disabled={disabled}
        aria-label={`${video.filename} kesim başlangıcı`} onChange={(event) => { setStart(event.target.value); setError(""); }} />
        <button type="button" disabled={disabled} onClick={() => { setStart(formatTime(playhead)); setError(""); }}>Şu anı başlangıç yap</button></label>
      <label>Bitiş<input type="text" inputMode="decimal" placeholder="00:10.000" value={end} disabled={disabled}
        aria-label={`${video.filename} kesim bitişi`} onChange={(event) => { setEnd(event.target.value); setError(""); }} />
        <button type="button" disabled={disabled} onClick={() => { setEnd(formatTime(playhead)); setError(""); }}>Şu anı bitiş yap</button></label>
    </div>
    <button type="button" className="merge-cut-add" disabled={disabled || duration === null} onClick={addCut}>＋ Bu aralığı çıkar</button>
    {error && <p className="merge-cut-error" role="alert">{error}</p>}
    {cuts.length > 0 && <><ol className="merge-cut-list" aria-label={`${video.filename} çıkarılacak aralıklar`}>{cuts.map((cut, index) => <li key={`${cut.start}-${cut.end}`}>
      <button type="button" className="merge-cut-seek" onClick={() => { setSkipCuts(false); seek(cut.start); }} aria-label={`${formatTime(cut.start)} konumuna git`}>{formatTime(cut.start)} → {formatTime(cut.end)}</button>
      <span>{formatTime(cut.end - cut.start)}</span><button type="button" className="merge-cut-restore" disabled={disabled}
        onClick={() => onChange(cuts.filter((_, cutIndex) => cutIndex !== index))} aria-label={`${formatTime(cut.start)} ile ${formatTime(cut.end)} arasını geri al`}>Geri al</button>
    </li>)}</ol><div className="merge-cut-preview-options"><label><input type="checkbox" checked={skipCuts} onChange={(event) => setSkipCuts(event.target.checked)} /> Önizlemede kesilen aralıkları atla</label>
      <button type="button" disabled={disabled} onClick={() => onChange([])}>Tüm kesimleri geri al</button></div></>}
    <p className="merge-cut-total">Kalan süre <strong>{duration !== null ? formatTime(Math.max(0, duration - removedDuration(cuts))) : "Hesaplanacak"}</strong></p>
  </details>;
}

export function VideoMerge({ project, onRefresh, onSelectVideo, onMergeQueued }: {
  project: ProjectDetail;
  onRefresh: () => Promise<void>;
  onSelectVideo: (videoId: string) => Promise<void>;
  onMergeQueued: (task: MergeTask) => void;
}) {
  const [orderedIds, setOrderedIds] = useState<string[]>(() => initialSelection(project));
  const [cuts, setCuts] = useState<Record<string, CutRange[]>>({});
  const [cutExportMode, setCutExportMode] = useState<"fast" | "precise">("fast");
  const [outputDirectory, setOutputDirectory] = useState(() => savedOutputDirectory(project.id));
  const [editorId, setEditorId] = useState<string | null>(() => project.videos.length === 1 ? project.videos[0].id : null);
  const [optimisticTask, setOptimisticTask] = useState<MergeTask | null>(null);
  const [pending, setPending] = useState<PendingAction | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const mounted = useRef(false);
  const generation = useRef(0);
  const busy = useRef(false);
  const projectId = useRef(project.id);
  const knownIds = useRef(new Set(project.videos.map((video) => video.id)));
  const workspace = useRef<HTMLElement>(null);
  const membership = project.videos.map((video) => video.id).join("|");
  const videosById = useMemo(() => new Map(project.videos.map((video) => [video.id, video])), [project.videos]);
  const selectedVideos = orderedIds.map((id) => videosById.get(id)).filter((video): video is ProjectVideo => !!video);
  const selectedIds = new Set(orderedIds);
  const totalDuration = selectedVideos.reduce((total, video) => total + (video.duration ?? 0), 0);
  const selectedCuts = Object.fromEntries(orderedIds.filter((id) => cuts[id]?.length).map((id) => [id, cuts[id]]));
  const cutCount = Object.values(selectedCuts).reduce((total, ranges) => total + ranges.length, 0);
  const removedSeconds = Object.values(selectedCuts).reduce((total, ranges) => total + removedDuration(ranges), 0);
  const hasCuts = cutCount > 0;
  const exportMode: ExportMode = hasCuts ? cutExportMode : "auto";
  const durationKnown = selectedVideos.every((video) => video.duration !== null);
  const tasks = optimisticTask && !project.merges.some((task) => task.id === optimisticTask.id)
    ? [optimisticTask, ...project.merges] : project.merges;
  const working = tasks.some(isActive);
  const selectionReady = (selectedVideos.length >= 2 || (selectedVideos.length === 1 && hasCuts))
    && selectedVideos.length <= MAX_MERGE_VIDEOS && selectedVideos.every(canUse);
  const firstVideo = selectedVideos[0];

  useEffect(() => {
    mounted.current = true;
    generation.current += 1;
    const node = workspace.current;
    return () => {
      mounted.current = false;
      generation.current += 1;
      node?.querySelectorAll("video").forEach((video) => video.pause());
    };
  }, [project.id]);

  useEffect(() => {
    if (projectId.current !== project.id) {
      projectId.current = project.id;
      knownIds.current = new Set(project.videos.map((video) => video.id));
      setOrderedIds(initialSelection(project));
      setCuts({});
      setCutExportMode("fast");
      setOutputDirectory(savedOutputDirectory(project.id));
      setEditorId(project.videos.length === 1 ? project.videos[0].id : null);
      setOptimisticTask(null);
      setPending(null);
      setError("");
      setNotice("");
      busy.current = false;
      return;
    }
    const current = new Set(project.videos.map((video) => video.id));
    const additions = project.videos.filter((video) => !knownIds.current.has(video.id)
      && video.kind !== "merged" && canUse(video)).map((video) => video.id);
    setOrderedIds((previous) => {
      const retained = previous.filter((id) => current.has(id));
      return additions.length || retained.length !== previous.length ? [...retained, ...additions] : previous;
    });
    knownIds.current = current;
  }, [project.id, membership]);

  const perform = async (kind: PendingAction, action: (version: number) => Promise<void>) => {
    if (busy.current) return;
    busy.current = true;
    const version = generation.current;
    setPending(kind);
    setError("");
    setNotice("");
    try {
      await action(version);
    } catch (reason) {
      if (mounted.current && generation.current === version) {
        setError(reason instanceof Error ? reason.message : "İşlem tamamlanamadı.");
      }
    } finally {
      if (mounted.current && generation.current === version) {
        busy.current = false;
        setPending(null);
      }
    }
  };

  const toggleVideo = (video: ProjectVideo) => {
    setOrderedIds((previous) => previous.includes(video.id)
      ? previous.filter((id) => id !== video.id) : [...previous, video.id]);
  };
  const moveVideo = (id: string, direction: -1 | 1) => {
    setOrderedIds((previous) => {
      const index = previous.indexOf(id);
      const nextIndex = index + direction;
      if (index < 0 || nextIndex < 0 || nextIndex >= previous.length) return previous;
      const next = [...previous];
      [next[index], next[nextIndex]] = [next[nextIndex], next[index]];
      return next;
    });
  };
  const rememberDirectory = (directory: string) => {
    setOutputDirectory(directory);
    try {
      if (directory) localStorage.setItem(`merge-output-directory:${project.id}`, directory);
      else localStorage.removeItem(`merge-output-directory:${project.id}`);
    } catch {
      // The chosen folder still works when browser storage is unavailable.
    }
  };
  const pickDirectory = () => {
    void perform("pick", async (version) => {
      const result = await api.pickMergeExportDirectory();
      if (mounted.current && generation.current === version && result.directory !== null) {
        rememberDirectory(result.directory);
      }
    });
  };
  const revealResult = (task: MergeTask) => {
    void perform("reveal", async (version) => {
      await api.revealMergeExport(project.id, task.id);
      if (mounted.current && generation.current === version) {
        setNotice("Kaydedilen video Dosya Gezgini’nde gösterildi. Dosyayı yeniden indirmen gerekmiyor.");
      }
    });
  };
  const startMerge = (
    ids: string[], requestedCuts: Record<string, CutRange[]> = selectedCuts,
    mode: ExportMode = exportMode, directory: string | null = outputDirectory.trim() || null,
  ) => {
    if (working || ids.length < 1 || (ids.length === 1 && !requestedCuts[ids[0]]?.length)) return;
    const snapshot = [...ids];
    const cutSnapshot = Object.fromEntries(snapshot.filter((id) => requestedCuts[id]?.length)
      .map((id) => [id, requestedCuts[id].map((cut) => ({ ...cut }))]));
    void perform("merge", async (version) => {
      const task = await api.mergeVideos(project.id, snapshot, cutSnapshot, mode, directory);
      if (!mounted.current || generation.current !== version) return;
      setOptimisticTask(task);
      onMergeQueued(task);
      setNotice("Dışa aktarma başlatıldı. MP4 doğrudan kayıt klasörüne yazılacak; tamamlanınca dosyayı klasörde gösterebilirsin.");
      try {
        await onRefresh();
      } catch {
        throw new Error("Dışa aktarma başlatıldı ancak durum yenilenemedi. Durumu yenile düğmesini kullanabilirsin.");
      }
    });
  };
  const useResult = (videoId: string) => {
    void perform("select", async (version) => {
      await onSelectVideo(videoId);
      if (mounted.current && generation.current === version) {
        setNotice("Çıktı videosu seçildi. LongToShorts ve Konuşma Temizleme sekmeleri artık bu videoda çalışır.");
      }
    });
  };
  const pauseOtherPlayers = (current: HTMLVideoElement) => {
    workspace.current?.querySelectorAll("video").forEach((video) => {
      if (video !== current) video.pause();
    });
  };

  return <section className="video-merge" ref={workspace} aria-labelledby="video-merge-heading">
    <header className="merge-header">
      <div><div className="eyebrow">PROJE VİDEOLARI · BİRLEŞTİRME VE KESME</div>
        <h1 id="video-merge-heading">İstediğin bölümleri kes.<br /><span>Tek videoda dışa aktar.</span></h1>
        <p>{project.name} · {project.videos.length} video</p>
      </div>
      <div className="merge-header-note"><span aria-hidden="true">↗</span><p>Tek videodan aralıklar çıkar veya videoları birleştir. MP4 doğrudan seçtiğin klasöre kaydedilir ve projene eklenir.</p></div>
    </header>

    {error && <div className="merge-message merge-error" role="alert">{error}<button type="button" disabled={!!pending} onClick={() => void perform("refresh", async () => { await onRefresh(); })}>Durumu yenile</button></div>}
    {notice && <p className="merge-message merge-notice" role="status">{notice}</p>}

    {project.videos.length < 2 && <div className="merge-empty" role="status"><span aria-hidden="true">✂</span><div><h2>Tek videoyu da kesebilirsin</h2><p>“İzle ve kes” bölümünden çıkarılacak aralıkları belirle ve kalanını dışa aktar. Birleştirmek için “Video ekle” düğmesinden başka videolar ekleyebilirsin.</p></div></div>}

    <div className="merge-layout">
      <section className="merge-sources-panel" aria-labelledby="merge-sources-heading">
        <div className="merge-panel-heading"><div><span className="merge-kicker">01 · VİDEOLARI SEÇ</span><h2 id="merge-sources-heading">Projedeki videolar</h2></div><span>{selectedVideos.length} seçili</span></div>
        <p className="merge-panel-description">Çıktıya eklenecek videoları işaretle. “İzle ve kes” bölümünden her videoda çıkarılacak aralıkları belirle.</p>
        <div className="merge-source-list">{project.videos.map((video) => {
          const selected = selectedIds.has(video.id);
          const ready = canUse(video);
          return <article key={video.id} id={`merge-source-${video.id}`} className={`merge-source ${selected ? "selected" : ""}`}>
            <label className="merge-source-check"><input type="checkbox" checked={selected} disabled={!ready || pending === "merge"}
              onChange={() => toggleVideo(video)} aria-label={`Çıktıya ekle: ${video.filename}`} />
              <span className="merge-video-mark" aria-hidden="true">{video.kind === "merged" ? "↔" : "▶"}</span>
              <span className="merge-source-copy"><strong>{video.filename}</strong><small>{formatTime(video.duration)}{video.width && video.height ? ` · ${video.width} × ${video.height}` : ""}</small></span>
            </label>
            <div className="merge-source-footer"><span>{video.kind === "merged" ? "BİRLEŞTİRİLMİŞ" : video.source_type === "youtube" ? "YOUTUBE KAYNAĞI" : "KAYNAK VİDEO"}</span>{project.active_video_id === video.id && <b>İşlem için seçili</b>}{!ready && <small>Video hazırlanıyor</small>}</div>
            {ready && <SourceCutEditor key={`${project.id}-${video.id}`} video={video} cuts={cuts[video.id] ?? []}
              disabled={!selected || pending === "merge"} open={editorId === video.id}
              onOpenChange={(open) => setEditorId((previous) => open ? video.id : previous === video.id ? null : previous)}
              onChange={(ranges) => setCuts((previous) => ({ ...previous, [video.id]: ranges }))} onPlay={pauseOtherPlayers} />}
          </article>;
        })}</div>
      </section>

      <section className="merge-plan-panel" aria-labelledby="merge-plan-heading">
        <div className="merge-panel-heading"><div><span className="merge-kicker">02 · ÇIKTIYI HAZIRLA</span><h2 id="merge-plan-heading">Sıra ve dışa aktarma</h2></div><span aria-hidden="true">↓</span></div>
        <p className="merge-panel-description">Videolar aşağıdaki sırayla oynar; işaretlediğin aralıklar çıkarılır. Tek videoyu kesmek için yalnızca o videoyu seç.</p>
        {selectedVideos.length ? <ol className="merge-order-list">{selectedVideos.map((video, index) => <li key={video.id}>
          <span className="merge-order-number">{String(index + 1).padStart(2, "0")}</span>
          <div><strong>{video.filename}</strong><small>{formatTime(video.duration !== null ? Math.max(0, video.duration - removedDuration(cuts[video.id] ?? [])) : null)}{cuts[video.id]?.length ? ` · ${cuts[video.id].length} kesim` : ""}</small></div>
          <div className="merge-order-actions"><button type="button" disabled={index === 0 || pending === "merge"} onClick={() => moveVideo(video.id, -1)} aria-label={`${video.filename} videosunu yukarı taşı`}>↑</button><button type="button" disabled={index === selectedVideos.length - 1 || pending === "merge"} onClick={() => moveVideo(video.id, 1)} aria-label={`${video.filename} videosunu aşağı taşı`}>↓</button><button type="button" disabled={pending === "merge"} onClick={() => toggleVideo(video)} aria-label={`${video.filename} videosunu birleştirmeden çıkar`}>×</button></div>
        </li>)}</ol> : <p className="merge-no-selection">Birleştirme sırasını oluşturmak için soldan video seç.</p>}
        <dl className="merge-summary"><div><dt>Seçilen video</dt><dd>{selectedVideos.length}</dd></div><div><dt>Yaklaşık çıktı süresi</dt><dd>{durationKnown ? formatTime(Math.max(0, totalDuration - removedSeconds)) : "Hesaplanacak"}</dd></div><div><dt>Çıktı kadrajı</dt><dd>{firstVideo?.width && firstVideo.height ? `${firstVideo.width} × ${firstVideo.height}` : "—"}</dd></div></dl>
        {hasCuts ? <fieldset className="merge-export-mode" disabled={!!pending}><legend>Dışa aktarma yöntemi</legend>
          <label className={cutExportMode === "fast" ? "selected" : ""}><input type="radio" name={`merge-export-${project.id}`} checked={cutExportMode === "fast"} onChange={() => setCutExportMode("fast")} /><span><strong>Hızlı · görüntüyü yeniden kodlamaz</strong><small>Görüntü kalitesi korunur. Kesimler ana karelere göre genişleyebilir; seçimin çevresinden ek süre çıkabilir. Ses eşzamanlama için işlenebilir.</small></span></label>
          <label className={cutExportMode === "precise" ? "selected" : ""}><input type="radio" name={`merge-export-${project.id}`} checked={cutExportMode === "precise"} onChange={() => setCutExportMode("precise")} /><span><strong>Hassas · seçtiğin zamanlardan keser</strong><small>Videoyu yeniden kodlar. Farklı boyut ve biçimleri birleştirebilir; uzun veya yüksek çözünürlüklü videolarda daha fazla zaman alır.</small></span></label>
          <p>{cutCount} aralık · {formatTime(removedSeconds)} çıkarılacak. Hızlı yöntemde gerçek çıktı süresi değişebilir.</p>
        </fieldset> : <p className="merge-hint">Uyumlu videolar görüntü yeniden kodlanmadan birleştirilir. Farklı biçim veya boyutlarda ilk videonun kadrajına dönüştürme daha uzun sürer.</p>}
        <fieldset className="merge-destination" disabled={!!pending}>
          <legend>Kayıt konumu</legend>
          <label htmlFor={`merge-output-directory-${project.id}`}>MP4’ün kaydedileceği klasör</label>
          <div className="merge-destination-controls"><input id={`merge-output-directory-${project.id}`} type="text"
            value={outputDirectory} maxLength={32767} spellCheck={false} autoComplete="off"
            placeholder="Varsayılan: projenin çıktı klasörü" aria-describedby={`merge-output-help-${project.id}`}
            onChange={(event) => rememberDirectory(event.target.value)} />
            <button type="button" className="merge-outline-button" onClick={pickDirectory}>{pending === "pick" ? "Klasör seçici açık…" : "Klasör seç"}</button></div>
          {outputDirectory && <button type="button" className="merge-destination-reset" onClick={() => rememberDirectory("")}>Varsayılan klasörü kullan</button>}
          <p id={`merge-output-help-${project.id}`}>Dışa aktarma dosyayı doğrudan buraya kaydeder. Tekrar indirmen gerekmez. Alanı boş bırakırsan projenin çıktı klasörü kullanılır.</p>
        </fieldset>
        <button type="button" className="primary-button merge-start-button" disabled={!selectionReady || !!pending || working} onClick={() => startMerge(orderedIds)}>{pending === "merge" ? "Başlatılıyor…" : working ? "Dışa aktarma devam ediyor…" : hasCuts ? "Kesimleri uygula ve dışa aktar" : "Seçilen videoları birleştir"}<span aria-hidden="true">↗</span></button>
        {!selectionReady && <p className="merge-hint">{selectedVideos.length > MAX_MERGE_VIDEOS ? `Bir seferde en fazla ${MAX_MERGE_VIDEOS} video birleştirebilirsin.` : selectedVideos.length === 1 ? "Tek video için “İzle ve kes” bölümünden en az bir çıkarılacak aralık ekle." : "Kesmek için bir video, birleştirmek için en az iki hazır video seç."}</p>}
        <p className="merge-original-note">Kaynak videoların korunur. Birleştirme yapmazsan diğer sekmeler üstte seçtiğin videoda çalışır.</p>
      </section>
    </div>

    {!!tasks.length && <section className="merge-results" aria-labelledby="merge-results-heading"><div className="merge-panel-heading"><div><span className="merge-kicker">VİDEO ÇIKTILARI</span><h2 id="merge-results-heading">Dışa aktarılan videolar</h2></div><button type="button" className="merge-outline-button" disabled={!!pending} onClick={() => void perform("refresh", async () => { await onRefresh(); })}>{pending === "refresh" ? "Yenileniyor…" : "Durumu yenile"}</button></div>
      <p className="merge-results-description">Dışa aktarılan MP4 zaten bilgisayarında kayıtlıdır. “Dosyayı klasörde göster” ile ulaşabilir veya “Bu videoyu kullan” ile düzenlemeye devam edebilirsin.</p>
      {tasks.map((task) => {
        const selected = !!task.output_video_id && project.active_video_id === task.output_video_id;
        const taskCuts = task.cuts ?? {};
        const taskCutCount = Object.values(taskCuts).reduce((total, ranges) => total + ranges.length, 0);
        const title = task.status === "completed" ? "Dışa aktarma tamamlandı" : task.status === "failed" ? "Dışa aktarma tamamlanamadı" : task.status === "queued" ? "Dışa aktarma sırada" : "Video dışa aktarılıyor";
        const retryReady = (task.video_ids.length >= 2 || (task.video_ids.length === 1 && taskCutCount > 0))
          && task.video_ids.every((id) => { const video = videosById.get(id); return !!video && canUse(video); });
        return <article className={`merge-result ${task.status}`} key={task.id}><div className="merge-result-heading"><span className="merge-result-dot" aria-hidden="true" /><div><h3>{title}</h3><p>{task.status === "failed" ? task.error || "İşlem tamamlanamadı. Aynı sırayla yeniden deneyebilirsin." : task.status === "completed" ? task.filename || "Birleştirilmiş video hazır" : task.status === "queued" ? "Sırası geldiğinde otomatik başlayacak." : `%${Math.round(task.progress * 100)} · Yeni video hazırlanıyor`}</p></div><span className="merge-result-count">{task.video_ids.length} video</span></div>
          {isActive(task) && <progress value={task.progress} max={1} aria-label="Video dışa aktarma ilerlemesi" />}
          <div className="merge-result-sources" aria-label="Bu çıktının video sırası">{task.video_ids.map((id, index) => <span key={`${id}-${index}`}><b>{index + 1}</b>{videosById.get(id)?.filename ?? "Kaynak video"}</span>)}</div>
          {taskCutCount > 0 && <p className="merge-result-cut-note">{taskCutCount} kesim · {task.export_mode === "precise" ? "Hassas dışa aktarma" : "Hızlı dışa aktarma · kesimler ana karelere göre genişleyebilir"}{task.status === "completed" && task.output_video_id && videosById.get(task.output_video_id)?.duration != null ? ` · Gerçek süre ${formatTime(videosById.get(task.output_video_id)!.duration)}` : ""}</p>}
          {task.output_directory && task.status !== "completed" && <p className="merge-result-directory">Kayıt klasörü: <span>{task.output_directory}</span></p>}
          {task.status === "completed" && task.output_video_id && <><div className="merge-result-location"><span>MP4 bilgisayarına kaydedildi</span>{task.output_path && <code>{task.output_path}</code>}<p>Dosya hazır; yeniden indirmen gerekmiyor.</p></div><div className="merge-result-actions"><button type="button" className="secondary-button" disabled={!!pending} onClick={() => revealResult(task)}>{pending === "reveal" ? "Açılıyor…" : "Dosyayı klasörde göster"}</button><button type="button" className={selected ? "merge-result-selected" : "merge-outline-button"} disabled={selected || !!pending} onClick={() => useResult(task.output_video_id!)}>{selected ? "✓ İşlem için seçili" : pending === "select" ? "Seçiliyor…" : "Bu videoyu kullan"}</button><button type="button" className="merge-outline-button" disabled={!!pending || !videosById.has(task.output_video_id)} onClick={() => {
            setOrderedIds([task.output_video_id!]); setEditorId(task.output_video_id!); setNotice("Çıktı videosu kesmek için seçildi. “İzle ve kes” bölümünden aralık ekle.");
            document.getElementById(`merge-source-${task.output_video_id}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
          }}>Bu videoyu kes ✂</button></div><details className="merge-result-copy"><summary>Başka bir kopya kaydet</summary><p>Bu seçenek, hazır MP4’ün bir kopyasını tarayıcının indirme konumuna kaydeder.</p><a href={`/api/projects/${project.id}/merges/${task.id}/download`} download={task.filename ?? "duzenlenmis.mp4"}>Kopyasını indir ↓</a></details><details className="merge-result-preview"><summary>Çıktı videosunu izle</summary><video controls playsInline preload="none" src={`/api/jobs/${task.output_video_id}/source`} onPlay={(event) => pauseOtherPlayers(event.currentTarget)} /></details></>}
          {task.status === "failed" && <button type="button" className="merge-outline-button merge-retry" disabled={!retryReady || !!pending || working} onClick={() => startMerge(task.video_ids, taskCuts, task.export_mode ?? "auto", task.output_directory ?? null)}>Aynı kesimlerle yeniden dene</button>}
        </article>;
      })}
    </section>}
  </section>;
}
