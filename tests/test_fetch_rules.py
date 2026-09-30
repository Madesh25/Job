"""Fetch rules of 30 Sep: 7 titles, remote EU jobs, every fitting job saved, today's best 30
screened first, the best link kept."""

from datetime import date

from test_runner import TODAY, S

from jobengine.bot_state import FakeBotState
from jobengine.config_store import ConfigStore
from jobengine.resume import header
from jobengine.sweep import best
from jobengine.sweep.dedupe import IndexRow, link_rank, update_plan
from jobengine.sweep.models import RawPosting
from jobengine.sweep.normalize import Rules, Skipped, normalize
from jobengine.sweep.runner import fake_deps, run_sweep

RULES = Rules.from_config(S.sweep)
ACTIVE = ["Poland", "Netherlands", "Ireland"]


def raw(location, title="DevOps Engineer"):
    return RawPosting(source="adzuna", board="Adzuna", title=title, company="Acme",
                      location_text=location, url="https://www.adzuna.pl/details/1",
                      posting_id="1")


def test_the_seven_titles_are_searched():
    assert "devsecops engineer" in S.sweep["jooble"]["search_terms"]
    assert "infrastructure engineer" in S.sweep["jooble"]["search_terms"]
    assert "kubernetes engineer" in S.sweep["jooble"]["search_terms"]
    assert "devsecops" in S.sweep["adzuna"]["what_or"]


def test_remote_eu_jobs_are_kept_as_other_remote():
    for text in ("Remote, EU", "Remote (Europe)", "Fully remote - EMEA", "Remote within the EU"):
        job = normalize(raw(text), RULES, ACTIVE)
        assert not isinstance(job, Skipped), text
        assert (job.country, job.city) == ("Other", "Remote")
    # Remote with no region, or a region-less other country, is still left out.
    assert isinstance(normalize(raw("Remote"), RULES, ACTIVE), Skipped)
    assert isinstance(normalize(raw("Berlin, Germany"), RULES, ACTIVE), Skipped)
    # A remote job in Poland stays a Poland job.
    job = normalize(raw("Remote, Poland"), RULES, ACTIVE)
    assert job.country == "Poland"
    none = Rules(RULES.title_include, RULES.title_exclude, RULES.locations)
    assert isinstance(normalize(raw("Remote, EU"), none, ACTIVE), Skipped)


def test_every_fitting_job_is_saved_and_scored():
    state = FakeBotState()
    summary = run_sweep(S, fake_deps(S), TODAY, state=state)
    assert S.sweep["daily_new_limit"] == 0 and summary.not_kept == 0
    scores = best.scores(state, TODAY)
    assert len(scores) == summary.new > 0


def test_a_daily_limit_still_works_when_set():
    limited = S.model_copy(update={"sweep": {**S.sweep, "daily_new_limit": 2}})
    summary = run_sweep(limited, fake_deps(limited), TODAY, state=FakeBotState())
    assert summary.new == 2 and summary.not_kept > 0


def test_best_scores_are_per_day_and_merge():
    state = FakeBotState()
    best.record(state, TODAY, {"a-1": 5})
    best.record(state, TODAY, {"b-2": 9})
    assert best.scores(state, TODAY) == {"a1": 5, "b2": 9}
    assert best.scores(state, date(2026, 10, 2)) == {}
    best.record(state, date(2026, 10, 2), {"c-3": 1})
    assert best.scores(state, date(2026, 10, 2)) == {"c3": 1}


def test_screening_takes_the_best_scored_rows_first():
    from jobengine.screen.runner import fake_deps as screen_deps
    from jobengine.screen.runner import unscreened_rows

    deps = screen_deps(S)
    rows = unscreened_rows(deps, TODAY)
    assert len(rows) >= 2
    last = rows[-1].page_id
    best.record(deps.state, TODAY, {last: 99})
    assert unscreened_rows(deps, TODAY)[0].page_id == last
    assert unscreened_rows(deps)[0].page_id == rows[0].page_id  # no date: newest first


def test_link_rank_and_the_better_link_replaces_the_old_one():
    assert link_rank("https://careers.acme.com/jobs/1") == 3
    assert link_rank("https://justjoin.it/job-offer/acme") == 2
    assert link_rank("https://www.linkedin.com/jobs/view/1") == 2
    assert link_rank("https://www.adzuna.pl/details/1") == 1
    assert link_rank("https://jooble.org/desc/1") == 1
    assert link_rank(None) == 0
    row = IndexRow(page_id="p", dedupe_key="acme|devops engineer|warszawa",
                   posting_ids=["adzuna:1"], times_seen=1, first_seen=TODAY,
                   url="https://www.adzuna.pl/details/1")
    better = normalize(raw("Warszawa, Poland"), RULES, ACTIVE)
    better = better.__class__(**{**vars(better), "url": "https://careers.acme.com/jobs/1",
                                 "posting_ref": "ats:9", "source": "ats"})
    plan = update_plan(row, better, TODAY)
    assert plan.props["URL"] == "https://careers.acme.com/jobs/1"
    assert plan.moved_link == "https://www.adzuna.pl/details/1"
    worse = better.__class__(**{**vars(better), "url": "https://jooble.org/desc/9",
                                "posting_ref": "jooble:9", "source": "jooble"})
    plan = update_plan(row, worse, TODAY)
    assert "URL" not in plan.props and plan.moved_link is None


def test_resume_says_europe_for_a_remote_eu_job():
    job = {"Role": "DevOps Engineer", "City": "Remote", "Country": "Other"}
    assert header.relocation_line(S, ConfigStore.fake(), job) == (
        "Chennai, India | Open to relocate to Europe")
    assert header.place(job) == "Europe"
