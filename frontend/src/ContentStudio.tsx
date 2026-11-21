import { type FormEvent, useEffect, useState } from "react";
import { ApiError, api } from "./api";
import { TranscriptReader } from "./TranscriptReader";
import type {
  ContentEditBeat, ContentHistoryEntry, ContentHistoryItem, ContentOptions, ContentOptionsRequest, ContentPackage,
} from "./types";
import "./content-studio.css";

const referenceSources = [
  { id: "none", label: "Sadece konu" },
  { id: "youtube", label: "YouTube bağlantısı" },
  { id: "video", label: "Video yükle" },
  { id: "transcript", label: "Konuşma metni" },
] as const;

type ReferenceSource = typeof referenceSources[number]["id"];

const durations = [15, 30, 60, 90, 120, 180, 240, 360, 480, 600, 720, 900, 1200];
const historyDate = new Intl.DateTimeFormat("tr-TR", { dateStyle: "medium", timeStyle: "short" });

function formatDuration(seconds: number) {
  const rounded = Math.round(seconds);
  const minutes = Math.floor(rounded / 60);
  const remainder = rounded % 60;
  return [minutes ? `${minutes} dk` : "", remainder ? `${remainder} sn` : ""].filter(Boolean).join(" ");
}

function editNotes(beat: ContentEditBeat) {
  return [
    { label: "Görüntü", text: beat.visual },
    { label: "Kesme ve tempo", text: beat.edit },
    { label: "Ses", text: beat.audio },
    { label: "Ekran yazısı", text: beat.on_screen },
    { label: "Geçiş", text: beat.transition },
  ].filter(({ text }) => text);
}

