import { useEffect, useState } from "react";
import { api } from "./api";
import { SubscriptionEditor } from "./SubscriptionEditor";
import type { ClipCandidate, EdgeEffect, EditSettings, ExportItem, HookSuggestion, VideoEffect } from "./types";

export const EDIT_STYLE_LABELS = {
  off: "Orijinal", clean: "Sade", dynamic: "Dinamik", cinematic: "Sinematik",
};
const STYLES: { value: EditSettings["style"]; description: string }[] = [
  { value: "off", description: "Mevcut altyazı ve kadraj" },
  { value: "clean", description: "Kısa altyazılar, sabit görüntü" },
  { value: "dynamic", description: "Canlı vurgu, kısa yakınlaşmalar" },
  { value: "cinematic", description: "Sıcak tonlu yazı, yavaş yakınlaşma" },
];

const TRANSITIONS: { value: EdgeEffect; label: string; description: string }[] = [
  { value: "none", label: "Geçiş yok", description: "Görüntü doğrudan devam eder." },
  { value: "fade_white", label: "Beyaz flaş", description: "Beyaz ışıkla sahne değişimini belirginleştirir." },
  { value: "fade_black", label: "Siyah geçiş", description: "Kısa bir kararma ile sakin bir ara verir." },
  { value: "dissolve", label: "Yumuşak çözünme", description: "Görüntüler birbirine yumuşakça karışır." },
  { value: "smooth_wipe", label: "Akıcı kaydırma", description: "Yeni sahne yumuşak bir hareketle açılır." },
  { value: "circle_reveal", label: "Dairesel açılma", description: "Görüntü merkezden dışarıya açılır." },
  { value: "pixelize", label: "Piksel geçişi", description: "Kısa bir mozaik dokusuyla sahne değişir." },
  { value: "zoom_blur", label: "Zoom + blur", description: "Yakınlaşma ve bulanıklık hareketi vurgular." },
  { value: "slide", label: "Panel kaydırma", description: "Hareketli bir panel görüntüyü açar." },
  { value: "diagonal", label: "Diyagonal geçiş", description: "Çapraz hareketle dinamik bir geçiş oluşturur." },
  { value: "rgb_split", label: "RGB ayrışma", description: "Kısa bir renk ayrışmasıyla vurgu yapar." },
];
const FEATURED_TRANSITIONS: EdgeEffect[] = ["fade_white", "dissolve", "smooth_wipe", "circle_reveal"];
export const VIDEO_EFFECT_LABELS: Record<VideoEffect, string> = {
  none: "Orijinal", cinematic: "Sinematik", vivid: "Canlı", warm: "Sıcak",
  cool: "Soğuk", monochrome: "Siyah beyaz", vintage: "Vintage",
};
const VIDEO_EFFECTS: { value: VideoEffect; label: string; description: string }[] = [
  { value: "none", label: "Orijinal", description: "Kaynak renkleri koru" },
  { value: "cinematic", label: "Sinematik", description: "Dengeli kontrast, film hissi" },
  { value: "vivid", label: "Canlı", description: "Daha güçlü renkler" },
  { value: "warm", label: "Sıcak", description: "Altın ve gün batımı tonları" },
  { value: "cool", label: "Soğuk", description: "Mavi ve serin tonlar" },
  { value: "monochrome", label: "Siyah beyaz", description: "Renklerden arınmış görüntü" },
  { value: "vintage", label: "Vintage", description: "Yumuşak, nostaljik tonlar" },
];

function normalizedSettings(settings: EditSettings): EditSettings {
  return { ...settings,
    hook_transition: settings.hook_transition ?? "fade_white",
    hook_transition_duration: settings.hook_transition_duration ?? 0.5,
    video_effect: settings.video_effect ?? "none",
    effect_intensity: settings.effect_intensity ?? 0.65,
  };
}

function toneFilter(effect: VideoEffect, strength: number): string {
  switch (effect) {
    case "cinematic": return `contrast(${1 + strength * 0.18}) saturate(${1 - strength * 0.12})`;
    case "vivid": return `saturate(${1 + strength * 0.55}) contrast(${1 + strength * 0.1})`;
    case "warm": return `sepia(${strength * 0.35}) saturate(${1 + strength * 0.25})`;
    case "cool": return `sepia(${strength * 0.3}) hue-rotate(${strength * 145}deg)`;
    case "monochrome": return `grayscale(${strength}) contrast(${1 + strength * 0.12})`;
    case "vintage": return `sepia(${strength * 0.6}) contrast(${1 - strength * 0.12})`;
    default: return "none";
  }
}

