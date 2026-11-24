"""The /jd backlog (S5, 23 Nov, your ask): /jd starts with today's top 5, and never
screened jobs more than 7 days old are set Expired once a day. Made-up jobs, no Notion."""

from datetime import date, timedelta

from test_review_one_at_a_time import make_desk

from jobengine.bot_state import FakeBotState
from jobengine.screen import backlog
from jobengine.screen.models import JobRow

TODAY = date(2026, 11, 24)


def row(pid, days, status="New", board="LinkedIn", posted=True):
    day = TODAY - timedelta(days=days)
    return JobRow(page_id=pid, company=f"Co {pid}", role="DevOps Engineer", status=status,
                  board=board, posted_date=day if posted else None,
                  swept_date=None if posted else day)


def test_only_never_screened_open_jobs_over_7_days_old_expire_oldest_first():
    rows = [row("a", 8), row("b", 7), row("c", 6), row("d", 30), row("e", 9, status="Skipped"),
            row("f", 12, posted=False), JobRow(page_id="g", company="G", role="R")]
    assert [r.page_id for r in backlog.to_expire(rows, TODAY)] == ["d", "f", "a", "b"]


def test_at_most_100_a_run_and_the_day_is_marked_when_none_is_left():
    state, updates = FakeBotState(), []
    old = [row(f"j{n}", 10) for n in range(130)]
    done, left = backlog.expire(old, lambda pid, props: updates.append((pid, props)), TODAY,
                                state)
    assert (done, left) == (100, 30) and backlog.due(state, TODAY)
    assert updates[0][1] == {"Status": "Expired", "Skip reason": "Expired",
                             "Last activity date": TODAY}
    assert backlog.expire(old[100:], lambda *a: None, TODAY, state) == (30, 0)
    assert not backlog.due(state, TODAY) and backlog.due(state, TODAY + timedelta(days=1))
    assert backlog.day_total(state, TODAY) == 130
    assert backlog.text(130, 0, 4).startswith("🧹 Old jobs cleared today: 130 never screened "
                                                "job(s) more than 7 days old are set Expired")


def test_jd_starts_with_the_top_and_the_old_job_expires_on_the_desk():
    d = make_desk()
    [menu] = d.jd("")
    assert "Today's top 1, best match first" in menu.text and "\n1. " in menu.text
    [first] = d.tap(f"jb:{backlog.TOP_BOARD}")
    assert first.text.startswith("Job 1 of 1 without a description")
    old = [pid for pid, v in d.repo.rows.items() if v.get("Screen verdict") == "Unscreened"
           and pid not in {r.page_id for r in d.jd_rows()}]
    replies = d.jd_backlog_tick()
    expired = [pid for pid, v in d.repo.rows.items() if v.get("Status") == "Expired"]
    assert replies[0].text.startswith("🧹 Old jobs cleared today: 1 never screened job(s)")
    assert expired == ["seed-repost"] and set(expired) <= set(old)
    assert d.jd_backlog_tick() == []  # once a day
    from jobengine.screen import runner

    assert not set(expired) & {r.page_id for r in runner.unscreened_rows(d.deps)}


def test_one_message_a_day_with_the_day_total(monkeypatch):
    """LOG-11 (24 Nov): "Old jobs cleared: 100 ... 102 more go in the next runs today", then
    "2" before /screen and again before /fetchreport. Now one message, when the day's
    clean-up is done, with the day's total."""
    monkeypatch.setattr(backlog, "MAX_PER_RUN", 1)
    d = make_desk()
    for n in range(2):  # two more old never screened jobs: three runs of one
        d.repo.rows[f"old-{n}"] = {**d.repo.rows["seed-repost"], "Company": f"Old {n}"}
    assert d.jd_backlog_tick() == []  # 1 done, 2 left: no message yet
    assert d.jd_backlog_tick() == []  # 2 done, 1 left
    [reply] = d.jd_backlog_tick()
    assert reply.text.startswith("🧹 Old jobs cleared today: 3 never screened job(s)")
    assert "more go in the next runs" not in reply.text
    assert d.jd_backlog_tick() == []  # done for today
    expired = [pid for pid, v in d.repo.rows.items() if v.get("Status") == "Expired"]
    assert len(expired) == 3
