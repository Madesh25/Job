"""Run Log (18 Nov, your request): one Notion row per scheduled run, so the days after go-live
show what ran, when, how long, how it went and what it did.

A run is one scheduled step on your PC (telegram_bot._scheduled: Autopilot, Mail queue ...)
or one Cloud Run task (web/app.py: /tasks/autopilot ...). While it runs, the numbers the day
ledger records (new jobs, screened, applied, mails sent, AI cost) and the warning and error
lines the bot logs are tallied for it in memory, so the Run Log costs no Notion read. When the
run ends, one row is written: Result OK, Problems (an error was logged) or Failed (the step
stopped with an error), its seconds, its numbers, the first lines of its report and why it
failed. A run that did nothing (most ticks: "not due yet") writes no row. Keys, emails, tokens
and the query part of links are blanked. Writing the row never fails the run.

Two runs at the same moment (a /fetch you send while a scheduled step runs) both count the
other's numbers: the Run Log is a record of the day, not an accounting.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

log = logging.getLogger("jobengine.track")

SUMMARY_LINES = 4
SUMMARY_CHARS = 600
FAILURE_CHARS = 600

# Cloud Run task names (web/app.py) -> the Step the Run Log shows.
TASK_STEPS = {
    "daily": "Daily check", "digest": "Weekly digest", "sweep": "Fetch",
    "autopilot": "Autopilot", "mail": "Mail queue", "replies": "Reply check",
    "check": "Afternoon check", "report": "Daily report", "nudge": "Apply nudge",
    "apply": "Auto-apply", "health": "Morning check",
}

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_LINK_TAIL = re.compile(r"(https?://[^\s?#]+)[?#][^\s)]*")
_SECRET = re.compile(r"\b(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{32,}\b")
_BEARER = re.compile(r"(?i)\b(bearer\s+|(?:api_?)?(?:token|key|secret|password)\s*[=:]\s*)\S+")


def redact(text: str) -> str:
    """Text safe for Notion: no email address, no key or token, no link query."""
    text = _LINK_TAIL.sub(r"\1", str(text or ""))
    text = _EMAIL.sub("<email>", text)
    text = _BEARER.sub(r"\1<hidden>", text)
    return _SECRET.sub("<hidden>", text)


def where() -> str:
    """Cloud Run sets K_SERVICE in every container it starts."""
    return "Cloud Run" if os.environ.get("K_SERVICE") else "PC"


@dataclass
class Run:
    step: str
    started: datetime
    clock: float = field(default_factory=time.monotonic)
    new_jobs: int = 0
    screened: int = 0
    applied: int = 0
    mails_sent: int = 0
    sites_blocked: int = 0
    cost: float = 0.0
    errors: int = 0
    warnings: int = 0
    texts: list[str] = field(default_factory=list)
    # The full report for the row's page (23 Nov: Telegram gets the short /fetch summary,
    # the long one is kept here); empty: the page holds what was sent (texts).
    details: list[str] = field(default_factory=list)
    failure: str = ""

    def note(self, replies: Sequence[Any] | None) -> None:
        """What the run reported (Reply objects or plain text)."""
        for reply in replies or []:
            text = getattr(reply, "text", reply)
            if isinstance(text, str) and text.strip():
                self.texts.append(text)

    def fail(self, exc: BaseException | str) -> None:
        self.failure = redact(str(exc) or type(exc).__name__)[:FAILURE_CHARS]

    def did_something(self) -> bool:
        return bool(self.failure or self.errors or self.texts or self.new_jobs or self.screened
                    or self.applied or self.mails_sent or self.sites_blocked or self.cost)

    def result(self) -> str:
        if self.failure:
            return "Failed"
        return "Problems" if self.errors else "OK"

    def summary(self) -> str:
        lines = [line.strip() for text in self.texts for line in text.splitlines()
                 if line.strip()]
        return redact("\n".join(lines[:SUMMARY_LINES]))[:SUMMARY_CHARS]

    def body(self) -> list[str]:
        """The row's page text: the full report, one line per paragraph, keys and emails
        blanked."""
        return [redact(line.rstrip()) for text in (self.details or self.texts)
                for line in text.splitlines() if line.strip()]

    def row(self, ended: float | None = None) -> dict[str, Any]:
        seconds = round((ended if ended is not None else time.monotonic()) - self.clock)
        return {
            "Run": name(self),
            "Started": self.started.isoformat(timespec="seconds"),
            "Step": self.step, "Where": where(), "Result": self.result(),
            "Seconds": seconds, "New jobs": self.new_jobs, "Screened": self.screened,
            "Applied": self.applied, "Mails sent": self.mails_sent,
            "Sites blocked": self.sites_blocked,
            "AI cost": round(self.cost, 4), "Errors": self.errors, "Warnings": self.warnings,
            "Summary": self.summary(), "Failure": self.failure,
        }


_lock = threading.Lock()
_active: list[Run] = []
_local = threading.local()


def _each(change: Callable[[Run], None]) -> None:
    with _lock:
        for run in _active:
            change(run)


def bump(name: str, n: float = 1) -> None:
    """A day ledger count (track/day_ledger.py) of the runs going on now."""
    def change(run: Run) -> None:
        if name.startswith("screened."):
            run.screened += int(n)
        elif name == "applied":
            run.applied += int(n)
        elif name == "mails_sent":
            run.mails_sent += int(n)
        elif name == "new_jobs":
            run.new_jobs += int(n)
        elif name == "sites_blocked":
            run.sites_blocked += int(n)
        elif name == "cost":
            run.cost += float(n)

    _each(change)


def note(replies: Sequence[Any] | None) -> None:
    """What the run on this thread reported (a Cloud Run task sends it to Telegram)."""
    run = getattr(_local, "run", None)
    if run is not None:
        run.note(replies)


def details(texts: Sequence[str]) -> None:
    """The full report of the run on this thread, for its Run Log page (not sent)."""
    run = getattr(_local, "run", None)
    if run is not None:
        run.details.extend(t for t in texts if t and t.strip())


def name(run: Run) -> str:
    """The row's name in Run Log, as written ("/fetch 10:18")."""
    return f"{run.step} {run.started.strftime('%H:%M')}"