function TransitionPicker({ id, title, value, duration, disabled, onChange, onDuration }: {
  id: string; title: string; value: EdgeEffect; duration: number; disabled: boolean;
  onChange: (value: EdgeEffect) => void; onDuration: (value: number) => void;
}) {
  return <div className="transition-picker">
    <div className="effect-gallery" aria-label={`${title} örnekleri`}>
      {FEATURED_TRANSITIONS.map((choice) => {
        const option = TRANSITIONS.find((item) => item.value === choice)!;
        return <button type="button" key={choice} title={option.description} disabled={disabled}
          aria-label={`${title}: ${option.label}`} aria-pressed={value === choice}
          className={`effect-choice ${choice} ${value === choice ? "selected" : ""}`}
          onClick={() => onChange(choice)}><i aria-hidden="true" /><span>{option.label}</span></button>;
      })}
    </div>
    <div className="transition-fields">
      <label className="motovlog-field" htmlFor={`${id}-choice`}>{title}
        <select id={`${id}-choice`} value={value} disabled={disabled} aria-describedby={`${id}-help`}
          onChange={(event) => onChange(event.target.value as EdgeEffect)}>
          {TRANSITIONS.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
        </select>
      </label>
      <label className="motovlog-field" htmlFor={`${id}-duration`}>Süre (sn)
        <input id={`${id}-duration`} type="number" min={0.1} max={2} step={0.1} value={duration}
          disabled={disabled || value === "none"} onChange={(event) => onDuration(Number(event.target.value))} />
      </label>
    </div>
    <p className="motovlog-help transition-description" id={`${id}-help`}>
      {TRANSITIONS.find((option) => option.value === value)?.description}
    </p>
  </div>;
}

function timeText(seconds: number): string {
  const rounded = Math.max(0, Math.round(seconds * 100));
  return `${Math.floor(rounded / 6000)}:${((rounded % 6000) / 100).toFixed(2).padStart(5, "0")}`;
}

function timeValue(text: string): number | null {
  const parts = text.trim().replace(",", ".").split(":");
  if (parts.some((part) => !/^\d+(\.\d+)?$/.test(part))) return null;
  const values = parts.map(Number);
  if (values.length > 3 || values.some((value) => !Number.isFinite(value))) return null;
  if (values.length > 1 && (values.slice(1).some((value) => value >= 60)
      || values.slice(0, -1).some((value) => !Number.isInteger(value)))) return null;
  return values.reduce((total, value) => total * 60 + value, 0);
}

export function MotovlogEditor({ clip, onSaved, onPreview, preview, sourceDuration, previewTime, playbackRate }: {
  clip: ClipCandidate;
  onSaved: (clip: ClipCandidate) => void;
  onPreview: (clip: ClipCandidate) => Promise<void>;
  preview?: ExportItem;
  sourceDuration: number | null;
  previewTime: number;
  playbackRate: number;
}) {
  const [draft, setDraft] = useState<EditSettings>(() => normalizedSettings(clip.edit_settings));
  const [suggestions, setSuggestions] = useState<HookSuggestion[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [hookStart, setHookStart] = useState(timeText(clip.edit_settings.hook_range?.source_start ?? clip.start));
  const [hookEnd, setHookEnd] = useState(timeText(clip.edit_settings.hook_range?.source_end ?? Math.min(clip.end, clip.start + 3)));
  const [showHook, setShowHook] = useState(false);
  const savedSettings = JSON.stringify(normalizedSettings(clip.edit_settings));
  const transcript = JSON.stringify(clip.subtitles);
  useEffect(() => {
    const settings = JSON.parse(savedSettings) as EditSettings;
    setDraft(settings);
    setHookStart(timeText(settings.hook_range?.source_start ?? clip.start));
    setHookEnd(timeText(settings.hook_range?.source_end ?? Math.min(clip.end, clip.start + 3)));
    setShowHook(false);
  }, [clip.id, savedSettings]);
  useEffect(() => { setSuggestions(null); setMessage(""); setError(""); }, [clip.id, transcript]);
  const hookEnabled = !!draft.hook_range;
  const startValue = timeValue(hookStart);
  const endValue = timeValue(hookEnd);
  const hookValid = startValue !== null && endValue !== null
    && endValue - startValue >= 0.3 - 0.000001 && endValue - startValue <= 15 + 0.000001
    && (sourceDuration === null || endValue <= sourceDuration);
  const dirty = JSON.stringify(draft) !== savedSettings || (hookEnabled && (
    startValue !== draft.hook_range?.source_start || endValue !== draft.hook_range?.source_end
  ));
  const previewBusy = preview?.status === "queued" || preview?.status === "rendering";
  const enabled = draft.style !== "off";
  const update = (patch: Partial<EditSettings>) => {
    setDraft((current) => ({ ...current, ...patch }));
    setMessage("");
    setError("");
  };
  const suggest = async () => {
    setLoading(true);
    setError("");
    try { setSuggestions(await api.suggestHooks(clip.job_id, clip.id)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Öneriler alınamadı."); }
    finally { setLoading(false); }
  };
  const settingsForSave = (): EditSettings => {
    if (hookEnabled && !hookValid) {
      throw new Error("Hook başlangıç ve bitişini kaynak video içinde, 0.3–15 saniyelik bir aralık olarak gir.");
    }
    const durations = [
      ...(hookEnabled && draft.hook_transition !== "none" ? [draft.hook_transition_duration] : []),
      ...(draft.intro_effect !== "none" ? [draft.intro_duration] : []),
      ...(draft.outro_effect !== "none" ? [draft.outro_duration] : []),
    ];
    if (durations.some((duration) => !Number.isFinite(duration) || duration < 0.1 || duration > 2)) {
      throw new Error("Geçiş sürelerini 0.1–2 saniye arasında gir.");
    }
    const subscription = draft.subscription;
    if (subscription.enabled && (!subscription.channel_name.trim() || !Number.isFinite(subscription.start)
        || subscription.start < 0 || subscription.start > 86400 || !Number.isFinite(subscription.duration)
        || subscription.duration < 1 || subscription.duration > 5)) {
      throw new Error("Kanal adını, geçerli bir başlangıç zamanını ve 1–5 saniyelik süreyi gir.");
    }
    return { ...draft, hook_range: hookEnabled ? { source_start: startValue!, source_end: endValue! } : null };
  };
  const save = async (makePreview = false) => {
    setSaving(true);
    setError("");
    setMessage("");
    try {
      const updated = await api.updateClip(clip.job_id, clip.id, { edit_settings: settingsForSave() });
      onSaved(updated);
      if (makePreview) await onPreview(updated);
      setMessage(makePreview ? "Kurgu kaydedildi. Önizleme hazırlanıyor." : "Kurgu kaydedildi. Yeni dışa aktarmalarda uygulanacak.");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Kurgu kaydedilemedi."); }
    finally { setSaving(false); }
  };
  const sampleWords = (clip.subtitles[0]?.text ?? "Konuşman burada öne çıkar").split(/\s+/)
    .slice(0, draft.style === "dynamic" ? 3 : draft.style === "cinematic" ? 5 : 4);
  return (
    <section className="motovlog-panel" aria-label="Motovlog kurgusu">
      <div className="motovlog-heading">
        <div><span>KONUŞMALI MOTOVLOG</span><h3>Klibine karakter kat</h3></div>
        <span className="motovlog-badge">{dirty ? "Kaydedilmedi" : "Klip ayarı"}</span>
      </div>
      <p className="motovlog-intro">Hook bölümünü başa taşı, sahneler arasına geçiş ekle ve videonun renklerini düzenle.</p>
      <div className="hook-range-panel">
        <label className="motovlog-check">
          <input type="checkbox" checked={hookEnabled} disabled={saving} onChange={(event) => {
            update({ hook_range: event.target.checked ? {
              source_start: startValue ?? clip.start,
              source_end: endValue ?? Math.min(clip.end, clip.start + 3),
            } : null });
            setShowHook(false);
          }} />
          <strong>Videodan hook bölümü ekle</strong>
        </label>
        <p className="motovlog-help">Seçtiğin görüntü ve ses önce oynar, ardından ana klip devam eder.</p>
        {hookEnabled && <>
          <p className="hook-source-label">Kaynak videodaki zaman · saniye veya dk:sn yazabilirsin</p>
          <div className="hook-time-grid">
            <label className="motovlog-field">Hook başlangıcı
              <input value={hookStart} inputMode="decimal" placeholder="2:14.00" disabled={saving}
                onChange={(event) => { setHookStart(event.target.value); setShowHook(false); setError(""); setMessage(""); }} />
            </label>
            <label className="motovlog-field">Hook bitişi
              <input value={hookEnd} inputMode="decimal" placeholder="2:17.00" disabled={saving}
                onChange={(event) => { setHookEnd(event.target.value); setShowHook(false); setError(""); setMessage(""); }} />
            </label>
          </div>
          <div className="hook-range-actions">
            <button type="button" className="tiny-button" disabled={saving}
              onClick={() => { setHookStart(timeText(previewTime)); setShowHook(false); }}>
              Başlangıcı oynatma anından al
            </button>
            <button type="button" className="tiny-button" disabled={saving}
              onClick={() => { setHookEnd(timeText(previewTime)); setShowHook(false); }}>
              Bitişi oynatma anından al
            </button>
            <button type="button" className="tiny-button" disabled={!hookValid || saving}
              onClick={() => setShowHook((value) => !value)}>{showHook ? "Hook oynatıcısını kapat" : "Hook bölümünü dinle"}</button>
          </div>
          <label className="motovlog-check hook-repeat">
            <input type="checkbox" checked={draft.hook_remove_from_body} disabled={saving}
              onChange={(event) => update({ hook_remove_from_body: event.target.checked })} />
            Aynı bölümü ana klipte tekrar oynatma
          </label>
          <p className={`motovlog-help ${hookValid ? "" : "hook-invalid"}`} aria-live="polite">
            {hookValid ? `${timeText(startValue!)} → ${timeText(endValue!)} · ${(endValue! - startValue!).toFixed(2)} sn hook → Ana klip`
              : "Kaynak video içinde, 0.3–15 saniyelik bir aralık gir."}
          </p>
          <div className="hook-transition-panel">
            <div className="effects-section-heading">
              <div><span>SAHNE DEĞİŞİMİ</span><h4>Hook <span aria-hidden="true">→</span> Ana video</h4></div>
              <span className={`transition-status ${draft.hook_transition !== "none" ? "active" : ""}`}>
                {draft.hook_transition !== "none" ? "Geçiş açık" : "Doğrudan kesim"}
              </span>
            </div>
            <p className="motovlog-help">Hook bittiğinde ana videoya geçtiğini belirginleştir. Beyaz flaş güçlü bir ayrım sağlar.</p>
            <TransitionPicker id={`hook-${clip.id}`} title="Hook sonrası geçiş" value={draft.hook_transition}
              duration={draft.hook_transition_duration} disabled={saving}
              onChange={(hook_transition) => update({ hook_transition })}
              onDuration={(hook_transition_duration) => update({ hook_transition_duration })} />
          </div>
          {showHook && hookValid && <video className="hook-source-player" controls playsInline autoPlay
            src={`/api/jobs/${clip.job_id}/source#t=${startValue},${endValue}`}
            onLoadedMetadata={(event) => { event.currentTarget.currentTime = startValue!; }}
            onPlay={(event) => { if (event.currentTarget.currentTime >= endValue!) event.currentTarget.currentTime = startValue!; }}
            onTimeUpdate={(event) => { if (event.currentTarget.currentTime >= endValue!) event.currentTarget.pause(); }} />}
        </>}
      </div>
      <div className="effects-section-heading edge-section-heading">
        <div><span>İLK VE SON KARE</span><h4>Açılış ve kapanış geçişleri</h4></div>
        <span className="effects-engine">FFmpeg</span>
      </div>
      <div className="edge-effects" aria-label="Açılış ve kapanış geçişleri">
        {(["intro", "outro"] as const).map((edge) => <div className="edge-effect" key={edge}>
          <h5>{edge === "intro" ? "01 / Açılış" : "02 / Kapanış"}</h5>
          <TransitionPicker id={`${edge}-${clip.id}`} title={edge === "intro" ? "Açılış geçişi" : "Kapanış geçişi"}
            value={draft[`${edge}_effect`]} duration={draft[`${edge}_duration`]} disabled={saving}
            onChange={(value) => update({ [`${edge}_effect`]: value })}
            onDuration={(value) => update({ [`${edge}_duration`]: value })} />
        </div>)}
      </div>
      <label className="motovlog-check edge-audio">
        <input type="checkbox" checked={draft.fade_audio}
          disabled={saving || (draft.intro_effect === "none" && draft.outro_effect === "none"
            && (!hookEnabled || draft.hook_transition === "none"))}
          onChange={(event) => update({ fade_audio: event.target.checked })} />
        Seçilen geçişlerde sesi de yumuşat
      </label>
      <p className="motovlog-help">Süreler son çıktıdaki saniyelerdir. Kısa kliplerde geçiş süresi otomatik kısalır.</p>
      <div className="video-effects-panel" aria-label="Video efektleri">
        <div className="effects-section-heading">
          <div><span>RENK VE ATMOSFER</span><h4>Video efektleri</h4></div>
          <span className="transition-status">{draft.video_effect === "none" ? "Orijinal renkler" : `%${Math.round(draft.effect_intensity * 100)} yoğunluk`}</span>
        </div>
        <p className="motovlog-help">Bir görünüm seç; efekt tüm videoya uygulanır. Yazı ve hareket tarzını aşağıdan ayrıca ayarlayabilirsin.</p>
        <fieldset className="video-effect-gallery" disabled={saving}>
          <legend className="sr-only">Video görünümü seç</legend>
          {VIDEO_EFFECTS.map(({ value, label, description }) => <label key={value}
            className={`video-effect-choice ${draft.video_effect === value ? "selected" : ""}`}>
            <input type="radio" name={`video-effect-${clip.id}`} value={value} checked={draft.video_effect === value}
              onChange={() => update({ video_effect: value })} />
            <span className="tone-swatch" aria-hidden="true" style={{ filter: toneFilter(value, 0.85) }}><i /></span>
            <strong>{label}</strong><small>{description}</small>
          </label>)}
        </fieldset>
        <div className="video-effect-detail">
          <div className="effect-intensity-control">
            <label htmlFor={`effect-intensity-${clip.id}`}>Efekt yoğunluğu <output>{Math.round(draft.effect_intensity * 100)}%</output></label>
            <input id={`effect-intensity-${clip.id}`} type="range" min={0} max={1} step={0.05}
              value={draft.effect_intensity} disabled={saving || draft.video_effect === "none"}
              aria-valuetext={`Yüzde ${Math.round(draft.effect_intensity * 100)}`}
              onChange={(event) => update({ effect_intensity: Number(event.target.value) })} />
            <div className="effect-intensity-scale"><span>Hafif</span><span>Güçlü</span></div>
            <p className="motovlog-help">Gerçek sonucu görmek için “Kaydet ve önizle”yi kullan.</p>
          </div>
          <div className="tone-preview" aria-label="Seçilen rengin temsili örneği">
            <div className="tone-preview-scene" style={{ filter: toneFilter(draft.video_effect, draft.effect_intensity) }} aria-hidden="true"><i /></div>
            <span>{VIDEO_EFFECTS.find((effect) => effect.value === draft.video_effect)?.label} · Temsili renk örneği</span>
          </div>
        </div>
      </div>
      <div className="effects-section-heading">
        <div><span>ALTYAZI VE HAREKET</span><h4>Yazı ve hareket tarzı</h4></div>
      </div>
      <fieldset className="motovlog-styles" disabled={saving}>
        <legend className="sr-only">Yazı ve hareket tarzı</legend>
        {STYLES.map(({ value, description }) => (
          <label key={value} className={`motovlog-style ${draft.style === value ? "selected" : ""}`}>
            <input type="radio" name={`edit-style-${clip.id}`} value={value}
              checked={draft.style === value} onChange={() => update({ style: value })} />
            <strong>{EDIT_STYLE_LABELS[value]}</strong><small>{description}</small>
          </label>
        ))}
      </fieldset>
      {enabled && (
        <div className="motovlog-body">
          <div className="motovlog-settings">
            <label className="motovlog-field">
              <span>Açılış yazısı <small>{draft.hook_text.length}/90</small></span>
              <textarea maxLength={90} rows={3} value={draft.hook_text} disabled={saving}
                placeholder="İlk saniyelerde merak uyandıran kısa bir cümle…"
                onChange={(event) => update({ hook_text: event.target.value })} />
            </label>
            <p className="motovlog-help">Videonun başında yazı olarak görünür. Boş bırakırsan eklenmez.</p>
            <button type="button" className="tiny-button" onClick={suggest} disabled={loading || saving}>
              {loading ? "Konuşma inceleniyor…" : "Konuşmadan açılış öner"}
            </button>
            {suggestions !== null && (
              <div className="hook-suggestions" aria-live="polite">
                {suggestions.length ? suggestions.map((item) => (
                  <button key={item.text} type="button" disabled={saving}
                    onClick={() => update({ hook_text: item.text })}>
                    <small>{item.reason}</small><span>“{item.text}”</span>
                  </button>
                )) : <p>Kısa bir açılış alıntısı bulunamadı. Kendi yazını girebilirsin.</p>}
                {!!suggestions.length && <p>Kaydedilmiş konuşmadan alıntılar. İçeriğe uygun olanı seçip düzenle.</p>}
              </div>
            )}
            <div className="motovlog-controls">
              <label className="motovlog-field">Yazının süresi
                <select value={draft.hook_duration} disabled={saving}
                  onChange={(event) => update({ hook_duration: Number(event.target.value) })}>
                  {[1, 1.5, 2, 2.8, 3.5, 4, 5].map((seconds) => (
                    <option key={seconds} value={seconds}>{seconds} saniye</option>
                  ))}
                </select>
              </label>
              {draft.style !== "clean" && <label className="motovlog-check">
                <input type="checkbox" checked={draft.punch_ins} disabled={saving}
                  onChange={(event) => update({ punch_ins: event.target.checked })} />
                Hafif yakınlaşmalar
              </label>}
            </div>
          </div>
          <div className={`motovlog-sample ${draft.style}`} aria-label="Yazı stili örneği">
            <span className="sample-label">YAZI STİLİ ÖRNEĞİ</span>
            {draft.hook_text && <strong className="sample-hook">{draft.hook_text}</strong>}
            <div className="sample-caption">{sampleWords.map((word, index) => (
              <span key={index} className={index === 1 ? "accent" : ""}>{word} </span>
            ))}</div>
            <small>Yerleşim temsili. Kurgu, dışa aktarılan videoya işlenir.</small>
          </div>
        </div>
      )}
      <SubscriptionEditor clip={clip} editSettings={{ ...draft,
        hook_range: hookEnabled && hookValid ? { source_start: startValue!, source_end: endValue! } : draft.hook_range,
      }} playbackRate={playbackRate} disabled={saving} hookValid={!hookEnabled || hookValid}
        onChange={(subscription) => update({ subscription })} />
      <div className="motovlog-footer">
        <button type="button" className="secondary-button" disabled={saving || !dirty} onClick={() => void save()}>
          {saving ? "Kaydediliyor…" : "Kurgu ayarlarını kaydet"}
        </button>
        <button type="button" className="secondary-button" disabled={saving || previewBusy} onClick={() => void save(true)}>
          {previewBusy ? `Önizleme %${Math.round(preview.progress * 100)}` : "Kaydet ve önizle"}
        </button>
        <p role="status" className={error ? "error" : "success"}>{error || message}</p>
      </div>
      {preview && <div className="motovlog-rendered-preview" aria-live="polite">
        <strong>Son oluşturulan kurgu önizlemesi</strong>
        {previewBusy && <p>Video hazırlanıyor… %{Math.round(preview.progress * 100)}</p>}
        {preview.status === "failed" && <p className="error">{preview.error}</p>}
        {preview.status === "completed" && <video key={preview.id} controls playsInline preload="metadata"
          src={`/api/exports/${preview.id}/download`} />}
        <small>Önizleme 540 × 960; MP4 dışa aktarımı 1080 × 1920. Ayar değiştirince yeni önizleme oluştur.</small>
      </div>}
    </section>
  );
}
