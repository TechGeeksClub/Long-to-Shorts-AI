from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import create_router
from app.config import Settings, settings
from app.codex_runner import cancel_active_codex_runs
from app.database import Database
from app.processor import Processor
from app.queue import WorkQueue
from app.speech_api import create_speech_router
from app.speech_cleanup import SpeechCleanupService
from app.project_api import create_project_router
from app.projects import ProjectsService


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def create_app(app_settings: Settings = settings, start_worker: bool = True) -> FastAPI:
    app_settings.ensure_directories()
    database = Database(app_settings.database_path)
    database.initialize()
    processor = Processor(database, app_settings)
    speech_cleanup = SpeechCleanupService(database, app_settings)
    speech_cleanup.initialize()
    projects = ProjectsService(database, app_settings, speech_cleanup.has_active_work)
    projects.initialize()
    work_queue = WorkQueue(
        processor.analyze, processor.export, processor.correct_subtitles,
        processor.generate_youtube, processor.import_youtube,
        speech_cleanup.detect, speech_cleanup.render, projects.render,
    )

    def cancel_active_work() -> None:
        processor.cancel_imports()
        cancel_active_codex_runs()
        speech_cleanup.cancel()
        projects.cancel()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        database.interrupt_active_jobs()
        speech_cleanup.interrupt_active()
        projects.interrupt_active()
        if start_worker:
            work_queue.start()
        yield
        if start_worker:
            work_queue.stop(cancel_active_work)

    application = FastAPI(
        title="Long to Shorts",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.database = database
    application.state.work_queue = work_queue
    application.state.settings = app_settings
    application.state.speech_cleanup = speech_cleanup
    application.state.projects = projects
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.include_router(create_router(database, work_queue, app_settings))
    application.include_router(create_speech_router(speech_cleanup, work_queue))
    application.include_router(create_project_router(projects, work_queue))

    if app_settings.frontend_dist.exists():
        assets = app_settings.frontend_dist / "assets"
        if assets.exists():
            application.mount("/assets", StaticFiles(directory=assets), name="assets")

        @application.get("/{path:path}", include_in_schema=False)
        def frontend(path: str) -> FileResponse:
            requested = app_settings.frontend_dist / path
            if path and requested.is_file():
                return FileResponse(requested)
            return FileResponse(app_settings.frontend_dist / "index.html")
    else:

        @application.get("/", include_in_schema=False)
        def root() -> dict[str, str]:
            return {
                "message": "Frontend henüz derlenmedi.",
                "docs": "/docs",
            }

    return application


app = create_app()

