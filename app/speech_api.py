from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse

from app.local_files import FileBrowserError, reveal_file
from app.local_requests import require_local_request
from app.queue import WorkQueue
from app.speech_cleanup import (
    SpeechCleanupError, SpeechCleanupService, SpeechCleanupState,
    SpeechExport, SpeechExportRequest, SpeechManualRegionRequest, SpeechSelectionUpdate,
)


def create_speech_router(service: SpeechCleanupService, queue: WorkQueue) -> APIRouter:
    router = APIRouter(prefix="/api/jobs/{job_id}/speech-cleanup", tags=["speech-cleanup"])

    def call(handler, *args):
        try:
            return handler(*args)
        except SpeechCleanupError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @router.get("", response_model=SpeechCleanupState)
    def state(job_id: str):
        return call(service.state, job_id)

    @router.post("/detect", response_model=SpeechCleanupState, status_code=202)
    def detect(job_id: str):
        return call(service.queue_detection, job_id, queue.enqueue_speech_detection)

    @router.put("/selection", response_model=SpeechCleanupState)
    def selection(job_id: str, payload: SpeechSelectionUpdate):
        return call(service.save_selection, job_id, payload.removed_region_ids)

    @router.post("/manual-regions", response_model=SpeechCleanupState, status_code=201)
    def add_manual_region(job_id: str, payload: SpeechManualRegionRequest):
        return call(service.add_manual_region, job_id, payload)

    @router.delete("/manual-regions/{region_id}", response_model=SpeechCleanupState)
    def delete_manual_region(job_id: str, region_id: str):
        return call(service.delete_manual_region, job_id, region_id)

    @router.post("/exports", response_model=SpeechExport, status_code=202)
    def export(job_id: str, payload: SpeechExportRequest):
        return call(service.queue_export, job_id, payload.mode, queue.enqueue_speech_export)

    def completed_export_path(job_id: str, export_id: str) -> Path:
        record = call(service.export_record, job_id, export_id)
        if record["status"] != "completed" or not record["path"]:
            raise HTTPException(status_code=409, detail="Konuşma temizleme çıktısı henüz hazır değil.")
        path = Path(record["path"])
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Çıktı dosyası bulunamadı.")
        return path

    @router.post("/exports/{export_id}/reveal", status_code=204)
    def reveal(job_id: str, export_id: str, request: Request):
        require_local_request(request)
        path = completed_export_path(job_id, export_id)
        try:
            reveal_file(path)
        except FileBrowserError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return Response(status_code=204)

    @router.get("/exports/{export_id}/download")
    def download(job_id: str, export_id: str):
        path = completed_export_path(job_id, export_id)
        return FileResponse(path, media_type="video/mp4", filename=path.name)

    return router
