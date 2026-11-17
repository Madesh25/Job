"""Claude as the safety net only (27 Oct, your decision): DeepSeek does the work, Claude
answers only when DeepSeek fails, at most Config ai.claude_cap a month ($5), and the AI watch
tells you when DeepSeek keeps failing."""

from datetime import date, datetime

import pytest
from test_telegram_bot import Clock, prod_settings, settings

from jobengine import llm
from jobengine.bot_state import FakeBotState
from jobengine.config_store import ConfigStore
from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.track import cost_guard, costs

TODAY = date(2026, 10, 27)
CAP = ConfigStore.from_values({"ai.claude_cap": "5"})


def test_the_month_is_counted_per_provider():
    state = FakeBotState()
    costs.record(state, TODAY, "score", 0.02, "deepseek")
    costs.record(state, TODAY, "resume", 1.5)  # Claude
    costs.record(state, TODAY, "score", 0.5, "claude")
    assert costs.provider_spent(state, TODAY, "claude") == 2.0
    assert costs.provider_spent(state, TODAY, "deepseek") == 0.02
    assert costs.provider_spent(state, date(2026, 11, 1), "claude") == 0.0
    sink = costs.sink(state, lambda: TODAY)
    sink("cover", 1.0, "gemini")
    sink("cover", 1.0)  # an old caller: Claude
    assert costs.provider_spent(state, TODAY, "gemini") == 1.0
    assert costs.provider_spent(state, TODAY, "claude") == 3.0


def test_at_the_cap_only_claude_stops():
    state = FakeBotState()
    costs.record(state, TODAY, "resume", 5.01)
    why = cost_guard.blocked(CAP, state, TODAY, "resume", "claude")
    assert why.startswith("Claude paused: this month's Claude cost (about $5.01) reached "
                          "Config ai.claude_cap ($5.00). DeepSeek keeps working")
    assert cost_guard.blocked(CAP, state, TODAY, "resume", "any") is None  # DeepSeek may run
    assert cost_guard.blocked(CAP, state, TODAY, "reply", "claude") is None  # replies read
    assert cost_guard.blocked(ConfigStore.fake(), state, TODAY, "resume", "claude") is None


class FailingDeepSeek:
    def complete_json(self, stage, system, user, **kwargs):
        raise llm.LLMError("DeepSeek call failed: HTTP 402 Insufficient Balance")


class Claude:
    usage = llm.Usage()

    def __init__(self):
        self.calls = 0

    def complete_json(self, *args, **kwargs):
        self.calls += 1
        return {"ok": True}


def test_a_failed_deepseek_call_goes_to_claude_until_its_cap(monkeypatch):
    state = FakeBotState()
    claude = Claude()
    config = ConfigStore.from_values({"ai.claude_cap": "5", "model.score": "deepseek-v4-flash"})
    router = llm.ModelRouter(prod_settings(), config, claude=claude, deepseek=FailingDeepSeek())
    monkeypatch.setattr(llm, "_cost_guard", cost_guard.guard(state, lambda: TODAY))
    llm.take_fallbacks()
    assert router.complete_json("score", "s", "u") == {"ok": True}
    [(when, stage, model, error)] = llm.take_fallbacks()
    assert (stage, model) == ("score", "deepseek-v4-flash")
    assert error == "DeepSeek call failed: HTTP 402 Insufficient Balance"
    costs.record(state, TODAY, "score", 5.0)
    with pytest.raises(llm.LLMPausedError, match="Claude paused"):
        router.complete_json("score", "s", "u")
    assert claude.calls == 1


def test_claude_alerts_once_at_80_and_at_100():
    state = FakeBotState()
    costs.record(state, TODAY, "score", 3.9)
    assert cost_guard.claude_alert(CAP, state, TODAY) is None
    costs.record(state, TODAY, "score", 0.2)
    assert cost_guard.claude_alert(CAP, state, TODAY).startswith(
        "🛟 Claude (the safety net) cost about $4.10 of its $5.00 for October")
    assert cost_guard.claude_alert(CAP, state, TODAY) is None
    costs.record(state, TODAY, "score", 1.0)
    assert cost_guard.claude_alert(CAP, state, TODAY).startswith(
        "⛔ Claude paused for the rest of October: about $5.10 used of $5.00")
    assert cost_guard.claude_health_line(CAP, state, TODAY)[1].startswith("Claude paused")


def test_the_ai_watch_says_what_failed_at_most_every_3_hours():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d.deps.config = lambda: CAP
    llm.take_fallbacks()
    llm.note_fallback("score", "deepseek-v4-flash", "HTTP 503")
    llm.note_fallback("score", "deepseek-v4-flash", "HTTP 503")
    assert d.ai_cap_tick() == []  # 2: not yet
    llm.note_fallback("resume", "deepseek-v4-flash", "broken JSON twice")
    [reply] = d.ai_cap_tick()
    assert reply.text.startswith("🛟 AI watch: deepseek-v4-flash failed 3 times since ")
    assert "(screening 2, resumes 1). Claude answered them instead. Claude this month: about " \
           "$0.00 of $5.00." in reply.text
    assert "Last error: broken JSON twice" in reply.text
    for _ in range(3):
        llm.note_fallback("score", "deepseek-v4-flash", "HTTP 503")
    assert d.ai_cap_tick() == []  # within 3 hours: kept for the next message


def test_watch_text_when_claude_is_paused():
    fails = [(datetime(2026, 10, 27, 9, 10), "people", "gemini-3.5-flash-lite", "")] * 3
    text = cost_guard.watch_text(fails, fails[0][0], 5.2, 5.0, True)
    assert text.startswith("🛟 AI watch: gemini-3.5-flash-lite failed 3 times since 09:10 "
                           "(contact search 3). Claude is paused")
    assert "Last error: no error text" in text
