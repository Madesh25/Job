"""LOG-09 (24 Nov test log, T23-01 and T23-03): /fetchreport adzuna still listed all 6 sources
at the top; the last line said 25 jobs per source and reason while about 8 per source were
shown; the Run Log said Sites blocked 64 while the /fetch page listed 30."""

import logging

from jobengine.sweep.models import MAX_DROPPED_PER_REASON, LossReport, SweepSummary
from jobengine.track import run_log
from jobengine.track.error_log import Collector

LABELS = {"gmail": "Email alerts", "adzuna": "Adzuna", "jooble": "Jooble"}


def report():
    r = LossReport()
    for source in LABELS:
        r.add(source, "saved as a new job", "")
        for i in range(30):
            r.add(source, "title not DevOps-type", f"{source} job {i}")
    r.add("gmail", "Needs Dutch", "Dutch job | Acme")
    return r


def test_a_source_word_shows_only_that_source_at_the_top():
    text = report().report_text(LABELS, "2026-11-24", "adzuna")
    top = text.split("\n\n")[0]
    assert "- Adzuna: 31 read" in top
    assert "Email alerts" not in top and "Jooble" not in top
    # A reason word keeps the sources that have that reason.
    dutch = report().report_text(LABELS, "2026-11-24", "dutch").split("\n\n")[0]
    assert "- Email alerts:" in dutch and "Adzuna" not in dutch


def test_the_last_line_says_what_is_shown_and_how_to_see_more():
    text = report().report_text(LABELS, "2026-11-24", "title")
    assert "title not DevOps-type (90):" in text
    assert text.count("[Adzuna]") in (13, 14)  # 40 taken in turn from 3 sources
    assert (f"(Shown: up to 40 jobs per reason, taken in turn from each source. The report "
            f"keeps up to {MAX_DROPPED_PER_REASON} jobs per source and reason; the counts "
            "are complete. Add a source for more of one, for example /fetchreport title "
            "adzuna.)") in text


def test_two_words_narrow_to_one_source_and_reason():
    text = report().report_text(LABELS, "2026-11-24", "title adzuna")
    assert text.count("[Adzuna]") == MAX_DROPPED_PER_REASON  # every kept job
    assert "[Jooble]" not in text and "Dutch job" not in text
    assert "title not DevOps-type (30):" in text
    assert "- ... and 5 more" in text
    assert "taken in turn" not in text and "Add a source" not in text
    assert "No dropped jobs match 'title zzz'." in report().report_text(LABELS, "x",
                                                                         "title zzz")


def test_the_fetch_page_counts_its_lists():
    text = SweepSummary(blocked_sites=["A (HTTP 403)", "B (job board HTTP 404)"],
                        no_board_sites=["C"]).friendly_text()
    assert "- Blocked us (2): A (HTTP 403), B (job board HTTP 404)" in text
    assert "- No job board found (1): C" in text


class Table:
    def __init__(self):
        self.created = []

    def create(self, values):
        self.created.append(values)


def refused(collector, host, times):
    for _ in range(times):
        collector.emit(logging.LogRecord(
            "jobengine.sweep", logging.INFO, "/app/src/jobengine/sweep/fulltext.py", 1,
            "job page not read https://%s/job/1: HTTP 403", (host,), None))


def test_a_site_refusing_many_times_is_one_site_blocked():
    from datetime import UTC, datetime

    rows, collector = Table(), Collector()
    with run_log.recorded("Fetch", rows, lambda: datetime(2026, 11, 24, 10, tzinfo=UTC)):
        refused(collector, "www.pracuj.pl", 5)
        refused(collector, "jobs.example.com", 2)
    assert rows.created[0]["Sites blocked"] == 2  # was 7: every refused request


def test_a_fetch_writes_the_number_its_page_lists():
    from datetime import UTC, datetime

    rows, collector = Table(), Collector()
    with run_log.recorded("Fetch", rows, lambda: datetime(2026, 11, 24, 10, tzinfo=UTC)):
        refused(collector, "www.pracuj.pl", 3)
        run_log.set_sites_blocked(30)  # the runner, from the page's "Blocked us" list
        refused(collector, "late.example.com", 1)
    assert rows.created[0]["Sites blocked"] == 30
