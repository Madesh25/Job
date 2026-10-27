"""The problems the bot logged, per week, for the weekly audit (27 Oct, your request).

Every WARNING or ERROR line of the bot's own loggers is counted in memory under a short
pattern (numbers, ids and links blanked, so the same problem counts as one). The counts are
written to Bot State `audit.issues` at most every FLUSH_EVERY, by a scheduled tick, so a
burst of warnings never means a burst of Notion writes. Counts of a run that stops before
its next write are lost; the audit says so. Only the last KEEP_WEEKS weeks are kept.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

KEY = "audit.issues"  # bot_state: {"weeks": {"2026-10-26": {"ERROR|jobengine.x|text": n}}}
KEEP_WEEKS = 3
MAX_PATTERNS = 60  # per week, the most frequent
FLUSH_EVERY = timedelta(minutes=30)
TEXT_CHARS = 140

_lock = threading.Lock()
_pending: Counter[str] = Counter()
_last_flush: datetime | None = None

_URL = re.compile(r"https?://\S+")
_HEX = re.compile(r"\b[0-9a-f]{8}(?:-?[0-9a-f]{4}){3}-?[0-9a-f]{12}\b|\b[0-9a-f]{16,}\b", re.I)
_NUM = re.compile(r"\d+")


def week_of(day: date) -> date:
    """The Monday of `day`'s week."""
    return day - timedelta(days=day.weekday())


def pattern(record: logging.LogRecord) -> str:
    """LEVEL|logger|message with links, ids and numbers blanked."""
    try:
        text = record.getMessage()
    except Exception:  # a bad format string still counts
        text = str(record.msg)
    text = _NUM.sub("N", _HEX.sub("<id>", _URL.sub("<link>", text.splitlines()[0] if text
                                                     else "")))
    return f"{record.levelname}|{record.name}|{' '.join(text.split())[:TEXT_CHARS]}"


class Collector(logging.Handler):
    """Counts the bot's warnings and errors in memory."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        if not record.name.startswith("jobengine"):
            return
        with _lock:
            _pending[pattern(record)] += 1


def install(root: logging.Logger | None = None) -> Collector:
    """Add the collector once (the bot calls this at start)."""
    root = root or logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, Collector):
            return handler
    collector = Collector()
    root.addHandler(collector)
    return collector


def pending() -> int:
    with _lock:
        return sum(_pending.values())


def flush(state: Any, today: date, now: datetime, force: bool = False) -> bool:
    """Write the counts gathered since the last write into this week's entry. False when
    nothing was written (nothing new, or the last write was less than FLUSH_EVERY ago)."""
    global _last_flush
    with _lock:
        if not _pending or state is None:
            return False
        if not force and _last_flush is not None and now - _last_flush < FLUSH_EVERY:
            return False
        counts = Counter(_pending)
        _pending.clear()
        _last_flush = now
    try:
        value = state.get(KEY) or {}
        weeks: dict[str, dict[str, int]] = dict(value.get("weeks") or {})
        key = week_of(today).isoformat()
        merged = Counter(weeks.get(key) or {})
        merged.update(counts)
        weeks[key] = dict(merged.most_common(MAX_PATTERNS))
        state.set(KEY, {"weeks": dict(sorted(weeks.items())[-KEEP_WEEKS:])})
        return True
    except Exception:  # the audit is a report: give the counts back for the next try
        with _lock:
            _pending.update(counts)
        logging.getLogger("jobengine.audit").debug("issue counts not written", exc_info=True)
        return False


def week(state: Any, monday: date) -> dict[str, int]:
    value = (state.get(KEY) if state is not None else None) or {}
    return dict((value.get("weeks") or {}).get(monday.isoformat()) or {})


def reset() -> None:
    """Tests only."""
    global _last_flush
    with _lock:
        _pending.clear()
        _last_flush = None
