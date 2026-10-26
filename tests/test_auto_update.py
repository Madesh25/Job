"""Monthly /update by itself (Autopilot I5, 26 Oct): 5 days before the /fetch gate closes the
bot runs the research once and sends the tips with Adopt / Reject. The fake Config says the
gate was stamped on 23 Sep for 30 days, so it closes on 23 Oct."""

from datetime import UTC, date, datetime

from test_telegram_bot import Clock, settings

from jobengine.bot_state import FakeBotState
from jobengine.config_store import ConfigStore
from jobengine.screen.desk import fake_desk
from jobengine.strategy import auto_update
from jobengine.sweep.fakes import FAKE_TODAY

STAMPED = date(2026, 9, 23)


def at(day, hour=2, minute=0):
    """UTC; 02:00 UTC is 07:30 in India."""
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def local(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute)


def test_due_from_five_days_before_once_per_gate_period():
    config = ConfigStore.fake()
    state = FakeBotState()
    assert not auto_update.due(config, state, STAMPED, local(17, 9), False)  # 6 days left
    assert not auto_update.due(config, state, STAMPED, local(18, 6, 59), False)  # too early
    assert auto_update.due(config, state, STAMPED, local(18, 7), False)  # 5 days left
    assert not auto_update.due(config, state, STAMPED, local(18, 7), True)  # review open
    auto_update.mark(state, STAMPED, date(2026, 10, 18), ok=True)
    assert not auto_update.due(config, state, STAMPED, local(25, 9), False)  # done this period
    # The next period (the review stamped 20 Oct) runs again 5 days before it closes.
    assert auto_update.due(config, state, date(2026, 10, 20), datetime(2026, 11, 14, 9), False)


def test_a_failed_run_tries_again_the_next_day():
    config = ConfigStore.fake()
    state = FakeBotState()
    auto_update.mark(state, STAMPED, date(2026, 10, 19), ok=False)
    assert not auto_update.due(config, state, STAMPED, local(19, 20), False)
    assert auto_update.due(config, state, STAMPED, local(20, 7), False)


def test_off_when_config_says_false():
    off = ConfigStore.from_values({"strategy.auto_update": "false",
                                   "strategy_refresh_days": "30"})
    assert not auto_update.due(off, FakeBotState(), STAMPED, local(20, 9), False)
    assert auto_update.is_on(ConfigStore.fake())


def test_intro_says_when_fetch_closes():
    assert "(/fetch closes in 4 days)" in auto_update.intro(4)
    assert "(/fetch closes in 1 day)" in auto_update.intro(1)
    assert "(/fetch closes today)" in auto_update.intro(0)
    assert "(/fetch is blocked)" in auto_update.intro(-2)


def test_the_bot_runs_the_research_once_and_sends_the_tips():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d._track_now = lambda: at(19)  # 4 days left
    replies = d.auto_update_tick()
    assert replies[0].text.startswith("🤖 Monthly strategy research, run by itself (/fetch "
                                      "closes in 4 days). Adopt or reject each tip below")
    assert replies[1].text.startswith("Research done: ")
    assert any(b[0] == "Adopt" for r in replies for b in r.buttons or [])
    assert d.auto_update_tick() == []
    d._track_now = lambda: at(20)
    assert d.auto_update_tick() == []  # the review is open: /update shows the tips again


def test_not_while_a_review_you_started_is_open():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d.update_command("")
    d._track_now = lambda: at(19)
    assert d.auto_update_tick() == []


def test_research_that_cannot_run_says_so_and_tries_tomorrow():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d.strategy.llm = lambda config: None  # no ANTHROPIC_API_KEY
    d._track_now = lambda: at(19)
    replies = d.auto_update_tick()
    assert replies[0].text == auto_update.FAILED
    assert replies[1].text.startswith("/update cannot research")
    assert d.auto_update_tick() == []
    d._track_now = lambda: at(20)
    assert d.auto_update_tick()[0].text == auto_update.FAILED
