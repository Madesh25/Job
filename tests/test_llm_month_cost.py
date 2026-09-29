"""PR 18: the month's Claude API cost in the weekly digest (track/costs.py)."""

import threading
from datetime import date

import pytest
from test_llm import CONFIG, FakeSDK, reply, settings
from test_telegram_bot import Clock
from test_telegram_bot import settings as bot_settings

from jobengine import llm
from jobengine.bot_state import FakeBotState
from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.track import costs

OCT = date(2026, 10, 10)


@pytest.fixture(autouse=True)
def no_sink():
    yield
    llm.set_cost_sink(None)


def test_every_call_adds_to_the_month():
    state = FakeBotState()
    llm.set_cost_sink(costs.sink(state, lambda: OCT))
    sdk = FakeSDK([reply('{"ok": true}'), reply('{"ok": true}')])
    client = llm.AnthropicLLM(settings("dev"), CONFIG, client=sdk)
    client.complete_json("score", "s", "u")
    client.complete_json("tailor", "s", "u")
    value = state.get(costs.KEY)
    # Haiku in dev: 10 tokens in at $1/M plus 5 out at $5/M = $0.000035 a call.
    assert value["month"] == "2026-10" and value["calls"] == 2
    assert value["total"] == pytest.approx(0.00007)
    assert value["total"] == pytest.approx(client.usage.cost)
    assert set(value["stages"]) == {"score", "tailor"}


def test_web_searches_are_counted_on_top():
    state = FakeBotState()
    llm.set_cost_sink(costs.sink(state, lambda: OCT))
    llm.record_cost("strategy", llm.WEB_SEARCH_DOLLARS * 3)
    assert state.get(costs.KEY)["total"] == pytest.approx(0.03)


def test_a_broken_sink_never_fails_a_call():
    def broken(stage, dollars):
        raise RuntimeError("Notion is down")

    llm.set_cost_sink(broken)
    sdk = FakeSDK([reply('{"ok": true}')])
    assert llm.AnthropicLLM(settings(), CONFIG, client=sdk).complete_json("score", "s", "u") == {
        "ok": True}


def test_new_month_starts_from_zero_and_keeps_last_month():
    state = FakeBotState()
    costs.record(state, date(2026, 9, 30), "score", 2.5)
    costs.record(state, date(2026, 10, 1), "tailor", 0.4)
    value = state.get(costs.KEY)
    assert (value["month"], value["total"], value["calls"]) == ("2026-10", 0.4, 1)
    assert value["last"] == {"month": "2026-09", "total": 2.5}


def test_month_line():
    state = FakeBotState()
    assert costs.month_line(state, OCT) == "Claude API in October: nothing used yet."
    costs.record(state, date(2026, 9, 20), "score", 3.1)
    for stage, dollars in (("score", 0.9), ("tailor", 0.6), ("email", 0.001)):
        costs.record(state, OCT, stage, dollars)
    assert costs.month_line(state, OCT) == (
        "Claude API in October so far: about $1.50 in 3 calls (screening and replies $0.90, "
        "resumes $0.60); on pace for about $4.65 this month. Last month: about $3.10. "
        "(Estimate from token counts; the Anthropic console has the exact bill.)")


def test_threads_do_not_lose_calls():
    state = FakeBotState()
    threads = [threading.Thread(target=lambda: [costs.record(state, OCT, "score", 0.01)
                                                for _ in range(50)]) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state.get(costs.KEY)["calls"] == 200


def test_the_weekly_digest_has_the_line():
    d = fake_desk(bot_settings(), FAKE_TODAY, now=Clock())
    d.track.state.set(costs.KEY, {"month": "2026-10", "total": 1.234, "calls": 12,
                                  "stages": {"score": 1.234}, "last": None})
    text = d.digest_command()[0].text
    assert "Claude API in October so far: about $1.23 in 12 calls (screening and replies " \
        "$1.23)" in text
