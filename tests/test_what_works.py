"""What works (S6, 23 Nov, your ask): found > approved > applied > answered by source,
country and verdict, the speed from posting to applying, and a tip. Made-up jobs."""

from datetime import date, timedelta

from test_telegram_bot import CHAT_ID, Clock, settings

from jobengine import telegram_bot as tb
from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.track import what_works as ww

D = date(2026, 11, 10)


def job(board, status="New", country="Poland", verdict="Skip", posted=None, applied=None):
    return {"Board": board, "Status": status, "Country": country, "Screen verdict": verdict,
            "Posted date": posted, "Applied date": applied}


JOBS = (
    [job("Company sites", "Applied", verdict="Apply high", posted=D, applied=D)]
    + [job("Company sites", "Interview", verdict="Apply high", posted=D,
           applied=D + timedelta(days=1))]
    + [job("Company sites", "Approved", verdict="Apply normal") for _ in range(4)]
    + [job("Company sites") for _ in range(6)]
    + [job("Adzuna", "Applied", "Netherlands", "Apply normal", D, D + timedelta(days=6))]
    + [job("Adzuna", country="Netherlands") for _ in range(39)]
    + [job("Jooble") for _ in range(31)]
)


def test_groups_count_each_stage():
    rows = ww.groups(JOBS, "Board")
    sites = rows["Company sites"]
    assert (sites.found, sites.approved, sites.applied, sites.answered, sites.interview) == \
        (12, 6, 2, 1, 1)
    assert rows["Jooble"].found == 31 and rows["Jooble"].approved == 0


def test_speed_from_posting_to_applying():
    assert ww.speed(JOBS) == (3, 1.0, 2)
    assert ww.speed([job("X")]) == (0, None, 0)


def test_the_block_with_its_tips():
    text = ww.text(JOBS)
    lines = text.splitlines()
    assert lines[0].startswith("📈 What works (all jobs in Notion")
    assert "By source:" in lines and "By country:" in lines and "By verdict:" in lines
    assert "- Company sites: 12 > 6 > 2 > 1 (50% approved), 1 interview(s)" in lines
    assert "- Jooble: 31 > 0 > 0 > 0 (0% approved)" in lines
    assert ("Speed: 3 applied job(s), median 1 day(s) from posting to applying; 2 of 3 within "
            "2 days") in text
    assert "Most approvals for what it finds: Company sites (6 of 12, 50%)." in text
    assert "Many found, none approved: Jooble: candidates to drop (FETCH 11)." in text
    assert ww.text([]) == "What works: no jobs in Notion yet."


def test_more_than_six_groups_are_folded():
    many = [job(f"Board {n}") for n in range(9)]
    lines = ww.split_lines(ww.groups(many, "Board"))
    assert len(lines) == 7 and lines[-1].startswith("- others (3): 3 > 0")


def test_stats_works_command_and_the_digest():
    desk = fake_desk(settings(), FAKE_TODAY, now=Clock())
    update = {"update_id": 1, "message": {"chat": {"id": int(CHAT_ID)},
                                          "text": "/stats_works"}}
    [reply] = tb.desk_replies(tb._expanded(update), settings(), desk)
    assert reply.text.startswith("📈 What works")
    [digest] = desk.digest_command()
    assert "📈 What works" in digest.text
    assert "/stats works: what works" in tb.HELP_TEXT
