import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import type { AnalysisStatus, ClipCandidate, TopicAnalysis, TopicSection } from "./types";

function topicTime(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

export function TopicAnalysisPanel({ analysis, clips = [], onActivate, onCreate, busy = false }: {
  analysis?: TopicAnalysis | null;
  clips?: ClipCandidate[];
  onActivate?: (clip: ClipCandidate) => void;
  onCreate?: (topic: TopicSection) => Promise<void>;
  busy?: boolean;
}) {
  const [status, setStatus] = useState<AnalysisStatus | null>(null);
  const [checking, setChecking] = useState(true);
  const [statusError, setStatusError] = useState("");
  const [creating, setCreating] = useState<string | null>(null);
  const [createError, setCreateError] = useState("");
  const create = async (topic: TopicSection) => {
    setCreating(topic.id); setCreateError("");
    try { await onCreate?.(topic); }
    catch (error) { setCreateError(error instanceof Error ? error.message : "Kesit oluşturulamadı."); }
    finally { setCreating(null); }
  };
  const checkStatus = useCallback(async () => {
    setChecking(true);
    setStatusError("");
    try { setStatus(await api.analysisStatus()); }
    catch (reason) {
      setStatus(null);
      setStatusError(reason instanceof Error ? reason.message : "Codex bağlantı bilgisi alınamadı.");
    } finally { setChecking(false); }
  }, []);
  useEffect(() => { void checkStatus(); }, [checkStatus, analysis?.created_at]);
  const ready = !!status?.available && !!status.authenticated;
  const hasTopics = !!analysis?.topics.length;
  return (
    <section className="topic-analysis-panel" aria-label="Codex konu analizi">
      <div className="topic-analysis-heading">
        <div>
          <span>KONU BÜTÜNLÜĞÜYLE KESİTLER</span>
          <h2>{hasTopics ? "Konuşmanın konu haritası" : "Konuşmayı Codex okusun"}</h2>
        </div>
        <div className={`codex-status ${ready ? "ready" : "attention"}`} role="status">
          <i />{checking ? "Codex kontrol ediliyor" : ready ? "Codex hazır" : "Codex bağlantısını kontrol et"}
        </div>
      </div>
      <p className="topic-analysis-summary">{analysis?.summary
        || "Konuşmanın tamamı okunur, konular ayrılır; başlangıcı, açıklaması ve sonucu bir arada olan kesitler önerilir."}</p>
      {(status?.message || statusError) && !checking && (
        <div className={`codex-connection-message ${ready ? "" : "attention"}`}>
          <span>{statusError || status?.message}</span>
          <button type="button" onClick={() => void checkStatus()}>Durumu yenile</button>
        </div>
      )}
      {!ready && !checking && !statusError && !status?.message && (
        <button type="button" className="tiny-button" onClick={() => void checkStatus()}>Codex bağlantısını kontrol et</button>
      )}
      {hasTopics && analysis && (
        <details className="topic-map" open>
          <summary>{analysis.topics.length} konu · Konuşmanın tamamı</summary>
          <div className="topic-map-grid">
            {analysis.topics.map((topic, index) => {
              const clip = clips.find((item) => item.topic_id === topic.id && analysis.clip_ids.includes(item.id))
                ?? clips.find((item) => item.topic_id === topic.id);
              return (
                <article className="topic-map-card" key={topic.id}>
                  <div className="topic-map-time"><b>{String(index + 1).padStart(2, "0")}</b>
                    <span>{topicTime(topic.start)} → {topicTime(topic.end)}</span></div>
                  <h3>{topic.title}</h3>
                  <p>{topic.summary}</p>
                  {clip && onActivate ? (
                    <button type="button" onClick={() => onActivate(clip)}>{clip.selection_method === "manual" ? "Oluşturduğun kesiti aç" : "Önerilen kesiti aç"}</button>
                  ) : <small>Bu başlık için yeni kesit önerilmedi.</small>}
                  {onCreate && <button type="button" disabled={busy || creating !== null}
                    onClick={() => void create(topic)}>{creating === topic.id ? "Oluşturuluyor…" : "Bu konudan kesit oluştur"}</button>}
                </article>
              );
            })}
          </div>
        </details>
      )}
      {createError && <p className="error-banner" role="alert">{createError}</p>}
      <p className="analysis-preservation-note">Yeni analiz mevcut kesitlerine eklenir. Manuel kesitlerin, düzenlemelerin ve çıktılarının kaydı korunur.</p>
      <p className="analysis-privacy-note">Video ve dışa aktarma yerelde kalır. Konuşma metni, konu analizi için oturum açtığın Codex üzerinden OpenAI’ya gönderilir.</p>
    </section>
  );
}
