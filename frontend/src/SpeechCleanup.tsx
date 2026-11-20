import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import type { JobDetail, SpeechCleanupState, SpeechRegion } from "./types";
import "./speech-cleanup.css";

type PreviewMode = "source" | "cleaned";
type PendingAction = "detect" | "selection" | "export" | "manual";

function timeLabel(value: number): string {
  const safe = Math.max(0, Number.isFinite(value) ? value : 0);
  const hundredths = Math.round(safe * 100);
  const hours = Math.floor(hundredths / 360000);
  const minutes = Math.floor(hundredths / 6000) % 60;
  const seconds = `${String(Math.floor(hundredths / 100) % 60).padStart(2, "0")}.${String(hundredths % 100).padStart(2, "0")}`;
  return `${hours ? `${hours}:${String(minutes).padStart(2, "0")}` : String(minutes).padStart(2, "0")}:${seconds}`;
}

function parseTime(value: string): number | null {
  const parts = value.trim().replace(",", ".").split(":");
  if (parts.length > 3 || parts.some((part, index) =>
    !(index === parts.length - 1 ? /^\d+(?:\.\d{1,3})?$/ : /^\d+$/).test(part))) return null;
  const values = parts.map(Number);
  if (values.some((part, index) => !Number.isFinite(part) || (index > 0 && part >= 60))) return null;
  const seconds = values.reduce((total, part) => total * 60 + part, 0);
  return Number.isFinite(seconds) ? seconds : null;
}

function inputTimeLabel(value: number): string {
  const milliseconds = Math.floor(Math.max(0, value) * 1000);
  const hours = Math.floor(milliseconds / 3600000);
  const minutes = Math.floor(milliseconds / 60000) % 60;
  const seconds = ((milliseconds % 60000) / 1000).toFixed(3).padStart(6, "0");
  return `${hours ? `${hours}:${String(minutes).padStart(2, "0")}` : String(minutes).padStart(2, "0")}:${seconds}`;
}

function durationLabel(value: number): string {
  return `${Math.max(0, value).toLocaleString("tr-TR", { maximumFractionDigits: 1 })} sn`;
}

function paddingLabel(value: number): string {
  return `${Math.max(0, value).toLocaleString("tr-TR", { maximumFractionDigits: 2 })} sn`;
}

function unionRanges(regions: SpeechRegion[]): { start: number; end: number }[] {
  const merged: { start: number; end: number }[] = [];
  for (const region of [...regions].sort((left, right) => left.start - right.start)) {
    const previous = merged[merged.length - 1];
    if (previous && region.start <= previous.end) previous.end = Math.max(previous.end, region.end);
    else merged.push({ start: region.start, end: region.end });
  }
  return merged;
}

function isWorking(state: SpeechCleanupState | null): boolean {
  return !!state && (["queued", "detecting"].includes(state.status)
    || state.exports.some((item) => item.status === "queued" || item.status === "rendering"));
}

