"""LOG-13 (24 Nov test log, T23-16 note 1): /today said "Nothing new since the last check."
while its Run Log page listed "DRY RUN: 10 follow-ups not drafted" with 10 names. Follow-ups
due are for you: one line in the short message."""

from datetime import date, datetime

from jobengine.track.runner import DailyReport

NAMES = [f"Person {n} (Co {n})" for n in range(10)]


def report(**kw):
    return DailyReport(day=date(2026, 11, 24), since=datetime(2026, 11, 23, 21, 30),
                       tokens={"alerts": None, "sender": None}, **kw)


def test_follow_ups_due_are_in_the_short_message():
    text = report(followups_dry=NAMES).short_text()
    assert "Nothing new since the last check." not in text
    assert ("✉️ 10 follow-up(s) due, not drafted (DRY RUN): Person 0 (Co 0), "
            "Person 1 (Co 1), Person 2 (Co 2), Person 3 (Co 3), Person 4 (Co 4) and 5 more"
            ) in text
    # The full list stays on the Run Log page.
    assert "DRY RUN: 10 follow-ups not drafted (Person 0 (Co 0)" in report(
        followups_dry=NAMES).text()


def test_the_counts_stay_and_a_few_names_are_all_shown():
    text = report(followups_dry=NAMES[:2], sent=["A"]).short_text()
    assert "1 sent" in text
    assert text.endswith("2 follow-up(s) due, not drafted (DRY RUN): Person 0 (Co 0), "
                         "Person 1 (Co 1)")
    assert report().short_text().endswith("Nothing new since the last check.")
