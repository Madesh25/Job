"""3 Nov, your idea: the credit balancer runs DeepSeek's most expensive jobs on Gemini while
this month's Google credit (Config ai.gemini_cap) has room after a reserve for the
searches. A code rule: no AI, no Notion read per call."""

from datetime import date

import pytest
from test_claude_cap import prod_settings
from test_llm_gemini import Recorder

from jobengine import llm
from jobengine.bot_state import FakeBotState
from jobengine.config_store import ConfigStore
from jobengine.track import balancer, cost_guard, costs

DAY = date(2026, 11, 10)  # day 10 of 30: 21 days left counting today
CAP = {"ai.gemini_cap": "9.50"}


@pytest.fixture(autouse=True)
def no_hooks():
    yield
    llm.set_balancer(None)
    llm.set_cost_guard(None)


def config(**values):
    return ConfigStore.from_values({**CAP, **values})


def test_no_cap_or_switched_off_keeps_plan_c():
    state = FakeBotState()
    assert balancer.plan(ConfigStore.fake(), state, DAY).stages == []
    assert balancer.plan(config(**{"ai.balance": "off"}), state, DAY).stages == []
    assert balancer.line(ConfigStore.fake(), state, DAY) == (
        "Credit balancer: off (no Config ai.gemini_cap); plan C (Gemini only for the "
        "searches).")


def test_with_room_the_expensive_jobs_move_first():
    state = FakeBotState()
    costs.record(state, DAY, "people", 1.0, "gemini")  # searches: $2.10 for the rest
    costs.record(state, DAY, "resume", 0.10, "deepseek")  # $0.21 more, x4 on Gemini: $0.84
    costs.record(state, DAY, "score", 0.50, "deepseek")  # $1.05 more, x4 on Gemini: $4.20
    p = balancer.plan(config(), state, DAY)
    assert p.used == pytest.approx(1.0)
    assert p.reserve == pytest.approx(2.10)
    assert p.stages == ["resume", "cover", "apply", "score"]
    assert p.room == pytest.approx(9.50 - 1.0 - 2.10 - 0.84 - 4.20)
    assert balancer.line(config(), state, DAY) == (
        "Credit balancer: resumes, cover letters, form answers, screening run on "
        "gemini-3.5-flash-lite today (Gemini $1.00 of $9.50 this month, $2.10 kept for the "
        "searches).")


def test_a_job_too_big_for_the_room_stays_on_deepseek():
    state = FakeBotState()
    costs.record(state, DAY, "people", 1.0, "gemini")
    costs.record(state, DAY, "score", 1.0, "deepseek")  # x4 for 21 days: $8.40, too big
    costs.record(state, DAY, "tailor", 0.20, "deepseek")  # the old name of resumes
    p = balancer.plan(config(), state, DAY)
    assert p.stages == ["resume", "cover", "apply"]


def test_the_reserve_keeps_the_rest_for_the_searches():
    state = FakeBotState()
    costs.record(state, DAY, "fetch", 4.0, "gemini")  # searches: $8.40 for the rest
    p = balancer.plan(config(), state, DAY)
    assert p.stages == [] and p.room < 0
    assert balancer.line(config(), state, DAY).startswith(
        "Credit balancer: plan C today, the Google credit left is kept for the searches")
    # At least 20% of the cap is kept, even with no search yet.
    assert balancer.plan(config(), FakeBotState(), DAY).reserve == pytest.approx(1.90)


def test_the_plan_is_made_once_an_hour_not_per_call():
    reads = []

    class CountingState(FakeBotState):
        def get(self, key):
            reads.append(key)
            return super().get(key)

    clock = [0.0]
    hook = balancer.Balancer(CountingState(), lambda: DAY, clock=lambda: clock[0])
    assert hook("resume", config()) == "gemini-3.5-flash-lite"
    assert hook("tailor", config()) == "gemini-3.5-flash-lite"
    assert hook("score", config()) == "gemini-3.5-flash-lite"
    assert hook("people", config()) is None  # the searches are Gemini's own job
    assert len(reads) == 1
    clock[0] = 3601
    hook("resume", config(**{"ai.balance": "off"}))
    assert len(reads) == 1  # switched off: no read at all
    assert hook("resume", config()) is None  # the plan of this hour: off


def router(state, cfg, gemini_fails=False):
    claude, gemini, deepseek = Recorder(), Recorder(fail=gemini_fails), Recorder()
    llm.set_cost_guard(cost_guard.guard(state, lambda: DAY))
    llm.set_balancer(balancer.Balancer(state, lambda: DAY))
    route = llm.ModelRouter(prod_settings(), ConfigStore.from_values(cfg), claude=claude,
                            gemini=gemini, deepseek=deepseek)
    return route, claude, gemini, deepseek


PLAN_C = {**CAP, "model.score": "deepseek-v4-flash", "model.tailor": "deepseek-v4-flash",
          "model.email": "deepseek-v4-flash"}


def test_the_router_moves_only_the_planned_jobs():
    route, claude, gemini, deepseek = router(FakeBotState(), PLAN_C)
    assert route.complete_json("score", "s", "u") == {"from": "gemini-3.5-flash-lite"}
    assert route.complete_json("resume", "s", "u") == {"from": "gemini-3.5-flash-lite"}
    assert route.complete_json("email", "s", "u") == {"from": "deepseek-v4-flash"}
    assert deepseek.calls == [("email", "deepseek-v4-flash")]
    assert claude.calls == []


def test_a_failed_balanced_call_goes_back_to_deepseek_not_claude():
    route, claude, gemini, deepseek = router(FakeBotState(), PLAN_C, gemini_fails=True)
    assert route.complete_json("score", "s", "u") == {"from": "deepseek-v4-flash"}
    assert gemini.calls == [("score", "gemini-3.5-flash-lite")]
    assert claude.calls == [] and route.usage.fallbacks == 0


def test_at_the_gemini_cap_deepseek_takes_the_job_back():
    state = FakeBotState()
    route, claude, gemini, deepseek = router(state, PLAN_C)
    costs.record(state, DAY, "people", 9.60, "gemini")  # after the hour's plan was made
    route.complete_json("score", "s", "u")
    assert route.complete_json("score", "s", "u") == {"from": "deepseek-v4-flash"}


def test_rebuild_with_claude_is_never_moved():
    route, claude, gemini, deepseek = router(FakeBotState(), PLAN_C)
    route.overrides["resume"] = "claude-sonnet-5"
    assert route.complete_json("resume", "s", "u") == {"from": "claude-sonnet-5"}
    assert gemini.calls == []
