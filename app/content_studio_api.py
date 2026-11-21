from __future__ import annotations

import errno
import logging
import math
import re
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, TypeVar

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator

from app.api import ALLOWED_EXTENSIONS
from app.codex_runner import CodexError, run_codex_json
from app.config import Settings
from app.content_studio_prompts import (
    MAX_TARGET_SECONDS,
    MIN_TARGET_SECONDS,
    ContentOption,
    ContentOptions,
    ContentPackage,
    LegacyContentOption,
    StudioBlueprint,
    StudioWriting,
    build_blueprint_prompt,
    build_generate_prompt,
    build_options_prompt,
)
from app.database import Database
from app.local_requests import require_local_request
from app.media import MediaError, extract_audio
from app.transcription import TranscriptionError, transcribe
from app.uploads import SafeUploadRoute
from app.youtube_import import YoutubeImportError, download_youtube_video, normalize_youtube_url


logger = logging.getLogger(__name__)
MAX_BRIEF_CHARS = 12_000
MAX_TRANSCRIPT_CHARS = 100_000
WORDS_PER_MINUTE = 125
SPOKEN_WORD = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*")
Brief = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_BRIEF_CHARS)]
ReferenceURL = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2_048)]
ReferenceTranscript = Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_TRANSCRIPT_CHARS)]
TargetSeconds = Annotated[int, Field(strict=True, ge=MIN_TARGET_SECONDS, le=MAX_TARGET_SECONDS)]
StudioOutput = TypeVar("StudioOutput", bound=BaseModel)


class OptionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief: Brief
    reference_url: ReferenceURL = ""
    reference_transcript: ReferenceTranscript = ""
    target_seconds: TargetSeconds | None = None

    @model_validator(mode="after")
    def check_reference(self) -> OptionsRequest:
        if self.reference_url and self.reference_transcript:
            raise ValueError("Bağlantı veya konuşma metninden birini seç.")
        return self


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief: Brief
    reference_insights: Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_BRIEF_CHARS)]
    option: ContentOption | LegacyContentOption
    target_seconds: TargetSeconds | None = None


class ContentHistoryItem(BaseModel):
    id: str
    created_at: str
    brief_preview: str
    title: str
    target_seconds: int


class ContentHistoryEntry(BaseModel):
    id: str
    brief: str
    has_reference: bool
    data: ContentOptions


class ContentStudioRoute(SafeUploadRoute):
    def get_route_handler(self):
        handle = super().get_route_handler()

        async def local_handler(request: Request):
            require_local_request(request)
            return await handle(request)

        return local_handler


def _transcribe_reference(source: Path, settings: Settings, work_dir: Path) -> str:
    audio = work_dir / "reference.wav"
    try:
        extract_audio(source, audio, settings.ffmpeg)
        segments, _ = transcribe(audio, settings.whisper_model)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail="Videonun sesini çıkarmak için FFmpeg gerekli.") from exc
    except MediaError as exc:
        raise HTTPException(status_code=422, detail="Örnek videonun sesi okunamadı. Ses içeren bir video seç.") from exc
    except TranscriptionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    transcript = "\n".join(segment["text"].strip() for segment in segments if segment["text"].strip())
    if not transcript:
        raise HTTPException(status_code=422, detail="Örnek videoda konuşma bulunamadı.")
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        raise HTTPException(status_code=422, detail="Örnek videonun konuşma metni çok uzun. Daha kısa bir örnek seç.")
    return transcript


def _run_studio(
    prompt: str, output: type[StudioOutput], settings: Settings,
    *, check: Callable[[StudioOutput], None] | None = None,
) -> StudioOutput:
    schema = output.model_json_schema()
    correction = ""
    for _ in range(2):
        try:
            payload, _ = run_codex_json(
                prompt=correction + prompt, schema=schema, settings=settings, work_dir=settings.data_dir,
            )
        except CodexError as exc:
            detail = str(exc).replace("konu planı", "içerik planı")
            raise HTTPException(status_code=502, detail=detail) from exc
        try:
            result = output.model_validate(payload)
            if check:
                check(result)
            return result
        except ValidationError as exc:
            errors = exc.errors(include_input=False, include_context=False, include_url=False)
            reason = "; ".join(f"{'.'.join(map(str, item['loc']))}: {item['msg']}" for item in errors[:3])
        except ValueError as exc:
            reason = str(exc)
        correction = (
            "Önceki yanıt doğrulanamadı. Aşağıdaki sorunu gider ve tam çıktıyı yeniden yaz.\n"
            + reason[:1000] + "\n\n"
        )
    logger.warning("Codex returned an invalid %s response twice", output.__name__)
    raise HTTPException(status_code=502, detail="Codex geçerli bir içerik planı döndürmedi. Tekrar dene.")


def _check_target(actual: int, target_seconds: int | None) -> None:
    if target_seconds is not None and actual != target_seconds:
        raise ValueError(f"Planın hedef süresi {target_seconds} saniye olmalı.")


def _suggest_options(
    brief: str, transcript: str, target_seconds: int | None, settings: Settings, database: Database,
) -> ContentOptions:
    def check(options: ContentOptions) -> None:
        _check_target(options.target_seconds, target_seconds)
        if transcript and not options.reference_insights:
            raise ValueError("Referanstaki anlatım hakkında somut gözlemler yaz.")

    options = _run_studio(
        build_options_prompt(brief, transcript, target_seconds), ContentOptions, settings, check=check,
    )
    try:
        database.save_content_studio_options(brief, bool(transcript), options.model_dump_json())
    except sqlite3.Error as exc:
        logger.warning("Content studio history write failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Akış önerileri yerel geçmişe kaydedilemedi.") from exc
    return options


