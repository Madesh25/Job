"""Public holidays in Poland, the Netherlands and Ireland (5 Nov, the autopilot timetable).

A mail sent on a public holiday waits unread until the next working day, under everything
else. So the send window (mail/timing.py) skips the recipient country's public holidays and
the mail goes out on the next working morning instead. No library: fixed dates, the dates
that move with Easter, and Ireland's Monday holidays are worked out here.

- Poland (days off by law): 1 Jan, 6 Jan, Easter Monday, 1 May, 3 May, Corpus Christi,
  15 Aug, 1 Nov, 11 Nov, 24 Dec (from 2025), 25 Dec, 26 Dec. Easter and Whit Sunday fall on
  a Sunday anyway.
- Netherlands: 1 Jan, Easter Monday, King's Day (27 Apr, 26 Apr when the 27th is a
  Sunday), Ascension Day, Whit Monday, 25 Dec, 26 Dec. Good Friday and Liberation Day are
  not general days off, so mail goes out then.
- Ireland: 1 Jan, St Brigid's Day (the first Monday in February, or 1 Feb when it is a
  Friday), St Patrick's Day (17 Mar), Easter Monday, the first Monday in May, June and
  August, the last Monday in October, Christmas Day and St Stephen's Day. A fixed date on a
  weekend moves to the next working day, as Irish law does.

Notion Config `mail.holidays` = `off` sends on holidays too.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

SWITCH_KEY = "mail.holidays"  # Notion Config: "off" ignores public holidays


def easter(year: int) -> date:
    """Western Easter Sunday (the anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


def _nth_monday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(7 - first.weekday()) % 7 + 7 * (n - 1))


def _last_monday(year: int, month: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=last.weekday())


def _poland(year: int) -> dict[date, str]:
    e = easter(year)
    days = {date(year, 1, 1): "New Year's Day", date(year, 1, 6): "Epiphany",
            e + timedelta(days=1): "Easter Monday", date(year, 5, 1): "Labour Day",
            date(year, 5, 3): "Constitution Day", e + timedelta(days=60): "Corpus Christi",
            date(year, 8, 15): "Assumption Day", date(year, 11, 1): "All Saints' Day",
            date(year, 11, 11): "Independence Day", date(year, 12, 25): "Christmas Day",
            date(year, 12, 26): "Second Day of Christmas"}
    if year >= 2025:
        days[date(year, 12, 24)] = "Christmas Eve"
    return days


def _netherlands(year: int) -> dict[date, str]:
    e = easter(year)
    kings = date(year, 4, 27)
    if kings.weekday() == 6:
        kings = date(year, 4, 26)
    return {date(year, 1, 1): "New Year's Day", e + timedelta(days=1): "Easter Monday",
            kings: "King's Day", e + timedelta(days=39): "Ascension Day",
            e + timedelta(days=50): "Whit Monday", date(year, 12, 25): "Christmas Day",
            date(year, 12, 26): "Second Day of Christmas"}


def _ireland(year: int) -> dict[date, str]:
    days: dict[date, str] = {}
    fixed = [(date(year, 1, 1), "New Year's Day"), (date(year, 3, 17), "St Patrick's Day"),
             (date(year, 12, 25), "Christmas Day"), (date(year, 12, 26), "St Stephen's Day")]
    for day, name in fixed:
        while day.weekday() >= 5 or day in days:  # a weekend date moves to a working day
            day += timedelta(days=1)
        days[day] = name
    feb1 = date(year, 2, 1)
    days[feb1 if feb1.weekday() == 4 else _nth_monday(year, 2, 1)] = "St Brigid's Day"
    days[easter(year) + timedelta(days=1)] = "Easter Monday"
    days[_nth_monday(year, 5, 1)] = "May bank holiday"
    days[_nth_monday(year, 6, 1)] = "June bank holiday"
    days[_nth_monday(year, 8, 1)] = "August bank holiday"
    days[_last_monday(year, 10)] = "October bank holiday"
    return days


BUILDERS = {"Poland": _poland, "Netherlands": _netherlands, "Ireland": _ireland}


@lru_cache(maxsize=32)
def _year(country: str, year: int) -> dict[date, str]:
    build = BUILDERS.get(country)
    return build(year) if build else {}


def holiday(country: str | None, day: date) -> str | None:
    """The public holiday's name on this day in this country, or None."""
    return _year((country or "").strip(), day.year).get(day)


def enabled(config: object) -> bool:
    get = getattr(config, "get", None)
    value = get(SWITCH_KEY) if callable(get) else None
    return str(value or "").strip().lower() not in ("off", "false", "no")
