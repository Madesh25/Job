"""/end for long runs (/fetch, /screen, /autopilot): the run stops at its next safe point.

A long run calls `CONTROL.check()` at its safe points (every progress step); after /end it
raises Stopped there. What is already written (Notion rows, resumes) stays. Stopped is a
BaseException, so the `except Exception` blocks that keep one failing source or job from
stopping a run do not swallow it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager


class Stopped(BaseException):
    """Raised at a safe point after /end."""


class Control:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.running: str | None = None  # "/fetch" while a /fetch runs
        self.started = ""  # "10:02"

    @contextmanager
    def job(self, name: str) -> Iterator[None]:
        with self._lock:
            self.running, self.started = name, time.strftime("%H:%M")
            self._stop.clear()
        try:
            yield
        finally:
            with self._lock:
                self.running = None
                self._stop.clear()

    def request_stop(self) -> str | None:
        """Ask the running job to stop; return its name, or None when nothing runs."""
        with self._lock:
            if self.running is None:
                return None
            self._stop.set()
            return self.running

    def stopping(self) -> bool:
        return self._stop.is_set()

    def check(self) -> None:
        if self._stop.is_set():
            raise Stopped(self.running or "run")


CONTROL = Control()


def end_text(control: Control = CONTROL) -> str:
    """The /end reply."""
    name = control.request_stop()
    if name is None:
        return "Nothing is running."
    return (f"Stopping {name} (started {control.started}) at its next step. What is done so "
            "far is kept.")