def _check_writing(writing: StudioWriting, blueprint: StudioBlueprint) -> None:
    if [item.beat for item in writing.edit_plan] != [scene.title for scene in blueprint.scenes]:
        raise ValueError("Kurgu planı çekim planındaki sahne başlıklarını ve sırasını aynen kullanmalı.")

    target = blueprint.target_seconds
    counts = [len(SPOKEN_WORD.findall(item.spoken)) for item in writing.edit_plan]
    minimum = math.ceil(target * (1.2 if target < 30 else 1.5))
    maximum = math.ceil(target * 2.65)
    if not minimum <= sum(counts) <= maximum:
        raise ValueError(
            f"Konuşma {sum(counts)} kelime. {target} saniye için toplam {minimum}–{maximum} kelime gerekli. "
            "Her sahneyi belirlenen süreye göre yaz."
        )
    for index, (count, scene) in enumerate(zip(counts, blueprint.scenes), start=1):
        if count < scene.duration_seconds:
            raise ValueError(
                f"Çekim planındaki {index}. sahne {scene.duration_seconds} saniye. "
                "Konuşmayı bu sahnede somut ayrıntılar ve doğal geçişlerle geliştir."
            )


def create_content_studio_router(settings: Settings, database: Database) -> APIRouter:
    router = APIRouter(prefix="/api/content-studio", route_class=ContentStudioRoute)

    @router.get("/history", response_model=list[ContentHistoryItem])
    def list_history() -> list[ContentHistoryItem]:
        return [ContentHistoryItem(**dict(row)) for row in database.list_content_studio_history()]

    @router.get("/history/{entry_id}", response_model=ContentHistoryEntry)
    def get_history_entry(entry_id: str) -> ContentHistoryEntry:
        row = database.get_content_studio_options(entry_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Akış önerileri geçmişte bulunamadı.")
        return ContentHistoryEntry(
            id=row["id"], brief=row["brief"], has_reference=bool(row["has_reference"]),
            data=ContentOptions.model_validate_json(row["options_json"]),
        )

    @router.post("/options", response_model=ContentOptions)
    def create_options(payload: OptionsRequest) -> ContentOptions:
        transcript = payload.reference_transcript
        if payload.reference_url:
            try:
                url = normalize_youtube_url(payload.reference_url)
            except YoutubeImportError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            with TemporaryDirectory(prefix="content-studio-", dir=settings.data_dir) as temporary:
                work_dir = Path(temporary)
                try:
                    download = download_youtube_video(url, work_dir, settings)
                except YoutubeImportError as exc:
                    raise HTTPException(
                        status_code=502,
                        detail={"code": "youtube_reference_unavailable", "message": str(exc)},
                    ) from exc
                transcript = _transcribe_reference(download.source_path, settings, work_dir)
        return _suggest_options(payload.brief, transcript, payload.target_seconds, settings, database)

    @router.post("/options/video", response_model=ContentOptions)
    def create_video_options(
        brief: Annotated[str, Form()],
        video: Annotated[UploadFile, File()],
        target_seconds: Annotated[int | None, Form(ge=MIN_TARGET_SECONDS, le=MAX_TARGET_SECONDS)] = None,
    ) -> ContentOptions:
        brief = brief.strip()
        if not brief or len(brief) > MAX_BRIEF_CHARS:
            raise HTTPException(status_code=422, detail="Video fikrini 1–12.000 karakterle yaz.")
        suffix = Path(video.filename or "").suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            raise HTTPException(status_code=415, detail="Desteklenen video türleri MP4, MOV, MKV, M4V, AVI ve WEBM.")
        if video.size == 0:
            raise HTTPException(status_code=422, detail="Yüklenen video boş.")

        try:
            with TemporaryDirectory(prefix="content-studio-", dir=settings.data_dir) as temporary:
                work_dir = Path(temporary)
                source = work_dir / f"reference{suffix}"
                with source.open("wb") as destination:
                    shutil.copyfileobj(video.file, destination, length=settings.upload_chunk_size)
                video.file.close()
                transcript = _transcribe_reference(source, settings, work_dir)
        except OSError as exc:
            if exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) in {39, 112}:
                raise HTTPException(status_code=507, detail="Örnek videoyu işlemek için yeterli boş disk alanı yok.") from exc
            logger.warning("Content studio reference storage error (%s)", type(exc).__name__)
            raise HTTPException(status_code=500, detail="Örnek video işlenirken geçici dosyaya erişilemedi.") from exc
        return _suggest_options(brief, transcript, target_seconds, settings, database)

    @router.post("/generate", response_model=ContentPackage)
    def generate_package(payload: GenerateRequest) -> ContentPackage:
        blueprint = _run_studio(
            build_blueprint_prompt(payload.brief, payload.reference_insights, payload.option, payload.target_seconds),
            StudioBlueprint, settings, check=lambda value: _check_target(value.target_seconds, payload.target_seconds),
        )
        writing = _run_studio(
            build_generate_prompt(payload.brief, payload.reference_insights, payload.option, blueprint),
            StudioWriting, settings, check=lambda value: _check_writing(value, blueprint),
        )
        script = "\n\n".join(scene.spoken for scene in writing.edit_plan)
        return ContentPackage(
            **writing.model_dump(), script=script, target_seconds=blueprint.target_seconds,
            estimated_speech_seconds=round(len(SPOKEN_WORD.findall(script)) * 60 / WORDS_PER_MINUTE),
        )

    return router
