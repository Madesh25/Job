"""One morning message (S12, 23 Nov, your ask): the morning check ends with today's steps,
and a quiet 07:30 daily check sends nothing (its Run Log row keeps it). Made-up jobs."""

from datetime import UTC, date, datetime, time

from test_telegram_bot import Clock, settings

from jobengine import telegram_bot as tb
from jobengine.config_store import ConfigStore
from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.track import morning
from jobengine.track.runner import DailyReport

TUESDAY, SUNDAY = date(2026, 10, 27), date(2026, 11, 1)


def test_the_bot_line_in_time_order_with_the_weekend_checks():
    assert morning.bot_line(TUESDAY, ["07:00", "19:30"], "08:30", "21:45", "22:00") == (
        "Bot: 07:00 and 19:30 job search and screening · 08:30 Apply today · "
        "21:45 daily report · 22:00 apply nudge")
    sunday = morning.bot_line(SUNDAY, [], None, "21:45", None)
    assert sunday == ("Bot: 09:00 link check · 10:00 job board check · 19:30 screening check "
                      "· 20:00 weekly digest and audit · 21:45 daily report")


def test_your_steps():
    assert morning.you_lines(3, 12, 4, 11) == [
        "- Apply: 3 job(s) with a built resume wait for you (apply on the job page, then tap "
        "I applied)", "- Review: 12 job(s) in /pending (4 Apply high)",
        "- /jd: 11 job(s) of the last 3 days need a description"]
    assert morning.you_lines(0, None, None, 0) == [
        "- Nothing waits for you yet: the morning run brings new jobs."]


def desk_at_0635():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d.screen()
    d._track_now = lambda: datetime(2026, 10, 27, 1, 5, tzinfo=UTC)  # 06:35 India time
    return d


def test_the_morning_check_ends_with_today():
    d = desk_at_0635()
    [check] = tb._scheduled("Morning check", d.health_report_tick, d)
    text = check.text
    assert text.startswith("🩺 Morning check, Tue 27 Oct")
    today = text[text.index("\n\n📋 Today\n"):]
    assert "\nBot: " in today and "21:45 daily report" in today
    assert "\nYou:\n- Review: 11 job(s) in /pending (4 Apply high)" in today
    assert "- /jd: 1 job(s) of the last 3 days need a description" in today
    assert text.splitlines()[-1].startswith("📝 Full report in Notion: Run Log")
    assert "📋 Today" in "\n".join(d.run_log_table.bodies[-1])  # in the page too


def test_config_switches_the_steps_off():
    d = desk_at_0635()
    base = ConfigStore.fake().all()
    d.deps.config = lambda: ConfigStore.from_values({**base, "morning.one_message": "false"})
    assert "📋 Today" not in d.health_report_text()


def quiet_report():
    return DailyReport(day=TUESDAY, since=datetime(2026, 10, 26, 21, 30),
                       tokens={"alerts": None, "sender": None},
                       followups_dry=["Anna Example"])


def test_a_quiet_morning_check_sends_nothing_but_keeps_its_row():
    d = desk_at_0635()
    report = quiet_report()
    assert morning.quiet(report)
    replies = tb._scheduled("Daily check", lambda: d.daily_check_replies(report, time(7, 30)), d)
    assert replies == []
    assert d.run_log_table.created[-1]["Step"] == "Daily check"
    assert any(line.startswith("Daily check 2026-10-27") for line in d.run_log_table.bodies[-1])
    assert d.daily_check_replies(report, time(21, 30))  # the evening one always comes


def test_a_morning_check_with_news_still_comes():
    d = desk_at_0635()
    report = quiet_report()
    report.replies = ["Piotr Example (Vistula Cloud) -> Interview"]
    assert not morning.quiet(report)
    assert d.daily_check_replies(report, time(7, 30))
    report = quiet_report()
    report.tokens = {"alerts": "invalid_grant", "sender": None}
    assert not morning.quiet(report)
