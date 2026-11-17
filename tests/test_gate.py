from datetime import date

from jobengine.bot_state import FakeBotState
from jobengine.config_store import ConfigStore
from jobengine.settings import load_settings
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


# ---------------------------------------------------------------- Module 08: bot_state stamp


def test_dev_gate_reads_a_newer_bot_state_stamp():
    state = FakeBotState()
    state.set("last_strategy_update", {"date": "2026-09-25"})
    dev = load_settings("dev", {})
    stale = config("2026-08-01")
    assert strategy_gate(stale, TODAY) is not None
    assert strategy_gate(stale, TODAY, state, dev) is None
    state.set("last_strategy_update", {"date": "2026-07-01"})  # older than Config: ignored
    assert strategy_gate(config("2026-09-20"), TODAY, state, dev) is None


def test_prod_gate_ignores_bot_state():
    state = FakeBotState()
    state.set("last_strategy_update", {"date": "2026-09-25"})
    prod = load_settings("prod", {})
    assert strategy_gate(config("2026-08-01"), TODAY, state, prod) is not None
