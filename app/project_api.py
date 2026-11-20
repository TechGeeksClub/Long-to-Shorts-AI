from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from app.local_requests import require_local_request
from app.local_files import FileBrowserError, reveal_file
from app.local_video_picker import PickerError, choose_export_directory
from app.projects import (
    ActiveVideoUpdate, MergeRequest, MergeTask, ProjectDetail, ProjectError, ProjectsService, ProjectSummary,
    ProjectPathsRequest,
)
from app.queue import WorkQueue
from app.uploads import SafeUploadRoute


def create_project_router(service: ProjectsService, queue: WorkQueue) -> APIRouter:
    router = APIRouter(prefix="/api/projects", tags=["projects"], route_class=SafeUploadRoute)

    def call(handler, *args, **kwargs):
        try:
            return handler(*args, **kwargs)
        except ProjectError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    async def upload_files(request: Request, videos: list[UploadFile], project_id: str | None = None):
        try:
            if project_id is not None:
                service.detail(project_id)
            content_length = int(request.headers.get("content-length") or 0)
            return await service.upload(videos, content_length, project_id)
        except ProjectError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Videolar kaydedilemedi: {exc}") from exc

    @router.get("", response_model=list[ProjectSummary])
    def list_projects():
        return call(service.list_projects)

    @router.post("", response_model=ProjectDetail, status_code=201)
    async def create(request: Request, videos: list[UploadFile] = File(...)):
        return await upload_files(request, videos)

    @router.post("/from-paths", response_model=ProjectDetail, status_code=201)
    def create_from_paths(request: Request, payload: ProjectPathsRequest):
        require_local_request(request)
        return call(service.link_paths, payload.paths)

    @router.post("/pick-export-directory")
    def pick_export_directory(request: Request):
        require_local_request(request)
        try:
            return {"directory": choose_export_directory()}
        except PickerError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @router.get("/{project_id}", response_model=ProjectDetail)
    def detail(project_id: str):
        return call(service.detail, project_id)

    @router.post("/{project_id}/videos", response_model=ProjectDetail, status_code=201)
    async def add(project_id: str, request: Request, videos: list[UploadFile] = File(...)):
        return await upload_files(request, videos, project_id)

    @router.post("/{project_id}/videos/from-paths", response_model=ProjectDetail, status_code=201)
    def add_from_paths(project_id: str, request: Request, payload: ProjectPathsRequest):
        require_local_request(request)
        return call(service.link_paths, payload.paths, project_id)

    @router.put("/{project_id}/active-video", response_model=ProjectDetail)
    def select(project_id: str, payload: ActiveVideoUpdate):
        return call(service.select_video, project_id, payload.video_id)

    @router.post("/{project_id}/merges", response_model=MergeTask, status_code=202)
    def merge(project_id: str, request: Request, payload: MergeRequest):
        if payload.output_directory is not None:
            require_local_request(request)
        return call(service.queue_merge, project_id, payload.video_ids, queue.enqueue_merge,
                    cuts=payload.cuts, export_mode=payload.export_mode, output_directory=payload.output_directory)

    @router.post("/{project_id}/merges/{merge_id}/reveal", status_code=204)
    def reveal(project_id: str, merge_id: str, request: Request):
        require_local_request(request)
        record = call(service.merge_record, project_id, merge_id)
        if record["status"] != "completed" or not record["output_video_id"] or not record["path"]:
            raise HTTPException(status_code=409, detail="Dışa aktarılan video henüz hazır değil.")
        if not Path(record["path"]).is_file():
            raise HTTPException(status_code=404, detail="Dışa aktarılan video bulunamadı.")
        try:
            reveal_file(Path(record["path"]))
        except FileBrowserError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @router.get("/{project_id}/merges/{merge_id}/download")
    def download(project_id: str, merge_id: str):
        record = call(service.merge_record, project_id, merge_id)
        if record["status"] != "completed" or not record["output_video_id"] or not record["path"]:
            raise HTTPException(status_code=409, detail="Dışa aktarılan video henüz hazır değil.")
        path = Path(record["path"])
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Dışa aktarılan video bulunamadı.")
        return FileResponse(path, media_type="video/mp4", filename=path.name)

    @router.delete("/{project_id}", status_code=204)
    def delete(project_id: str):
        call(service.delete, project_id)

    return router
