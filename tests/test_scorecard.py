"""Daily scorecard (S1, 23 Nov, your ask): one line a day in the evening report and one row a
day in Notion Daily Scorecard. Made-up days, no Notion."""

import logging
from datetime import date, datetime

import pytest
from test_telegram_bot import settings

from jobengine.apply import form_bot
from jobengine.apply.form_bot import Result
from jobengine.bot_state import FakeBotState
from jobengine.notion_repo import FakeTable
from jobengine.track import day_ledger, run_log, scorecard

START = datetime(2026, 11, 23, 10, 15)
log = logging.getLogger("jobengine.test")

ENTRY = {"sweeps": [{"at": "07:02", "sources": {"gmail": 30}, "new": 12},
                    {"at": "19:31", "sources": {"ats": 2}, "new": 4}],
         "screened.Apply high": 2, "screened.Skip": 9, "resumes": 3, "forms": 2,
         "mails_sent": 1, "replies": 1, "cost": 0.123456}


@pytest.fixture()
def bound(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    root = logging.getLogger()
    counter = run_log.install(root)
    state, table = FakeBotState(), FakeTable()
    day_ledger.bind(state, lambda: START.date(), lambda: START)
    scorecard.bind(table, state, lambda: START)
    yield state, table
    root.removeHandler(counter)
    scorecard.bind(None, None, lambda: START)
    day_ledger.bind(None, lambda: START.date(), lambda: START)


def test_the_line_and_the_numbers_of_a_day():
    assert scorecard.numbers(ENTRY) == {"Found": 16, "Screened": 11, "Resumes": 3, "Forms": 2,
                                        "Mails": 1, "Replies": 1, "Problems": 0}
    assert scorecard.line(ENTRY) == ("📊 Today: Found 16 · Screened 11 · Resumes 3 · Forms 2 · "
                                     "Mails 1 · Replies 1 · Problems 0")
    assert scorecard.line({}) == ("📊 Today: Found 0 · Screened 0 · Resumes 0 · Forms 0 · "
                                  "Mails 0 · Replies 0 · Problems 0")
    bad = {**ENTRY, "problems": 2, "failed": ["Link check 10:15", "/fetch 19:31"]}
    assert scorecard.line(bad).splitlines()[1] == (
        "❌ Failed or with errors: Link check 10:15, /fetch 19:31 (Run Log has why)")
    row = scorecard.row(bad, date(2026, 11, 23), START)
    assert row["Result"] == "Problems" and row["Problems"] == 2
    assert row["Failed steps"] == "Link check 10:15, /fetch 19:31"
    assert row["AI cost"] == 0.1235 and row["Day"] == "2026-11-23"


def test_a_failed_run_is_named_and_the_row_kept_up_to_date(bound):
    state, table = bound
    with pytest.raises(RuntimeError), run_log.recorded("Link check", None, lambda: START):
        raise RuntimeError("Notion 500")
    entry = day_ledger.day(state, START.date())
    assert entry["failed"] == ["Link check 10:15"] and entry["problems"] == 1
    assert len(table.created) == 1
    assert table.created[0]["Result"] == "Problems"
    assert table.created[0]["Failed steps"] == "Link check 10:15"

    with run_log.recorded("/fetch", None, lambda: START) as run:  # a good run: same row
        run.new_jobs = 5
        day_ledger.add_sweep({"gmail": 5}, 5, 0)
    assert len(table.created) == 1 and table.created[0]["Found"] == 5

    with run_log.recorded("Idle tick", None, lambda: START):  # did nothing: no Notion write
        pass
    assert len(table.created) == 1


def test_a_logged_error_counts_as_a_problem(bound):
    state, _table = bound
    with run_log.recorded("/screen", None, lambda: START) as run:
        run.screened = 3
        log.error("AI said no")
    assert day_ledger.day(state, START.date())["failed"] == ["/screen 10:15"]


def test_the_scorecard_never_stops_a_run(bound):
    class Broken(FakeTable):
        def upsert(self, *a, **k):
            raise RuntimeError("Notion down")

    scorecard.bind(Broken(), bound[0], lambda: START)
    with run_log.recorded("/fetch", None, lambda: START) as run:
        run.new_jobs = 1  # ends normally, the scorecard problem is only logged


def test_a_filled_form_is_counted_a_stopped_one_is_not(bound, monkeypatch, tmp_path):
    import sys
    import types

    state, _table = bound
    fake = types.ModuleType("playwright.sync_api")  # CI has no Playwright: dry_run stops early
    fake.sync_playwright = None
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake)
    for stopped in ("", "the page asks for a captcha", ""):
        monkeypatch.setattr(form_bot, "_run",
                            lambda *a, stopped=stopped, **k: Result(url="x", stopped=stopped))
        form_bot.dry_run(settings(), "https://jobs.example.com/apply", {}, [], folder=tmp_path)
    assert day_ledger.day(state, START.date())["forms"] == 2
