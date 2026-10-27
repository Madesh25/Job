"""Weekly self-audit (27 Oct, your request): where the week went wrong, what works, what is
done again and again without results, written to Notion every Sunday. Made-up data."""

import logging
from datetime import UTC, date, datetime, timedelta

from test_telegram_bot import Clock, settings, talk

from jobengine.bot_state import FakeBotState
from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.track import audit, issue_log

MONDAY = date(2026, 10, 19)
SUNDAY = date(2026, 10, 25)


def job(applied, status="Applied", country="Poland", board="Pracuj.pl", role="DevOps Engineer",
        **kw):
    return {"Applied date": applied, "Status": status, "Country": country, "Board": board,
            "Role": role, "Screen verdict": "Apply high", **kw}


def data(**kw):
    base = {"today": SUNDAY, "jobs": [], "contacts": [], "ledger": {}, "issues": {}}
    base.update(kw)
    return audit.AuditInput(**base)


def lines(report, head):
    return dict(report.sections)[head]


# ---------------------------------------------------------------- schedule


def test_the_audited_week_and_when_it_is_due():
    assert audit.week_bounds(date(2026, 10, 27)) == (MONDAY, SUNDAY)
    assert audit.week_bounds(SUNDAY) == (MONDAY, SUNDAY)
    state = FakeBotState()
    # The first audit ever waits for a Sunday evening.
    assert not audit.due(state, datetime(2026, 10, 27, 9))
    assert not audit.due(state, datetime(2026, 10, 25, 19, 59))
    assert audit.due(state, datetime(2026, 10, 25, 20))
    assert audit.audited_week(datetime(2026, 10, 25, 20)) == (MONDAY, SUNDAY)
    audit.mark_done(state, MONDAY)
    assert not audit.due(state, datetime(2026, 10, 26, 9))
    assert not audit.due(state, datetime(2026, 11, 1, 19))  # that Sunday is not over
    assert audit.due(state, datetime(2026, 11, 1, 20))
    assert audit.due(state, datetime(2026, 11, 3, 8))  # missed on Sunday: the next tick


def test_the_morning_check_names_a_missing_or_unreviewed_audit():
    state = FakeBotState()
    audit.mark_done(state, MONDAY - timedelta(days=7))
    rows = [{"Week": "2026-W43", "Week start": MONDAY - timedelta(days=7), "Status": "New"}]
    blockers, soon = audit.morning_lines(state, datetime(2026, 10, 26, 6, 30), rows)
    assert blockers == []  # Monday: last night's audit may still come with the daily task
    assert soon[0].startswith("Weekly audit '2026-W43' is not reviewed yet")
    blockers, _ = audit.morning_lines(state, datetime(2026, 10, 27, 6, 30), rows)
    assert blockers == ["The weekly audit for the week of 19 Oct is missing. Fix: send /audit."]
    rows[0]["Status"] = "Actions set"
    assert audit.morning_lines(state, datetime(2026, 10, 27, 6, 30), rows)[1] == []
    assert audit.morning_lines(FakeBotState(), datetime(2026, 10, 27, 6, 30), [])[0] == []


# ---------------------------------------------------------------- what it finds


def test_scorecard_reads_this_week_and_the_settled_group():
    jobs = [job(date(2026, 10, 20)), job(date(2026, 10, 21)), job(date(2026, 10, 14)),
            job(date(2026, 10, 10), "Replied"), job(date(2026, 10, 5), "Interview"),
            job(date(2026, 10, 1), "Rejected"), job(date(2026, 9, 1), "Replied")]
    ledger = {"2026-10-20": {"screened.Apply high": 3, "screened.Skip": 5, "resumes": 2,
                             "mails_sent": 4, "replies": 1, "cost": 0.5,
                             "sweeps": [{"new": 7}, {"new": 3}]},
              "2026-10-18": {"mails_sent": 99}}  # the week before: not counted
    report = audit.build(data(jobs=jobs, ledger=ledger), MONDAY)
    assert report.title == "2026-W43: 19 Oct to 25 Oct"
    score = lines(report, "Scorecard")
    assert score[0] == "- Applications this week: 2 (+1 vs last week)"
    # Applied 7 to 35 days before Sunday: 14, 10, 5 and 1 Oct.
    assert score[1].startswith("- Reply rate of the settled group (applied 7 to 35 days ago): "
                               "2/4 = 50%; interviews 1, rejected 1")
    assert ("10 new jobs found, 8 screened (3 Apply high or normal), 2 resumes, 0 contacts, "
            "0 drafts, 4 mails sent, 1 replies, AI about $0.50") in score[3]
    assert report.props["Applied"] == 2 and report.props["Mails sent"] == 4
    assert report.props["Reply rate"] == 0.5 and report.props["Status"] == "New"
    assert report.props["Verdict"] == "Too little data"


