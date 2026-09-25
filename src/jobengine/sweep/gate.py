"""Strategy gate (spec section 7): /fetch is blocked when the strategy is stale."""

from __future__ import annotations

from datetime import date

from jobengine.config_store import ConfigStore


def strategy_gate(config: ConfigStore, today: date) -> str | None:
    """None when the sweep may run, otherwise the message explaining why not.

    Reads Config last_strategy_update (leading YYYY-MM-DD) and strategy_refresh_days.
    """
    updated = config.get_date_prefix("last_strategy_update")
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
