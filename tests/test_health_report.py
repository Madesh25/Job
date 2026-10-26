"""Morning check (Autopilot C, 26 Oct): what would stop the morning run comes first, with
the fix."""

from datetime import UTC, date, datetime

from test_telegram_bot import Clock, settings

from jobengine import http
from jobengine.bot_state import FakeBotState
from jobengine.config_store import ConfigStore
from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.track import health_report

TODAY = date(2026, 10, 27)


def test_due_once_a_day_from_0630_india_time():
    config = ConfigStore.fake()
    state = FakeBotState()
    assert not health_report.due(config, state, datetime(2026, 10, 27, 0, 55, tzinfo=UTC))
    at = datetime(2026, 10, 27, 1, 5, tzinfo=UTC)  # 06:35 in India
    assert health_report.due(config, state, at)
    health_report.mark_sent(state, TODAY)
    assert not health_report.due(config, state, at)
    off = ConfigStore.from_values({"schedule.health": "false"})
    assert not health_report.due(off, FakeBotState(), at)


def test_a_table_not_shared_is_named_with_the_fix():
    def query(source):
        if source == "c":
            raise http.HttpError("POST /v1/data_sources/c/query failed: HTTP 404", status=404)
        if source == "r":
            raise http.HttpError("POST failed: ReadTimeout")

    found = health_report.tables({"config": "c", "strategy": "s", "evidence_library": "x"},
                                 {"job_opportunities": "j", "resume_log": "r"})
    assert found == {"Config": "c", "Strategy": "s", "Job Opportunities": "j", "Resume Log": "r"}
    problems = health_report.table_problems(found, query)
    assert problems[0].startswith("Notion table Config: not shared with the bot (HTTP 404). "
                                  "Fix: open it in Notion, ••• > Connections")
    assert problems[1] == "Notion table Resume Log: POST failed: ReadTimeout"


def test_blockers_come_first_then_what_needs_you_soon():
    text = health_report.text(TODAY, ["The /fetch gate is closed: run /update"],
                              ["Gmail token mm: 1 day(s) left. Fix: /gmailtoken mm"],
                              ["AI in October: $3.10 so far"], "07:00 and 19:30")
    assert text.splitlines() == [
        "🩺 Morning check, Tue 27 Oct (before the 07:00 and 19:30 run)",
        "⛔ Blocks the run, fix first:", "- The /fetch gate is closed: run /update",
        "⚠️ Needs you soon:", "- Gmail token mm: 1 day(s) left. Fix: /gmailtoken mm",
        "✅ AI in October: $3.10 so far"]
    assert "✅ Nothing blocks today's run." in health_report.text(TODAY, [], [], [])


def test_the_bot_sends_it_once_before_the_run():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d._track_now = lambda: datetime(2026, 10, 27, 1, 5, tzinfo=UTC)
    base = ConfigStore.fake().all()
    d.deps.config = lambda: ConfigStore.from_values({
        **base, "last_strategy_update": "2026-10-25", "strategy_refresh_days": "30",
        "schedule.autopilot": "true"})
    [check] = d.health_report_tick()
    lines = check.text.splitlines()
    assert lines[0] == "🩺 Morning check, Tue 27 Oct (before the 07:00 and 19:30 run)"
    assert "✅ /fetch gate open for 28 more days" in lines
    assert any(line.startswith("✅ Notion: ") and line.endswith("tables reachable")
               for line in lines)
    assert d.health_report_tick() == []  # once a day

    def refused(source):
        raise http.HttpError("HTTP 404", status=404)

    d.table_query = refused
    d.deps.config = lambda: ConfigStore.from_values({**base, "schedule.autopilot": "false"})
    text = d.health_report_text()
    assert "⛔ Blocks the run, fix first:" in text
    assert "- The /fetch gate is closed: run /update (the monthly strategy check)." in text
    assert "- The autopilot schedule is off (Notion Config schedule.autopilot)." in text
    assert "Notion table Job Opportunities: not shared with the bot" in text