def test_leaks_stuck_jobs_bounces_and_fast_rejections():
    jobs = [job(None, "Resume built", **{"Last activity date": date(2026, 10, 20)})] * 5 + [
        job(date(2026, 10, 20), "Rejected", **{"Last activity date": date(2026, 10, 21)}),
        job(date(2026, 10, 19), "Rejected", **{"Last activity date": date(2026, 10, 20)}),
        job(date(2026, 10, 1), "Rejected", **{"Last activity date": date(2026, 10, 22)}),
        job(date(2026, 10, 2), "Rejected", **{"Last activity date": date(2026, 10, 22)})]
    jobs[-1] = dict(jobs[-1], **{"Last activity date": date(2026, 10, 23)})
    jobs.append(job(date(2026, 10, 18), "Rejected", **{"Last activity date": date(2026, 10, 20)}))
    contacts = [{"Status": "Bounced", "Source": "Hunter", "Last contacted": date(2026, 10, 22)},
                {"Status": "Bounced", "Source": "Hunter", "Last contacted": date(2026, 10, 23)},
                {"Status": "Bounced", "Source": "AI search", "Last contacted": date(2026, 10, 23)}]
    found = lines(audit.build(data(jobs=jobs, contacts=contacts), MONDAY),
                  "Where it went wrong this week")
    assert found[0].startswith("🔴 5 approved jobs with a resume are not applied after 2+ days")
    assert "🔴 3 mails bounced (wrong address); by source: Hunter 2, AI search 1." in found[1]
    assert found[2].startswith("🔴 5 rejections this week, median 2 days after applying; 3 "
                               "within 3 days, which is usually an automatic filter")


def test_the_same_thing_again_and_again_without_a_reply_is_named():
    old = date(2026, 10, 10)
    jobs = [job(old, country="Ireland") for _ in range(8)]
    jobs += [job(old, country="Poland", board="JustJoin IT", role="SRE")] * 3
    contacts = [{"Status": "Contacted", "Type": "Recruiter/TA", "Last contacted": old}] * 9
    jobs += [{"Board": "Jooble", "First seen": date(2026, 10, 12), "Screen verdict": "Skip"}] * 20
    stale = lines(audit.build(data(jobs=jobs, contacts=contacts), MONDAY),
                  "Done again and again without results (last 4 weeks)")
    assert stale[0] == ("🔴 Country 'Ireland': 8 applications in 4 weeks, 0 replies. Stop, or "
                        "change one thing (role level, resume focus, contacts) and measure.")
    assert any("Board 'Pracuj.pl': 8 applications" in s for s in stale)
    assert any(s.startswith("🔴 Mails to Recruiter/TA: 9 in 4 weeks, 0 replies.") for s in stale)
    assert any("Source 'Jooble': 20 jobs in 4 weeks, none Apply high or normal" in s
               for s in stale)
    assert not any("Poland" in s for s in stale)  # 3 tries: too few to judge


def test_what_works_compares_groups_of_five_or_more():
    old = date(2026, 10, 10)
    jobs = [job(old, "Replied", country="Netherlands")] * 2 + [job(old, country="Netherlands")] * 3
    jobs += [job(old, country="Poland")] * 5
    works = lines(audit.build(data(jobs=jobs), MONDAY), "What works (settled applications)")
    assert works[0] == "- By country: Netherlands 2/5 (40%); Poland 0/5 (0%)"
    empty = lines(audit.build(data(), MONDAY), "What works (settled applications)")
    assert empty[0].startswith("- Not enough settled applications yet")


def test_errors_days_off_and_your_marks():
    issues = {"ERROR|jobengine.track|daily check step replies failed: HTTP N": 4,
              "WARNING|jobengine.sweep|Adzuna gave HTTP N": 9}
    ledger = {f"2026-10-{d}": {"resumes": 1} for d in (19, 20, 21, 22)}
    notes = [{"date": "2026-10-23", "job": "Vistula Cloud, DevOps", "note": "wrong title"}]
    forms = [{"Date": date(2026, 10, 24), "Issue type": "Wrong answer"}]
    report = audit.build(data(issues=issues, ledger=ledger, spot_notes=notes,
                              form_issues=forms), MONDAY)
    found = lines(report, "Where it went wrong this week")
    assert found[0] == ("🔴 You marked 1 resume(s) wrong in the spot check: Vistula Cloud, "
                        "DevOps: wrong title")
    assert found[1] == "🟠 1 wrong answers in application forms (dry runs): Wrong answer 1"
    assert found[2].startswith("🔴 The bot recorded work on 4 of 7 days")
    assert found[3] == "🟠 4 errors and 9 warnings were logged. The most frequent:"
    assert found[4] == "🔵    9x warning in jobengine.sweep: Adzuna gave HTTP N"
    assert report.props["Errors"] == 4


