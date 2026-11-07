"""Phase 22 (7 Nov): /status said "Last daily check: never" although the check ran (it had a
problem, so it did not count) and did not show holiday mode (Q-06, Q-10)."""

from dataclasses import replace
from datetime import date, datetime

from test_track_runner import NOW, deps

from jobengine.track import commands, holiday, runner


def test_a_check_with_a_token_problem_is_shown_with_the_problem():
    d = replace(deps(), token_check=lambda name: "invalid_grant" if name == "alerts" else None)
    report = runner.run_daily(d, NOW)
    assert not report.ok
    assert d.state.get(runner.MARKER) is None  # not counted as done
    assert d.state.get(runner.LAST_RUN) == {"at": NOW.isoformat(), "ok": False,
                                            "problem": "Gmail alerts token: invalid_grant"}
    assert runner.last_check_line(d.state) == (
        "Last daily check: never (the last one, 2026-10-15 08:00, had a problem: Gmail "
        "alerts token: invalid_grant)")
    assert "had a problem: Gmail alerts token" in commands.status_lines(d, NOW.date())


def test_an_ok_check_is_the_last_daily_check():
    d = deps()
    assert runner.run_daily(d, NOW).ok
    assert runner.last_check_line(d.state) == "Last daily check: 2026-10-15 08:00"
    later = datetime.fromisoformat("2026-10-15T21:30:00+05:30")
    d2 = replace(d, token_check=lambda name: "expired" if name == "sender" else None)
    runner.run_daily(d2, later)
    assert runner.last_check_line(d.state) == (
        "Last daily check: 2026-10-15 08:00 (the last one, 2026-10-15 21:30, had a problem: "
        "Gmail sender token: expired)")


def test_status_shows_holiday_mode():
    d = deps()
    assert "Holiday mode" not in commands.status_lines(d, date(2026, 11, 7))
    holiday.pause(d.state, "2d", date(2026, 11, 7))
    text = commands.status_lines(d, date(2026, 11, 7))
    assert "🏖 Holiday mode on until Mon 09 Nov" in text and "/resume ends it." in text
    holiday.resume(d.state, date(2026, 11, 7))
    assert "Holiday mode" not in commands.status_lines(d, date(2026, 11, 7))
