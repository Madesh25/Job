from datetime import date

from jobengine.config_store import ConfigStore


def test_fake_config_reads_fixture():
    config = ConfigStore.fake()
    assert config.get("model.score") == "claude-haiku-4-5"
    assert config.get_int("strategy_refresh_days") == 30
    assert config.get_date_prefix("last_strategy_update") == date(2026, 9, 23)
    assert "large:" in config.notes("employer.size_rules")
    assert config.all()["countries.active"] == "Poland, Netherlands, Ireland"


def test_missing_and_invalid_values():
    config = ConfigStore.from_values({"a": "  ", "n": "abc", "d": "soon", "x": "7 days"})
    assert config.get("a") is None and config.get("a", "dflt") == "dflt"
    assert config.get("missing") is None
    assert config.get_int("n", 5) == 5
    assert config.get_int("x") == 7
    assert config.get_date_prefix("d") is None
    assert config.get_date_prefix("missing") is None
    assert "a" in config and "missing" not in config
