import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { api } from "./api";
import "./local-video-paths.css";

function splitPaths(value: string): string[] {
  return value.split(/\r?\n/).map((line) => {
    const path = line.trim();
    return path.startsWith('"') && path.endsWith('"') && path.length > 1
      ? path.slice(1, -1)
      : path;
  }).filter(Boolean);
}

export function LocalVideoPaths({ disabled, pending, onPaths }: {
  disabled: boolean;
  pending: boolean;
  onPaths: (paths: string[]) => Promise<boolean>;
}) {
  const [value, setValue] = useState("");
  const [picking, setPicking] = useState(false);
  const [pickerError, setPickerError] = useState("");
  const mounted = useRef(false);
  const submitting = useRef(false);
  const currentSubmit = useRef(onPaths);
  currentSubmit.current = onPaths;
  const inputId = useId();
  const paths = splitPaths(value);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  const save = async (selectedPaths: string[]) => {
    if (await currentSubmit.current(selectedPaths) && mounted.current) setValue("");
  };
  const pick = async () => {
    if (disabled || submitting.current) return;
    submitting.current = true;
    setPicking(true);
    setPickerError("");
    try {
      const { paths: selectedPaths } = await api.pickLocalVideos();
      if (mounted.current && selectedPaths.length) {
        setPicking(false);
        setValue(selectedPaths.join("\n"));
        await save(selectedPaths);
      }
    } catch (error) {
      if (mounted.current) setPickerError(error instanceof Error ? error.message : "Dosya seçici açılamadı.");
    } finally {
      submitting.current = false;
      if (mounted.current) setPicking(false);
    }
  };
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (disabled || submitting.current || !paths.length) return;
    submitting.current = true;
    setPickerError("");
    try {
      await save(paths);
    } finally {
      submitting.current = false;
    }
  };

  return <form className="local-video-paths" aria-busy={pending || picking} onSubmit={(event) => void submit(event)}>
    <div className="local-video-paths-header">
      <button className="primary-button" type="button" disabled={disabled || picking} onClick={() => void pick()}>
        {picking ? "Dosya seçici açık…" : pending ? "Videolar kontrol ediliyor…" : "Bilgisayardan seç"}
      </button>
      <p id={`${inputId}-source`}>Videolar diskindeki konumdan okunur. Projeyi kullanırken dosyaları aynı konumda tut.</p>
    </div>
    {pickerError && <p role="alert" className="local-video-paths-error">{pickerError} Dosya yollarını aşağıya yapıştırarak da ekleyebilirsin.</p>}
    <label htmlFor={inputId}>Video dosya yolları</label>
    <p id={`${inputId}-hint`}>Dosya Gezgini’nde videoları seçip <kbd>Ctrl+Shift+C</kbd> ile yollarını kopyala. Her satıra bir dosya yolu yapıştır.</p>
    <textarea id={inputId} aria-describedby={`${inputId}-hint ${inputId}-source`} rows={3}
      disabled={disabled || picking} spellCheck={false} value={value}
      placeholder={"D:\\Videolar\\video1.mp4\nD:\\Videolar\\video2.mp4"}
      onChange={(event) => setValue(event.target.value)} />
    <div className="local-video-paths-footer">
      <button className="secondary-button" type="submit" disabled={disabled || picking || paths.length === 0}>
        Dosya yolundan ekle
      </button>
    </div>
  </form>;
}
