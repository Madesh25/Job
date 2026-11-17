from datetime import date

import pytest

from jobengine.bot_state import FakeBotState
from jobengine.config_store import ConfigStore
from jobengine.contacts import finder
from jobengine.contacts.credits import Counter
from jobengine.outreach import planner
from jobengine.outreach.budget import (
    JobFacts,
    decide,
    month_capacity,
    parse_costs,
    priority,
    reserve,
    week_key,
    weekly_allowance,
    weeks_left,
)
from jobengine.reference import Reference
from jobengine.settings import load_settings

TODAY = date(2026, 10, 1)


def test_costs_and_capacity():
    assert parse_costs(None) == {"apollo": 4, "hunter": 1, "snov": 4, "prospeo": 5, "tomba": 1}
    assert parse_costs("apollo=2, Hunter = 3, tomba=2") == {
        "apollo": 2, "hunter": 3, "snov": 4, "prospeo": 5, "tomba": 2}
    counters = {"apollo": Counter(55, 75), "hunter": Counter(20, 25), "snov": None}
    assert month_capacity(counters, parse_costs(None)) == 20 // 4 + 5
    # The free plans add their own jobs once their counters exist.
    counters |= {"prospeo": Counter(0, 75), "tomba": Counter(5, 25)}
    assert month_capacity(counters, parse_costs(None)) == 20 // 4 + 5 + 75 // 5 + 20


def test_weeks_and_allowance():
    assert weeks_left(date(2026, 10, 1)) == 5
    assert weeks_left(date(2026, 10, 29)) == 1
    assert weeks_left(date(2026, 2, 28)) == 1
    assert weekly_allowance(55, date(2026, 10, 1)) == 11
    assert weekly_allowance(3, date(2026, 10, 29)) == 3
    assert weekly_allowance(0, TODAY) == 0
    assert week_key(TODAY) == "2026-W40"


@pytest.mark.parametrize(("facts", "expected"), [
    (JobFacts("Apply high", "Stated yes"), "A"),
    (JobFacts("Apply high", tier=2), "A"),
    (JobFacts("Apply high", on_ind_register=True), "A"),
    (JobFacts("Apply high", tier=5), "B"),
    (JobFacts("Apply normal", "Stated yes"), "C"),
    (JobFacts("Apply low"), "D"),
    (JobFacts("Needs review"), "D"),
])
def test_priority(facts, expected):
    assert priority(facts) == expected


def test_decide():
    high, normal, low = JobFacts("Apply high"), JobFacts("Apply normal"), JobFacts("Apply low")
    assert decide(high, 5, 4, 50).outreach  # last slot goes to Apply high
    assert not decide(high, 5, 5, 50).outreach
    assert reserve(6) == 2
    assert decide(normal, 6, 3, 50).outreach  # 3 left > reserve 2
    blocked = decide(normal, 6, 4, 50)  # 2 left = the reserve
    assert not blocked.outreach and "kept for Apply high" in blocked.reason
    assert not decide(low, 6, 0, 50).outreach
    none_left = decide(high, 6, 0, 0)
    assert not none_left.outreach and none_left.reason == "no provider credits left this month"


def test_plan_records_and_retries_do_not_spend_twice():
    state, config = FakeBotState(), ConfigStore.fake()
    values = {"Company": "Vistula Cloud", "Screen verdict": "Apply high"}
    first = planner.plan(state, config, Reference.fake(), values, "job-1", TODAY)
    assert first.outreach and first.priority == "A"  # Vistula Cloud is Tier 2
    again = planner.plan(state, config, Reference.fake(), values, "job-1", TODAY)
    assert again.outreach and again.reason == "already counted this week"
    assert state.get("outreach.week") == {"week": "2026-W40", "allowance": 11, "jobs": ["job-1"]}
    low = planner.plan(state, config, Reference.fake(), {"Screen verdict": "Apply low"},
                       "job-2", TODAY)
    assert not low.outreach
    planner.force(state, config, "job-2", TODAY)
    assert state.get("outreach.week")["jobs"] == ["job-1", "job-2"]


def test_new_week_gets_a_fresh_allowance():
    state = FakeBotState()
    state.set("outreach.week", {"week": "2026-W39", "allowance": 3, "jobs": ["x", "y", "z"]})
    budget = planner.load_budget(state, ConfigStore.fake(), TODAY)
    assert (budget.week, budget.used, budget.allowance) == ("2026-W40", 0, 11)


def test_apply_only_lookup_uses_free_sources_only():
    deps = finder.fake_deps(load_settings("local", {}))
    deps.today = lambda: TODAY
    result = finder.find_contacts(deps, "fixture-clean-pl", paid=False)
    assert deps.calls == []  # no provider searched
    assert result.status == "done" and not result.waiting_for_domain
    assert finder.APPLY_ONLY_NOTE in result.message
    assert {c.source for c in result.contacts} <= {"Apollo", "Hunter", "Snov", "Job posting"}
    assert all(c.cached or c.source == "Job posting" for c in result.contacts)
    paid = finder.fake_deps(load_settings("local", {}))
    paid.today = lambda: TODAY
    finder.find_contacts(paid, "fixture-clean-pl")
    assert paid.calls  # the paid waterfall runs by default


def test_apply_only_never_asks_for_a_domain():
    deps = finder.fake_deps(load_settings("local", {}))
    result = finder.find_contacts(deps, "fixture-no-domain", paid=False)
    assert result.status == "done" and not result.waiting_for_domain
