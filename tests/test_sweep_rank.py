from dataclasses import replace
from datetime import date

import pytest

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


def test_same_job_in_another_city_is_saved_once():
    from jobengine.sweep.dedupe import IndexRow
    from jobengine.sweep.runner import _one_per_job

    def job(key):
        return [replace(BASE, dedupe_key=key)]

    index = {"sii|devops engineer|warszawa": IndexRow(page_id="p",
                                                      dedupe_key="sii|devops engineer|warszawa")}
    ranked = [job("sii|devops engineer|krakow"), job("acme|sre|gdansk"),
              job("acme|sre|poznan"), job("acme|cloud engineer|poznan")]
    kept, dropped = _one_per_job(ranked, index)
    assert [g[0].dedupe_key for g in kept] == ["acme|sre|gdansk", "acme|cloud engineer|poznan"]
    assert dropped == 2


def test_experience_as_posted():
    from jobengine.sweep.normalize import experience

    assert experience("We need 2-3 years of DevOps work.") == "2-3 years"
    assert experience("5+ years with Kubernetes") == "5+ years"
    assert experience("At least 3 years in cloud, 2+ years Terraform") == "2+ years"
    assert experience("min. 4 lata doswiadczenia") == "4+ years"
    assert experience("3 years of experience") == "3 years"
    assert experience("Kubernetes and Terraform") is None
    assert experience(None) is None


def test_too_senior_uses_years_then_title():
    from jobengine.sweep.runner import too_senior

    assert too_senior(replace(BASE, years_required=5), 4)
    assert not too_senior(replace(BASE, years_required=4), 4)
    assert not too_senior(replace(BASE, years_required=3, seniority="Senior"), 4)
    assert too_senior(replace(BASE, seniority="Senior"), 4)  # no years: the title decides
    assert not too_senior(BASE, 4)


def test_senior_jobs_are_replaced_by_the_next_ones_down_the_list():
    from jobengine.sweep.models import SweepSummary
    from jobengine.sweep.runner import _pick

    s = load_settings("local", {})
    s.sweep["fulltext"] = {"lookahead": 0}
    deps = fake_deps(s)
    long = " ".join(["Kubernetes and Terraform on AWS every day."] * 20)
    pages = {"https://j.example.com/0": f"<main>{long} 5+ years required.</main>",
             "https://j.example.com/1": f"<main>{long} 6+ years required.</main>",
             "https://j.example.com/2": f"<main>{long} 2-3 years required.</main>",
             "https://j.example.com/3": f"<main>{long} 3+ years required.</main>"}
    read = []
    deps.page = lambda url: read.append(url) or (url, pages[url])
    ranked = [[replace(BASE, dedupe_key=str(i), url=f"https://j.example.com/{i}")]
              for i in range(4)]
    summary = SweepSummary()
    picked = _pick(s, deps, ranked, 2, None, TODAY, summary, lambda line: None)
    assert [g[0].experience for g in picked] == ["2-3 years", "3+ years"]
    assert summary.too_senior == 2 and len(read) == 4  # two more read to refill


def test_years_cell_as_posted_and_its_lowest_number():
    from jobengine.sweep.normalize import years_low, years_text

    assert years_text(2, "2-3 years") == "2-3 years"
    assert years_text(3, None) == "3 years"
    assert years_text(None, None) is None
    assert years_low("3-5 years") == 3
    assert years_low("5+ years") == 5
    assert years_low(4) == 4 and years_low(4.0) == 4
    assert years_low("") is None and years_low(None) is None


@pytest.mark.parametrize("text, kept", [
    ("1-2 years of experience", True),
    ("1-3 years of experience", True),
    ("2-3 years of experience", True),
    ("3-5 years of experience", True),  # the lowest number counts: 3
    ("4+ years of experience", True),
    ("5 years of experience", False),
    ("5+ years of experience", False),
    ("6+ years of experience", False),
    ("5-7 years of experience", False),
])
def test_your_experience_window(text, kept):
    from jobengine.sweep.normalize import years_required
    from jobengine.sweep.runner import too_senior

    job = replace(BASE, description=text, years_required=years_required(text))
    assert too_senior(job, 4) is not kept
