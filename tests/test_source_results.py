"""Fetch idea 4 (13 Nov): which source leads to applications and answers."""

from datetime import date

from jobengine.track.stats import board_results, board_text


def job(board, status, applied=None):
    return {"Board": board, "Status": status, "Applied date": applied}


JOBS = [job("LinkedIn", "New"), job("LinkedIn", "Screened"), job("LinkedIn", "Approved"),
        job("LinkedIn", "Applied", date(2026, 11, 1)),
        job("LinkedIn", "Interview", date(2026, 11, 2)),
        job("Adzuna", "Screened"), job("Adzuna", "Expired"), job("Adzuna", "New"),
        job("Company site", "Rejected", date(2026, 11, 3)), job("Company site", "Resume built"),
        job("Company site", "Skipped"),
        job("Glassdoor", "New"), job("IamExpat", "Applied", date(2026, 11, 4))]


def test_each_source_from_found_to_answered():
    rows = board_results(JOBS)
    li = rows["LinkedIn"]
    assert (li.found, li.approved, li.applied, li.answered, li.interview) == (5, 3, 2, 1, 1)
    site = rows["Company site"]
    assert (site.found, site.approved, site.applied, site.answered) == (3, 2, 1, 1)  # rejected
    assert rows["Adzuna"].approved == 0


def test_the_report_block():
    text = board_text(JOBS)
    assert text.splitlines() == [
        "Each source, all jobs in Notion (found > approved > applied > answered):",
        "- LinkedIn: 5 > 3 > 2 > 1, 1 interview",
        "- Company site: 3 > 2 > 1 > 1",
        "- Adzuna: 3 > 0 > 0 > 0",
        "- others (2 boards): 2 > 1 > 1 > 0",
        "A source with many found and none approved is a candidate to drop (decide in FETCH 11).",
    ]
    assert board_text([]) == ""


def test_fetchreport_adds_the_block():
    from test_review_one_at_a_time import make_desk

    from jobengine.sweep.models import LossReport
    from jobengine.sweep.runner import LAST_REPORT

    d = make_desk()
    report = LossReport().to_state(date(2026, 11, 13))
    d.state.set(LAST_REPORT, {**report, "at": "2026-11-13 07:00"})
    replies = d.fetchreport_command()
    assert replies[-1].text.startswith("Each source, all jobs in Notion")
