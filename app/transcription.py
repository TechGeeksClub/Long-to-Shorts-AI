from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.transcript_units import annotate_transcript_segments


class TranscriptionError(RuntimeError):
    pass


_models: dict[str, Any] = {}


def transcribe(
    audio_path: Path,
    model_name: str,
    progress_callback: Callable[[float], None] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise TranscriptionError(
            "faster-whisper kurulu değil. start.ps1 ile kurulumu tamamlayın."
        ) from exc

    if model_name not in _models:
        if progress_callback:
            progress_callback(0.02)
        try:
            _models[model_name] = WhisperModel(
                model_name,
                device="cpu",
                compute_type="int8",
                cpu_threads=6,
                num_workers=1,
            )
        except Exception as exc:
            raise TranscriptionError(
                f"Whisper modeli yüklenemedi: {exc}. İlk çalıştırmada internet gerekir."
            ) from exc

    try:
        segment_iterator, info = _models[model_name].transcribe(
            str(audio_path),
            beam_size=5,
            word_timestamps=True,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,
        )
        segments: list[dict[str, Any]] = []
        for index, segment in enumerate(segment_iterator):
            words = [
                {
                    "text": word.word.strip(),
                    "start": float(word.start),
                    "end": float(word.end),
                    "probability": float(word.probability),
                }
                for word in (segment.words or [])
                if word.word.strip() and word.start is not None and word.end is not None
            ]
            if words:
                segments.append(
                    {
                        "start": float(segment.start),
                        "end": float(segment.end),
                        "text": segment.text.strip(),
                        "words": words,
                        "avg_logprob": float(segment.avg_logprob),
                        "no_speech_prob": float(segment.no_speech_prob),
                    }
                )
            if progress_callback:
                progress_callback(min(0.98, 0.05 + index * 0.015))
    except Exception as exc:
        raise TranscriptionError(f"Konuşma tanıma başarısız: {exc}") from exc

    if not segments:
        raise TranscriptionError("Videoda yazıya dönüştürülebilecek konuşma bulunamadı.")
    if progress_callback:
        progress_callback(1.0)
    return annotate_transcript_segments(segments), str(info.language or "unknown")
