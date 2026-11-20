from __future__ import annotations

import json
import logging
import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import Settings
from app.agent_selection import select_agent_clips
from app.codex_runner import CodexError
from app.database import Database
from app.job_workspace import job_workspace
from app.editing import build_edit_timeline, punch_in_times, validate_edit_source
from app.face_tracking import center_crop, track_face_crop, write_crop_commands
from app.media import (
    MediaError,
    extract_audio,
    probe_media,
    render_cut_source,
    render_video,
)
from app.models import AnalysisReport, ClipCandidate, EditSettings, SubtitleCue, YoutubePackage
from app.youtube import generate_youtube_options, write_youtube_package
from app.youtube_import import YoutubeImportError, download_youtube_video
from app.seo import write_youtube_metadata
from app.subtitles import (
    apply_cut_ranges_to_subtitles,
    compose_subtitles_for_timeline,
    normalize_cues,
    normalize_cut_ranges,
    retime_cues_from_transcript,
    write_ass,
)
from app.timeline import TimelineSegment, build_timeline_segments
from app.transcription import TranscriptionError, transcribe
from app.transcript_units import annotate_transcript_segments
from app.transcript_correction import correct_saved_cues, correct_transcript, transcript_fingerprint


logger = logging.getLogger(__name__)


class Processor:
    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings
        self._import_cancelled = threading.Event()

    def cancel_imports(self) -> None:
        self._import_cancelled.set()

    def import_youtube(self, job_id: str) -> None:
        record = self.database.get_job_record(job_id)
        if record is None:
            return
        try:
            if record["source_type"] != "youtube" or not record["source_url"]:
                raise YoutubeImportError("İçe aktarılacak YouTube bağlantısı bulunamadı.")
            if self._import_cancelled.is_set():
                raise YoutubeImportError("YouTube içe aktarma uygulama kapanırken durduruldu.")
            self.database.update_job(
                job_id, status="importing", progress=0, stage="YouTube videosu hazırlanıyor", error=None,
            )

            def progress(value: float, stage: str) -> None:
                self.database.update_job(job_id, progress=min(1, max(0, value)), stage=stage)

            result = download_youtube_video(
                record["source_url"], job_workspace(self.settings, job_id), self.settings,
                progress_callback=progress, cancel_event=self._import_cancelled,
            )
            self.database.update_job(
                job_id, source_path=str(result.source_path),
                filename=f"{result.title.strip() or result.video_id}{result.source_path.suffix}",
                source_url=result.url, progress=0, stage="Video indirildi; analiz başlıyor",
            )
            if self._import_cancelled.is_set():
                raise YoutubeImportError("YouTube içe aktarma uygulama kapanırken durduruldu.")
        except Exception as exc:
            logger.warning("YouTube import failed for %s: %s", job_id, exc, exc_info=not isinstance(exc, YoutubeImportError))
            interrupted = self._import_cancelled.is_set()
            self.database.update_job(
                job_id, status="interrupted" if interrupted else "failed",
                stage="YouTube içe aktarma durduruldu" if interrupted else "YouTube videosu alınamadı",
                error=str(exc) if isinstance(exc, YoutubeImportError) else "Video içe aktarılırken beklenmeyen bir hata oluştu.",
            )
            return
        # Keep download and analysis in one worker item so no other work can
        # observe an imported source still waiting behind unrelated exports.
        self.analyze(job_id)

    def analyze(self, job_id: str) -> None:
        record = self.database.get_job_record(job_id)
        if record is None:
            return
        job_dir = job_workspace(self.settings, job_id)
        source = Path(record["source_path"])
        audio_path = job_dir / "audio.wav"
        transcript_path = job_dir / "transcript.json"
        try:
            job_dir.mkdir(exist_ok=True, parents=True)
            self.database.update_job(
                job_id,
                status="analyzing",
                progress=0.02,
                stage="Video doğrulanıyor",
                error=None,
            )
            def transcription_progress(value: float) -> None:
                self.database.update_job(
                    job_id,
                    progress=0.14 + value * 0.66,
                    stage="Konuşma yazıya dönüştürülüyor",
                )

            saved_transcript = Path(record["transcript_path"]) if record["transcript_path"] else transcript_path
            if saved_transcript.is_file():
                transcript = json.loads(saved_transcript.read_text(encoding="utf-8"))
                segments = transcript["segments"]
                language = str(transcript.get("language") or record["language"] or "unknown")
                duration = float(record["duration"] or 0)
                if duration <= 0:
                    info = probe_media(source, self.settings.ffprobe)
                    duration = info.duration
                    self.database.update_job(job_id, duration=duration, width=info.width,
                                             height=info.height, has_audio=int(info.has_audio))
                self.database.update_job(
                    job_id,
                    progress=0.78,
                    stage="Kayıtlı konuşma metni Codex için hazırlanıyor",
                )
            else:
                missing = self.settings.validate_binaries()
                if missing:
                    raise MediaError(f"Eksik araçlar: {', '.join(missing)}")
                info = probe_media(source, self.settings.ffprobe)
                duration = info.duration
                self.database.update_job(
                    job_id, duration=duration, width=info.width, height=info.height,
                    has_audio=int(info.has_audio), progress=0.08, stage="Ses hazırlanıyor",
                )
                extract_audio(source, audio_path, self.settings.ffmpeg)
                self.database.update_job(job_id, progress=0.14, stage="Konuşma yazıya dönüştürülüyor")
                segments, language = transcribe(
                    audio_path,
                    self.settings.whisper_model,
                    transcription_progress,
                )
            segments = annotate_transcript_segments(segments)
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
                stage="Codex konuşmanın tamamını okuyarak konu planı çıkarıyor",
            )
            if self.settings.transcript_correction_enabled:
                segments = self._correct_job_transcript(job_id, transcript_path, segments, language)

            def agent_progress(stage: str, value: float) -> None:
                self.database.update_job(job_id, progress=.84 + min(1, max(0, value)) * .15, stage=stage)

            result = select_agent_clips(
                job_id=job_id,
                segments=segments,
                duration=duration,
                settings=self.settings,
                work_dir=job_dir / "analysis",
                progress_callback=agent_progress,
            )
            report = AnalysisReport(
                model=result.model, summary=result.summary, topics=result.topics,
                clip_ids=[clip.id for clip in result.clips], created_at=datetime.now(UTC),
            )
            self.database.merge_agent_analysis(job_id, result.clips, report)
        except (MediaError, TranscriptionError, CodexError) as exc:
            logger.warning("Analysis failed for %s: %s", job_id, exc)
            self.database.update_job(
                job_id,
                status="failed",
                stage="Codex konu analizi tamamlanamadı" if isinstance(exc, CodexError) else "İşlem başarısız",
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

    def _correct_job_transcript(self, job_id: str, path: Path, segments: list, language: str) -> list:
        previous = self.database.get_job(job_id)
        if previous and previous.correction and previous.correction.fingerprint == transcript_fingerprint(segments):
            return segments
        corrected, report = correct_transcript(segments, self.settings, path.parent / "analysis",
            lambda stage, value: self.database.update_job(job_id, stage=stage, progress=.8 + value * .04))
        original = path.with_name("transcript.original.json")
        if not original.exists():
            shutil.copy2(path, original)
        temporary = path.with_suffix(".correcting.json")
        temporary.write_text(json.dumps({"language": language, "segments": corrected}, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        previous = self.database.get_job(job_id)
        if previous:
            for clip in previous.clips:
                updated = correct_saved_cues(clip.subtitles, report.corrections, clip.start, clip.cut_ranges)
                if updated != clip.subtitles:
                    self.database.update_clip(clip.id, subtitles=updated)
        self.database.update_job(job_id, correction_json=report.model_dump_json())
        return corrected

    def correct_subtitles(self, job_id: str) -> None:
        job = self.database.get_job_record(job_id)
        if job is None:
            return
        try:
            self.database.update_job(job_id, status="analyzing", stage="Codex altyazıyı kontrol ediyor", error=None)
            path = Path(job["transcript_path"])
            transcript = json.loads(path.read_text(encoding="utf-8"))
            self._correct_job_transcript(job_id, path, transcript["segments"], transcript.get("language", "tr"))
            self.database.update_job(job_id, status="ready", progress=1, stage="Altyazı düzeltmeleri hazır", error=None)
        except Exception as exc:
            logger.exception("Subtitle correction failed for %s", job_id)
            self.database.update_job(job_id, status="failed", stage="Altyazı düzeltilemedi", error=str(exc))

    def generate_youtube(self, clip_id: str) -> None:
        clip = self.database.get_clip(clip_id)
        if clip is None:
            return
        try:
            self.database.update_clip(clip_id, youtube=clip.youtube.model_copy(update={"status": "generating", "error": None}))
            record = self.database.get_job_record(clip.job_id)
            timeline = build_edit_timeline(build_timeline_segments(clip_start=clip.start, clip_end=clip.end,
                cut_ranges=clip.cut_ranges, insert_ranges=clip.insert_ranges), clip.edit_settings)
            cues = self._sync_subtitles_for_export(record, clip, clip.cut_ranges, timeline)
            options = generate_youtube_options(clip.model_copy(update={"subtitles": cues}), self.settings,
                job_workspace(self.settings, clip.job_id) / "youtube" / clip.id)
            self.database.update_clip(clip_id, youtube=clip.youtube.model_copy(update={"options": options, "status": "ready", "error": None}))
        except Exception as exc:
            logger.exception("YouTube generation failed for %s", clip_id)
            self.database.update_clip(clip_id, youtube=clip.youtube.model_copy(update={"status": "failed", "error": str(exc)}))

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
        job_dir = job_workspace(self.settings, job_id)
        work_dir = job_dir / "work" / export_id
        exports_dir = job_dir / "exports"
        work_dir.mkdir(parents=True, exist_ok=True)
        exports_dir.mkdir(parents=True, exist_ok=True)
        is_preview = bool(export_record["is_preview"])
        prefix = "preview" if is_preview else "short"
        export_name = f"{prefix}-{clip.rank}-{export_id[:8]}"
        export_dir = exports_dir / export_name
        export_dir.mkdir(parents=True, exist_ok=True)
        output = export_dir / f"{export_name}.mp4"
        metadata_path = export_dir / "youtube-seo.md"
        try:
            edit_settings = EditSettings.model_validate_json(export_record["edit_settings_json"])
            validate_edit_source(edit_settings, float(job["duration"] or 0))
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
            timeline = build_timeline_segments(
                clip_start=clip.start,
                clip_end=clip.end,
                cut_ranges=cut_ranges,
                insert_ranges=clip.insert_ranges,
            )
            timeline = build_edit_timeline(timeline, edit_settings)
            render_source = source
            render_start = clip.start
            render_end = clip.end
            if cut_ranges or clip.insert_ranges or edit_settings.hook_range:
                self.database.update_job(
                    job_id,
                    stage=f"{clip.rank}. klibin zaman çizelgesi oluşturuluyor",
                )
                composed_source = work_dir / "composed-source.mp4"

                def composition_progress(value: float) -> None:
                    self.database.update_export(export_id, progress=0.01 + value * 0.14)

                composed_duration = render_cut_source(
                    source=source,
                    output=composed_source,
                    kept_ranges=[
                        (segment.source_start, segment.source_end)
                        for segment in timeline
                    ],
                    ffmpeg=self.settings.ffmpeg,
                    progress_callback=composition_progress,
                    max_width=960 if is_preview else None,
                )
                render_source = composed_source
                render_start = 0.0
                render_end = composed_duration

            source_width, source_height = int(job["width"]), int(job["height"])
            if is_preview and render_source != source and source_width > 960:
                composed_info = probe_media(render_source, self.settings.ffprobe)
                source_width, source_height = composed_info.width, composed_info.height
            tracking_active = clip.framing_mode == "fill" and clip.face_tracking_enabled
            if clip.framing_mode == "fill":
                if tracking_active:
                    keyframes, crop_width, crop_height = track_face_crop(
                        source=render_source,
                        clip_start=render_start,
                        clip_end=render_end,
                        source_width=source_width,
                        source_height=source_height,
                        models_dir=self.settings.models_dir,
                    )
                    write_crop_commands(keyframes, work_dir / "crop.cmd")
                else:
                    keyframes, crop_width, crop_height = center_crop(
                        source_width,
                        source_height,
                    )
                if not is_preview:
                    self.database.update_clip(clip.id, crop_keyframes=keyframes)
            else:
                crop_width = None
                crop_height = None
                if not is_preview:
                    self.database.update_clip(clip.id, crop_keyframes=[])
            self.database.update_job(
                job_id,
                stage=f"{clip.rank}. klip altyazısı sese senkronize ediliyor",
            )
            synced_subtitles = self._sync_subtitles_for_export(
                job,
                clip,
                cut_ranges,
                timeline,
            )
            if (
                not clip.insert_ranges and not edit_settings.hook_range
                and synced_subtitles != clip.subtitles
            ):
                self.database.update_clip(clip.id, subtitles=synced_subtitles)
                clip = clip.model_copy(update={"subtitles": synced_subtitles})
            write_ass(
                synced_subtitles,
                clip.start,
                work_dir / "subtitles.ass",
                subtitle_margin_v=int(export_record["subtitle_margin_v"]),
                subtitle_font_family=str(export_record["subtitle_font_family"]),
                edit_settings=edit_settings,
                playback_rate=float(export_record["playback_rate"]),
                clip_duration=render_end - render_start,
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
                edit_settings=edit_settings,
                punch_in_times=punch_in_times(synced_subtitles, clip.start),
                output_width=540 if is_preview else 1080,
                subtitle_margin_v=int(export_record["subtitle_margin_v"]),
                hook_boundary_seconds=(
                    timeline[0].duration
                    if len(timeline) > 1 and timeline[0].kind == "hook" else None
                ),
            )
            if not is_preview:
                write_youtube_metadata(
                    job=job,
                    clip=clip.model_copy(update={"subtitles": synced_subtitles}),
                    export_id=export_id,
                    output=output,
                    destination=metadata_path,
                    settings=self.settings,
                    llm_seo_enabled=bool(export_record["llm_seo_enabled"]),
                )
                write_youtube_package(source, output, clip.model_copy(update={"subtitles": synced_subtitles}),
                    YoutubePackage.model_validate_json(export_record["youtube_json"]), self.settings)
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
        timeline: list[TimelineSegment],
    ) -> list[SubtitleCue]:
        composed = bool(clip.insert_ranges) or any(segment.kind == "hook" for segment in timeline)
        transcript_value = job["transcript_path"]
        transcript_path = Path(transcript_value) if transcript_value else None
        if transcript_path is None or not transcript_path.is_file():
            base_subtitles = normalize_cues(clip.subtitles, clip.start, clip.end)
            if composed:
                return compose_subtitles_for_timeline(
                    base_cues=base_subtitles,
                    transcript_segments=[],
                    timeline=timeline,
                    output_start=clip.start,
                    base_start=clip.start,
                    base_end=clip.end,
                )
            return apply_cut_ranges_to_subtitles(
                base_subtitles,
                clip.start,
                clip.end,
                cut_ranges,
            )
        try:
            transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
            segments = transcript["segments"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("Subtitle sync skipped for clip %s: %s", clip.id, exc)
            base_subtitles = normalize_cues(clip.subtitles, clip.start, clip.end)
            if composed:
                return compose_subtitles_for_timeline(
                    base_cues=base_subtitles,
                    transcript_segments=[],
                    timeline=timeline,
                    output_start=clip.start,
                    base_start=clip.start,
                    base_end=clip.end,
                )
            return apply_cut_ranges_to_subtitles(
                base_subtitles,
                clip.start,
                clip.end,
                cut_ranges,
            )
        source_subtitles = retime_cues_from_transcript(
            clip.subtitles,
            segments,
            clip.start,
            clip.end,
        )
        if composed:
            return compose_subtitles_for_timeline(
                base_cues=source_subtitles,
                transcript_segments=segments,
                timeline=timeline,
                output_start=clip.start,
                base_start=clip.start,
                base_end=clip.end,
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
