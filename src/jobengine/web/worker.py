"""One background thread with an in-process queue (spec section 2).

Telegram updates are queued so the webhook answers at once; scheduled tasks go through the
same thread (and wait for it), so the bot's state is only ever used by one thread.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

log = logging.getLogger("jobengine.web")


class Worker:
    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[Callable[[], Any], Future[Any]] | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _ensure_started(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="jobengine-worker",
                                                daemon=True)
                self._thread.start()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                job, future = item
                try:
                    future.set_result(job())
                except Exception as exc:  # a failed job never stops the worker
                    log.exception("background job failed")
                    future.set_exception(exc)
            finally:
                self._queue.task_done()

    def submit(self, job: Callable[[], Any]) -> Future[Any]:
        """Queue a job and return at once."""
        future: Future[Any] = Future()
        self._ensure_started()
        self._queue.put((job, future))
        return future

    def call(self, job: Callable[[], Any], timeout: float | None = None) -> Any:
        """Run a job on the worker thread and wait for its result (or its exception)."""
        return self.submit(job).result(timeout=timeout)

    def join(self) -> None:
        """Wait until every queued job is done (tests)."""
        self._queue.join()

    def stop(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._queue.put(None)
            self._thread.join(timeout=5)
