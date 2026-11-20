from __future__ import annotations

import math
from pathlib import Path


SPEECH_SAMPLE_RATE = 16_000


class SpeechActivityError(RuntimeError):
    """The additional acoustic speech pass could not produce usable timings."""


def detect_speech_activity(audio_path: Path) -> list[tuple[float, float]]:
    """Find spoken audio independently of the words Whisper could transcribe.

    This pass is specific to speech cleanup. It deliberately retains short and
    quieter utterances and does not change the transcription settings used by
    LongToShorts. The bundled Silero model runs locally without downloading a
    second transcription model. Final mute padding is applied by the caller.
    """
    try:
        import numpy as np
        from faster_whisper.audio import decode_audio
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        audio = decode_audio(str(audio_path), sampling_rate=SPEECH_SAMPLE_RATE)
        if audio.ndim != 1 or not np.isfinite(audio).all():
            raise ValueError("Invalid mono audio samples")
        if not len(audio):
            return []
        timestamps = get_speech_timestamps(
            audio,
            vad_options=VadOptions(
                threshold=0.35,
                neg_threshold=0.20,
                min_speech_duration_ms=80,
                min_silence_duration_ms=250,
                speech_pad_ms=80,
            ),
            sampling_rate=SPEECH_SAMPLE_RATE,
        )
    except Exception as exc:
        raise SpeechActivityError("Konuşma seslerinin ek zaman kontrolü tamamlanamadı.") from exc

    duration = len(audio) / SPEECH_SAMPLE_RATE
    ranges: list[tuple[float, float]] = []
    for timestamp in timestamps:
        try:
            start = float(timestamp["start"]) / SPEECH_SAMPLE_RATE
            end = float(timestamp["end"]) / SPEECH_SAMPLE_RATE
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(start) or not math.isfinite(end):
            continue
        start, end = round(max(0, start), 3), round(min(duration, end), 3)
        if end > start:
            ranges.append((start, end))

    merged: list[tuple[float, float]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
