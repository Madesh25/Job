"""Today's best new jobs, for screening (your decision of 30 Sep).

/fetch saves every job that passes the rules. Its free match score (sweep/rank.py) is kept
here, per page, for today, so /screen takes the best ones first (screening.daily_limit, 30):
they are today's /pending list. The other new rows stay Unscreened in Notion, never lost.
Bot State only: no Notion column is needed.
"""

from __future__ import annotations

from datetime import date
from typing import Any

KEY = "sweep.best_today"  # {"date": "YYYY-MM-DD", "scores": {page id: score}}
MAX_KEPT = 400


def record(state: Any, today: date, scores: dict[str, int]) -> None:
    """Add this sweep's scores to today's (a second /fetch the same day adds to them)."""
    if state is None or not scores:
        return
    value = state.get(KEY) or {}
    kept = dict(value.get("scores") or {}) if value.get("date") == today.isoformat() else {}
    kept.update(scores)
    best = dict(sorted(kept.items(), key=lambda item: -item[1])[:MAX_KEPT])
    state.set(KEY, {"date": today.isoformat(), "scores": best})


def scores(state: Any, today: date) -> dict[str, int]:
    """Today's scores by page id without dashes, or {} (another day, or nothing yet)."""
    value = state.get(KEY) if state is not None else None
    if not value or value.get("date") != today.isoformat():
        return {}
    return {pid.replace("-", ""): int(score) for pid, score in (value.get("scores") or {}).items()}