class Counter(logging.Handler):
    """Counts the bot's warning and error lines for the runs going on now."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        if not record.name.startswith("jobengine"):
            return
        if str(record.msg).startswith("DRY RUN"):  # the dry run doing what it should
            return
        if record.levelno >= logging.ERROR:
            _each(lambda run: setattr(run, "errors", run.errors + 1))
        else:
            _each(lambda run: setattr(run, "warnings", run.warnings + 1))


def install(root: logging.Logger | None = None) -> Counter:
    """Add the counter once (the bot and the web service call this at start)."""
    root = root or logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, Counter):
            return handler
    counter = Counter()
    root.addHandler(counter)
    return counter


def write(table: Any, run: Run) -> None:
    """The run's row, when it did something and the Run Log is writable here."""
    if table is None or not run.did_something():
        return
    try:
        lines = run.body()
        if lines:
            table.create(run.row(), lines)
        else:
            table.create(run.row())
    except Exception as exc:  # the Run Log never fails the run it records
        log.info("run log row not written for %s: %s", run.step, exc)


@contextmanager
def recorded(step: str, table: Any, now: Callable[[], datetime]) -> Iterator[Run]:
    """Record one run: its numbers while it runs, its row when it ends. An exception is
    recorded as Failed and raised again."""
    try:
        started = now()
    except Exception:  # a clock that needs Notion must not stop the run
        started = datetime.now().astimezone()
    run = Run(step, started)
    outer = getattr(_local, "run", None)
    _local.run = run
    with _lock:
        _active.append(run)
    try:
        yield run
    except BaseException as exc:
        run.fail(exc)
        raise
    finally:
        with _lock:
            _active[:] = [other for other in _active if other is not run]
        _local.run = outer
        write(table, run)
        _score(run)
        flush_errors()


def _score(run: Run) -> None:
    """The daily scorecard (S1): a run that failed or logged an error is named there, and
    today's row is brought up to date."""
    from jobengine.track import day_ledger, scorecard

    problem = run.result() != "OK"
    if problem:
        day_ledger.note_problem(name(run))
    if problem or run.did_something():  # an idle tick does not write to Notion
        scorecard.refresh()


def flush_errors() -> None:
    """The Error Log's rows of what was logged up to now (track/error_log.py)."""
    from jobengine.track import error_log

    try:
        error_log.flush()
    except Exception as exc:  # never fails the run
        log.info("error log not written: %s", exc)


def wrap(step: str, body: Callable[[], Any], table: Any,
         now: Callable[[], datetime]) -> Callable[[], Any]:
    """`body` recorded as one run each time it is called (Cloud Run task bodies)."""
    def run_body() -> Any:
        with recorded(step, table, now) as run:
            result = body()
            if isinstance(result, dict) and result.get("ok") is False and result.get("error"):
                run.fail(str(result["error"]))  # a task that caught its own failure
            return result
    return run_body
