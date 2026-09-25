"""Strategy gate (spec section 7): /fetch is blocked when the strategy is stale."""

from __future__ import annotations

from datetime import date
from typing import Any

from jobengine.config_store import ConfigStore

STAMP_KEY = "last_strategy_update"


def last_update(config: ConfigStore, state: Any = None, s: Any = None) -> date | None:
    """Config last_strategy_update. Outside prod, a newer bot_state date (stamped by /update,
    Config is not writable there) wins. In prod only Config counts."""
    updated = config.get_date_prefix(STAMP_KEY)
    if state is None or s is None or s.app_env == "prod":
        return updated
    try:
        stamped = date.fromisoformat(str((state.get(STAMP_KEY) or {}).get("date") or ""))
    except ValueError:
        return updated
    return stamped if updated is None or stamped > updated else updated


def strategy_gate(config: ConfigStore, today: date, state: Any = None,
                  s: Any = None) -> str | None:
    """None when the sweep may run, otherwise the message explaining why not.

    Reads Config last_strategy_update (leading YYYY-MM-DD) and strategy_refresh_days.
    """
    updated = last_update(config, state, s)
    refresh_days = config.get_int("strategy_refresh_days")
    if updated is None or refresh_days is None:
        return (
            "/fetch is blocked: Config last_strategy_update or strategy_refresh_days is "
            "missing or invalid. Run /update first."
        )
    if (today - updated).days > refresh_days:
        return (
            f"/fetch is blocked: strategy last updated {updated.isoformat()}, "
            f"older than {refresh_days} days. Run /update first."
        )
    return None
