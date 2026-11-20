import { useId, useRef, useState, type ChangeEvent } from "react";
import { LocalVideoPaths } from "./LocalVideoPaths";
import type { ProjectDetail } from "./types";
import "./project-videos.css";

export function ProjectVideos({ project, busy, addingPaths, switching, progress, onSelect, onFiles, onPaths }: {
  project: ProjectDetail;
  busy: boolean;
  addingPaths: boolean;
  switching: boolean;
  progress: number;
  onSelect: (videoId: string) => void;
  onFiles: (files: File[]) => void;
  onPaths: (paths: string[]) => Promise<boolean>;
}) {
  const picker = useRef<HTMLInputElement>(null);
  const [showAdd, setShowAdd] = useState(false);
  const addPanelId = useId();
  const accept = (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.currentTarget.files ?? []);
    event.currentTarget.value = "";
    if (files.length) onFiles(files);
  };
  return <section className="project-videos" aria-label="Proje videoları">
    <div className="project-videos-title"><span>PROJE · {project.video_count} VİDEO</span><strong>{project.name}</strong></div>
    <div className="project-videos-controls">
      <label><span>İşlem yapılacak video</span><select value={project.active_video_id ?? ""}
        disabled={busy || switching} onChange={(event) => onSelect(event.target.value)}>
        {!project.active_video_id && <option value="" disabled>Video seç</option>}
        {project.videos.map((video) => <option key={video.id} value={video.id}>
          {video.kind === "merged" ? "Birleştirilmiş · " : ""}{video.filename}
        </option>)}
      </select></label>
      <button type="button" disabled={busy || switching} aria-expanded={showAdd}
        aria-controls={showAdd ? addPanelId : undefined} onClick={() => setShowAdd((open) => !open)}>
        <span aria-hidden="true">{showAdd ? "−" : "+"}</span>{showAdd ? "Video eklemeyi kapat" : "Projeye video ekle"}
      </button>
    </div>
    <p role="status">{switching ? "Seçilen video açılıyor…" : busy
      ? addingPaths ? "Videolar kontrol ediliyor…"
        : `Yükleme: %${Math.round(progress * 100)}${progress >= 1 ? " · Videolar doğrulanıyor…" : ""}`
      : "LongToShorts ve Konuşma Temizleme, yukarıda seçilen video üzerinde çalışır. Her videonun düzenlemeleri ayrı saklanır."}</p>
    {busy && !addingPaths && <progress value={progress} max={1} aria-label="Videoların yüklenme ilerlemesi" />}
    {showAdd && <div className="project-videos-add" id={addPanelId}>
      <LocalVideoPaths disabled={busy || switching} pending={addingPaths} onPaths={async (paths) => {
        const saved = await onPaths(paths);
        if (saved) setShowAdd(false);
        return saved;
      }} />
      <input ref={picker} className="project-videos-input" type="file" multiple
        accept=".mp4,.mov,.mkv,.m4v,.avi,.webm" aria-label="Kopyalanacak video dosyaları"
        onChange={accept} disabled={busy || switching} />
      <button className="project-videos-copy" type="button" disabled={busy || switching}
        onClick={() => picker.current?.click()}>Kopyalayarak yükle</button>
    </div>}
  </section>;
}
