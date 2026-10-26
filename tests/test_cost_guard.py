"""AI cost guard (Autopilot I6, 26 Oct): Config ai.monthly_cap warns at 80% and pauses the paid
AI at 100% until the 1st, or until you allow it for the rest of the month."""

from datetime import UTC, date, datetime

import pytest
from test_telegram_bot import Clock, settings

from jobengine import llm
from jobengine.bot_state import FakeBotState
from jobengine.config_store import ConfigStore
from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.track import cost_guard, costs

TODAY = date(2026, 10, 26)
CAP = ConfigStore.from_values({"ai.monthly_cap": "$10"})


def spend(state, dollars, day=TODAY):
    costs.record(state, day, "score", dollars)


def test_cap_reads_dollars_and_empty_means_none():
    assert cost_guard.cap(CAP) == 10.0
    assert cost_guard.cap(ConfigStore.from_values({"ai.monthly_cap": "7,5"})) == 7.5
    assert cost_guard.cap(ConfigStore.from_values({"ai.monthly_cap": "0"})) is None
    assert cost_guard.cap(ConfigStore.fake()) is None


def test_paused_at_the_cap_but_replies_are_still_read():
    state = FakeBotState()
    spend(state, 9.99)
    assert cost_guard.blocked(CAP, state, TODAY, "score") is None
    spend(state, 0.02)
    why = cost_guard.blocked(CAP, state, TODAY, "resume")
    assert why.startswith("AI paused: this month's AI cost (about $10.01) reached Config "
                          "ai.monthly_cap ($10.00).")
    assert cost_guard.blocked(CAP, state, TODAY, "reply") is None
    assert cost_guard.blocked(ConfigStore.fake(), state, TODAY, "resume") is None  # no cap
    # A new month starts again from zero.
    assert cost_guard.blocked(CAP, state, date(2026, 11, 1), "resume") is None


def test_allow_lifts_the_pause_for_this_month_only():
    state = FakeBotState()
    spend(state, 12)
    cost_guard.allow(state, TODAY)
    assert cost_guard.blocked(CAP, state, TODAY, "resume") is None
    spend(state, 12, date(2026, 11, 2))
    assert cost_guard.blocked(CAP, state, date(2026, 11, 2), "resume")


def test_the_router_refuses_a_paused_call(monkeypatch):
    calls = []

    class Claude:
        usage = llm.Usage()

        def complete_json(self, *args, **kwargs):
            calls.append(args)
            return {}

    router = llm.ModelRouter(settings(), CAP, claude=Claude())
    monkeypatch.setattr(llm, "_cost_guard", lambda stage, config: "AI paused: test")
    with pytest.raises(llm.LLMPausedError, match="AI paused: test"):
        router.complete_json("score", "s", "u")
    with pytest.raises(llm.LLMError):
        router.submit_batch("score", "s", [("a", "b")])
    monkeypatch.setattr(llm, "_cost_guard", lambda stage, config: None)
    assert router.complete_json("score", "s", "u") == {}
    monkeypatch.setattr(llm, "_cost_guard", lambda stage, config: 1 / 0)  # a broken check
    assert router.complete_json("score", "s", "u") == {} and len(calls) == 2


def test_alerts_once_at_80_and_once_at_100():
    state = FakeBotState()
    spend(state, 7)
    assert cost_guard.alert(CAP, state, TODAY) is None
    spend(state, 1.5)
    text, button = cost_guard.alert(CAP, state, TODAY)
    assert text.startswith("💸 AI cost: about $8.50 of your $10.00 cap for October (85%).")
    assert not button
    assert cost_guard.alert(CAP, state, TODAY) is None
    spend(state, 2)
    text, button = cost_guard.alert(CAP, state, TODAY)
    assert text.startswith("⛔ AI paused: about $10.50 used, your cap for October is $10.00")
    assert button
    assert cost_guard.alert(CAP, state, TODAY) is None


def test_morning_check_lines():
    state = FakeBotState()
    assert cost_guard.health_line(CAP, state, TODAY) is None
    spend(state, 8)
    assert cost_guard.health_line(CAP, state, TODAY) == (
        "soon", "AI cost at 80% of the October cap (Config ai.monthly_cap).")
    spend(state, 3)
    kind, text = cost_guard.health_line(CAP, state, TODAY)
    assert kind == "blocker" and text.startswith("AI paused: the October cap")


def test_the_bot_sends_the_pause_with_the_allow_button():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d._track_now = lambda: datetime(2026, 10, 26, 9, tzinfo=UTC)
    d.deps.config = lambda: CAP
    spend(d.state, 11)
    replies = d.ai_cap_tick()
    assert replies[0].buttons == [("Allow AI for the rest of October", "ai:on")]
    assert d.ai_cap_tick() == []
    assert d.tap("ai:on")[0].text.startswith("✅ AI allowed for the rest of October.")
    assert cost_guard.blocked(CAP, d.state, TODAY, "resume") is None
