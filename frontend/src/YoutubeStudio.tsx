import { useEffect, useState } from "react";
import { api } from "./api";
import type { ClipCandidate, YoutubeBrief, YoutubePackage } from "./types";

type Draft = Pick<YoutubePackage, "brief" | "title" | "description" | "thumbnail_text">;
const pick = (value: YoutubePackage): Draft => ({ brief: value.brief, title: value.title,
  description: value.description, thumbnail_text: value.thumbnail_text });

export function YoutubeStudio({ clip, sourceDuration, previewTime, onSaved, onRefresh, onExport, busy }: {
  clip: ClipCandidate;
  sourceDuration: number | null;
  previewTime: number;
  onSaved: (clip: ClipCandidate) => void;
  onRefresh: () => Promise<void>;
  onExport: (clip: ClipCandidate) => Promise<void>;
  busy: boolean;
}) {
  const [draft, setDraft] = useState<Draft>(() => pick(clip.youtube));
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const saved = JSON.stringify(pick(clip.youtube));
  useEffect(() => { setDraft(JSON.parse(saved) as Draft); setMessage(""); setError(""); }, [clip.id, saved]);
  const generating = ["queued", "generating"].includes(clip.youtube.status);
  useEffect(() => {
    if (!generating) return;
    const timer = window.setInterval(() => { void onRefresh().catch(() => setError("Bağlantı kesildi. Sayfayı yenileyebilirsin.")); }, 2000);
    return () => window.clearInterval(timer);
  }, [generating, onRefresh]);
  const dirty = JSON.stringify(draft) !== saved;
  const updateBrief = (patch: Partial<YoutubeBrief>) => { setDraft((value) => ({ ...value, brief: { ...value.brief, ...patch } })); setMessage(""); };
  const run = async (action: "save" | "generate" | "export") => {
    setSaving(true); setError(""); setMessage("");
    try {
      if (action === "export" && (!draft.title.trim() || !draft.description.trim())) {
        throw new Error("Önce başlık ve açıklama seç veya kendi metnini yaz.");
      }
      const updated = await api.saveYoutube(clip, draft);
      onSaved(updated);
      if (action === "generate") { onSaved(await api.generateYoutube(updated)); await onRefresh(); }
      if (action === "export") await onExport(updated);
      setMessage(action === "generate" ? "Codex seçenekleri hazırlıyor…" : action === "export" ? "Video, kapak ve yayın metinleri için dışa aktarma başlatıldı." : "YouTube seçimin ve kapak tasarımı kaydedildi.");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "İşlem tamamlanamadı."); }
    finally { setSaving(false); }
  };
  const disabled = saving || generating || busy;
  return <section className="youtube-studio" aria-label="YouTube yayın stüdyosu">
    <div className="youtube-heading"><div><span>YOUTUBE · KLİP {clip.rank}</span><h2>Yayına hazırla</h2></div>
      <b>{generating ? "CODEX HAZIRLIYOR" : dirty ? "KAYDEDİLMEDİ" : "KESİTE ÖZEL"}</b></div>
    <p className="youtube-intro">İçeriğin yönünü belirle. Başlık ve açıklama seçeneklerini seç; kapak gerçek videondan tasarlansın.</p>
    <div className="youtube-layout"><div className="youtube-form">
      <div className="youtube-questions">
        <label>Kime hitap ediyor?<input maxLength={250} value={draft.brief.audience} disabled={disabled}
          onChange={(event) => updateBrief({ audience: event.target.value })} /></label>
        <label>Hangi fikri öne çıkaralım?<textarea rows={2} maxLength={600} value={draft.brief.focus} disabled={disabled}
          placeholder="Örn. kısa boylu sürücünün rampada yaşadığı zorluk"
          onChange={(event) => updateBrief({ focus: event.target.value })} /></label>
        <div className="youtube-field-pair"><label>Anlatım tonu<select value={draft.brief.tone} disabled={disabled}
          onChange={(event) => updateBrief({ tone: event.target.value as YoutubeBrief["tone"] })}>
          <option value="natural">Samimi ve doğal</option><option value="curious">Merak uyandıran</option><option value="informative">Bilgilendirici</option></select></label>
          <label>Kanal adı<input maxLength={70} value={draft.brief.channel_name} disabled={disabled}
            placeholder="İsteğe bağlı" onChange={(event) => updateBrief({ channel_name: event.target.value })} /></label></div>
      </div>
      <button type="button" className="primary-button" disabled={disabled} onClick={() => void run("generate")}>
        {generating ? "Codex seçenekleri yazıyor…" : "Codex ile başlık, açıklama ve kapak yazısı üret"}</button>
      <label className="youtube-main-field">Başlık <small>{draft.title.length}/100</small>
        <input maxLength={100} value={draft.title} disabled={disabled} placeholder="Bir öneri seç veya kendi başlığını yaz"
          onChange={(event) => setDraft({ ...draft, title: event.target.value })} /></label>
      <div className="youtube-choices">{clip.youtube.options.titles.map((title) => <button key={title} type="button"
        disabled={disabled} className={draft.title === title ? "selected" : ""} onClick={() => setDraft({ ...draft, title })}>{title}</button>)}</div>
      <label className="youtube-main-field">Açıklama <small>{draft.description.length}/5000</small>
        <textarea rows={5} maxLength={5000} value={draft.description} disabled={disabled} placeholder="Önerilerden seçip düzenleyebilirsin"
          onChange={(event) => setDraft({ ...draft, description: event.target.value })} /></label>
      <div className="youtube-descriptions">{clip.youtube.options.descriptions.map((description, index) => <details key={index}>
        <summary>Açıklama seçeneği {index + 1}{draft.description === description ? " · Seçili" : ""}</summary><p>{description}</p>
        <button type="button" className="tiny-button" disabled={disabled} onClick={() => setDraft({ ...draft, description })}>Bu açıklamayı seç</button>
      </details>)}</div>
    </div><div className="youtube-cover">
      <span>KAPAK TASARIMI</span>
      <label>Kapakta ne yazsın?<input maxLength={90} value={draft.thumbnail_text} disabled={disabled}
        placeholder="Kısa ve güçlü bir ifade" onChange={(event) => setDraft({ ...draft, thumbnail_text: event.target.value })} /></label>
      <div className="youtube-choices">{clip.youtube.options.thumbnail_texts.map((text) => <button key={text} type="button"
        disabled={disabled} className={draft.thumbnail_text === text ? "selected" : ""} onClick={() => setDraft({ ...draft, thumbnail_text: text })}>{text}</button>)}</div>
      <div className="youtube-field-pair"><label>Tasarım<select value={draft.brief.thumbnail_style} disabled={disabled}
        onChange={(event) => updateBrief({ thumbnail_style: event.target.value as YoutubeBrief["thumbnail_style"] })}>
        <option value="bold">Büyük yazı / koyu degrade</option><option value="editorial">Açık editoryal kart</option><option value="neon">Diyagonal neon</option></select></label>
        <label>Vurgu rengi<select value={draft.brief.accent} disabled={disabled}
          onChange={(event) => updateBrief({ accent: event.target.value as YoutubeBrief["accent"] })}>
          <option value="lime">Limon yeşili</option><option value="orange">Turuncu</option><option value="cyan">Buz mavisi</option></select></label></div>
      <label>Kapak boyutu<select value={draft.brief.aspect} disabled={disabled}
        onChange={(event) => updateBrief({ aspect: event.target.value as YoutubeBrief["aspect"] })}>
        <option value="portrait">Dikey · 1080 × 1920</option><option value="landscape">Yatay · 1280 × 720</option></select></label>
      <label>Kaynak kare zamanı (saniye)<input type="number" min={0} max={sourceDuration ?? undefined} step={.01}
        value={draft.brief.frame_time ?? ""} disabled={disabled} placeholder={`${(clip.start + .6).toFixed(2)} otomatik`}
        onChange={(event) => updateBrief({ frame_time: event.target.value === "" ? null : Number(event.target.value) })} /></label>
      <button type="button" className="tiny-button" disabled={disabled} onClick={() => updateBrief({ frame_time: Math.round(previewTime * 100) / 100 })}>Kareyi oynatma anından al</button>
      {clip.youtube.revision && <figure className={`youtube-thumbnail ${clip.youtube.brief.aspect}`}>
        <img src={`/api/jobs/${clip.job_id}/clips/${clip.id}/youtube/thumbnail?v=${clip.youtube.revision}`} alt="Kaydedilmiş YouTube kapak tasarımı" />
        <figcaption>{dirty ? "Önceki kayıtlı tasarım. Değişiklikleri kaydederek yenile." : "Kaydedilmiş kapak tasarımı"}</figcaption>
      </figure>}
      <small>Gerçek video karesi kullanılır. Tasarımı görmek için kaydet; yeni sorularla seçenekleri yeniden üretebilirsin.</small>
    </div></div>
    <div className="youtube-footer"><button type="button" className="secondary-button" disabled={disabled} onClick={() => void run("save")}>Seçimi kaydet ve kapağı hazırla</button>
      <button type="button" className="primary-button" disabled={disabled} onClick={() => void run("export")}>Bu kesiti YouTube paketi olarak dışa aktar</button>
      <p role="status" className={error || clip.youtube.error ? "error" : "success"}>{error || clip.youtube.error || message}</p></div>
    <p className="youtube-package-note">Çıktı klasörü: MP4 + thumbnail.jpg + title.txt + description.txt + youtube.json. Hepsini ZIP olarak da indirebilirsin.</p>
  </section>;
}
