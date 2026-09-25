from datetime import date

from jobengine.config_store import ConfigStore
from jobengine.sweep.gate import strategy_gate

TODAY = date(2026, 10, 1)


def config(updated, days="30"):
    return ConfigStore.from_values(
        {"last_strategy_update": updated, "strategy_refresh_days": days}
    )


def test_open_at_thirty_days():
    assert strategy_gate(config("2026-09-01 (initial setup)"), TODAY) is None


def test_blocked_at_thirty_one_days():
    message = strategy_gate(config("2026-08-31"), TODAY)
    assert message == (
        "/fetch is blocked: strategy last updated 2026-08-31, older than 30 days. "
        "Run /update first."
    )


def test_blocked_when_config_missing_or_invalid():
    assert "missing or invalid" in strategy_gate(ConfigStore.from_values({}), TODAY)
    assert "missing or invalid" in strategy_gate(config("soon", "30"), TODAY)
    assert "missing or invalid" in strategy_gate(config("2026-09-30", "thirty"), TODAY)
