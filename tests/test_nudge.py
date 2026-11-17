"""Apply nudge (Autopilot H7, 25 Oct): from 22:00 India time, kind reminders until tonight's
jobs are applied."""

from datetime import date, datetime, timedelta, timezone

from test_telegram_bot import Clock, settings

from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.track import nudge

IST = timezone(timedelta(hours=5, minutes=30))


class At:
    def __init__(self, hour, minute=0, day=1):
        self.now = datetime(2026, 10, day, hour, minute, tzinfo=IST)

    def __call__(self):
        return self.now

    def plus(self, minutes):
        self.now += timedelta(minutes=minutes)


def make_desk(clock, ready=3):
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d._track_now = clock
    for page_id in list(d.repo.rows)[:ready]:
        d.repo.rows[page_id]["Status"] = "Resume built"
    return d


def test_evening_crosses_midnight():
    assert nudge.evening_of(datetime(2026, 10, 2, 0, 30, tzinfo=IST)) == date(2026, 10, 1)
    assert nudge.evening_of(datetime(2026, 10, 1, 22, 0, tzinfo=IST)) == date(2026, 10, 1)


def test_first_message_then_at_most_5_reminders_30_minutes_apart():
    clock = At(21, 59)
    d = make_desk(clock)
    assert d.nudge_tick() == []  # before 22:00
    clock.plus(1)
    [first] = d.nudge_tick()
    assert first.text.startswith("🎯 3 job(s) ready to apply, about 30 minutes in all")
    assert [data for _, data in first.buttons] == ["en:low", "en:ok", "en:high", "sz:1", "dd:1"]
    assert d.nudge_tick() == []  # not again within 30 minutes
    sent = 1
    for _ in range(10):
        clock.plus(30)
        sent += len(d.nudge_tick())
    assert sent == nudge.MAX_REMINDERS


def test_energy_goal_one_job_at_a_time_and_well_done():
    clock = At(22, 5)
    d = make_desk(clock)
    d.nudge_tick()
    goal, card = d.tap("en:low")
    assert goal.text.startswith("Tonight's goal: 1 job(s).")
    assert card.text.startswith("Job 1 of 1: ")
    applied = card.buttons[0][1]
    assert applied.startswith("ia:")
    done, finish = d.tap(applied)
    assert done.text.startswith("Marked as applied")
    assert finish.text == "🎉 Well done: 1 application(s) tonight. See you tomorrow."
    clock.plus(30)
    assert d.nudge_tick() == []  # goal reached: no more reminders tonight


def test_snooze_and_done_for_today():
    clock = At(22, 0)
    d = make_desk(clock)
    d.nudge_tick()
    [snoozed] = d.tap("sz:1")
    assert snoozed.text == "😴 Snoozed: the next reminder comes after 23:00."
    clock.plus(30)
    assert d.nudge_tick() == []
    clock.plus(31)
    [reminder] = d.nudge_tick()
    assert reminder.text.startswith("⏰ 3 job(s) still ready.")
    [bye] = d.tap("dd:1")
    assert bye.text.startswith("Done for today. Rest well")
    clock.plus(30)
    assert d.nudge_tick() == []


def test_streak_grows_on_evenings_with_an_application():
    from jobengine.bot_state import FakeBotState

    state = FakeBotState()
    day = date(2026, 10, 1)
    assert nudge.record_streak(state, day) == 1
    assert nudge.record_streak(state, day) == 1  # same evening counts once
    assert nudge.record_streak(state, day + timedelta(days=1)) == 2
    assert nudge.streak(state, day + timedelta(days=2)) == 2
    assert nudge.streak(state, day + timedelta(days=4)) == 0  # a missed evening: fresh start
    assert nudge.record_streak(state, day + timedelta(days=4)) == 1


def test_nothing_ready_or_switched_off_means_no_nudge():
    assert make_desk(At(22, 0), ready=0).nudge_tick() == []
    d = make_desk(At(22, 0))
    from jobengine.config_store import ConfigStore

    store = ConfigStore.from_values({**ConfigStore.fake().all(), nudge.SWITCH_KEY: "false"})
    d.deps.config = lambda: store
    assert d.nudge_tick() == []


def test_weekend_line():
    [first] = make_desk(At(22, 0, day=3)).nudge_tick()  # Saturday
    assert "Weekend: High catches up on everything left this week." in first.text
