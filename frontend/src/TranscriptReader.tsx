import { Fragment, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { buildReadingScript, readingWordDuration } from "./transcriptReading";
import "./transcript-reader.css";

type ReaderSource = { text: string; url?: never } | { url: string; text?: never };
type ReaderProps = ReaderSource & { language?: string | null };
type Playback = "ready" | "playing" | "paused" | "complete";

const playbackLabels: Record<Playback, string> = {
  ready: "Başlat",
  playing: "Duraklat",
  paused: "Devam et",
  complete: "Tekrar başlat",
};
const readingSpeeds = Array.from({ length: 13 }, (_, index) => 80 + 10 * index);

function ReadingFlow({ text, language }: { text: string; language: string }) {
  const { words, passages, passageByWord } = useMemo(() => buildReadingScript(text), [text]);
  const [position, setPosition] = useState<{ index: number; playback: Playback }>({ index: 0, playback: "ready" });
  const [speed, setSpeed] = useState(130);
  const stage = useRef<HTMLParagraphElement>(null);
  const activeWord = useRef<HTMLSpanElement>(null);
  const positionId = useId();
  const { index, playback } = position;
  const currentWord = words[index];

  useLayoutEffect(() => {
    const container = stage.current;
    const word = activeWord.current;
    if (!container || !word || container.scrollHeight <= container.clientHeight) return;
    const viewport = container.getBoundingClientRect();
    const bounds = word.getBoundingClientRect();
    if (bounds.top < viewport.top) container.scrollTop += bounds.top - viewport.top;
    else if (bounds.bottom > viewport.bottom) container.scrollTop += bounds.bottom - viewport.bottom;
  }, [index]);

  useEffect(() => {
    if (playback !== "playing" || !currentWord) return;
    const timeout = window.setTimeout(() => {
      setPosition((current) => {
        if (current !== position) return current;
        if (index === words.length - 1) return { index, playback: "complete" };
        return { index: index + 1, playback: "playing" };
      });
    }, readingWordDuration(currentWord, speed));
    return () => window.clearTimeout(timeout);
  }, [currentWord, index, playback, position, speed, words.length]);

  if (!currentWord) return <p role="status">Okunabilecek kelime bulunamadı.</p>;

  const passage = passages[passageByWord[index]];
  const turkish = /^tr(?:-|$)/i.test(language);
  const seek = (nextIndex: number) => {
    setPosition((current) => ({
      index: nextIndex,
      playback: current.playback === "complete" ? "paused" : current.playback,
    }));
  };
  const togglePlayback = () => {
    setPosition((current) => ({
      index: current.playback === "complete" ? 0 : current.index,
      playback: current.playback === "playing" ? "paused" : "playing",
    }));
  };

  return <>
    <p ref={stage} className="transcript-reader-stage" lang={language}>
      {words.slice(passage.start, passage.end).map((word, offset) => {
        const wordIndex = passage.start + offset;
        const active = index === wordIndex;
        return <Fragment key={wordIndex}>
          {offset > 0 && " "}
          <span className="transcript-reader-word" ref={active ? activeWord : undefined} aria-current={active || undefined}>
            <span className="transcript-reader-word-original">{word.text}</span>
            <span className="transcript-reader-word-active">
              {turkish ? word.text.toLocaleUpperCase("tr-TR") : word.text.toUpperCase()}
            </span>
          </span>
        </Fragment>;
      })}
    </p>
    <div className="transcript-reader-position">
      <label htmlFor={positionId}>Kelime {index + 1} / {words.length}</label>
      <input id={positionId} type="range" min={1} max={words.length} value={index + 1}
        disabled={words.length === 1} onChange={(event) => seek(Number(event.target.value) - 1)} />
    </div>
    <div className="transcript-reader-controls">
      <button type="button" className="primary-button" onClick={togglePlayback}>{playbackLabels[playback]}</button>
      <button type="button" className="secondary-button" disabled={index === 0}
        onClick={() => seek(index - 1)}>Önceki kelime</button>
      <button type="button" className="secondary-button" disabled={index === words.length - 1}
        onClick={() => seek(index + 1)}>Sonraki kelime</button>
      <button type="button" className="secondary-button" onClick={() => setPosition({ index: 0, playback: "ready" })}>
        Başa dön
      </button>
      <label className="transcript-reader-speed">
        Okuma hızı
        <select value={speed} onChange={(event) => setSpeed(Number(event.target.value))}>
          {readingSpeeds.map((rate) => <option key={rate} value={rate}>{rate} kelime/dk</option>)}
        </select>
      </label>
    </div>
  </>;
}

function TranscriptReaderDialog({ text, url, language, onClose }: ReaderProps & { onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const headingId = useId();
  const [loadedText, setLoadedText] = useState<string | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (dialog.current && !dialog.current.open) dialog.current.showModal();
  }, []);

  useEffect(() => {
    if (!url) return;
    const controller = new AbortController();
    const loadTranscript = async () => {
      try {
        const response = await fetch(url, { signal: controller.signal });
        if (!response.ok) throw new Error(String(response.status));
        const body = await response.text();
        if (!controller.signal.aborted) setLoadedText(body);
      } catch {
        if (!controller.signal.aborted) setLoadError(true);
      }
    };
    void loadTranscript();
    return () => controller.abort();
  }, [url, attempt]);

  const script = text ?? loadedText;
  return <dialog ref={dialog} className="transcript-reader" aria-labelledby={headingId} onClose={onClose}>
    <header className="transcript-reader-header">
      <h2 id={headingId}>Canlı okuma</h2>
      <button type="button" onClick={() => dialog.current?.close()}>Kapat</button>
    </header>
    <div className="transcript-reader-body">
      {script !== null ? <ReadingFlow key={text} text={script} language={language && language !== "unknown" ? language : "tr"} />
        : loadError ? <>
          <p role="alert">Konuşma metni açılamadı.</p>
          <button type="button" className="secondary-button" onClick={() => {
            setLoadError(false);
            setAttempt((current) => current + 1);
          }}>Yeniden dene</button>
        </> : <p role="status">Konuşma metni yükleniyor…</p>}
    </div>
  </dialog>;
}

export function TranscriptReader(props: ReaderProps) {
  const [open, setOpen] = useState(false);
  return <>
    <button type="button" className="secondary-button" aria-haspopup="dialog" onClick={() => setOpen(true)}>
      Canlı oku
    </button>
    {open && <TranscriptReaderDialog {...props} onClose={() => setOpen(false)} />}
  </>;
}
