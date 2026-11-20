import { useState } from "react";
import { api } from "./api";
import type { JobDetail } from "./types";

export function TranscriptCorrection({ job, onRefresh }: { job: JobDetail; onRefresh: () => Promise<void> }) {
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");
  const [reverting, setReverting] = useState<number | null>(null);
  const busy = ["queued", "importing", "analyzing", "exporting"].includes(job.status);
  const correct = async () => {
    setStarting(true); setError("");
    try { await api.correctTranscript(job.id); await onRefresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Düzeltme başlatılamadı."); }
    finally { setStarting(false); }
  };
  const revert = async (wordId: number) => {
    setReverting(wordId); setError("");
    try { await api.revertCorrection(job.id, wordId); await onRefresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Geri alınamadı."); }
    finally { setReverting(null); }
  };
  return <section className="correction-panel">
    <div><span>ALTYAZI KONTROLÜ</span><h3>Konuşma aynı, yazım daha doğru</h3>
      <p>Codex bariz duyum hatalarını bağlama göre düzeltir; kelime zamanları korunur. Emin olmadığı sözcükleri değiştirmemesi istenir.</p></div>
    <button type="button" className="secondary-button" onClick={() => void correct()} disabled={starting || busy}>
      {starting ? "Başlatılıyor…" : "Altyazıyı Codex ile düzelt"}</button>
    {job.correction && <details><summary>{job.correction.corrections.length} kelime düzeltildi · Değişiklikleri gör</summary>
      <div className="correction-list">{job.correction.corrections.map((item) => <div key={item.word_id}>
        <span>{Math.floor(item.start / 60)}:{String(Math.floor(item.start % 60)).padStart(2, "0")}</span>
        <del>{item.original}</del><strong>{item.replacement}</strong><small>{item.reason}</small>
        <button type="button" className="tiny-button" disabled={busy || reverting !== null}
          onClick={() => void revert(item.word_id)}>{reverting === item.word_id ? "Geri alınıyor…" : "Bu düzeltmeyi geri al"}</button>
      </div>)}</div>
      {!job.correction.corrections.length && <p>Bariz bir hata bulunmadı.</p>}
    </details>}
    {error && <p className="error-banner" role="alert">{error}</p>}
  </section>;
}
