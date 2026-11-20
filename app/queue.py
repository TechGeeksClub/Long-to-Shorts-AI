from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkItem:
    kind: Literal["analyze", "export", "correct", "youtube", "youtube_import", "speech_detect", "speech_export", "merge"]
    item_id: str


class WorkQueue:
    def __init__(
        self,
        analyze_handler: Callable[[str], None],
        export_handler: Callable[[str], None],
        correction_handler: Callable[[str], None] | None = None,
        youtube_handler: Callable[[str], None] | None = None,
        youtube_import_handler: Callable[[str], None] | None = None,
        speech_detection_handler: Callable[[str], None] | None = None,
        speech_export_handler: Callable[[str], None] | None = None,
        merge_handler: Callable[[str], None] | None = None,
    ):
        self._queue: queue.Queue[WorkItem | None] = queue.Queue()
        self._handlers = {
            "analyze": analyze_handler,
            "export": export_handler,
            "correct": correction_handler or (lambda _: None),
            "youtube": youtube_handler or (lambda _: None),
            "youtube_import": youtube_import_handler or (lambda _: None),
            "speech_detect": speech_detection_handler or (lambda _: None),
            "speech_export": speech_export_handler or (lambda _: None),
            "merge": merge_handler or (lambda _: None),
        }
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="media-worker", daemon=True)
        self._thread.start()

    def stop(self, cancel_active: Callable[[], None] | None = None) -> None:
        if not self._thread:
            return
        self._stopping.set()
        if cancel_active:
            cancel_active()
        self._queue.put(None)
        self._thread.join(timeout=5)

    def enqueue_analysis(self, job_id: str) -> None:
        if self._stopping.is_set():
            raise RuntimeError("Uygulama kapanıyor; yeni analiz başlatılamaz.")
        self._queue.put(WorkItem("analyze", job_id))

    def enqueue_export(self, export_id: str) -> None:
        if self._stopping.is_set():
            raise RuntimeError("Uygulama kapanıyor; yeni dışa aktarma başlatılamaz.")
        self._queue.put(WorkItem("export", export_id))

    def enqueue_correction(self, job_id: str) -> None:
        if self._stopping.is_set():
            raise RuntimeError("Uygulama kapanıyor.")
        self._queue.put(WorkItem("correct", job_id))

    def enqueue_youtube(self, clip_id: str) -> None:
        if self._stopping.is_set():
            raise RuntimeError("Uygulama kapanıyor.")
        self._queue.put(WorkItem("youtube", clip_id))

    def enqueue_youtube_import(self, job_id: str) -> None:
        if self._stopping.is_set():
            raise RuntimeError("Uygulama kapanıyor; YouTube videosu içe aktarılamaz.")
        self._queue.put(WorkItem("youtube_import", job_id))

    def enqueue_speech_detection(self, job_id: str) -> None:
        if self._stopping.is_set():
            raise RuntimeError("Uygulama kapanıyor; konuşma tespiti başlatılamaz.")
        self._queue.put(WorkItem("speech_detect", job_id))

    def enqueue_speech_export(self, export_id: str) -> None:
        if self._stopping.is_set():
            raise RuntimeError("Uygulama kapanıyor; konuşma temizleme çıktısı başlatılamaz.")
        self._queue.put(WorkItem("speech_export", export_id))

    def enqueue_merge(self, merge_id: str) -> None:
        if self._stopping.is_set():
            raise RuntimeError("Uygulama kapanıyor; video birleştirme başlatılamaz.")
        self._queue.put(WorkItem("merge", merge_id))

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            if self._stopping.is_set():
                self._queue.task_done()
                continue
            try:
                self._handlers[item.kind](item.item_id)
            except Exception:
                logger.exception("Unhandled worker error for %s %s", item.kind, item.item_id)
            finally:
                self._queue.task_done()