export function SpeechCleanup({ job }: { job: JobDetail }) {
  const [state, setState] = useState<SpeechCleanupState | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [pending, setPending] = useState<PendingAction | null>(null);
  const [preview, setPreview] = useState<PreviewMode>("source");
  const [search, setSearch] = useState("");
  const [currentTime, setCurrentTime] = useState(0);
  const [mediaDuration, setMediaDuration] = useState(0);
  const [playingRegion, setPlayingRegion] = useState<string | null>(null);
  const [mediaError, setMediaError] = useState(false);
  const [openingExport, setOpeningExport] = useState<string | null>(null);
  const [manualStart, setManualStart] = useState("");
  const [manualEnd, setManualEnd] = useState("");
  const [manualText, setManualText] = useState("");
  const [manualError, setManualError] = useState("");
  const [manualMessage, setManualMessage] = useState("");
  const workspace = useRef<HTMLElement>(null);
  const video = useRef<HTMLVideoElement>(null);
  const mounted = useRef(false);
  const generation = useRef(0);
  const busy = useRef(false);
  const playbackEnd = useRef<number | null>(null);
  const savedMute = useRef<boolean | null>(null);
  const revealRequest = useRef<symbol | null>(null);

  const importing = job.status === "importing" || (job.status === "queued" && job.source_type === "youtube");
  const sourceBusy = ["queued", "importing", "analyzing"].includes(job.status);
  const duration = state?.duration ?? job.duration ?? mediaDuration;
  const regions = state?.regions ?? [];
  const removedIds = useMemo(() => new Set(state?.removed_region_ids ?? []), [state?.removed_region_ids]);
  const removedRanges = useMemo(
    () => state?.mute_ranges ?? unionRanges(regions.filter((region) => removedIds.has(region.id))),
    [state?.mute_ranges, regions, removedIds],
  );
  const removedDuration = removedRanges.reduce((total, range) => total + range.end - range.start, 0);
  const visibleRegions = regions.filter((region) => region.text.toLocaleLowerCase("tr-TR").includes(search.trim().toLocaleLowerCase("tr-TR")));
  const detecting = state?.status === "queued" || state?.status === "detecting";
  const working = isWorking(state);
  const disabledSelection = !!pending || state?.status !== "ready";
  const disabledManual = !!pending || !state || detecting || sourceBusy;
  const manualCount = regions.filter((region) => region.manual).length;

  useEffect(() => {
    mounted.current = true;
    const version = ++generation.current;
    setLoading(true);
    setError("");
    setOpeningExport(null);
    setManualStart("");
    setManualEnd("");
    setManualText("");
    setManualError("");
    setManualMessage("");
    revealRequest.current = null;
    void api.getSpeechCleanup(job.id)
      .then((next) => { if (mounted.current && generation.current === version) setState(next); })
      .catch((reason) => { if (mounted.current && generation.current === version) setError(reason instanceof Error ? reason.message : "Konuşma alanı açılamadı."); })
      .finally(() => { if (mounted.current && generation.current === version) setLoading(false); });
    return () => { mounted.current = false; generation.current += 1; revealRequest.current = null; };
  }, [job.id]);

  useEffect(() => {
    const element = workspace.current;
    return () => { element?.querySelectorAll("video").forEach((player) => player.pause()); };
  }, []);

  useEffect(() => {
    if (!working) return;
    let cancelled = false;
    let inFlight = false;
    const timer = window.setInterval(() => {
      if (inFlight || busy.current) return;
      inFlight = true;
      const version = generation.current;
      void api.getSpeechCleanup(job.id)
        .then((next) => { if (!cancelled && mounted.current && generation.current === version) setState(next); })
        .catch((reason) => { if (!cancelled && mounted.current) setError(reason instanceof Error ? reason.message : "İşlem durumu yenilenemedi."); })
        .finally(() => { inFlight = false; });
    }, 1500);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [job.id, working]);

  useEffect(() => {
    const player = video.current;
    if (!player) return;
    if (preview === "cleaned") {
      if (savedMute.current === null) savedMute.current = player.muted;
    } else if (savedMute.current !== null) {
      player.muted = savedMute.current;
      savedMute.current = null;
    }
    let frame = 0;
    let lastPaint = 0;
    const tick = (now: number) => {
      const position = player.currentTime;
      if (playbackEnd.current !== null && position >= playbackEnd.current) {
        player.pause();
        playbackEnd.current = null;
        setPlayingRegion(null);
      }
      if (preview === "cleaned") {
        const removed = removedRanges.find((range) => position >= range.start && position < range.end);
        player.muted = savedMute.current === true || !!removed;
      }
      if (now - lastPaint >= 100) {
        setCurrentTime(player.currentTime);
        lastPaint = now;
      }
      frame = window.requestAnimationFrame(tick);
    };
    frame = window.requestAnimationFrame(tick);
    return () => window.cancelAnimationFrame(frame);
  }, [preview, removedRanges, duration, loading, importing]);

  const perform = async (kind: PendingAction, action: () => Promise<SpeechCleanupState>): Promise<boolean> => {
    if (busy.current) return false;
    busy.current = true;
    const version = ++generation.current;
    setPending(kind);
    setError("");
    try {
      const next = await action();
      if (!mounted.current || generation.current !== version) return false;
      setState(next);
      return true;
    } catch (reason) {
      if (mounted.current && generation.current === version) setError(reason instanceof Error ? reason.message : "İşlem tamamlanamadı.");
      return false;
    } finally {
      if (mounted.current && generation.current === version) setPending(null);
      busy.current = false;
    }
  };

  const selectRegions = (ids: string[]) => {
    void perform("selection", () => api.saveSpeechSelection(job.id, ids));
  };
  const usePlayhead = (boundary: "start" | "end") => {
    const position = video.current?.currentTime ?? currentTime;
    const value = inputTimeLabel(duration > 0 ? Math.min(duration, position) : position);
    if (boundary === "start") setManualStart(value);
    else setManualEnd(value);
    setManualError("");
    setManualMessage("");
  };
  const addManualRegion = async () => {
    if (disabledManual) return;
    const start = parseTime(manualStart);
    const end = parseTime(manualEnd);
    setManualMessage("");
    if (start === null || end === null) {
      setManualError("Başlangıç ve bitişi saniye veya saat:dakika:saniye biçiminde yaz. Örnek: 01:12.500");
      return;
    }
    if (end <= start || (duration > 0 && end > duration)) {
      setManualError("Bitiş başlangıçtan sonra olmalı ve aralık video süresini aşmamalı.");
      return;
    }
    setManualError("");
    if (await perform("manual", () => api.addManualSpeechRegion(job.id, start, end, manualText.trim()))) {
      setManualStart("");
      setManualEnd("");
      setManualText("");
      setManualMessage("Manuel aralık eklendi ve susturulmak üzere seçildi.");
    }
  };
  const deleteManualRegion = async (region: SpeechRegion) => {
    setManualMessage("");
    if (await perform("manual", () => api.deleteManualSpeechRegion(job.id, region.id))) {
      if (playingRegion === region.id) {
        video.current?.pause();
        playbackEnd.current = null;
        setPlayingRegion(null);
      }
      setManualMessage("Manuel aralık silindi.");
    }
  };
  const toggleRegion = (region: SpeechRegion) => {
    const next = new Set(removedIds);
    if (next.has(region.id)) next.delete(region.id);
    else next.add(region.id);
    selectRegions([...next]);
  };
  const playRegion = (region: SpeechRegion) => {
    const player = video.current;
    if (!player) return;
    setPreview("source");
    playbackEnd.current = region.end;
    setPlayingRegion(region.id);
    player.currentTime = region.start;
    void player.play()
      .then(() => { if (!mounted.current) player.pause(); })
      .catch(() => { if (mounted.current) setError("Önizleme başlatılamadı. Video üzerindeki oynat düğmesini kullanabilirsin."); });
  };
  const choosePreview = (next: PreviewMode) => {
    playbackEnd.current = null;
    setPlayingRegion(null);
    setPreview(next);
  };
  const exportVideo = () => {
    if (!state) return;
    void perform("export", async () => {
      const exported = await api.exportSpeechCleanup(job.id, "mute");
      return { ...state, exports: [exported, ...state.exports] };
    });
  };
  const revealExport = async (exportId: string) => {
    if (revealRequest.current) return;
    const request = Symbol();
    revealRequest.current = request;
    setOpeningExport(exportId);
    setError("");
    try {
      await api.revealSpeechExport(job.id, exportId);
    } catch (reason) {
      if (mounted.current && revealRequest.current === request) {
        setError(reason instanceof Error ? reason.message : "Dosya Gezgini açılamadı.");
      }
    } finally {
      if (mounted.current && revealRequest.current === request) {
        revealRequest.current = null;
        setOpeningExport(null);
      }
    }
  };

  return <section ref={workspace} className="speech-cleanup" aria-labelledby="speech-cleanup-heading">
    <header className="speech-header">
      <div>
        <div className="eyebrow">TÜM VİDEO · KONUŞMA TEMİZLEME</div>
        <h1 id="speech-cleanup-heading">Konuşmaları bul.<br /><span>İstediğini sustur.</span></h1>
        <p>{job.filename}{duration > 0 && <> · {timeLabel(duration)}</>}</p>
      </div>
      <div className="speech-separate-note"><span aria-hidden="true">↗</span><p>Bu alanın seçimleri ve çıktıları ayrı saklanır.<br />LongToShorts kliplerin ve kaynak videon korunur.</p></div>
    </header>

    {error && <div className="speech-message speech-error" role="alert">{error}
      {!state && <button type="button" onClick={() => void perform("detect", () => api.getSpeechCleanup(job.id))}>Yeniden dene</button>}
    </div>}
    {loading ? <div className="speech-empty" role="status">Konuşma alanı açılıyor…</div> : <>
      <div className="speech-grid">
        <div className="speech-player-column">
          <div className="speech-player-toolbar">
            <div className="speech-preview-switch" role="group" aria-label="Önizleme kaynağı">
              <button type="button" aria-pressed={preview === "source"} onClick={() => choosePreview("source")}>Orijinal</button>
              <button type="button" aria-pressed={preview === "cleaned"} disabled={!removedIds.size} onClick={() => choosePreview("cleaned")}>Temizlenmiş önizleme</button>
            </div>
            <span>{preview === "cleaned" ? "SEÇİLEN ARALIKLAR SESSİZ" : "KAYNAK VİDEO"}</span>
          </div>
          {importing ? <div className="speech-video-unavailable">Kaynak video indiriliyor. İndirme tamamlandığında konuşmaları bulabilirsin.</div>
            : <video ref={video} className="speech-source-player" controls playsInline preload="metadata"
              src={`/api/jobs/${job.id}/source`} aria-label="Konuşma temizleme video önizlemesi"
              onLoadedMetadata={(event) => { setMediaDuration(event.currentTarget.duration); setMediaError(false); }}
              onError={() => setMediaError(true)} />}
          {mediaError && <p className="speech-hint">Bu video tarayıcıda oynatılamıyor. Konuşma aralıklarını seçip MP4 olarak dışa aktarabilirsin.</p>}
          <div className="speech-player-time"><strong>{timeLabel(currentTime)}</strong><span>/ {timeLabel(duration)} · kaynak zamanı</span>
            {playingRegion && <button type="button" onClick={() => { playbackEnd.current = null; setPlayingRegion(null); }}>Tüm videoya dön</button>}
          </div>
          <div className="speech-timeline" role="group" aria-label="Videodaki konuşma aralıkları">
            {duration > 0 && regions.map((region) => <button key={region.id} type="button"
              className={removedIds.has(region.id) ? "removed" : ""}
              style={{ left: `${region.start / duration * 100}%`, width: `${Math.max(0.15, (region.end - region.start) / duration * 100)}%` }}
              title={`${timeLabel(region.start)} – ${timeLabel(region.end)} · ${region.text}`}
              aria-label={`${timeLabel(region.start)} ile ${timeLabel(region.end)} arasını dinle`}
              onClick={() => playRegion(region)} />)}
            {duration > 0 && <i style={{ left: `${Math.min(100, currentTime / duration * 100)}%` }} />}
          </div>
          <div className="speech-timeline-key"><span><i /> Konuşma</span><span><i /> Susturulacak</span><small>Bir aralığa tıklayıp dinle.</small></div>

          <section className="speech-export-panel" aria-labelledby="speech-export-heading">
            <span className="speech-kicker">KONUŞMA SESİNİ TEMİZLE</span><h2 id="speech-export-heading">Görüntü kesintisiz devam eder.</h2>
            <p className="speech-mute-description">Seçtiğin konuşma aralıkları sessizleştirilir. Videonun görüntüsü ve toplam süresi korunur. Bu aralıklardaki arka plan sesi ve müzik de susturulur.</p>
            {state?.mute_padding_before !== undefined && state.mute_padding_after !== undefined && <p className="speech-hint">Susturma, konuşmanın başlangıcından {paddingLabel(state.mute_padding_before)} önce başlar ve bitişinden {paddingLabel(state.mute_padding_after)} sonra biter.</p>}
            <dl className="speech-summary"><div><dt>Seçilen konuşma</dt><dd>{removedIds.size} / {regions.length}</dd></div><div><dt>Susturulacak süre</dt><dd>{durationLabel(removedDuration)}</dd></div><div><dt>Çıktı süresi</dt><dd>{timeLabel(duration)}</dd></div></dl>
            <button type="button" className="primary-button speech-export-button" disabled={state?.status !== "ready" || !removedIds.size || !!pending || working || sourceBusy}
              onClick={exportVideo}>{pending === "export" ? "Çıktı hazırlanıyor…" : "Temizlenmiş videoyu dışa aktar"}<span aria-hidden="true">↗</span></button>
            <p className="speech-hint">Kaynak kadrajı korunarak yeni bir MP4 oluşturulur. Seçimlerin bu projede kaydedilir.</p>
          </section>
        </div>

        <section className="speech-regions-panel" aria-labelledby="speech-regions-heading">
          <div className="speech-region-heading"><div><span className="speech-kicker">KONUŞMA ARALIKLARI</span><h2 id="speech-regions-heading">{regions.length ? `${regions.length} aralık${manualCount ? ` · ${manualCount} manuel` : ""}` : "Videodaki konuşmalar"}</h2></div>
            <button type="button" className="speech-outline-button" disabled={!!pending || working || sourceBusy || !state}
              onClick={() => void perform("detect", () => api.detectSpeech(job.id))}>{pending === "detect" || detecting ? "Bulunuyor…" : state?.status === "ready" ? "Yeniden tara" : "Konuşmaları bul"}</button>
          </div>
          {sourceBusy && <p className="speech-message" role="status">Projenin mevcut işlemi devam ediyor. Tamamlandığında konuşma temizlemeyi başlatabilirsin.</p>}
          {detecting && <div className="speech-detection" role="status"><strong>{state?.status === "queued" ? "Konuşma analizi sırada" : "Konuşma zamanları çıkarılıyor"}</strong><span>%{Math.round((state?.progress ?? 0) * 100)}</span><progress aria-label="Konuşma analizi ilerlemesi" value={state?.progress ?? 0} max={1} /><p>Konuşmalar yerel olarak bulunur. Bu adım Codex konu analizi gerektirmez.</p></div>}
          {state?.error && <p className="speech-message speech-error" role="alert">{state.error}</p>}
          <form className="speech-manual-form" aria-labelledby="speech-manual-heading" onSubmit={(event) => { event.preventDefault(); void addManualRegion(); }}>
            <div><span className="speech-kicker">SEN DE EKLE</span><h3 id="speech-manual-heading">Manuel aralık ekle</h3></div>
            <p>Temizlemek istediğin bölümün başlangıcını ve bitişini seç. Otomatik tarama yapmadan da ekleyebilirsin.</p>
            <div className="speech-manual-times">
              <div><label>Başlangıç<input type="text" inputMode="decimal" placeholder="00:12.500" value={manualStart} disabled={disabledManual}
                aria-describedby="speech-manual-time-hint" aria-invalid={!!manualError}
                onChange={(event) => { setManualStart(event.target.value); setManualError(""); setManualMessage(""); }} /></label>
                <button type="button" aria-label="Başlangıcı videodan al" disabled={disabledManual || mediaError || importing} onClick={() => usePlayhead("start")}>Videodan al</button></div>
              <div><label>Bitiş<input type="text" inputMode="decimal" placeholder="00:16.000" value={manualEnd} disabled={disabledManual}
                aria-describedby="speech-manual-time-hint" aria-invalid={!!manualError}
                onChange={(event) => { setManualEnd(event.target.value); setManualError(""); setManualMessage(""); }} /></label>
                <button type="button" aria-label="Bitişi videodan al" disabled={disabledManual || mediaError || importing} onClick={() => usePlayhead("end")}>Videodan al</button></div>
            </div>
            <p id="speech-manual-time-hint" className="speech-hint">Saniye, dakika:saniye veya saat:dakika:saniye yazabilirsin. Örnek: 12,5 veya 01:12.500.</p>
            <label className="speech-manual-note">Not (isteğe bağlı)<input type="text" placeholder="Örn. arada kalan konuşma" maxLength={500} value={manualText} disabled={disabledManual}
              onChange={(event) => setManualText(event.target.value)} /></label>
            {manualError && <p className="speech-message speech-error" role="alert">{manualError}</p>}
            <div className="speech-manual-footer"><span>Eklediğin aralık susturulmak üzere seçilir.</span><button type="submit" className="speech-outline-button" disabled={disabledManual}>{pending === "manual" ? "Kaydediliyor…" : "+ Aralığı ekle"}</button></div>
            <div className="speech-manual-status" role="status">{manualMessage}</div>
          </form>
          {!!regions.length && <>
            <div className="speech-bulk-actions"><button type="button" disabled={disabledSelection || removedIds.size === regions.length} onClick={() => selectRegions(regions.map((region) => region.id))}>Tüm konuşmaları sustur</button><button type="button" disabled={disabledSelection || !removedIds.size} onClick={() => selectRegions([])}>Tümünü geri al</button><span role="status">{pending === "selection" ? "Kaydediliyor…" : `${removedIds.size} aralık susturulacak`}</span></div>
            <label className="speech-search"><span>Konuşma metninde ara</span><input type="search" placeholder="Bir kelime veya cümle…" value={search} onChange={(event) => setSearch(event.target.value)} /></label>
            <div className="speech-region-list">{visibleRegions.map((region) => {
              const removed = removedIds.has(region.id);
              const active = currentTime >= region.start && currentTime < region.end;
              return <article key={region.id} className={`speech-region ${removed ? "removed" : ""} ${active ? "playing" : ""}`}>
                <div className="speech-region-top"><button type="button" className="speech-range-play" onClick={() => playRegion(region)} aria-label={`${timeLabel(region.start)} ile ${timeLabel(region.end)} arasındaki konuşmayı dinle`}><span aria-hidden="true">▶</span><time>{timeLabel(region.start)} – {timeLabel(region.end)}</time></button><span>{region.manual && <b className="speech-manual-badge">Manuel</b>}{durationLabel(region.end - region.start)}</span></div>
                <p>{region.text || "Konuşma"}</p>
                <div className="speech-region-bottom"><small>{removed ? "Sesi susturulacak" : "Sesi korunacak"}</small><div className="speech-region-actions">{region.manual && <button type="button" className="speech-manual-delete" disabled={disabledManual} onClick={() => void deleteManualRegion(region)} aria-label={`${timeLabel(region.start)} ile ${timeLabel(region.end)} arasındaki manuel aralığı sil`}>Sil</button>}<button type="button" disabled={disabledSelection} aria-pressed={removed} onClick={() => toggleRegion(region)}>{removed ? "↶ Geri al" : "× Bu konuşmayı sustur"}</button></div></div>
              </article>;
            })}</div>
            {!visibleRegions.length && <p className="speech-empty">Aramana uyan konuşma bulunamadı.</p>}
          </>}
          {!regions.length && !detecting && <div className="speech-empty"><span className="speech-empty-mark" aria-hidden="true">≋</span><h3>Henüz aralık yok</h3><p>Yukarıdan kendi aralığını ekleyebilir veya “Konuşmaları bul” ile otomatik tarama yapabilirsin. Seçtiğin aralıklar burada listelenir.</p></div>}
        </section>
      </div>

      {!!state?.exports.length && <section className="speech-exports" aria-labelledby="speech-exports-heading"><div className="speech-region-heading"><div><span className="speech-kicker">KONUŞMA TEMİZLEME ÇIKTILARI</span><h2 id="speech-exports-heading">Temizlenmiş videolar</h2></div><small>Her çıktı, oluşturulduğu andaki seçimi kullanır.</small></div>
        {state.exports.map((item) => <article key={item.id} className={`speech-export ${item.status}`}><div className="speech-export-details"><span className="speech-export-dot" aria-hidden="true" /><div><strong>{item.mode === "cut" ? "Konuşmalar kesildi" : "Konuşma aralıkları susturuldu"}</strong><p>{item.status === "completed" ? <>{item.filename}{item.duration != null && ` · ${timeLabel(item.duration)}`}</> : item.status === "failed" ? item.error || "Çıktı oluşturulamadı." : item.status === "queued" ? "Dışa aktarma sırada…" : `%${Math.round(item.progress * 100)} · Video hazırlanıyor…`}</p></div>{item.status === "completed" && <button type="button" className="speech-reveal-button" disabled={openingExport !== null} aria-busy={openingExport === item.id} onClick={() => void revealExport(item.id)}>{openingExport === item.id ? "Açılıyor…" : "Dosya Gezgini’nde göster"}</button>}</div>
          {(item.status === "queued" || item.status === "rendering") && <progress aria-label="Video dışa aktarma ilerlemesi" value={item.progress} max={1} />}
          {item.status === "completed" && <details><summary>Temizlenmiş videoyu izle</summary><video controls playsInline preload="none" src={`/api/jobs/${job.id}/speech-cleanup/exports/${item.id}/download`} /></details>}
        </article>)}
      </section>}
    </>}
  </section>;
}