def test_last_weeks_actions_and_the_verdict():
    old = date(2026, 10, 10)
    jobs = [job(old, "Replied")] * 3 + [job(old)] * 9
    previous = [{"Week": "2026-W42: 12 Oct to 18 Oct", "Reply rate": 0.1, "Applied": 20,
                 "Status": "Actions set", "Actions": "Apply only to Apply high in Ireland"}]
    report = audit.build(data(jobs=jobs, previous=previous), MONDAY)
    actions = lines(report, "Last week's actions")
    assert actions[0] == ("- Last week (2026-W42: 12 Oct to 18 Oct): status Actions set; reply "
                          "rate 10% -> 25%; applications 20 -> 0.")
    assert actions[1] == "- Actions you set: Apply only to Apply high in Ireland"
    assert report.props["Verdict"] == "Improving"
    assert audit.verdict(0.05, 12, previous) == "Worse"
    assert audit.verdict(0.11, 12, previous) == "Flat"
    assert lines(report, "For Opus")[0] == audit.OPUS_PROMPT


# ---------------------------------------------------------------- the error log


def test_warnings_are_counted_as_patterns_and_written_at_most_every_30_minutes():
    issue_log.reset()
    logger = logging.getLogger("jobengine.test_audit")
    collector = issue_log.install(logger)
    assert issue_log.install(logger) is collector  # once
    logger.warning("Adzuna gave HTTP %s for https://x.example/a?id=%s", 503, 12)
    logger.warning("Adzuna gave HTTP %s for https://x.example/b", 502)
    logger.info("not counted")
    logging.getLogger("urllib3").warning("not ours")
    assert issue_log.pending() == 2
    state = FakeBotState()
    now = datetime(2026, 10, 22, 9)
    assert issue_log.flush(state, now.date(), now)
    assert issue_log.week(state, MONDAY) == {
        "WARNING|jobengine.test_audit|Adzuna gave HTTP N for <link>": 2}
    logger.error("boom %s", 1)
    assert not issue_log.flush(state, now.date(), now + timedelta(minutes=10))  # too soon
    assert issue_log.flush(state, now.date(), now + timedelta(minutes=31))
    assert issue_log.week(state, MONDAY)["ERROR|jobengine.test_audit|boom N"] == 1
    logger.removeHandler(collector)
    issue_log.reset()


# ---------------------------------------------------------------- the bot


def sunday_evening():
    return datetime(2026, 10, 25, 15, tzinfo=UTC)  # 20:30 in India


def test_the_bot_writes_the_audit_to_notion_on_sunday_evening():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d._track_now = sunday_evening
    replies = d.weekly_audit_tick()
    assert replies[0].text.startswith("🧭 Weekly audit 2026-W43: 19 Oct to 25 Oct:")
    assert replies[0].text.endswith("https://www.notion.so/fake1")
    [props] = d.audit_table.created
    assert props["Week"] == "2026-W43: 19 Oct to 25 Oct" and props["Status"] == "New"
    heads = [text for kind, text in d.audit_table.bodies[0] if kind == "heading_2"]
    assert heads == ["Scorecard", "Where it went wrong this week",
                     "Done again and again without results (last 4 weeks)",
                     "What works (settled applications)", "Last week's actions", "For Opus"]
    assert d.weekly_audit_tick() == []
    d._audit_checked = None
    assert d.weekly_audit_tick() == []  # done for that week


def test_audit_command_and_the_telegram_only_audit_without_a_table():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d._track_now = lambda: datetime(2026, 10, 27, 4, tzinfo=UTC)
    d.audit_table = None
    sent = [t for _, t in talk(d, "/audit").sent]
    assert sent[0].startswith("[LOCAL] 🧭 Weekly audit 2026-W43")
    assert sent[1].startswith("[LOCAL] DRY RUN: there is no Weekly Audit table in this "
                              "environment, so the audit is here only.")
    assert "Scorecard" in sent[1]
