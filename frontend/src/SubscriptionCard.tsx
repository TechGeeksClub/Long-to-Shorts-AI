import { useEffect, useMemo, useRef, useState } from "react";
import type { SubscriptionSettings } from "./types";

export function SubscriptionCard({ settings, replay = 0 }: {
  settings: SubscriptionSettings;
  replay?: number;
}) {
  const [source, setSource] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [reducedMotion, setReducedMotion] = useState(() => window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const activeUrl = useRef("");
  const name = settings.channel_name.trim() || "Oban Otağı";

  useEffect(() => {
    const preference = window.matchMedia("(prefers-reduced-motion: reduce)");
    const changed = () => setReducedMotion(preference.matches);
    preference.addEventListener("change", changed);
    return () => preference.removeEventListener("change", changed);
  }, []);

  const query = useMemo(() => new URLSearchParams({
    channel_name: name,
    text: settings.text,
    show_tagline: String(settings.show_tagline),
    use_photo: String(settings.use_photo),
    click_effect: String(settings.click_effect),
    duration: String(Number.isFinite(settings.duration) ? Math.min(5, Math.max(1, settings.duration)) : 4.2),
    animation: settings.animation,
    position: settings.position,
    animated: String(!reducedMotion),
    replay: String(replay),
  }).toString(), [name, settings.text, settings.show_tagline, settings.use_photo, settings.click_effect,
    settings.duration, settings.animation, settings.position, reducedMotion, replay]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    const timer = window.setTimeout(() => {
      void (async () => {
        try {
          const response = await fetch(`/api/branding/subscription-preview?${query}`, { signal: controller.signal });
          if (!response.ok || !response.headers.get("content-type")?.startsWith("image/")) {
            throw new Error("Kart önizlemesi hazırlanamadı. Tekrar oynatmayı dene.");
          }
          const blob = await response.blob();
          if (controller.signal.aborted) return;
          const nextUrl = URL.createObjectURL(blob);
          const previousUrl = activeUrl.current;
          activeUrl.current = nextUrl;
          setSource(nextUrl);
          if (previousUrl) URL.revokeObjectURL(previousUrl);
        } catch (reason) {
          if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Önizleme yüklenemedi.");
        } finally {
          if (!controller.signal.aborted) setLoading(false);
        }
      })();
    }, 300);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [query]);

  useEffect(() => () => {
    if (activeUrl.current) URL.revokeObjectURL(activeUrl.current);
  }, []);

  return <div className="subscription-card-preview" aria-busy={loading}>
    {source ? <img className="subscription-card" src={source}
      width={920} height={232} alt={`${name} · Abone Ol butonu ve bildirim zili animasyonu`}
      onError={() => setError("Kart önizlemesi görüntülenemedi. Tekrar oynatmayı dene.")} />
      : <div className="subscription-card-placeholder" aria-hidden="true"><span /> <i /><i /></div>}
    <span className={`subscription-preview-status ${error ? "error" : ""}`} role="status">
      {error || (loading ? "Önizleme hazırlanıyor…" : reducedMotion ? "Hareket azaltma açık · Sabit önizleme" : "")}
    </span>
  </div>;
}
