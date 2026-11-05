"""5 Nov (the autopilot timetable): the daily check by itself at 07:30 and 21:30 India time,
on the laptop and on Cloud Run."""

from datetime import datetime
from zoneinfo import ZoneInfo

from test_telegram_bot import settings

from jobengine.bot_state import FakeBotState
from jobengine.config_store import ConfigStore
from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.track import timed

IST = ZoneInfo("Asia/Kolkata")


def at(hour, minute=0, day=1):
    return datetime(2026, 10, day, hour, minute, tzinfo=IST)


def test_the_two_check_times_once_each_within_their_window():
    config, state = ConfigStore.from_values({}), FakeBotState()
    assert timed.due(config, state, at(7, 29)) is None
    first = timed.due(config, state, at(7, 31))
    assert f"{first:%H:%M}" == "07:30"
    timed.mark(state, at(7, 31).date(), first)
    assert timed.due(config, state, at(8, 0)) is None  # once
    assert timed.due(config, state, at(11, 0)) is None  # after the 3-hour window
    evening = timed.due(config, state, at(21, 40))
    assert f"{evening:%H:%M}" == "21:30"
    timed.mark(state, at(21, 40).date(), evening)
    assert state.get(timed.LAST_KEY) == {"date": "2026-10-01", "done": ["07:30", "21:30"]}
    assert f"{timed.due(config, state, at(7, 45, day=2)):%H:%M}" == "07:30"  # next day
    off = ConfigStore.from_values({timed.SWITCH_KEY: "false"})
    assert timed.due(off, FakeBotState(), at(7, 31)) is None
    other = ConfigStore.from_values({timed.TIMES_KEY: "06:00"})
    assert [f"{t:%H:%M}" for t in timed.times(other)] == ["06:00"]


def test_the_extras_of_the_cloud_run_task_come_once_a_day():
    state = FakeBotState()
    assert timed.extras_due(state, at(7, 30).date())
    assert not timed.extras_due(state, at(21, 30).date())
    assert timed.extras_due(state, at(7, 30, day=2).date())


def test_the_laptop_bot_runs_the_check_by_itself_and_autopilot_when_names_it():
    clock = {"now": at(7, 35)}
    d = fake_desk(settings(), FAKE_TODAY, now=lambda: clock["now"])
    d._track_now = lambda: clock["now"]
    replies = d.tracking_tick()
    assert replies and replies[0].text  # the daily check report
    assert d.tracking_tick() == []  # once for 07:30
    clock["now"] = at(21, 31)
    assert d.tracking_tick()
    assert ("Daily check: by itself at 07:30 and 21:30 (replies, applications, rejections, "
            "follow-ups).") in d.autopilot_when()[0].text
