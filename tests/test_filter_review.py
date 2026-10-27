"""Filter review in the weekly audit (28 Oct, your request): the jobs a filter may have got
wrong, both ways, for Opus. The filters themselves never change. Made-up jobs."""

from datetime import date

from test_runner import TODAY, S
from test_weekly_audit import MONDAY, SUNDAY, data, lines

from jobengine.bot_state import FakeBotState
from jobengine.sweep import filter_watch
from jobengine.sweep.models import KEPT_BUCKETS, SAVED
from jobengine.sweep.runner import fake_deps, run_sweep
from jobengine.track import audit

HEAD = "Fetch and filters: jobs a filter may have got wrong"


def test_near_titles_and_devops_titles_left_out_are_kept_others_not():
    w = filter_watch.Watch()
    w.title_drop("title not in scope", "Cloud Infrastructure Specialist", "ING (Amsterdam)")
    w.title_drop("title not in scope", "Cloud  Infrastructure Specialist", "ING (Utrecht)")
    w.title_drop("title not in scope", "Platform Developer", "Allegro (Poznań)")
    w.title_drop("title not in scope", "Java Developer", "Allegro (Poznań)")  # no near word
    w.title_drop("title excluded (staff)", "DevOps Engineer, Staff Augmentation", "X (Kraków)")
    w.title_drop("title excluded (manager)", "Sales Manager", "X (Kraków)")  # not DevOps-type
    assert w.totals == {"near": 3, "excluded": 1, "low": 0}
    assert w.lists["near"]["cloud infrastructure specialist"] == [
        2, "Cloud Infrastructure Specialist", "cloud", "ING (Amsterdam)"]
    assert w.lists["excluded"]["devops engineer, staff augmentation"][2] == "staff"


def test_the_lists_are_capped_the_counts_are_complete(monkeypatch):
    monkeypatch.setattr(filter_watch, "MAX_TITLES", 2)
    w = filter_watch.Watch()
    for i in range(5):
        w.add("near", f"Cloud Role {i}", "cloud", "X")
    assert w.totals["near"] == 5 and len(w.lists["near"]) == 2


def test_each_fetch_adds_to_its_week():
    state = FakeBotState()
    run_sweep(S, fake_deps(S), TODAY, state=state)
    week = filter_watch.week_of(state.get(filter_watch.KEY), filter_watch.monday_of(TODAY))
    assert week["runs"] == 1
    excluded = week["excluded"]
    assert excluded["lead platform engineer"][:3] == [1, "Lead Platform Engineer", "lead"]
    read = sum(v[0] for v in week["sources"].values())
    run_sweep(S, fake_deps(S), TODAY, state=state)
    again = filter_watch.week_of(state.get(filter_watch.KEY), filter_watch.monday_of(TODAY))
    assert again["runs"] == 2
    assert sum(v[0] for v in again["sources"].values()) == 2 * read
    assert again["totals"]["excluded"] == 2 * week["totals"]["excluded"]
    assert again["excluded"]["lead platform engineer"][0] == 2


def test_only_the_last_two_weeks_are_kept():
    state = FakeBotState()
    w = filter_watch.Watch()
    for day in (date(2026, 10, 12), date(2026, 10, 19), date(2026, 10, 26)):
        filter_watch.save(state, day, w, {"adzuna": {SAVED: 2, "other country": 3}},
                          {"adzuna": "Adzuna"}, KEPT_BUCKETS, SAVED)
    weeks = state.get(filter_watch.KEY)["weeks"]
    assert sorted(weeks) == ["2026-10-19", "2026-10-26"]
    assert weeks["2026-10-26"]["sources"] == {"Adzuna": [5, 2]}
    assert weeks["2026-10-26"]["reasons"] == {"other country": 3}


def week_value():
    w = filter_watch.Watch()
    w.title_drop("title not in scope", "Cloud Infrastructure Specialist", "ING (Amsterdam)")
    w.title_drop("title not in scope", "Cloud Infrastructure Specialist", "ING (Utrecht)")
    w.title_drop("title excluded (lead)", "Lead DevOps Engineer", "Bolt (Warszawa)")
    w.add("low", "DevOps Engineer", "", "Acme (Dublin)")
    state = FakeBotState()
    filter_watch.save(state, SUNDAY, w,
                      {"adzuna": {SAVED: 4, "title not DevOps-type": 50},
                       "ats": {SAVED: 6, "other country": 9}},
                      {"adzuna": "Adzuna", "ats": "Company sites"}, KEPT_BUCKETS, SAVED)
    return filter_watch.week_of(state.get(filter_watch.KEY), MONDAY)


def test_the_audit_lists_jobs_the_filter_may_have_missed():
    report = audit.build(data(fetch=week_value()), MONDAY)
    got = lines(report, HEAD)
    assert got[:4] == [
        "- 1 fetch: 69 postings read, 10 saved as new jobs (company boards: only titles in "
        "scope are counted as read).",
        "- Per source (read / saved): Adzuna 54 / 4; Company sites 15 / 6",
        "- Dropped by reason: title not DevOps-type 50, other country 9",
        "🟠 Maybe missed by the title filter: 2 postings in your countries with a near word in "
        "the title but not one of your titles (1 different title(s)). The most frequent:"]
    assert "   2x Cloud Infrastructure Specialist [word: cloud], e.g. ING (Amsterdam)" in got
    assert "   1x Lead DevOps Engineer [excluded: lead], e.g. Bolt (Warszawa)" in got
    assert "   1x DevOps Engineer, e.g. Acme (Dublin)" in got
    assert got[-1].startswith("- The bot never changes the filters.")


def test_the_audit_lists_saved_jobs_that_were_not_your_profile():
    jobs = [
        {"First seen": MONDAY, "Screen verdict": "Skip", "Skip reason": "Too senior",
         "Role": "DevOps Engineer III"},
        {"First seen": MONDAY, "Screen verdict": "Skip", "Skip reason": "Too senior",
         "Role": "DevOps Engineer III"},
        {"First seen": MONDAY, "Screen verdict": "Apply high", "Role": "SRE"},
        {"First seen": date(2026, 10, 1), "Status": "Declined", "Last activity date": SUNDAY,
         "Role": "Data Platform Engineer"},
    ]
    got = lines(audit.build(data(jobs=jobs), MONDAY), HEAD)
    assert got[0] == "- No /fetch data for this week (kept from 28 Oct, by every /fetch)."
    assert got[1] == ("🟠 Saved but not your profile: screening said Skip for 2 of the 3 new "
                      "jobs this week (Too senior 2); you declined 1. Their titles:")
    assert got[2:4] == ["   2x DevOps Engineer III", "   1x Data Platform Engineer"]


def test_opus_is_told_never_to_loosen_the_strict_rules():
    assert "Never suggest loosening the seniority, country, language, sponsorship or " \
           "office-only rules" in audit.OPUS_PROMPT
