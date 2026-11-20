import { type FormEvent, useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { YoutubeChannelVideos } from "./types";

function youtubeUrl(value: string): URL | null {
  try {
    const url = new URL(/^https?:\/\//i.test(value.trim()) ? value.trim() : `https://${value.trim()}`);
    if (!["https:", "http:"].includes(url.protocol) || url.username || url.password || url.port) return null;
    if (!["youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"].includes(url.hostname.toLowerCase())) return null;
    return url;
  } catch { return null; }
}

function channelUrl(value: string): string | null {
  const url = youtubeUrl(value);
  if (!url || url.hostname.includes("youtu.be")) return null;
  if (!/^\/(?:@[^/]+|channel\/[^/]+|c\/[^/]+|user\/[^/]+)(?:\/(?:videos|featured|shorts|streams))?\/?$/.test(url.pathname)) return null;
  return url.href;
}

function videoUrl(value: string): string | null {
  const url = youtubeUrl(value);
  if (!url) return null;
  const id = url.hostname.includes("youtu.be") ? url.pathname.match(/^\/([^/]+)\/?$/)?.[1] :
    url.pathname === "/watch" ? url.searchParams.get("v") : url.pathname.match(/^\/(?:shorts|live|embed)\/([^/]+)\/?$/)?.[1];
  return id && /^[A-Za-z0-9_-]{11}$/.test(id) ? `https://www.youtube.com/watch?v=${id}` : null;
}

function durationLabel(value: number | null): string | null {
  if (value === null || !Number.isFinite(value)) return null;
  const seconds = Math.max(0, Math.floor(value));
  const minutes = Math.floor(seconds / 60);
  return minutes >= 60
    ? `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`
    : `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

export function YoutubeImport({ busy, onImport }: { busy: boolean; onImport: (url: string) => Promise<void> }) {
  const [channel, setChannel] = useState("");
  const [directVideo, setDirectVideo] = useState("");
  const [result, setResult] = useState<YoutubeChannelVideos | null>(null);
  const [browsing, setBrowsing] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");
  const [directError, setDirectError] = useState("");
  const [importingUrl, setImportingUrl] = useState<string | null>(null);
  const browseRequest = useRef<AbortController | null>(null);
  useEffect(() => () => browseRequest.current?.abort(), []);
  const disabled = busy || importingUrl !== null;
  const loading = browsing || loadingMore;

  const browse = async (event: FormEvent) => {
    event.preventDefault();
    if (disabled || loading) return;
    const normalized = channelUrl(channel);
    if (!normalized) {
      setError("Geçerli bir YouTube kanal bağlantısı gir. Örnek: youtube.com/@kanaladi");
      return;
    }
    browseRequest.current?.abort();
    const controller = new AbortController();
    browseRequest.current = controller;
    setBrowsing(true);
    setResult(null);
    setError("");
    try { setResult(await api.browseYoutubeChannel(normalized, 0, controller.signal)); }
    catch (reason) {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Kanal videoları yüklenemedi.");
    } finally { if (!controller.signal.aborted) setBrowsing(false); }
  };

  const loadMore = async () => {
    if (disabled || loading || !result || result.next_offset === null) return;
    const controller = new AbortController();
    browseRequest.current = controller;
    setLoadingMore(true);
    setError("");
    try {
      const next = await api.browseYoutubeChannel(result.channel_url, result.next_offset, controller.signal);
      const seen = new Set(result.videos.map((video) => video.id));
      setResult({ ...next, videos: [...result.videos, ...next.videos.filter((video) => !seen.has(video.id))] });
    } catch (reason) {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Diğer videolar yüklenemedi.");
    } finally { if (!controller.signal.aborted) setLoadingMore(false); }
  };

  const importVideo = async (url: string, direct = false) => {
    if (disabled || loading) return;
    setImportingUrl(url);
    if (direct) setDirectError(""); else setError("");
    try { await onImport(url); }
    catch (reason) {
      const message = reason instanceof Error ? reason.message : "Video içe aktarılamadı.";
      if (direct) setDirectError(message); else setError(message);
    } finally { setImportingUrl(null); }
  };

  const importDirect = (event: FormEvent) => {
    event.preventDefault();
    const normalized = videoUrl(directVideo);
    if (!normalized) {
      setDirectError("Geçerli bir YouTube video bağlantısı gir. Kanal veya oynatma listesi bağlantısı kullanma.");
      return;
    }
    void importVideo(normalized, true);
  };

  return <div className="youtube-import">
    <div className="youtube-import-heading">
      <span className="youtube-source-mark" aria-hidden="true">▶</span>
      <div><h2>Kanalından bir video seç</h2>
        <p>Kanal bağlantını gir, videolarını listele ve birini yeni proje olarak ekle. Seçtiğin video indirilir; altyazı ve Codex konu analizi otomatik başlar.</p></div>
    </div>
    <form className="youtube-source-form" onSubmit={(event) => void browse(event)}>
      <label htmlFor="youtube-channel-url">YouTube kanal bağlantısı</label>
      <div className="youtube-source-input-row">
        <input id="youtube-channel-url" type="text" inputMode="url" autoComplete="url" spellCheck={false}
          placeholder="https://www.youtube.com/@kanaladi" value={channel}
          disabled={disabled || loading} onChange={(event) => { setChannel(event.target.value); setError(""); setResult(null); }}
          aria-describedby="youtube-channel-help" aria-invalid={Boolean(error)} required />
        <button className="primary-button" type="submit" disabled={disabled || loading || !channel.trim()}>
          {browsing ? "Videolar yükleniyor…" : "Videoları listele"}
        </button>
      </div>
      <small id="youtube-channel-help">@kanaladi, /channel/…, /c/… veya /user/… bağlantısını kullanabilirsin.</small>
    </form>
    {error && <p className="youtube-source-error" role="alert">{error}</p>}
    <div aria-live="polite" aria-busy={loading}>
      {browsing && <p className="youtube-source-status">Kanalındaki videolar getiriliyor. Bu işlem biraz sürebilir.</p>}
      {result && <section className="youtube-channel-results" aria-label="Kanal videoları">
        <div className="youtube-channel-heading">
          <h3><a href={result.channel_url} target="_blank" rel="noopener noreferrer">{result.channel_title}</a></h3>
          <span>{result.videos.length} video yüklendi{result.has_more ? " · Daha fazla video var" : ""}</span>
        </div>
        {!result.videos.length && <p className="youtube-source-status">Bu kanalda içe aktarılabilecek video bulunamadı. Aşağıdan doğrudan video bağlantısıyla da deneyebilirsin.</p>}
        <div className="youtube-video-grid">
          {result.videos.map((video) => <article className="youtube-source-video" key={video.id}>
            <div className="youtube-source-thumbnail">
              {video.thumbnail_url ? <img src={video.thumbnail_url} alt="" loading="lazy" referrerPolicy="no-referrer" /> : <span aria-hidden="true">▶</span>}
              {durationLabel(video.duration) && <small>{durationLabel(video.duration)}</small>}
            </div>
            <div className="youtube-source-video-body">
              <h4>{video.title}</h4>
              <button className="secondary-button" type="button" disabled={disabled || loading} onClick={() => void importVideo(video.url)}
                aria-label={`${video.title} videosunu içe aktar`}>
                {importingUrl === video.url ? "İçe aktarma başlatılıyor…" : "Bu videoyu içe aktar"}
              </button>
            </div>
          </article>)}
        </div>
        {result.has_more && result.next_offset !== null && <button className="secondary-button youtube-load-more" type="button"
          disabled={disabled || loading} onClick={() => void loadMore()}>
          {loadingMore ? "Diğer videolar yükleniyor…" : "Daha fazla video göster"}
        </button>}
      </section>}
    </div>
    <details className="youtube-direct-source">
      <summary>Doğrudan video bağlantısı kullan</summary>
      <form className="youtube-source-form" onSubmit={importDirect}>
        <label htmlFor="youtube-video-url">YouTube video bağlantısı</label>
        <div className="youtube-source-input-row">
          <input id="youtube-video-url" type="text" inputMode="url" autoComplete="url" spellCheck={false}
            placeholder="https://www.youtube.com/watch?v=…" value={directVideo} disabled={disabled || loading}
            onChange={(event) => { setDirectVideo(event.target.value); setDirectError(""); }} aria-invalid={Boolean(directError)} required />
          <button className="primary-button" type="submit" disabled={disabled || loading || !directVideo.trim()}>
            {importingUrl && importingUrl === videoUrl(directVideo) ? "Başlatılıyor…" : "Videoyu içe aktar"}
          </button>
        </div>
        {directError && <p className="youtube-source-error" role="alert">{directError}</p>}
      </form>
    </details>
  </div>;
}
