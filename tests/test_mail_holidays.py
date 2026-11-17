"""5 Nov (the autopilot timetable): no mail on a public holiday in the recipient's country;
it goes out the next working morning."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from jobengine.config_store import ConfigStore
from jobengine.mail import holidays, timing

WARSAW = ZoneInfo("Europe/Warsaw")
W = timing.window({}, ConfigStore.from_values({}))  # Mon to Fri, 08:00 to 10:00


def test_easter_and_the_dates_that_move_with_it():
    assert holidays.easter(2026) == date(2026, 4, 5)
    assert holidays.easter(2027) == date(2027, 3, 28)
    assert holidays.holiday("Poland", date(2026, 6, 4)) == "Corpus Christi"
    assert holidays.holiday("Netherlands", date(2026, 5, 14)) == "Ascension Day"
    assert holidays.holiday("Netherlands", date(2026, 5, 25)) == "Whit Monday"
    assert holidays.holiday("Ireland", date(2026, 4, 6)) == "Easter Monday"


def test_each_country_has_its_own_days():
    assert holidays.holiday("Poland", date(2026, 11, 11)) == "Independence Day"
    assert holidays.holiday("Netherlands", date(2026, 11, 11)) is None
    assert holidays.holiday("Poland", date(2026, 12, 24)) == "Christmas Eve"  # from 2025
    assert holidays.holiday("Netherlands", date(2026, 4, 27)) == "King's Day"
    assert holidays.holiday("Netherlands", date(2025, 4, 26)) == "King's Day"  # 27th a Sunday
    assert holidays.holiday("Ireland", date(2026, 3, 17)) == "St Patrick's Day"
    assert holidays.holiday("Ireland", date(2026, 2, 2)) == "St Brigid's Day"
    assert holidays.holiday("Ireland", date(2027, 2, 1)) == "St Brigid's Day"
    assert holidays.holiday("Ireland", date(2026, 10, 26)) == "October bank holiday"
    assert holidays.holiday("Germany", date(2026, 12, 25)) is None  # not a target country


def test_an_irish_holiday_on_a_weekend_moves_to_the_next_working_day():
    # 2026: St Stephen's Day is a Saturday, so Monday 28 Dec is the day off.
    assert holidays.holiday("Ireland", date(2026, 12, 28)) == "St Stephen's Day"
    # 2027: Christmas is a Saturday and St Stephen's a Sunday: Monday and Tuesday off.
    assert holidays.holiday("Ireland", date(2027, 12, 27)) == "Christmas Day"
    assert holidays.holiday("Ireland", date(2027, 12, 28)) == "St Stephen's Day"


def test_the_window_stays_closed_on_a_holiday_and_opens_the_next_working_morning():
    wednesday_11_nov = datetime(2026, 11, 11, 8, 30, tzinfo=WARSAW)
    assert not timing.inside(W, "Poland", wednesday_11_nov)
    assert timing.inside(W, "Netherlands", wednesday_11_nov)  # not a Dutch holiday
    tuesday = datetime(2026, 11, 10, 11, 0, tzinfo=WARSAW)  # after Tuesday's window
    assert timing.next_start(W, "Poland", tuesday) == datetime(2026, 11, 12, 8, 0,
                                                               tzinfo=WARSAW)
    assert timing.next_start(W, "Netherlands", tuesday) == datetime(
        2026, 11, 11, 8, 0, tzinfo=ZoneInfo("Europe/Amsterdam"))
    # Christmas: Thursday 24 Dec in Poland, then the 25th and 26th, then the weekend.
    wednesday = datetime(2026, 12, 23, 11, 0, tzinfo=WARSAW)
    assert timing.next_start(W, "Poland", wednesday) == datetime(2026, 12, 28, 8, 0,
                                                                 tzinfo=WARSAW)


def test_config_off_sends_on_holidays_and_mailqueue_names_the_coming_ones():
    off = timing.window({}, ConfigStore.from_values({holidays.SWITCH_KEY: "off"}))
    assert not off.holidays
    assert timing.inside(off, "Poland", datetime(2026, 11, 11, 8, 30, tzinfo=WARSAW))
    assert off.text() == "Mon to Fri, 08:00 to 11:00 the recipient's time"
    assert timing.holidays_ahead(W, date(2026, 11, 5)) == [
        "Wed 11 Nov: Independence Day in Poland"]
    assert timing.holidays_ahead(off, date(2026, 11, 5)) == []
