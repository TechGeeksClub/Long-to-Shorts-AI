from __future__ import annotations

import errno
import logging
import shutil
import tempfile
from contextlib import suppress
from pathlib import Path

from fastapi import HTTPException, Request, params
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from python_multipart.exceptions import MultipartParseError
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect

from app.config import Settings


logger = logging.getLogger(__name__)
SPACE_CHECK_INTERVAL = 64 * 1024 * 1024


class UploadPreflightRequest(BaseModel):
    size_bytes: int = Field(ge=0)
    file_count: int = Field(ge=1, le=100)


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}" if unit in {"MB", "GB", "TB"} else f"{amount:.0f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TB"


def _same_volume(first: Path, second: Path) -> bool:
    return first.stat().st_dev == second.stat().st_dev


def _storage_error(exc: OSError) -> HTTPException:
    if exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)} or getattr(exc, "winerror", None) in {39, 112}:
        return HTTPException(
            status_code=507,
            detail="Video yüklenemedi: diskte veya geçici yükleme alanında boş yer kalmadı. Diskte yer açıp dosyaları yeniden seçerek tekrar dene.",
        )
    if isinstance(exc, PermissionError):
        return HTTPException(
            status_code=500,
            detail="Geçici yükleme dosyasına yazılamadı. Windows geçici klasörünün ve video klasörünün yazma izinlerini kontrol edip tekrar dene.",
        )
    return HTTPException(
        status_code=500,
        detail="Geçici yükleme alanına erişilemedi. Diskin kullanılabilir ve yazılabilir olduğunu kontrol edip tekrar dene.",
    )


def _validate_space(settings: Settings, payload_size: int, *, buffered_size: int = 0, next_chunk_size: int = 0) -> None:
    """Account for temporary spools and destination copies at the same time."""
    try:
        destination = settings.data_dir.resolve()
        temporary = Path(tempfile.gettempdir()).resolve()
        destination_free = shutil.disk_usage(destination).free
        temporary_free = shutil.disk_usage(temporary).free
        destination_required = payload_size + max(0, settings.minimum_free_bytes)
        temporary_required = max(0, payload_size - buffered_size, next_chunk_size)
        if _same_volume(destination, temporary):
            free = min(destination_free, temporary_free)
            required = destination_required + temporary_required
            if free < required:
                raise HTTPException(status_code=507, detail=(
                    "Videoları yüklemek için yeterli boş disk alanı yok. "
                    f"Boş: {_format_bytes(free)}, gereken: yaklaşık {_format_bytes(required)}. "
                    "Geçici yükleme kopyası ve videoların kaydı için diskte yer açıp tekrar dene."
                ))
        elif destination_free < destination_required:
            raise HTTPException(status_code=507, detail=(
                "Video klasörünün bulunduğu diskte yeterli boş alan yok. "
                f"Boş: {_format_bytes(destination_free)}, gereken: yaklaşık {_format_bytes(destination_required)}. "
                "Diskte yer açıp tekrar dene."
            ))
        elif temporary_free < temporary_required:
            raise HTTPException(status_code=507, detail=(
                "Windows geçici yükleme alanında yeterli boş yer yok. "
                f"Boş: {_format_bytes(temporary_free)}, gereken: yaklaşık {_format_bytes(temporary_required)}. "
                "Geçici klasörün bulunduğu diskte yer açıp tekrar dene."
            ))
    except OSError as exc:
        logger.warning("Upload storage preflight failed", exc_info=True)
        raise _storage_error(exc) from exc


def validate_upload_space(settings: Settings, payload_size: int) -> None:
    if payload_size < 0:
        raise HTTPException(status_code=400, detail="Yükleme boyutu geçersiz.")
    _validate_space(settings, payload_size)


def _content_length(request: Request) -> int | None:
    value = request.headers.get("content-length")
    if value is None:
        return None
    if not value or not value.isascii() or not value.isdecimal():
        raise HTTPException(status_code=400, detail="Yükleme boyutu başlığı geçersiz. Dosyaları yeniden seçip tekrar dene.")
    return int(value)


class SafeMultiPartParser(MultiPartParser):
    async def parse(self):
        try:
            return await super().parse()
        except BaseException:
            # Starlette closes these on MultiPartException only. Disk errors,
            # disconnected clients and cancellation also need deterministic cleanup.
            for file in self._files_to_close_on_error:
                with suppress(Exception):
                    file.close()
            raise


class SafeUploadRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        is_form = bool(self.body_field and isinstance(self.body_field.field_info, params.Form))

        async def safe_upload(request: Request):
            content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
            if not is_form or request.method != "POST" or content_type != "multipart/form-data":
                return await original(request)

            settings = request.app.state.settings
            expected = _content_length(request)
            validate_upload_space(settings, expected or 0)

            async def checked_stream():
                received = 0
                next_check = SPACE_CHECK_INTERVAL
                async for chunk in request.stream():
                    previous = received
                    received += len(chunk)
                    if received >= next_check:
                        _validate_space(settings, max(expected or 0, received),
                                        buffered_size=previous, next_chunk_size=len(chunk))
                        next_check = received + SPACE_CHECK_INTERVAL
                    yield chunk

            try:
                parser = SafeMultiPartParser(request.headers, checked_stream(), max_files=100)
                form = await parser.parse()
            except (MultiPartException, MultipartParseError) as exc:
                raise HTTPException(status_code=400, detail="Video yükleme formu geçersiz veya eksik. Dosyaları yeniden seçip tekrar dene.") from exc
            except OSError as exc:
                logger.warning("Multipart upload storage failed", exc_info=True)
                raise _storage_error(exc) from exc
            except ClientDisconnect as exc:
                raise HTTPException(status_code=400, detail="Video yükleme bağlantısı kesildi. Dosyaları yeniden seçip tekrar dene.") from exc
            except HTTPException:
                raise
            except Exception as exc:
                logger.exception("Multipart upload could not be parsed")
                raise HTTPException(status_code=400, detail="Video yükleme verileri okunamadı. Dosyaları yeniden seçip tekrar dene.") from exc

            # Keep FastAPI's File validation and OpenAPI contract, while avoiding
            # a second parse. Request.form() uses this Starlette request cache.
            request._form = form
            try:
                return await original(request)
            finally:
                try:
                    await form.close()
                except Exception:
                    logger.warning("Multipart upload files could not be closed", exc_info=True)

        return safe_upload
