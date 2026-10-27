"""Weekly link check (Autopilot I1, 26 Oct): open jobs whose posting closed become Expired.
Made-up pages."""

from datetime import UTC, date, datetime

from test_telegram_bot import Clock, settings

from jobengine import http
from jobengine.bot_state import FakeBotState
from jobengine.config_store import ConfigStore
from jobengine.screen.desk import fake_desk
from jobengine.screen.models import JobRow
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.track import expired_check

TODAY = date(2026, 10, 25)  # a Sunday


def pages(closed=(), gone=(), refused=()):
    opened = []

    def get_page(url):
        opened.append(url)
        if url in gone:
            raise http.HttpError(f"GET {url} failed: HTTP 404", status=404)
        if url in refused:
            raise http.HttpError(f"GET {url} failed: HTTP 403", status=403)
        text = "<p>This job has expired.</p>" if url in closed else "<p>Apply now</p>"
        return url, text

    get_page.opened = opened
    return get_page


def row(name, url, status="Screened", expires=None):
    return JobRow(page_id=name, company=name.title(), role="DevOps Engineer", url=url,
                  status=status, expires=expires)


def test_due_on_sunday_from_0900_then_weekly():
    config = ConfigStore.fake()
    state = FakeBotState()
    assert not expired_check.due(config, state, datetime(2026, 10, 25, 8, 59))
    assert not expired_check.due(config, state, datetime(2026, 10, 24, 12))  # Saturday
    assert expired_check.due(config, state, datetime(2026, 10, 25, 9))
    expired_check.mark_done(state, TODAY)
    assert not expired_check.due(config, state, datetime(2026, 10, 25, 18))
    assert not expired_check.due(config, state, datetime(2026, 10, 31, 12))
    assert expired_check.due(config, state, datetime(2026, 11, 1, 9))
    off = ConfigStore.from_values({"schedule.expired_check": "false"})
    assert not expired_check.due(off, FakeBotState(), datetime(2026, 10, 25, 9))


def test_closed_gone_and_past_expires_are_found_unclear_counts_as_open():
    get_page = pages(closed={"https://a.example/1"}, gone={"https://b.example/2"},
                     refused={"https://c.example/3"})
    rows = [row("alpha", "https://a.example/1"), row("beta", "https://b.example/2", "Approved"),
            row("gamma", "https://c.example/3"), row("delta", "https://d.example/4"),
            row("epsilon", "https://www.linkedin.com/jobs/view/1"),
            row("zeta", None, "Resume built", expires=date(2026, 10, 20))]
    result = expired_check.check(rows, get_page, TODAY)
    assert [(r.page_id, why) for r, why in result.closed] == [
        ("alpha", 'the job page says "this job has expired"'),
        ("beta", "the job page answers HTTP 404 (not found)"),
        ("zeta", "its Expires date 2026-10-20 has passed")]
    assert result.checked == 4 and result.not_opened == 1
    assert not any("linkedin" in url for url in get_page.opened)
    lines = result.text().splitlines()
    assert lines[0] == "🧹 Weekly link check: 3 of 6 open jobs are closed and now Expired:"
    assert lines[2] == ("- Beta, DevOps Engineer (Approved): the job page answers HTTP 404 "
                        "(not found)")
    assert "1 not opened (LinkedIn, a blocked site or no link)." in lines


def test_at_most_the_weekly_limit_is_opened():
    rows = [row(f"j{i}", f"https://x.example/{i}") for i in range(5)]
    result = expired_check.check(rows, pages(), TODAY, limit=3)
    assert result.checked == 3 and result.left == 2
    assert result.text().splitlines() == [
        "🧹 Weekly link check: 3 open job link(s) checked, none closed.",
        "2 wait for next week (at most 80 links a week)."]


def test_the_bot_marks_closed_open_jobs_expired_once_a_week():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d.repo.rows["pl-clean"]["Status"] = "Approved"
    d.repo.rows["pl-b2b"]["Status"] = "Screened"
    d.get_page = pages(gone={"https://jobs.example.com/pl-clean"})
    d._track_now = lambda: datetime(2026, 10, 25, 4, tzinfo=UTC)  # 09:30 in India, Sunday
    text = d.expired_check_tick()[0].text
    assert text.startswith("🧹 Weekly link check: 1 of ")
    assert d.repo.rows["pl-clean"]["Status"] == "Expired"
    assert d.repo.rows["pl-b2b"]["Status"] == "Screened"
    assert d.repo.rows["pl-polish-required"]["Status"] == "New"  # not an open status
    assert "https://jobs.example.com/pl-polish-required" not in d.get_page.opened
    assert d.expired_check_tick() == []
