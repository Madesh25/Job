"""The weekly job board check (28 Oct, your request): --check-boards by itself on Sundays on
the prod bot, a message only when a board changed. Made-up boards."""

from datetime import UTC, date, datetime

from test_telegram_bot import Clock, prod_settings, settings

from jobengine.bot_state import FakeBotState
from jobengine.config_store import ConfigStore
from jobengine.screen.desk import fake_desk
from jobengine.sweep import board_watch
from jobengine.sweep.boards import Check
from jobengine.sweep.fakes import FAKE_TODAY

SUNDAY = datetime(2026, 11, 1, 10, 0)
NONE = ConfigStore.from_values({})


def check(name, result="OK", jobs=20, here=5):
    return Check(name, "PL", f"https://boards.example.com/{name}", "Notion", result, "greenhouse",
                 jobs if result == "OK" else None, here if result == "OK" else None)


def test_on_only_for_the_prod_bot_unless_config_says():
    assert board_watch.is_on(prod_settings(), NONE)
    assert not board_watch.is_on(settings(), NONE)
    assert board_watch.is_on(settings(), ConfigStore.from_values({"schedule.board_check": "true"}))
    assert not board_watch.is_on(prod_settings(),
                                 ConfigStore.from_values({"schedule.board_check": "false"}))


def test_sunday_from_10_then_weekly():
    s, state = prod_settings(), FakeBotState()
    assert not board_watch.due(s, NONE, state, datetime(2026, 11, 1, 9, 59))
    assert board_watch.due(s, NONE, state, SUNDAY)
    assert not board_watch.due(s, NONE, state, datetime(2026, 11, 2, 10))  # first: a Sunday
    board_watch.record(state, date(2026, 11, 1), [check("a")])
    assert not board_watch.due(s, NONE, state, datetime(2026, 11, 7, 12))
    assert board_watch.due(s, NONE, state, datetime(2026, 11, 8, 10))
    assert board_watch.due(s, NONE, state, datetime(2026, 11, 10, 10))  # missed Sunday


def test_the_first_run_sends_a_short_summary():
    state = FakeBotState()
    week = board_watch.record(state, date(2026, 11, 1), [
        check("Acme"), check("Beta", "HTTP 404"),
        check("Gamma", "own job site, covered by your alerts")])
    assert board_watch.text(week) == (
        "🔎 Weekly job board check: 1 of 3 boards readable, 1 with jobs in PL/NL/IE. (First "
        "run: from next week you hear only about changes.)\n"
        "Not readable now (1): Beta (HTTP 404)")


def test_a_quiet_week_sends_nothing_and_changes_are_named():
    state = FakeBotState()
    board_watch.record(state, date(2026, 11, 1), [
        check("Acme"), check("Beta", "HTTP 404"), check("Delta", jobs=40, here=8),
        check("Echo", jobs=30, here=3), check("Fox")])
    same = board_watch.record(state, date(2026, 11, 8), [
        check("Acme"), check("Beta", "HTTP 404"), check("Delta", jobs=40, here=8),
        check("Echo", jobs=30, here=3), check("Fox")])
    assert board_watch.text(same) == ""
    week = board_watch.record(state, date(2026, 11, 15), [
        check("Acme", "HTTP 404"), check("Beta"), check("Delta", jobs=12, here=2),
        check("Echo", jobs=30, here=0), check("Fox", jobs=11, here=5)])
    assert week.changes == [
        "🔴 Acme: the board stopped working (HTTP 404); it had 20 jobs. Check its Careers URL "
        "in Target Companies.",
        "🟢 Beta: the board works again (20 jobs, 5 in PL/NL/IE).",
        "🟠 Delta: 12 jobs, last week 40: the board may have moved or changed its search.",
        "🟠 Echo: no jobs in PL/NL/IE now (last week 3).",
    ]  # Fox: 20 to 11 is not half
    assert board_watch.text(week).startswith(
        "🔎 Weekly job board check: 4 of 5 boards readable, 3 with jobs in PL/NL/IE.\n"
        "Changed since last week (4):\n🔴 Acme")


def test_the_audit_lines():
    state = FakeBotState()
    board_watch.record(state, date(2026, 11, 1), [check("Acme")])
    value = state.get(board_watch.KEY)
    assert board_watch.audit_lines(value, date(2026, 10, 26), date(2026, 11, 1)) == [
        "- 1 of 1 boards readable, 1 with jobs in PL/NL/IE (checked 01 Nov).",
        "- No board changed since last week."]
    assert board_watch.audit_lines(value, date(2026, 11, 2), date(2026, 11, 8))[0].startswith(
        "- No board check this week")


def test_the_bot_runs_it_on_sunday_and_keeps_it_for_the_audit():
    d = fake_desk(prod_settings(), FAKE_TODAY, now=Clock())
    seen = []

    def reader(today, folder):
        seen.append((today, folder.is_dir()))
        return [check("Acme"), check("Beta", "HTTP 404")]

    d.board_reader = reader
    d._track_now = lambda: datetime(2026, 11, 1, 5, tzinfo=UTC)  # 10:30 in India, Sunday
    [reply] = d.board_check_tick()
    assert reply.text.startswith("🔎 Weekly job board check: 1 of 2 boards readable")
    assert seen == [(date(2026, 11, 1), True)]
    assert d.board_check_tick() == []  # done for the week
    assert d.state.get(board_watch.KEY)["ok"] == 1


def test_not_on_the_dev_bot_by_default():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d.board_reader = lambda today, folder: [check("Acme")]
    d._track_now = lambda: datetime(2026, 11, 1, 5, tzinfo=UTC)
    assert d.board_check_tick() == []
