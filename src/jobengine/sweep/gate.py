"""Strategy gate (spec section 7): /fetch is blocked when the strategy is stale."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date


def strategy_gate(config: Mapping[str, str], today: date) -> str | None:
    """None when the sweep may run, otherwise the message explaining why not.

    config maps Config keys to values, for example
    {"last_strategy_update": "2026-09-23 (initial setup)", "strategy_refresh_days": "30"}.
    """
    raw_date = (config.get("last_strategy_update") or "").strip()
    raw_days = (config.get("strategy_refresh_days") or "").strip()
    match = re.match(r"(\d{4}-\d{2}-\d{2})", raw_date)
    if not match or not raw_days.isdigit():
        return (
            "/fetch is blocked: Config last_strategy_update or strategy_refresh_days is "
            "missing or invalid. Run /update first."
        )
    updated = date.fromisoformat(match.group(1))
    refresh_days = int(raw_days)
    if (today - updated).days > refresh_days:
        return (
            f"/fetch is blocked: strategy last updated {updated.isoformat()}, "
            f"older than {refresh_days} days. Run /update first."
        )
    return None
