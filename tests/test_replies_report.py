"""PR 9: the weekly "what gets replies" report (track/stats.replies_report)."""

from datetime import date, timedelta

from test_telegram_bot import Clock, settings

from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.track.stats import replies_report

TODAY = date(2026, 10, 15)


def job(board, country, status, days_ago=5):
    return {"Board": board, "Country": country, "Status": status,
            "Applied date": TODAY - timedelta(days=days_ago)}


def contact(kind, status, days_ago=5):
    return {"Type": kind, "Status": status, "Last contacted": TODAY - timedelta(days=days_ago)}


JOBS = [
    job("Company site", "Poland", "Interview"),
    job("Company site", "Poland", "Replied"),
    job("Company site", "Netherlands", "Applied"),
    job("Adzuna", "Poland", "Applied"),
    job("Adzuna", "Netherlands", "Ghosted"),
    job("Adzuna", "Netherlands", "Applied"),
    job("Adzuna", "Ireland", "Applied", days_ago=40),  # outside the 30 days
]
CONTACTS = [
    contact("Peer engineer", "Replied"), contact("Peer engineer", "Replied"),
    contact("Peer engineer", "Contacted"),
    contact("Recruiter/TA", "Contacted"), contact("Recruiter/TA", "Contacted"),
    contact("Recruiter/TA", "Replied"),
    contact("Hiring", "Drafted"),  # never mailed: not counted
]


def test_report_by_board_country_and_mail_with_a_tip():
    text = replies_report(JOBS, CONTACTS, TODAY)
    lines = text.splitlines()
    assert lines[0] == ("What gets replies (last 30 days, 6 applied, 2 replied, "
                        "1 interviews):")
    assert lines[1] == ("- By board: Company site 2/3 replied (67%), 1 interview; "
                        "Adzuna 0/3 replied (0%)")
    assert lines[2] == ("- By country: Poland 2/3 replied (67%), 1 interview; "
                        "Netherlands 0/3 replied (0%)")
    assert lines[3] == ("- By mail: referral ask (engineers) 2/3 replied (67%); "
                        "cold mail (HR) 1/3 replied (33%)")
    assert lines[4] == (
        "Put more effort where it works: board: most replies from Company site (67%), fewest "
        "from Adzuna (0%); country: most replies from Poland (67%), fewest from Netherlands "
        "(0%); mail: referral ask (engineers) gets more replies (67%) than cold mail (HR) "
        "(33%).")


def test_no_tip_from_small_groups():
    text = replies_report(JOBS[:2] + JOBS[3:4], [], TODAY)
    assert text.endswith("Not enough data for a tip yet (needs at least 3 in two groups).")
    assert "Put more effort" not in text


def test_nothing_yet():
    assert replies_report([], [], TODAY) == ("What gets replies (last 30 days): nothing "
                                             "applied or mailed yet.")


def test_the_weekly_digest_ends_with_the_report():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    text = d.digest_command()[0].text
    assert "\n\nWhat gets replies (last 30 days, " in text
    assert "- By mail: referral ask (engineers) " in text
