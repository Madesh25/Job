"""Run Log (18 Nov, your request): one Notion row per scheduled run with what it did and how
it went. Made-up runs, no Notion."""

import logging
from datetime import datetime

import pytest
from test_autopilot_schedule import make_desk
from test_telegram_bot import Clock, settings
from test_web_app import Recorder
from test_web_app import settings as web_settings

from jobengine import telegram_bot as tb
from jobengine.bot_state import FakeBotState
from jobengine.notion_repo import FakeTable
from jobengine.screen.desk import Reply, fake_desk
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.track import day_ledger, run_log
from jobengine.web import app as web

START = datetime(2026, 11, 18, 7, 0)
log = logging.getLogger("jobengine.test")


@pytest.fixture(autouse=True)
def counted(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    root = logging.getLogger()
    counter = run_log.install(root)
    state = FakeBotState()
    day_ledger.bind(state, lambda: START.date(), lambda: START)
    yield
    root.removeHandler(counter)
    day_ledger.bind(None, lambda: START.date(), lambda: START)


def test_redact_blanks_emails_keys_tokens_and_link_queries():
    text = ("mail jane.doe@example.com at https://jobs.example.com/apply?token=abc#x "
            "key=sk-live-1 Bearer abcdef abcdefghijklmnopqrstuvwxyz0123456789ABCD "
            "the token has expired")
    out = run_log.redact(text)
    assert "jane.doe" not in out and "<email>" in out
    assert "https://jobs.example.com/apply " in out and "token=abc" not in out
    assert "sk-live-1" not in out and "abcdef " not in out
    assert "0123456789" not in out
    assert out.endswith("the token has expired")  # plain words stay


def test_a_run_that_did_nothing_writes_no_row():
    table = FakeTable()
    with run_log.recorded("Autopilot", table, lambda: START):
        pass
    assert table.created == []


def test_a_run_row_has_its_numbers():
    table = FakeTable()
    with run_log.recorded("Autopilot", table, lambda: START) as run:
        day_ledger.add_sweep({"gmail": 5}, new=3, updated=1)
        day_ledger.count("screened.Apply high", 2)
        day_ledger.count("screened.Skip")
        day_ledger.count("applied")
        day_ledger.count("mails_sent", 2)
        day_ledger.add_cost(FakeBotState(), START.date(), 0.0123)
        log.warning("a site was slow")
        run.note([Reply("Autopilot done\n3 new jobs"), "plain text"])
    [row] = table.created
    assert row["Run"] == "Autopilot 07:00" and row["Step"] == "Autopilot"
    assert row["Where"] == "PC" and row["Result"] == "OK"
    assert (row["New jobs"], row["Screened"], row["Applied"], row["Mails sent"]) == (3, 3, 1, 2)
    assert row["AI cost"] == 0.0123 and row["Warnings"] == 1 and row["Errors"] == 0
    assert row["Summary"] == "Autopilot done\n3 new jobs\nplain text"
    assert row["Failure"] == "" and row["Started"].startswith("2026-11-18T07:00")


def test_a_logged_error_makes_the_result_problems():
    table = FakeTable()
    with run_log.recorded("Mail queue", table, lambda: START):
        log.error("send failed")
        logging.getLogger("other.library").error("not ours")
    [row] = table.created
    assert row["Result"] == "Problems" and row["Errors"] == 1


def test_an_exception_is_failed_redacted_and_raised_again():
    table = FakeTable()
    with pytest.raises(RuntimeError), run_log.recorded("Fetch", table, lambda: START):
        raise RuntimeError("login failed for jane.doe@example.com")
    [row] = table.created
    assert row["Result"] == "Failed" and row["Failure"] == "login failed for <email>"


def test_a_broken_table_never_fails_the_run():
    class Broken:
        def create(self, props):
            raise RuntimeError("Notion is down")

    with run_log.recorded("Fetch", Broken(), lambda: START) as run:
        run.note(["done"])  # no exception comes out


def test_numbers_go_only_to_the_runs_going_on():
    table = FakeTable()
    day_ledger.count("applied")  # before the run: not counted
    with run_log.recorded("Auto-apply", table, lambda: START):
        day_ledger.count("applied")
    day_ledger.count("applied")
    [row] = table.created
    assert row["Applied"] == 1


def test_cloud_run_is_named():
    assert run_log.where() == "PC"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("K_SERVICE", "job-engine")
        assert run_log.where() == "Cloud Run"


def test_wrap_marks_a_task_that_caught_its_own_failure():
    table = FakeTable()
    body = run_log.wrap("Autopilot", lambda: {"ok": False, "error": "Adzuna is down"}, table,
                        lambda: START)
    assert body() == {"ok": False, "error": "Adzuna is down"}
    [row] = table.created
    assert row["Result"] == "Failed" and row["Failure"] == "Adzuna is down"


def test_a_scheduled_step_writes_one_row():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d._track_now = lambda: datetime(2026, 10, 1, 7, 5)
    tb._scheduled("Autopilot", lambda: [Reply("Autopilot: 2 jobs screened")], d)
    tb._scheduled("Mail queue", lambda: [], d)  # nothing to do: no row

    def broken():
        raise RuntimeError("invalid_grant: Token has been expired")

    tb._scheduled("Reply check", broken, d)
    first, second = d.run_log_table.created
    assert first["Step"] == "Autopilot" and first["Result"] == "OK"
    assert first["Summary"] == "Autopilot: 2 jobs screened" and first["Run"] == "Autopilot 07:05"
    assert second["Step"] == "Reply check" and second["Result"] == "Failed"
    assert "Token has been expired" in second["Failure"]


def test_a_cloud_run_task_writes_a_failed_row(tmp_path, monkeypatch):
    monkeypatch.setenv("K_SERVICE", "job-engine")
    s = web_settings()
    d = make_desk(tmp_path, s=s)
    client = tb.TelegramClient(Recorder())

    def broken(progress):
        raise RuntimeError("Adzuna is down")

    web.desk_tasks(s, d, client, broken).autopilot()
    [row] = d.run_log_table.created
    assert row["Step"] == "Autopilot" and row["Where"] == "Cloud Run"
    assert row["Result"] == "Failed" and row["Failure"] == "Adzuna is down"


def test_a_fetch_by_hand_gets_its_row_and_a_stopped_one_too(monkeypatch):
    """20 Nov: your /fetch hung, was stopped and left no Run Log row (only scheduled steps
    had rows)."""
    from test_telegram_bot import FakeTelegram

    from jobengine.jobctl import Stopped

    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d._track_now = lambda: datetime(2026, 11, 20, 10, 11)
    client = tb.TelegramClient(FakeTelegram())

    def fetch(progress):
        day_ledger.add_sweep({"gmail": 51}, new=4, updated=2)
        return "Job search finished\nNew jobs added to Notion: 4\n\n\U0001F449 Next: /screen"

    tb.run_fetch(client, settings(), "1", fetch, desk=d)

    def hung(progress):
        raise Stopped()

    tb.run_fetch(client, settings(), "1", hung, desk=d)
    done, stopped = d.run_log_table.created
    assert done["Step"] == "/fetch" and done["Run"] == "/fetch 10:11"
    assert done["Result"] == "OK" and done["New jobs"] == 4
    assert done["Summary"] == "Job search finished\nNew jobs added to Notion: 4"
    assert stopped["Result"] == "Failed" and stopped["Failure"] == "stopped by /end"


def test_a_screen_by_hand_gets_its_row():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d._track_now = lambda: datetime(2026, 11, 20, 11, 0)
    client = tb.TelegramClient(__import__("test_telegram_bot").FakeTelegram())

    def screen(scope, progress, limit):
        day_ledger.count("screened.Apply high", 2)
        return [Reply("Screened 2 jobs")]

    d.screen_replies = screen
    tb.run_screen(client, settings(), "1", d)
    [row] = d.run_log_table.created
    assert row["Step"] == "/screen" and row["Screened"] == 2 and row["Result"] == "OK"
