import { TranscriptReader } from "./TranscriptReader";

export function TranscriptActions({ jobId, language }: { jobId: string; language: string | null }) {
  const url = `/api/jobs/${jobId}/transcript/download`;
  return (
    <div className="transcript-actions">
      <TranscriptReader key={jobId} url={url} language={language} />
      <a className="transcript-download" href={url} download>Konuşma metnini indir (.txt)</a>
    </div>
  );
}
