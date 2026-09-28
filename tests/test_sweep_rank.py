from dataclasses import replace
from datetime import date

from jobengine.bot_state import FakeBotState
from jobengine.reference import Reference
from jobengine.settings import load_settings
from jobengine.sweep.models import Job
from jobengine.sweep.rank import Ranker
from jobengine.sweep.runner import DAY_KEY, _rank, fake_deps, run_sweep

TODAY = date(2026, 10, 1)
RANKER = Ranker(Reference.fake())

BASE = Job(
    source="adzuna", board="Adzuna", company="Example Hosting", role="DevOps Engineer",
    city="Krakow", country="Poland", url="https://jobs.example.com/1", posted_date=None,
    salary=None, seniority="Unknown", years_required=None, dedupe_key="k", posting_ref="adzuna:1",
    description="We run services.", description_is_snippet=True,
)


def score(**changes):
    return RANKER.score(replace(BASE, **changes), TODAY)


def test_named_skills_raise_the_score():
    plain = score()
    assert score(description="We run Kubernetes, Terraform and Prometheus.") == plain + 4 + 4 + 3
    assert score(description="Our CI uses GitHub Actions.") == plain + 3  # Active Term Map row


def test_known_gaps_language_and_years_lower_it():
    plain = score()
    assert score(description="Java and Nomad in production.") == plain - 4
    assert score(description="Fluent Polish is required.") == plain - 6
    assert score(description="Polish is a plus.") == plain
    assert score(years_required=7) == plain - 8
    assert score(years_required=3) == plain + 2


def test_target_company_seniority_freshness_and_full_description():
    plain = score()
    assert score(company="Vistula Cloud") == plain + 6  # Tier 2
    assert score(company="Odra Systems S.A.") == plain + 3  # Tier 5
    assert score(seniority="Mid") == plain + 2
    assert score(seniority="Senior") == plain - 3
    assert score(posted_date=date(2026, 9, 30)) == plain + 2
    assert score(posted_date=date(2026, 9, 1)) == plain - 2
    assert score(description="We run services.", description_is_snippet=False) == plain + 1


def fresh(limit):
    s = load_settings("local", {})
    s.sweep["daily_new_limit"] = limit
    return s


def test_sweep_keeps_only_the_best_new_jobs_and_counts_the_day():
    s = fresh(3)
    state = FakeBotState()
    deps = fake_deps(s)
    summary = run_sweep(s, deps, TODAY, state=state)
    assert summary.new == 3
    assert summary.not_kept == 7
    assert state.get(DAY_KEY) == {"date": TODAY.isoformat(), "count": 3}
    assert any("Kept the best 3 of 10 new jobs (daily limit 3, 0 already added today)" in n
               for n in summary.notes)
    assert "Weaker matches not saved (daily limit): 7" in summary.friendly_text()

    again = run_sweep(s, deps, TODAY, state=state)  # same day: the limit is used up
    assert again.new == 0
    assert again.not_kept == 7


def test_next_day_gets_a_new_allowance():
    s = fresh(3)
    state = FakeBotState()
    state.set(DAY_KEY, {"date": "2026-09-30", "count": 3})
    summary = run_sweep(s, fake_deps(s), TODAY, state=state)
    assert summary.new == 3


def test_rank_orders_by_score_then_newest():
    s = load_settings("local", {})
    deps = fake_deps(s)
    weak = replace(BASE, dedupe_key="weak", years_required=8)
    strong = replace(BASE, dedupe_key="strong", description="Kubernetes and Terraform daily.")
    newer = replace(BASE, dedupe_key="newer", posted_date=date(2026, 9, 12))
    older = replace(BASE, dedupe_key="older", posted_date=date(2026, 9, 10))
    ranked = _rank(deps, [[weak], [older], [strong], [newer]], TODAY)
    assert [g[0].dedupe_key for g in ranked] == ["strong", "newer", "older", "weak"]


def test_rank_without_reference_is_newest_first():
    deps = fake_deps(load_settings("local", {}))
    deps.reference = None
    a = replace(BASE, dedupe_key="a", posted_date=date(2026, 9, 1))
    b = replace(BASE, dedupe_key="b", posted_date=date(2026, 9, 20))
    assert [g[0].dedupe_key for g in _rank(deps, [[a], [b]], TODAY)] == ["b", "a"]
