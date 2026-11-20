import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { SubscriptionCard } from "./SubscriptionCard";
import type { ClipCandidate, EditSettings, SubscriptionSettings, SubscriptionSuggestions } from "./types";

export function SubscriptionEditor({ clip, editSettings, playbackRate, disabled, hookValid, onChange }: {
  clip: ClipCandidate;
  editSettings: EditSettings;
  playbackRate: number;
  disabled: boolean;
  hookValid: boolean;
  onChange: (value: SubscriptionSettings) => void;
}) {
  const value = editSettings.subscription;
  const [result, setResult] = useState<SubscriptionSuggestions | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [replay, setReplay] = useState(0);
  const [showPlacement, setShowPlacement] = useState(false);
  const pending = useRef<AbortController | null>(null);
  const context = JSON.stringify({ id: clip.id, start: clip.start, end: clip.end,
    subtitles: clip.subtitles, cuts: clip.cut_ranges, inserts: clip.insert_ranges, brief: clip.youtube.brief,
    hook: editSettings.hook_range, removeHook: editSettings.hook_remove_from_body,
    style: editSettings.style, opening: editSettings.hook_text, openingDuration: editSettings.hook_duration,
    playbackRate });
  useEffect(() => {
    pending.current?.abort();
    setResult(null); setError(""); setLoading(false);
    return () => { pending.current?.abort(); };
  }, [context]);
  const update = (patch: Partial<SubscriptionSettings>) => onChange({ ...value, ...patch });
  const suggest = async () => {
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    setLoading(true); setError("");
    try {
      const suggestions = await api.suggestSubscriptions(clip, editSettings, playbackRate, controller.signal);
      if (!controller.signal.aborted) setResult(suggestions);
    } catch (reason) {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Mesaj önerileri alınamadı.");
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  };
  return <section className="subscription-panel" aria-label="Aboneliği teşvik et">
    <label className="motovlog-check">
      <input type="checkbox" checked={value.enabled} disabled={disabled || loading}
        onChange={(event) => update({ enabled: event.target.checked,
          channel_name: value.channel_name || "Oban Otağı",
          start: Number.isFinite(value.start) ? Math.min(86400, Math.max(0, value.start)) : 8,
          duration: Number.isFinite(value.duration) ? Math.min(5, Math.max(1, value.duration)) : 4.2,
        })} />
      <strong>Aboneliği teşvik et</strong>
    </label>
    <p className="motovlog-help">Kanal fotoğrafın, Abone Ol butonu ve bildirim zili; tek bir akıcı animasyonda. Kart videonun yatay merkezine hizalanır.</p>
    {value.enabled && <>
      <div className="subscription-demo-stage" aria-label="Abonelik kartı stil örneği">
        <span className="subscription-preview-label">KART ÖNİZLEMESİ</span>
        <SubscriptionCard settings={value} replay={replay} />
      </div>
      <div className="subscription-replay">
        <button type="button" className="tiny-button" onClick={() => setReplay((count) => count + 1)}>Animasyonu tekrar oynat</button>
        <span>{value.click_effect ? "Abone Ol → Abone Olundu → Zil" : "Kartın giriş ve çıkış animasyonu"}</span>
        <button type="button" className="subscription-placement-button" aria-expanded={showPlacement}
          onClick={() => setShowPlacement((visible) => !visible)}>{showPlacement ? "Yerleşimi gizle" : "Yerleşimi göster"}</button>
      </div>
      {showPlacement && <div className="subscription-placement" aria-label="9:16 kart yerleşimi">
        <div className={`subscription-placement-frame ${value.position}`} aria-hidden="true">
          <span className="subscription-center-guide" />
          <span className="subscription-placement-marker"><i /><b /><em /></span>
          <span className="subscription-placement-caption">Altyazı</span>
          <span className="subscription-player-controls"><i /><i /><i /></span>
          <span className="subscription-player-bottom" />
        </div>
        <div><strong>Her iki kenara eşit mesafe</strong>
          <p>{value.position === "above_subtitles" ? "Kart altyazıların üstünde görünür; görüntünün orta çizgisine göre ortalanır."
            : "Kart üst bölümde, aynı orta çizgi üzerinde görünür."}</p>
          <small>Yerleşim şeması. Kadrajı, yüzleri ve altyazı konumunu “Kaydet ve önizle” ile kontrol et.</small>
        </div>
      </div>}
      <label className="motovlog-field subscription-channel"><span>Kartta görünecek kanal adı <small>{value.channel_name.length}/50</small></span>
        <input maxLength={50} value={value.channel_name} disabled={disabled || loading} placeholder="Oban Otağı"
          onChange={(event) => update({ channel_name: event.target.value })} />
      </label>
      <div className="subscription-controls">
            <label className="motovlog-field">Kartın konumu
              <select value={value.position} disabled={disabled || loading}
                onChange={(event) => update({ position: event.target.value as SubscriptionSettings["position"] })}>
                <option value="above_subtitles">Altyazıların üstü · önerilen</option>
                <option value="top">Üst bölüm</option>
              </select>
            </label>
            <label className="motovlog-field">Başlangıç (çıktı saniyesi)
              <input type="number" min={0} max={86400} step={0.1} value={Number.isFinite(value.start) ? value.start : ""} disabled={disabled || loading}
                onChange={(event) => update({ start: event.currentTarget.valueAsNumber })} />
            </label>
            <label className="motovlog-field">Gösterim süresi (1–5 sn)
              <input type="number" min={1} max={5} step={0.1} value={Number.isFinite(value.duration) ? value.duration : ""} disabled={disabled || loading}
                onChange={(event) => update({ duration: event.currentTarget.valueAsNumber })} />
            </label>
          <label className="motovlog-field subscription-animation">Animasyon
            <select value={value.animation} disabled={disabled || loading}
              onChange={(event) => update({ animation: event.target.value as SubscriptionSettings["animation"] })}>
              <option value="fade">Yumuşak belirme</option>
              <option value="slide">Hafif yukarı kayma</option>
            </select>
          </label>
      </div>
      <div className="subscription-toggles">
        <label className="motovlog-check"><input type="checkbox" checked={value.use_photo} disabled={disabled || loading}
          onChange={(event) => update({ use_photo: event.target.checked })} />Kanal fotoğrafını kullan</label>
        <label className="motovlog-check"><input type="checkbox" checked={value.click_effect} disabled={disabled || loading}
          onChange={(event) => update({ click_effect: event.target.checked })} />Abone Ol ve zil tıklaması</label>
      </div>
      <p className="motovlog-help">{value.position === "above_subtitles" ? "Altyazıların üstünde, videonun tam ortasında." : "Üst bölümde, videonun tam ortasında. Açılış yazısı ve yüzlerle çakışmadığını kontrol et."} Tıklama akışı için 4–5 sn önerilir.</p>
      <p className="motovlog-help">Zamanlar {playbackRate.toFixed(2)}× dışa aktarma hızındaki son videoya göredir. Gerçek sonucu “Kaydet ve önizle” ile izle.</p>
      <details className="subscription-extra">
        <summary>Alt satırı özelleştir</summary>
        <label className="motovlog-check"><input type="checkbox" checked={value.show_tagline} disabled={disabled || loading}
          onChange={(event) => update({ show_tagline: event.target.checked })} />Kanal adının altında kısa bir mesaj göster</label>
        {value.show_tagline && <>
          <label className="motovlog-field"><span>Kısa alt satır <small>{value.text.length}/90</small></span>
            <input maxLength={90} value={value.text} disabled={disabled || loading}
              placeholder="Yolda görüşürüz" onChange={(event) => update({ text: event.target.value })} />
          </label>
          <button type="button" className="tiny-button subscription-generate" disabled={disabled || loading || !hookValid}
            onClick={() => void suggest()}>{loading ? "Codex önerileri hazırlıyor…" : "Codex ile alt satır ve zaman öner"}</button>
        </>}
      </details>
      {result && value.show_tagline && <div className="subscription-suggestions" aria-live="polite">
        {result.options.map((item) => <button key={item.text} type="button" disabled={disabled || loading}
          className={value.text === item.text && value.start === item.start ? "selected" : ""}
          onClick={() => update({ text: item.text, start: item.start, duration: item.duration })}>
          <strong>{item.text}</strong>
          <small>{item.start.toFixed(1)} sn'de · {item.duration} sn — {item.reason}</small>
        </button>)}
        <p className="motovlog-help">Öneriler {result.output_duration.toFixed(1)} saniyelik kayıtlı kesit ve mevcut hook ayarlarına göre hazırlandı. Mesajı seçip düzenleyebilirsin.</p>
      </div>}
      {error && <p className="subscription-error" role="alert">{error}</p>}
    </>}
  </section>;
}
