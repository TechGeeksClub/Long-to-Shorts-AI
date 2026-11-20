import threading

import pytest

from app.queue import WorkQueue


def test_shutdown_cancels_active_work_and_does_not_start_queued_jobs() -> None:
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def analyze(job_id: str) -> None:
        calls.append(job_id)
        started.set()
        release.wait(timeout=3)

    queue = WorkQueue(analyze, lambda _: None)
    queue.start()
    queue.enqueue_analysis("active")
    assert started.wait(timeout=3)
    queue.enqueue_analysis("queued")
    queue.stop(release.set)
    assert calls == ["active"]
    assert not queue._thread.is_alive()
    with pytest.raises(RuntimeError):
        queue.enqueue_analysis("after-stop")