function message(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

function packageText(result: ContentPackage) {
  const duration = [
    result.target_seconds && `Hedef video süresi • ${formatDuration(result.target_seconds)}`,
    result.estimated_speech_seconds && `Tahmini konuşma süresi • ${formatDuration(result.estimated_speech_seconds)}`,
  ].filter(Boolean).join("\n");
  return [
    duration,
    "KONUŞMA METNİ",
    result.script,
    "KURGU PLANI",
    result.edit_plan.map((scene) => [
      scene.beat, scene.spoken && `Konuşma\n${scene.spoken}`,
      ...editNotes(scene).map(({ label, text }) => `${label}\n${text}`),
    ].filter(Boolean).join("\n\n")).join("\n\n——\n\n"),
    "BAŞLIK ÖNERİLERİ",
    result.seo.titles.join("\n"),
    "YOUTUBE AÇIKLAMASI",
    result.seo.description,
    "ANAHTAR KELİMELER",
    result.seo.keywords.join(", "),
  ].filter(Boolean).join("\n\n");
}

export function ContentStudio() {
  const [brief, setBrief] = useState("");
  const [targetSeconds, setTargetSeconds] = useState<number>();
  const [source, setSource] = useState<ReferenceSource>("none");
  const [url, setUrl] = useState("");
  const [transcript, setTranscript] = useState("");
  const [video, setVideo] = useState<File | null>(null);
  const [analysis, setAnalysis] = useState<{ data: ContentOptions; payload: ContentOptionsRequest; video: File | null } | null>(null);
  const [history, setHistory] = useState<ContentHistoryItem[] | null>(null);
  const [archived, setArchived] = useState<ContentHistoryEntry | null>(null);
  const [historyError, setHistoryError] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [result, setResult] = useState<ContentPackage | null>(null);
  const [busy, setBusy] = useState<"options" | "generate" | "history" | null>(null);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [optionsError, setOptionsError] = useState<{ message: string; offerAlternatives: boolean } | null>(null);
  const [generateError, setGenerateError] = useState("");
  const [copyStatus, setCopyStatus] = useState("");

  useEffect(() => {
    let ignore = false;
    api.listContentHistory().then(
      (items) => {
        if (ignore) return;
        setHistory(items);
        setHistoryError("");
      },
      () => {
        if (ignore) return;
        setHistoryError("Akış önerisi geçmişi yüklenemedi.");
      },
    );
    return () => { ignore = true; };
  }, [analysis]);

  const payload: ContentOptionsRequest = {
    brief: brief.trim(),
    target_seconds: targetSeconds,
    ...(source === "youtube" ? { reference_url: url.trim() } : {}),
    ...(source === "transcript" ? { reference_transcript: transcript.trim() } : {}),
  };
  const referenceVideo = source === "video" ? video : null;
  const ready = payload.brief && (
    source === "none" || payload.reference_url || payload.reference_transcript || referenceVideo
  );
  const matchesInput = analysis
    && analysis.payload.brief === payload.brief
    && analysis.payload.target_seconds === payload.target_seconds
    && analysis.payload.reference_url === payload.reference_url
    && analysis.payload.reference_transcript === payload.reference_transcript
    && analysis.video === referenceVideo;
  const current = archived ?? (matchesInput ? {
    brief: analysis.payload.brief,
    has_reference: source !== "none",
    data: analysis.data,
  } : null);
  const option = current?.data.options.find((item) => item.id === selectedId);
  const analyzing = busy === "options";
  const generating = busy === "generate";

  const changeSource = (next: ReferenceSource, focusField?: string) => {
    if (next === source) return;
    setSource(next);
    setOptionsError(null);
    if (focusField) requestAnimationFrame(() => document.getElementById(focusField)?.focus());
  };

  const resetSelection = () => {
    setSelectedId(null);
    setResult(null);
    setGenerateError("");
    setCopyStatus("");
  };

  const suggest = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy || !ready) return;
    setBusy("options");
    setOptionsError(null);
    setUploadProgress(0);
    try {
      const data = referenceVideo
        ? await api.suggestContentOptionsFromVideo(payload.brief, referenceVideo, setUploadProgress, payload.target_seconds)
        : await api.suggestContentOptions(payload);
      setAnalysis({ data, payload, video: referenceVideo });
      setArchived(null);
      resetSelection();
    } catch (error) {
      setOptionsError({
        message: message(error, "Akış önerileri hazırlanamadı."),
        offerAlternatives: source === "youtube" && error instanceof ApiError && error.code === "youtube_reference_unavailable",
      });
    } finally {
      setBusy(null);
    }
  };

  const openHistory = async (id: string) => {
    if (busy || archived?.id === id) return;
    setBusy("history");
    setHistoryError("");
    try {
      setArchived(await api.getContentHistoryEntry(id));
      resetSelection();
    } catch (error) {
      setHistoryError(message(error, "Geçmişteki akış önerileri açılamadı."));
    } finally {
      setBusy(null);
    }
  };

  const generate = async () => {
    if (busy || !current || !option) return;
    setBusy("generate");
    setGenerateError("");
    setCopyStatus("");
    try {
      setResult(await api.generateContentPackage({
        brief: current.brief,
        target_seconds: current.data.target_seconds ?? (archived ? undefined : analysis?.payload.target_seconds),
        reference_insights: current.data.reference_insights,
        option,
      }));
    } catch (error) {
      setGenerateError(message(error, "İçerik hazırlanamadı."));
    } finally {
      setBusy(null);
    }
  };

  const copy = async (text: string, success: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopyStatus(success);
    } catch {
      setCopyStatus("Panoya erişilemedi. Metni seçip kopyalayabilirsin.");
    }
  };

  return (
    <main className="content-studio">
      <header className="content-studio-header">
        <h1>İçerik Stüdyosu</h1>
        <p>Konunu anlat, istersen bir örnek ekle. Seçtiğin akıştan Codex konuşma metni, kurgu planı ve YouTube başlıkları hazırlasın.</p>
      </header>

      <form className="content-studio-form" onSubmit={(event) => void suggest(event)} aria-busy={analyzing}>
        <div className="content-studio-form-grid">
          <div className="content-studio-card content-studio-fields">
            <label htmlFor="content-brief">Videonun konusu ve notların</label>
            <textarea id="content-brief" rows={7} required maxLength={12000} disabled={busy !== null}
              placeholder="Forza 250’den sonra tek bir ADV mi alsam, şehir içiyle uzun yolu iki motora mı bölsem? Parkta ağırlığını hissettiğim bir anı anlatmak istiyorum."
              aria-describedby="content-brief-hint" value={brief} onChange={(event) => setBrief(event.target.value)} />
            <small id="content-brief-hint">Yaşadığın anları, aklındaki soruyu ve gerçekten çekebileceğin görüntüleri yaz.</small>
            <div className="content-studio-duration">
              <label htmlFor="content-duration">Hedef video süresi</label>
              <select id="content-duration" value={targetSeconds ?? ""} disabled={busy !== null}
                aria-describedby="content-duration-hint"
                onChange={(event) => setTargetSeconds(event.target.value ? Number(event.target.value) : undefined)}>
                <option value="">Otomatik</option>
                {durations.map((seconds) => <option key={seconds} value={seconds}>{formatDuration(seconds)}</option>)}
              </select>
              <small id="content-duration-hint">Otomatik seçim notlarında belirttiğin süreyi kullanır. Varsayılan 6 dk.</small>
            </div>
          </div>

          <fieldset className="content-studio-card content-studio-fields" disabled={busy !== null} aria-labelledby="content-source-label">
            <span className="content-studio-label" id="content-source-label">İlham kaynağı <small>(isteğe bağlı)</small></span>
            <div className="content-studio-sources" role="group" aria-label="Örnek içeriğin biçimi">
              {referenceSources.map(({ id, label }) => (
                <button type="button" key={id} aria-pressed={id === source} onClick={() => changeSource(id)}>{label}</button>
              ))}
            </div>
            {source === "none" && <p>Yalnızca kendi notlarınla akış önerileri hazırla.</p>}
            {source === "youtube" && (
              <div className="content-studio-fields">
                <label htmlFor="content-url">Örnek YouTube videosu</label>
                <input id="content-url" type="text" inputMode="url" spellCheck={false} autoComplete="url" required
                  placeholder="https://www.youtube.com/watch?v=…" maxLength={2048}
                  value={url} onChange={(event) => {
                    setUrl(event.target.value);
                    setOptionsError(null);
                  }} />
                <small>Videodaki konuşma çıkarılır ve açılış, geçişler, anlatım temposu incelenir.</small>
              </div>
            )}
            <div className="content-studio-fields" hidden={source !== "video"}>
              <label htmlFor="content-video">Bilgisayarındaki örnek video</label>
              <input id="content-video" type="file" accept=".mp4,.mov,.mkv,.m4v,.avi,.webm"
                onChange={(event) => setVideo(event.target.files?.[0] ?? null)} />
            </div>
            {source === "transcript" && (
              <div className="content-studio-fields">
                <label htmlFor="content-transcript">Örnek videonun konuşma metni</label>
                <textarea id="content-transcript" rows={5} required maxLength={100000}
                  placeholder="Örnek videonun konuşma metnini buraya yapıştır."
                  aria-describedby="content-transcript-hint"
                  value={transcript} onChange={(event) => setTranscript(event.target.value)} />
                <small id="content-transcript-hint">YouTube’da “Transkripti göster” seçeneği varsa metni oradan kopyalayabilirsin.</small>
              </div>
            )}
          </fieldset>
        </div>
        <div className="content-studio-action">
          <button type="submit" className="primary-button" disabled={busy !== null || !ready}>
            {analyzing ? "Akışlar hazırlanıyor…" : analysis ? "Akışları yeniden öner" : "Akış önerileri al"}
          </button>
          {analyzing && <span role="status">
            {referenceVideo && uploadProgress < 1
              ? `Video yükleniyor (%${Math.round(uploadProgress * 100)})`
              : "Codex konunu değerlendiriyor ve her seçenek için bölüm bölüm bir plan hazırlıyor."}
          </span>}
          {analysis && !matchesInput && !archived && !analyzing && <span role="status">Girdiler değişti. Akışları güncelle.</span>}
        </div>
        {optionsError && (
          <div className="error-banner">
            <div role="alert">{optionsError.message}</div>
            {optionsError.offerAlternatives && (
              <div className="content-studio-action">
                <button type="button" className="secondary-button" onClick={() => changeSource("video", "content-video")}>
                  Video dosyası yükle
                </button>
                <button type="button" className="secondary-button" onClick={() => changeSource("transcript", "content-transcript")}>
                  Konuşma metni yapıştır
                </button>
              </div>
            )}
          </div>
        )}
      </form>

      {(history || historyError) && (
        <section className="content-studio-section" aria-labelledby="content-history-heading">
          <h2 id="content-history-heading">Geçmiş akış önerileri</h2>
          {history?.length ? (
            <ul className="content-studio-history">
              {history.map((item) => (
                <li key={item.id}>
                  <button type="button" disabled={busy !== null} aria-pressed={archived?.id === item.id}
                    onClick={() => void openHistory(item.id)}>
                    <strong>{item.title}</strong>
                    <span>{item.brief_preview}</span>
                    <small><time dateTime={item.created_at}>{historyDate.format(new Date(item.created_at))}</time>
                      {" · "}{formatDuration(item.target_seconds)}</small>
                  </button>
                </li>
              ))}
            </ul>
          ) : !historyError && <p className="content-studio-history-empty">Henüz kaydedilmiş akış önerisi yok.</p>}
          {busy === "history" && <p role="status">Akış önerileri açılıyor…</p>}
          {historyError && <p className="error-banner" role="alert">{historyError}</p>}
        </section>
      )}

      {current && (
        <section key={archived?.id} className="content-studio-section" aria-labelledby="content-options-heading" aria-busy={generating}>
          <div className="content-studio-result-header">
            <h2 id="content-options-heading">Akışını seç</h2>
            {current.data.target_seconds && <span className="content-studio-options-duration">
              Planlanan süre <strong>{formatDuration(current.data.target_seconds)}</strong>
            </span>}
          </div>
          {archived && (
            <details className="content-studio-insights content-studio-disclosure">
              <summary>Bu önerilerin konusu ve notları</summary>
              <p>{archived.brief}</p>
            </details>
          )}
          {current.data.reference_insights && (
            <div className="content-studio-insights">
              <h3>{current.has_reference ? "Örnek videodan gözlemler" : "İçerik notu"}</h3>
              <p>{current.data.reference_insights}</p>
            </div>
          )}
          <fieldset className="content-studio-option-grid" aria-labelledby="content-options-heading" disabled={busy !== null}>
            {current.data.options.map((item, index) => (
              <article className="content-studio-option" key={item.id}>
                <label className="content-studio-option-heading">
                  <input type="radio" name="content-flow" aria-labelledby={`content-option-${index}`}
                    checked={selectedId === item.id}
                    onChange={() => {
                      setSelectedId(item.id);
                      setResult(null);
                      setGenerateError("");
                      setCopyStatus("");
                    }} />
                  <strong id={`content-option-${index}`}>{item.title}</strong>
                </label>
                {item.premise && <p className="content-studio-premise">{item.premise}</p>}
                <blockquote className="content-studio-hook">{item.hook}</blockquote>
                <dl className="content-studio-flow-notes">
                  <dt>Yaklaşım</dt><dd>{item.approach}</dd>
                  {item.payoff && <><dt>İzleyiciye vaadi</dt><dd>{item.payoff}</dd></>}
                  {item.tradeoff && <><dt>Çekimde dikkat edilecek nokta</dt><dd>{item.tradeoff}</dd></>}
                </dl>
                <details className="content-studio-disclosure">
                  <summary>Bölüm planı <span>{item.beats.length} bölüm</span></summary>
                  <ol>
                    {item.beats.map((beat, beatIndex) => typeof beat === "string" ? <li key={beatIndex}>{beat}</li> : (
                      <li key={beatIndex}>
                        <h4>{beat.title}</h4>
                        <p>{beat.story}</p>
                        <dl className="content-studio-flow-notes">
                          <dt>Görüntü</dt><dd>{beat.visual}</dd>
                          <dt>Geçiş</dt><dd>{beat.transition}</dd>
                        </dl>
                      </li>
                    ))}
                  </ol>
                </details>
              </article>
            ))}
          </fieldset>
          <div className="content-studio-action">
            <button className="primary-button" type="button" disabled={busy !== null || !option} onClick={() => void generate()}>
              {generating ? "İçerik hazırlanıyor…" : "Seçili akıştan içerik üret"}
            </button>
            {generating && <span role="status">
              Codex önce bölüm planını geliştiriyor, ardından konuşmayı ve kurgu notlarını yazıyor.
              Uzun videolarda bu işlem birkaç dakika sürebilir.
            </span>}
          </div>
          {generateError && <p className="error-banner" role="alert">{generateError}</p>}
        </section>
      )}

      {current && result && option && (
        <section className="content-studio-section" aria-labelledby="content-result-heading">
          <div className="content-studio-result-header">
            <div>
              <h2 id="content-result-heading">İçerik paketin</h2>
              <p>{option.title}</p>
              {(result.target_seconds || result.estimated_speech_seconds) && <div className="content-studio-result-duration">
                {result.target_seconds && <span>Hedef video süresi <strong>{formatDuration(result.target_seconds)}</strong></span>}
                {result.estimated_speech_seconds && <span>Tahmini konuşma <strong>{formatDuration(result.estimated_speech_seconds)}</strong></span>}
              </div>}
            </div>
            <button className="secondary-button" type="button" onClick={() => void copy(packageText(result), "İçerik paketi kopyalandı.")}>
              Paketi kopyala
            </button>
          </div>
          <p className="content-studio-copy-status" role="status">{copyStatus}</p>
          <div className="content-studio-result-grid">
            <article className="content-studio-card">
              <div className="content-studio-result-header">
                <h3>Konuşma metni</h3>
                <div className="transcript-actions">
                  <TranscriptReader text={result.script} />
                  <button className="secondary-button" type="button" onClick={() => void copy(result.script, "Konuşma metni kopyalandı.")}>
                    Metni kopyala
                  </button>
                </div>
              </div>
              <p className="content-studio-script">{result.script}</p>
            </article>
            <article className="content-studio-card">
              <h3>Kurgu planı</h3>
              <ol className="content-studio-edit-plan">
                {result.edit_plan.map((scene, index) => (
                  <li key={index}>
                    <details className="content-studio-disclosure" open={index === 0}>
                      <summary>{scene.beat}</summary>
                      {scene.spoken && <blockquote className="content-studio-scene-spoken">{scene.spoken}</blockquote>}
                      <dl>
                        {editNotes(scene).map(({ label, text }) => (
                          <div key={label}><dt>{label}</dt><dd>{text}</dd></div>
                        ))}
                      </dl>
                    </details>
                  </li>
                ))}
              </ol>
            </article>
            <article className="content-studio-card content-studio-seo">
              <h3>YouTube SEO</h3>
              <dl>
                <dt>Başlık önerileri</dt>
                <dd><ul>{result.seo.titles.map((title, index) => <li key={index}>{title}</li>)}</ul></dd>
                <dt>Açıklama</dt><dd>{result.seo.description}</dd>
                <dt>Anahtar kelimeler</dt><dd>{result.seo.keywords.join(", ")}</dd>
              </dl>
            </article>
          </div>
        </section>
      )}
    </main>
  );
}
