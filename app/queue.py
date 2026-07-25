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
    kind: Literal["analyze", "export"]
    item_id: str


class WorkQueue:
    def __init__(
        self,
        analyze_handler: Callable[[str], None],
        export_handler: Callable[[str], None],
    ):
        self._queue: queue.Queue[WorkItem | None] = queue.Queue()
        self._handlers = {
            "analyze": analyze_handler,
            "export": export_handler,
        }
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="media-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self._thread:
            return
        self._queue.put(None)
        self._thread.join(timeout=5)

    def enqueue_analysis(self, job_id: str) -> None:
        self._queue.put(WorkItem("analyze", job_id))

    def enqueue_export(self, export_id: str) -> None:
        self._queue.put(WorkItem("export", export_id))

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            try:
                self._handlers[item.kind](item.item_id)
            except Exception:
                logger.exception("Unhandled worker error for %s %s", item.kind, item.item_id)
            finally:
                self._queue.task_done()

