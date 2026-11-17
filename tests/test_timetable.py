"""5 Nov (the autopilot timetable): days per start time and /timetable."""

from datetime import datetime
from zoneinfo import ZoneInfo

from test_autopilot_schedule import Clock, Fetch, make_desk
from test_telegram_bot import ButtonTelegram, settings, update

from jobengine import telegram_bot as tb
from jobengine.screen import schedule

IST = ZoneInfo("Asia/Kolkata")


def test_days_per_start_time_from_the_yaml_and_config():
    d = make_desk_tmp()
    p = schedule.plan(d)
    assert p.when_text() == "07:00 Mon to Sat and 19:30 Mon to Fri"
    assert p.days_for(p.starts[1]) == (0, 1, 2, 3, 4)
    assert schedule._day_spec("Mon-Sat") == (0, 1, 2, 3, 4, 5)
    assert schedule._day_spec("Sat to Mon") == (5, 6, 0)
    assert schedule._day_spec("Mon, Wed") == (0, 2)
    assert schedule._day_spec("none") == ()
    d = make_desk_tmp(**{schedule.DAYS_KEY: "07:00 Mon-Fri; 19:30 none"})
    assert schedule.plan(d).when_text() == "07:00 Mon to Fri and 19:30 no day"


def make_desk_tmp(clock=None, **config):
    import tempfile
    from pathlib import Path

    return make_desk(Path(tempfile.mkdtemp()), clock=clock, **config)


def test_saturday_runs_only_in_the_morning_and_sunday_not_at_all():
    clock = Clock(hour=7, minute=5, day=3)  # Saturday 3 Oct
    d = make_desk_tmp(clock=clock)
    fetch = Fetch()
    assert d.autopilot_scheduled(fetch) is not None and fetch.calls == 1
    clock.now = clock.now.replace(hour=19, minute=35)
    assert d.autopilot_scheduled(fetch) is None  # no 19:30 run on Saturday
    clock.now = datetime(2026, 10, 4, 7, 5, tzinfo=IST)  # Sunday
    assert d.autopilot_scheduled(fetch) is None
    assert "Next: Mon 05 Oct 07:00 Asia/Kolkata" in d.autopilot_when()[0].text
    assert schedule.missed(d) == []  # Sunday has nothing to miss


def test_timetable_command():
    d = make_desk_tmp()
    fake = ButtonTelegram([[update(1, "/timetable")]])
    tb.poll_once(tb.TelegramClient(fake), settings(), None, desk=d)
    text = fake.sent[-1][1]
    assert "🗓 Timetable" in text
    assert "07:00 Autopilot (Mon to Sat)" in text and "19:30 Autopilot (Mon to Fri)" in text
    assert "Approvals: 10 a day; a morning run uses at most 60% of the day." in text
    assert "07:30 Daily check" in text and "21:30 Daily check" in text
    # 1 Oct is European summer time: 08:00 in Warsaw is 11:30 in India (12:30 from Nov).
    assert ("11:30 to 14:30 Mails to Poland and the Netherlands (08:00 to 11:00 their time, "
            "Mon to Fri)") in text
    assert "12:30 to 15:30 Mails to Ireland" in text
    assert "21:45 Daily report" in text and "22:00 Apply nudge" in text
    assert "Friday evening before /fetch closes" in text
    assert "timetable" in [c["command"] for c in tb.bot_commands()]


def test_timetable_names_the_coming_public_holidays():
    """TT-01 (6 Nov): /timetable lists the holidays of the next 14 days, as /mailqueue does."""
    d = make_desk_tmp()
    d._track_now = Clock(day=20)  # Tue 20 Oct
    text = d.timetable_command()[0].text
    assert ("No mails on these public holidays (next 14 days): Mon 26 Oct: October bank "
            "holiday in Ireland.") in text
    d._track_now = Clock(day=1)  # 1 to 14 Oct: none
    text = d.timetable_command()[0].text
    assert "No public holiday in Poland, the Netherlands or Ireland in the next 14 days." in text
