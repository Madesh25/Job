"""3 Nov (your decision): the first 5,000 Google searches of a month are free and no longer
counted at the paid price, and Gemini has its own monthly cap, Config ai.gemini_cap: past it
DeepSeek takes Gemini's work and the Google searches wait for the 1st."""

from datetime import date

import pytest
from test_claude_cap import prod_settings
from test_llm_gemini import Recorder

from jobengine import llm
from jobengine.bot_state import FakeBotState
from jobengine.config_store import ConfigStore
from jobengine.track import cost_guard, costs

TODAY = date(2026, 11, 3)
CAP = ConfigStore.from_values({"ai.gemini_cap": "9.50"})


@pytest.fixture(autouse=True)
def no_sink_or_guard():
    yield
    llm.set_cost_sink(None)
    llm.set_cost_guard(None)


def test_the_first_5000_searches_are_free():
    state = FakeBotState()
    costs.record(state, TODAY, "fetch", 0.001, "gemini", searches=4998)
    assert costs.provider_spent(state, TODAY, "gemini") == pytest.approx(0.001)
    costs.record(state, TODAY, "fetch", 0.001, "gemini", searches=5)  # 3 of them paid
    assert costs.searches_used(state, TODAY) == 5003
    assert costs.provider_spent(state, TODAY, "gemini") == pytest.approx(
        0.002 + 3 * llm.GEMINI_SEARCH_DOLLARS)
    costs.record(state, TODAY, "people", 0.0, "gemini", searches=2)  # all paid now
    assert state.get(costs.KEY)["total"] == pytest.approx(0.002 + 5 * llm.GEMINI_SEARCH_DOLLARS)
    assert costs.searches_used(state, date(2026, 12, 1)) == 0  # a new month starts free
    assert "Google searches: 5,005 (5,000 a month are free)." in costs.month_line(state, TODAY)


def test_paid_searches_counts_only_past_the_free_ones():
    assert costs.paid_searches(0, 10) == 0
    assert costs.paid_searches(4995, 10) == 5
    assert costs.paid_searches(6000, 10) == 10


def test_a_gemini_search_sends_its_searches_to_the_month():
    state = FakeBotState()
    llm.set_cost_sink(costs.sink(state, lambda: TODAY))
    llm.record_cost("fetch", 0.0004, llm.GEMINI, searches=3)
    llm.record_cost("score", 0.0001, llm.DEEPSEEK)  # an old-style call still works
    value = state.get(costs.KEY)
    assert value["searches"] == 3 and value["calls"] == 2
    assert value["total"] == pytest.approx(0.0005)


def test_at_the_cap_only_gemini_stops():
    state = FakeBotState()
    costs.record(state, TODAY, "fetch", 9.50, "gemini")
    why = cost_guard.blocked(CAP, state, TODAY, "fetch", "gemini")
    assert why.startswith("Gemini paused: this month's Gemini cost (about $9.50) reached "
                          "Config ai.gemini_cap ($9.50). DeepSeek does its other work")
    assert cost_guard.blocked(CAP, state, TODAY, "reply", "gemini") == why  # DeepSeek reads
    assert cost_guard.blocked(CAP, state, TODAY, "score", "any") is None
    assert cost_guard.blocked(CAP, state, TODAY, "score", "claude") is None
    assert cost_guard.blocked(ConfigStore.fake(), state, TODAY, "fetch", "gemini") is None
    costs.record(state, TODAY, "score", 20.0, "deepseek")  # other providers do not count
    assert not cost_guard.gemini_paused(
        ConfigStore.from_values({"ai.gemini_cap": "10"}), state, TODAY)


def router(config, state):
    claude, gemini, deepseek = Recorder(), Recorder(), Recorder()
    llm.set_cost_guard(cost_guard.guard(state, lambda: TODAY))
    route = llm.ModelRouter(prod_settings(), ConfigStore.from_values(config), claude=claude,
                            gemini=gemini, deepseek=deepseek)
    return route, claude, gemini, deepseek


def test_past_the_cap_deepseek_answers_and_searches_wait():
    state = FakeBotState()
    route, claude, gemini, deepseek = router(
        {"ai.gemini_cap": "9.50", "model.score": "gemini-3.5-flash-lite",
         "model.people": "gemini-3.5-flash-lite"}, state)
    assert route.complete_json("score", "s", "u") == {"from": "gemini-3.5-flash-lite"}
    costs.record(state, TODAY, "people", 9.60, "gemini")
    assert route.complete_json("score", "s", "u") == {"from": llm.GEMINI_PAUSED_MODEL}
    with pytest.raises(llm.LLMPausedError, match="Gemini paused"):
        route.complete_json_with_search("people", "s", "u")
    with pytest.raises(llm.LLMPausedError, match="Gemini paused"):
        route.complete_json("people", "s", "u")  # DeepSeek may not run a search stage
    assert gemini.calls == [("score", "gemini-3.5-flash-lite")]
    assert deepseek.calls == [("score", llm.GEMINI_PAUSED_MODEL)]
    assert claude.calls == []  # Claude's own cap is not spent on Gemini's work


def test_gemini_alerts_once_at_80_and_at_100():
    state = FakeBotState()
    costs.record(state, TODAY, "people", 7.0, "gemini")
    assert cost_guard.gemini_alert(CAP, state, TODAY) is None
    costs.record(state, TODAY, "people", 0.7, "gemini")
    assert cost_guard.gemini_alert(CAP, state, TODAY).startswith(
        "💳 Gemini cost about $7.70 of its $9.50 for November")
    assert cost_guard.gemini_alert(CAP, state, TODAY) is None
    assert cost_guard.gemini_health_line(CAP, state, TODAY) == (
        "soon", "Gemini used about $7.70 of $9.50 this month.")
    costs.record(state, TODAY, "people", 2.0, "gemini")
    assert cost_guard.gemini_alert(CAP, state, TODAY).startswith(
        "⛔ Gemini paused for the rest of November: about $9.70 used of $9.50")
    assert cost_guard.gemini_health_line(CAP, state, TODAY)[1].startswith("Gemini paused")
    assert cost_guard.gemini_alert(ConfigStore.fake(), state, TODAY) is None
