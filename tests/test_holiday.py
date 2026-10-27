"""Holiday mode (Autopilot I8, 27 Oct): /pause keeps finding and screening, approves and
mails nothing; /resume or the day you are back ends it."""

from datetime import UTC, date, datetime

from test_autopilot import Fetch, make_desk
from test_telegram_bot import Clock, settings, talk

from jobengine.bot_state import FakeBotState
from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.track import holiday

TODAY = date(2026, 10, 27)


def test_days_or_the_day_you_are_back():
    assert holiday.parse("5d", TODAY) == date(2026, 11, 1)
    assert holiday.parse("5 days", TODAY) == date(2026, 11, 1)
    assert holiday.parse("2026-11-05", TODAY) == date(2026, 11, 5)
    assert holiday.parse("2026-10-27", TODAY).startswith("That day has passed.")
    assert holiday.parse("90d", TODAY).startswith("At most 60 days.")
    assert holiday.parse("next week", TODAY) == holiday.USAGE


def test_on_until_the_day_you_are_back_then_a_welcome_once():
    state = FakeBotState()
    text = holiday.pause(state, "5d", TODAY)
    assert text.startswith("🏖 Holiday mode on until Sun 01 Nov (you are back that day).")
    assert holiday.active(state, date(2026, 10, 31)) == date(2026, 11, 1)
    assert holiday.active(state, date(2026, 11, 1)) is None
    assert holiday.ended(state, date(2026, 10, 31)) is None
    assert holiday.ended(state, date(2026, 11, 1)).startswith("✅ Welcome back")
    assert holiday.ended(state, date(2026, 11, 1)) is None  # once
    assert holiday.pause(state, "", TODAY) == holiday.USAGE


def test_resume_ends_it_early():
    state = FakeBotState()
    holiday.pause(state, "2026-11-05", TODAY)
    assert holiday.resume(state, TODAY).startswith("✅ Holiday mode ended")
    assert holiday.active(state, TODAY) is None
    assert holiday.resume(state, TODAY).startswith("Holiday mode is not on.")


def test_the_autopilot_fetches_and_screens_but_approves_nothing(tmp_path):
    desk = make_desk(tmp_path)
    holiday.pause(desk.state, "5d", desk.tracking_now().date())
    fetch = Fetch()
    replies = desk.autopilot(fetch)
    assert fetch.calls == 1
    summary = replies[0].text
    assert "Sent 10 jobs to the half-price batch" in summary
    assert "🏖 Holiday mode until" in summary and "wait in /pending. /resume ends it." in summary
    assert "Autopilot done" not in summary
    assert not any(r.document for r in replies)  # no resume built
    assert not any(v.get("Status") == "Approved" for v in desk.repo.rows.values())


def test_mail_queue_auto_apply_and_nudge_wait():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    holiday.pause(d.state, "5d", d.tracking_now().date())
    d.sending = lambda: True
    assert d.mail_queue_tick() == []
    assert d.on_holiday()


def test_commands_and_the_morning_check():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    assert talk(d, "/pause 3d").sent[0][1].startswith("[LOCAL] 🏖 Holiday mode on until")
    assert talk(d, "/pause").sent[0][1].startswith("[LOCAL] 🏖 Holiday mode is on until")
    assert "🏖 Holiday mode until" in d.health_report_text()
    assert talk(d, "/resume").sent[0][1].startswith("[LOCAL] ✅ Holiday mode ended")
    assert "Holiday mode" not in d.health_report_text()


def test_the_morning_check_on_the_day_you_are_back_starts_with_a_welcome():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d.state.set(holiday.KEY, {"back": "2026-10-27", "since": "2026-10-20"})
    d._track_now = lambda: datetime(2026, 10, 27, 1, 5, tzinfo=UTC)  # 06:35 in India
    replies = d.health_report_tick()
    assert replies[0].text.startswith("✅ Welcome back")
    assert replies[1].text.startswith("🩺 Morning check")
    assert d.state.get(holiday.KEY) is None


def test_follow_up_drafts_wait_while_you_are_away():
    from test_track_runner import NOW, deps, run

    d = deps()
    holiday.pause(d.state, "5d", NOW.date())
    assert run(d).followups == []
    d.state.delete(holiday.KEY)
    assert sorted(run(d).followups) == ["Anna Example", "Tomek Example"]
