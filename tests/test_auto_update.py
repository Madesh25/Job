"""Monthly /update by itself (Autopilot I5, 26 Oct; Friday evening since 5 Nov): the bot runs
the research once and sends the tips with Adopt / Reject, on the Friday evening before the
/fetch gate closes, else in its last 2 days. The fake Config says the gate was stamped on
23 Sep for 30 days, so it closes on Friday 23 Oct; Friday 16 Oct is 7 days before."""

from datetime import UTC, date, datetime

from test_telegram_bot import Clock, settings

from jobengine.bot_state import FakeBotState
from jobengine.config_store import ConfigStore
from jobengine.screen.desk import fake_desk
from jobengine.strategy import auto_update
from jobengine.sweep.fakes import FAKE_TODAY

STAMPED = date(2026, 9, 23)


def at(day, hour=2, minute=0):
    """UTC; 02:00 UTC is 07:30 in India, 13:30 UTC is 19:00."""
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def local(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute)


def test_on_the_friday_evening_before_the_gate_closes_once_per_period():
    config = ConfigStore.fake()
    state = FakeBotState()
    assert not auto_update.due(config, state, STAMPED, local(15, 20), False)  # Thursday
    assert not auto_update.due(config, state, STAMPED, local(16, 18, 59), False)  # too early
    assert auto_update.due(config, state, STAMPED, local(16, 19), False)  # Friday 19:00
    assert not auto_update.due(config, state, STAMPED, local(16, 19), True)  # review open
    assert not auto_update.due(config, state, STAMPED, local(9, 20), False)  # 14 days left
    auto_update.mark(state, STAMPED, date(2026, 10, 16), ok=True)
    assert not auto_update.due(config, state, STAMPED, local(22, 9), False)  # done this period
    # The next period (the review stamped 20 Oct, closing Thu 19 Nov): Friday 13 Nov.
    nxt = date(2026, 10, 20)
    assert auto_update.due(config, state, nxt, datetime(2026, 11, 13, 19, 30), False)
    assert not auto_update.due(config, state, nxt, datetime(2026, 11, 6, 19, 30), False)


def test_a_missed_or_failed_friday_runs_in_the_last_two_days():
    config = ConfigStore.fake()
    state = FakeBotState()
    assert not auto_update.due(config, state, STAMPED, local(20, 9), False)  # 3 days left
    assert not auto_update.due(config, state, STAMPED, local(21, 6, 59), False)
    assert auto_update.due(config, state, STAMPED, local(21, 7), False)  # 2 days left
    auto_update.mark(state, STAMPED, date(2026, 10, 21), ok=False)
    assert not auto_update.due(config, state, STAMPED, local(21, 20), False)  # tried today
    assert auto_update.due(config, state, STAMPED, local(22, 7), False)
    assert auto_update.due(config, state, STAMPED, local(25, 7), False)  # gate closed: still


def test_off_when_config_says_false():
    off = ConfigStore.from_values({"strategy.auto_update": "false",
                                   "strategy_refresh_days": "30"})
    assert not auto_update.due(off, FakeBotState(), STAMPED, local(20, 9), False)
    assert auto_update.is_on(ConfigStore.fake())


def test_intro_says_when_fetch_closes():
    assert "(/fetch closes in 4 days)" in auto_update.intro(4)
    assert "Saturday is a good day for it" in auto_update.intro(4)
    assert "(/fetch closes in 1 day)" in auto_update.intro(1)
    assert "(/fetch closes today)" in auto_update.intro(0)
    assert "(/fetch is blocked)" in auto_update.intro(-2)


def test_the_bot_runs_the_research_once_and_sends_the_tips():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d._track_now = lambda: at(16, 13, 30)  # Friday 16 Oct, 19:00 in India: 7 days left
    replies = d.auto_update_tick()
    assert replies[0].text.startswith("🤖 Monthly strategy research, run by itself (/fetch "
                                      "closes in 7 days). Adopt or reject each tip below")
    assert replies[1].text.startswith("Research done: ")
    assert any(b[0] == "Adopt" for r in replies for b in r.buttons or [])
    assert d.auto_update_tick() == []
    d._track_now = lambda: at(21)
    assert d.auto_update_tick() == []  # the review is open: /update shows the tips again


def test_not_while_a_review_you_started_is_open():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d.update_command("")
    d._track_now = lambda: at(16, 13, 30)
    assert d.auto_update_tick() == []


def test_research_that_cannot_run_says_so_and_tries_tomorrow():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d.strategy.llm = lambda config: None  # no ANTHROPIC_API_KEY
    d._track_now = lambda: at(16, 13, 30)
    replies = d.auto_update_tick()
    assert replies[0].text == auto_update.FAILED
    assert replies[1].text.startswith("/update cannot research")
    assert d.auto_update_tick() == []
    d._track_now = lambda: at(19)  # 4 days left: not yet the last two days
    assert d.auto_update_tick() == []
    d._track_now = lambda: at(21)
    assert d.auto_update_tick()[0].text == auto_update.FAILED
