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


def test_only_office_or_hybrid_jobs_in_your_countries_are_kept():
    # Your decision of 23 Oct: Poland, the Netherlands and Ireland, office or hybrid only.
    assert RULES.remote_jobs is False and RULES.remote_regions == ()
    for text in ("Remote, EU", "Remote (Europe)", "Fully remote - EMEA", "Remote",
                 "Remote, Poland", "Warsaw, Mazowieckie, Poland (Remote)", "Dublin (remote)",
                 "Krakow, praca zdalna"):
        assert isinstance(normalize(raw(text), RULES, ACTIVE), Skipped), text
    assert normalize(raw("Remote, Poland"), RULES, ACTIVE).reason == (
        "remote job (you want office or hybrid)")
    assert isinstance(normalize(raw("Warsaw", "DevOps Engineer (Remote)"), RULES, ACTIVE),
                      Skipped)
    for text in ("Warsaw, Mazowieckie, Poland (Hybrid)", "Amsterdam", "Poland (Remote/Hybrid)",
                 "Krakow (praca hybrydowa)", "Dublin, Ireland (On-site)"):
        job = normalize(raw(text), RULES, ACTIVE)
        assert not isinstance(job, Skipped), text
    assert isinstance(normalize(raw("Berlin, Germany"), RULES, ACTIVE), Skipped)
    # With remote jobs switched back on, the old rules apply again.
    old = Rules(RULES.title_include, RULES.title_exclude, RULES.locations, ("eu",))
    job = normalize(raw("Remote, EU"), old, ACTIVE)
    assert (job.country, job.city) == ("Other", "Remote")
    assert normalize(raw("Remote, Poland"), old, ACTIVE).country == "Poland"


def test_a_fully_remote_description_is_not_saved():
    from jobengine.sweep import fit

    assert fit.fully_remote("This is a fully remote role, work from anywhere in Poland.")
    assert fit.fully_remote("Praca 100% zdalna.") and fit.fully_remote("100% remote")
    assert not fit.fully_remote("Fully remote or hybrid from our Warsaw office.")
    assert not fit.fully_remote("This is not a fully remote role.")
    assert not fit.fully_remote("Remote work 2 days a week.")
    assert fit.cannot_take("Poland", "Fully remote team.", remote_ok=False) == "Fully remote"
    assert fit.cannot_take("Poland", "Fully remote team.") is None  # remote jobs allowed
    assert fit.skip_reason(fit.FULLY_REMOTE) == "Other"  # no new Notion option needed


def test_screening_skips_remote_rows_for_free():
    from jobengine.screen.gates import pre_gate
    from jobengine.screen.models import JobRow

    row = JobRow(page_id="p", company="Acme", role="DevOps Engineer", country="Other",
                 city="Remote")
    hit = pre_gate(row, None, 4, "Run Kubernetes.", remote_ok=False)
    assert (hit.gate, hit.reason, hit.detail) == (8, "Other",
                                                  "remote job, you want office or hybrid")
    office = JobRow(page_id="p", company="Acme", role="DevOps Engineer", country="Poland",
                    city="Warszawa")
    assert pre_gate(office, None, 4, "100% remote.", remote_ok=False).detail == "Fully remote"
    assert pre_gate(office, None, 4, "Hybrid, 2 days in the office.", remote_ok=False) is None


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


def test_systems_engineer_outside_it_is_excluded():
    """10 Oct: ABB "Power Systems Engineer" passed as a DevOps-type title."""
    from jobengine.settings import load_settings
    from jobengine.sweep.normalize import Rules, title_scope

    rules = Rules.from_config(load_settings("local", {}).sweep)
    for title in ("Power Systems Engineer", "Control Systems Engineer",
                  "Embedded Systems Engineer", "Electrical Systems Engineer"):
        assert title_scope(title, rules) is not None, title
    for title in ("Systems Engineer", "Linux Systems Engineer", "Cloud Systems Engineer",
                  "DevOps Engineer", "Junior Software Engineer (DevOps)"):
        assert title_scope(title, rules) is None, title
