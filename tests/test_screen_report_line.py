"""LOG-12 (24 Nov test log, T23-02 note 1): /fetch and the S10 messages end with the Run Log row
line, /screen did not, although its page holds the full text."""

from datetime import UTC, datetime

from test_review_one_at_a_time import make_desk

from jobengine.screen.models import ScreenSummary
from jobengine.track import run_log


class Table:
    def __init__(self):
        self.created = []

    def create(self, values):
        self.created.append(values)


def test_screen_says_where_its_full_report_is():
    d, rows = make_desk(), Table()
    with run_log.recorded("/screen", rows, lambda: datetime(2026, 11, 24, 10, 5, tzinfo=UTC)):
        text = d._summary_text(ScreenSummary(waiting_for_jd=0))
    assert text.startswith("Screening done: 0 screened")
    assert text.endswith('\U0001F4DD Full report in Notion: Run Log, row "/screen 10:05"')
    assert "screened earlier still wait in /pending.\n\U0001F4DD" in text  # the line before


def test_no_line_without_a_run_log_row():
    d = make_desk()
    assert "Full report in Notion" not in d._summary_text(ScreenSummary(waiting_for_jd=0))
    with run_log.recorded("/screen", None, lambda: datetime(2026, 11, 24, tzinfo=UTC)):
        assert "Full report in Notion" not in d._summary_text(ScreenSummary(waiting_for_jd=0))
