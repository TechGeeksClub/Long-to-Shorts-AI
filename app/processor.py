from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from app.config import Settings
from app.database import Database
from app.face_tracking import center_crop, track_face_crop, write_crop_commands
from app.media import (
    MediaError,
    extract_audio,
    load_rms_envelope,
    probe_media,
    render_cut_source,
    render_video,
)
from app.models import ClipCandidate, SubtitleCue
from app.scoring import score_candidates
from app.seo import write_youtube_metadata
from app.subtitles import (
    apply_cut_ranges_to_subtitles,
    kept_ranges_after_cuts,
    normalize_cues,
    normalize_cut_ranges,
    retime_cues_from_transcript,
    write_ass,
)
from app.transcription import TranscriptionError, transcribe


logger = logging.getLogger(__name__)


class Processor:
    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings

    def analyze(self, job_id: str) -> None:
        record = self.database.get_job_record(job_id)
        if record is None:
            return
        job_dir = Path(record["source_path"]).parent
        source = Path(record["source_path"])
        audio_path = job_dir / "audio.wav"
        transcript_path = job_dir / "transcript.json"
        try:
            self.database.update_job(
                job_id,
                status="analyzing",
                progress=0.02,
                stage="Video doğrulanıyor",
                error=None,
            )
            missing = self.settings.validate_binaries()
            if missing:
                raise MediaError(f"Eksik araçlar: {', '.join(missing)}")
            info = probe_media(source, self.settings.ffprobe)
            self.database.update_job(
                job_id,
                duration=info.duration,
                width=info.width,
                height=info.height,
                has_audio=int(info.has_audio),
                progress=0.08,
                stage="Ses hazırlanıyor",
            )
            extract_audio(source, audio_path, self.settings.ffmpeg)
            self.database.update_job(
                job_id,
                progress=0.14,
                stage="Konuşma yazıya dönüştürülüyor",
            )

            def transcription_progress(value: float) -> None:
                self.database.update_job(
                    job_id,
                    progress=0.14 + value * 0.66,
                    stage="Konuşma yazıya dönüştürülüyor",
                )

            if transcript_path.exists():
                transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
                segments = transcript["segments"]
                language = str(transcript.get("language") or record["language"] or "unknown")
                self.database.update_job(
                    job_id,
                    progress=0.78,
                    stage="Mevcut transkript konu başlıklarına ayrılıyor",
                )
            else:
                segments, language = transcribe(
                    audio_path,
                    self.settings.whisper_model,
                    transcription_progress,
                )
                transcript_path.write_text(
                    json.dumps(
                        {"language": language, "segments": segments},
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            self.database.update_job(
                job_id,
                language=language,
                transcript_path=str(transcript_path),
                progress=0.84,
                stage="En iyi bölümler seçiliyor",
            )
            envelope, bucket_seconds = load_rms_envelope(audio_path)
            clips = score_candidates(
                job_id=job_id,
                segments=segments,
                duration=info.duration,
                rms_envelope=envelope,
                rms_bucket_seconds=bucket_seconds,
                min_seconds=self.settings.min_clip_seconds,
                max_seconds=self.settings.max_clip_seconds,
                count=self.settings.candidate_count,
            )
            if not clips:
                raise MediaError("Uygun kısa video adayı oluşturulamadı.")
            previous_job = self.database.get_job(job_id)
            if previous_job:
                for export in previous_job.exports:
                    export_record = self.database.get_export_record(export.id)
                    if export_record and export_record["path"]:
                        export_path = Path(export_record["path"])
                        if (
                            export_path.parent.parent == job_dir / "exports"
                            and export_path.parent.name == export_path.stem
                        ):
                            shutil.rmtree(export_path.parent, ignore_errors=True)
                        else:
                            export_path.unlink(missing_ok=True)
            self.database.replace_clips(job_id, clips)
            self.database.update_job(
                job_id,
                status="ready",
                progress=1.0,
                stage=f"{len(clips)} klip adayı hazır",
                error=None,
            )
        except (MediaError, TranscriptionError) as exc:
            logger.warning("Analysis failed for %s: %s", job_id, exc)
            self.database.update_job(
                job_id,
                status="failed",
                stage="İşlem başarısız",
                error=str(exc),
            )
        except Exception as exc:
            logger.exception("Unexpected analysis error for %s", job_id)
            self.database.update_job(
                job_id,
                status="failed",
                stage="Beklenmeyen hata",
                error=f"Beklenmeyen hata: {exc}",
            )
        finally:
            audio_path.unlink(missing_ok=True)

    def export(self, export_id: str) -> None:
        export_record = self.database.get_export_record(export_id)
        if export_record is None:
            return
        job_id = export_record["job_id"]
        clip = self.database.get_clip(export_record["clip_id"])
        job = self.database.get_job_record(job_id)
        if clip is None or job is None:
            self.database.update_export(
                export_id, status="failed", error="İş veya klip bulunamadı."
            )
            return

        source = Path(job["source_path"])
        job_dir = source.parent
        work_dir = job_dir / "work" / export_id
        exports_dir = job_dir / "exports"
        work_dir.mkdir(parents=True, exist_ok=True)
        exports_dir.mkdir(parents=True, exist_ok=True)
        export_name = f"short-{clip.rank}-{export_id[:8]}"
        export_dir = exports_dir / export_name
        export_dir.mkdir(parents=True, exist_ok=True)
        output = export_dir / f"{export_name}.mp4"
        metadata_path = export_dir / "youtube-seo.md"
        try:
            self.database.update_export(
                export_id,
                status="rendering",
                progress=0.01,
                error=None,
            )
            self.database.update_job(
                job_id,
                status="exporting",
                stage=f"{clip.rank}. klip dikey kadraja alınıyor",
            )
            cut_ranges = normalize_cut_ranges(clip.cut_ranges, clip.start, clip.end)
            render_source = source
            render_start = clip.start
            render_end = clip.end
            if cut_ranges:
                self.database.update_job(
                    job_id,
                    stage=f"{clip.rank}. klipte işaretlenen kısımlar çıkarılıyor",
                )
                cut_source = work_dir / "cut-source.mp4"

                def cut_progress(value: float) -> None:
                    self.database.update_export(export_id, progress=0.01 + value * 0.14)

                kept_ranges = kept_ranges_after_cuts(clip.start, clip.end, cut_ranges)
                cut_duration = render_cut_source(
                    source=source,
                    output=cut_source,
                    kept_ranges=kept_ranges,
                    ffmpeg=self.settings.ffmpeg,
                    progress_callback=cut_progress,
                )
                render_source = cut_source
                render_start = 0.0
                render_end = cut_duration

            tracking_active = clip.framing_mode == "fill" and clip.face_tracking_enabled
            if clip.framing_mode == "fill":
                if tracking_active:
                    keyframes, crop_width, crop_height = track_face_crop(
                        source=render_source,
                        clip_start=render_start,
                        clip_end=render_end,
                        source_width=int(job["width"]),
                        source_height=int(job["height"]),
                        models_dir=self.settings.models_dir,
                    )
                    write_crop_commands(keyframes, work_dir / "crop.cmd")
                else:
                    keyframes, crop_width, crop_height = center_crop(
                        int(job["width"]),
                        int(job["height"]),
                    )
                self.database.update_clip(clip.id, crop_keyframes=keyframes)
            else:
                crop_width = None
                crop_height = None
                self.database.update_clip(clip.id, crop_keyframes=[])
            self.database.update_job(
                job_id,
                stage=f"{clip.rank}. klip altyazısı sese senkronize ediliyor",
            )
            synced_subtitles = self._sync_subtitles_for_export(job, clip, cut_ranges)
            if synced_subtitles != clip.subtitles:
                self.database.update_clip(clip.id, subtitles=synced_subtitles)
                clip = clip.model_copy(update={"subtitles": synced_subtitles})
            write_ass(
                clip.subtitles,
                clip.start,
                work_dir / "subtitles.ass",
                subtitle_margin_v=int(export_record["subtitle_margin_v"]),
                subtitle_font_family=str(export_record["subtitle_font_family"]),
            )
            self.database.update_export(export_id, progress=0.25)
            self.database.update_job(
                job_id,
                stage=f"{clip.rank}. klip işleniyor",
            )

            def render_progress(value: float) -> None:
                self.database.update_export(export_id, progress=0.25 + value * 0.75)

            render_video(
                source=render_source,
                output=output,
                work_dir=work_dir,
                start=render_start,
                end=render_end,
                crop_width=crop_width,
                crop_height=crop_height,
                framing_mode=clip.framing_mode,
                face_tracking_enabled=tracking_active,
                ffmpeg=self.settings.ffmpeg,
                playback_rate=float(export_record["playback_rate"]),
                balanced_vertical_offset=int(export_record["balanced_vertical_offset"]),
                progress_callback=render_progress,
            )
            write_youtube_metadata(
                job=job,
                clip=clip,
                export_id=export_id,
                output=output,
                destination=metadata_path,
                settings=self.settings,
                llm_seo_enabled=bool(export_record["llm_seo_enabled"]),
            )
            self.database.update_export(
                export_id,
                status="completed",
                progress=1.0,
                path=str(output),
                error=None,
            )
            self._refresh_job_export_status(job_id)
        except Exception as exc:
            logger.exception("Export failed for %s", export_id)
            output.unlink(missing_ok=True)
            self.database.update_export(
                export_id,
                status="failed",
                error=f"Dışa aktarma başarısız: {exc}",
            )
            self._refresh_job_export_status(job_id)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def _sync_subtitles_for_export(
        self,
        job: Any,
        clip: ClipCandidate,
        cut_ranges: list,
    ) -> list[SubtitleCue]:
        transcript_value = job["transcript_path"]
        transcript_path = Path(transcript_value) if transcript_value else None
        if transcript_path is None or not transcript_path.is_file():
            return normalize_cues(clip.subtitles, clip.start, clip.end)
        try:
            transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
            segments = transcript["segments"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("Subtitle sync skipped for clip %s: %s", clip.id, exc)
            return normalize_cues(clip.subtitles, clip.start, clip.end)
        source_subtitles = retime_cues_from_transcript(
            clip.subtitles,
            segments,
            clip.start,
            clip.end,
        )
        return apply_cut_ranges_to_subtitles(
            source_subtitles,
            clip.start,
            clip.end,
            cut_ranges,
        )

    def _refresh_job_export_status(self, job_id: str) -> None:
        job = self.database.get_job(job_id)
        if job is None:
            return
        active = [export for export in job.exports if export.status in {"queued", "rendering"}]
        failed = [export for export in job.exports if export.status == "failed"]
        if active:
            self.database.update_job(
                job_id,
                status="exporting",
                stage=f"{len(active)} dışa aktarma bekliyor",
            )
        elif failed and not any(export.status == "completed" for export in job.exports):
            self.database.update_job(
                job_id,
                status="ready",
                stage="Dışa aktarma başarısız",
                error=failed[0].error,
            )
        else:
            self.database.update_job(
                job_id,
                status="completed",
                progress=1.0,
                stage="Kısa videolar hazır",
                error=None,
            )
