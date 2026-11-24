"""Short messages (S10, 23 Nov, your ask): the daily check, the daily report, the morning
check, /health, the weekly digest, the autopilot summary and the dry run reports come to
Telegram short, with a last line naming the Run Log row whose page holds the full report.
Without a Run Log row (no table, or outside a run) the full text comes as before.
Made-up jobs and people."""

import threading
from datetime import UTC, datetime

from test_autopilot import Fetch, make_desk
from test_form_bot import VALUES
from test_telegram_bot import Clock, settings

from jobengine import telegram_bot as tb
from jobengine.apply import form_bot
from jobengine.apply.form_bot import Field, Result
from jobengine.notion_repo import FakeTable
from jobengine.screen.desk import fake_desk
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.track import digest, run_log

MARK = "📝 Full report in Notion: Run Log, row "
NOW = datetime(2026, 10, 1, 21, 50)


def test_brief_keeps_the_full_text_in_the_runs_page_and_names_the_row():
    assert run_log.brief("full\ntext", "short") == "full\ntext"  # no run: as before
    table = FakeTable()
    with run_log.recorded("Daily report", table, lambda: NOW) as run:
        text = run_log.brief("line one\nline two\nline three", "short")
        assert run_log.brief("tiny", "not shorter at all") == "tiny"
        run.note([text])
    assert text == f'short\n{MARK}"Daily report 21:50"'
    assert table.bodies == [["line one", "line two", "line three"]]  # the short one not again
    assert table.created[0]["Summary"] == "short"  # the row's summary without the Notion line
    with run_log.recorded("Daily report", None, lambda: NOW):  # no Run Log table here
        assert run_log.brief("full text", "short") == "full text"


def test_work_on_other_threads_joins_the_run():
    table = FakeTable()
    got = []
    with run_log.recorded("Auto-apply", table, lambda: NOW) as run:
        def fill():
            with run_log.joined(run):
                got.append(run_log.brief("the full dry run report", "short"))
        worker = threading.Thread(target=fill)
        worker.start()
        worker.join()
        run.note(got)
    assert got == [f'short\n{MARK}"Auto-apply 21:50"']
    assert table.bodies == [["the full dry run report"]]


def test_status_words_are_kept_and_keys_still_hidden():
    assert run_log.redact("Gmail sender token: OK") == "Gmail sender token: OK"
    assert run_log.redact("Anthropic key: missing") == "Anthropic key: missing"
    assert run_log.redact("token: abcd1234efgh") == "token: <hidden>"


def evening_desk():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d._track_now = lambda: NOW  # naive: tracking adds +05:30
    return d


def test_the_daily_report_is_short_in_telegram_and_full_in_the_mail_and_page():
    d = evening_desk()
    [reply] = tb._scheduled("Daily report", d.daily_report_tick, d)
    lines = reply.text.splitlines()
    assert lines[0] == "📊 Daily report, Thu 01 Oct"
    assert lines[1].startswith("📊 Today: Found 0")
    assert "🔎 Fetch: no /fetch ran today." not in lines and "💳" not in reply.text
    assert lines[-1].startswith(MARK + '"Daily report ')
    full = d.mails[0][1]
    assert "🔎 Fetch: no /fetch ran today." in full and "💳 Credits:" in full
    assert "🔎 Fetch: no /fetch ran today." in d.run_log_table.bodies[-1]


def test_the_morning_check_counts_the_checks_that_passed():
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d._track_now = lambda: datetime(2026, 10, 27, 1, 5, tzinfo=UTC)  # 06:35 India time
    [check] = tb._scheduled("Morning check", d.health_report_tick, d)
    lines = check.text.splitlines()
    assert lines[0].startswith("🩺 Morning check, Tue 27 Oct")
    assert "⛔ Blocks the run, fix first:" in lines  # what needs you stays in full
    assert any(line.startswith("✅ ") and line.endswith(" other check(s) passed.")
               for line in lines)
    assert not any(line.startswith("✅ Notion: ") for line in lines)
    page = d.run_log_table.bodies[-1]
    assert any(line.startswith("✅ Notion: ") for line in page)


def test_the_digest_keeps_the_numbers_and_the_tips():
    full = ("Weekly digest 2026-11-29\nThis week: applied 4\n\nWhat gets replies (...):\n"
            "- By board: LinkedIn 2/2\nPut more effort where it works: LinkedIn.\n\n"
            "📈 What works\nBy source:\n- LinkedIn: 3 > 3\n"
            "Most approvals for what it finds: LinkedIn (3 of 3).\n\n"
            "📬 Replies this week: 1 interview\nMost answers: the headline \"DevOps\" (2 of 3).")
    assert digest.short(full).splitlines() == [
        "Weekly digest 2026-11-29", "This week: applied 4", "",
        "📈 What gets replies, what works and the replies by resume headline and skill: in "
        "the full report.",
        "Put more effort where it works: LinkedIn.",
        "Most approvals for what it finds: LinkedIn (3 of 3).",
        "Most answers: the headline \"DevOps\" (2 of 3)."]


def test_the_autopilot_summary_names_only_the_jobs_that_need_you(tmp_path):
    d = make_desk(tmp_path)
    d.run_log_table = FakeTable()
    with run_log.recorded("Autopilot", d.run_log_table, lambda: NOW) as run:
        replies = d.autopilot(Fetch())
        run.note([r for r in replies if not r.document])
    summary = replies[0].text
    assert "Autopilot done: 7 jobs approved (4 Apply high, 3 Apply normal)." in summary
    assert "⚠️ Needs you:" in summary
    assert "Vistula Cloud, Platform Engineer (Apply normal): resume not built" in summary
    assert "Tulip Data B.V., Cloud Engineer" not in summary  # its resume follows below
    assert summary.endswith(f'{MARK}"Autopilot 21:50"')
    assert any(r.document and r.text.startswith("Tulip Data B.V., Cloud Engineer")
               for r in replies)
    page = d.run_log_table.bodies[-1]
    assert any(line.startswith("1. Tulip Data B.V., Cloud Engineer (Apply high)")
               for line in page)


def test_the_dry_run_report_leaves_the_filled_fields_to_the_page():
    fields = [Field("0", "text", "First name"), Field("1", "text", "Years of Ansible?",
                                                      required=True)]
    result = Result(url="https://jobs.example.com/apply",
                    answers=form_bot.plan(fields, VALUES, []))
    short = form_bot.report(result, "Northwind Cloud, DevOps Engineer", short=True)
    assert "Filled 1 of 2 field(s)." in short and "✅ First name" not in short
    assert "❗ Years of Ansible?: no answer yet" in short
    assert "/answer Years of Ansible? = <your answer>" in short
    assert "✅ First name: Madeshwaran" in form_bot.report(result, "Northwind Cloud")


def test_commands_with_a_long_report_get_their_run_log_row(tmp_path):
    d = make_desk(tmp_path)
    [health] = tb._reported("/health", d.health_command, d)
    assert health.text.startswith("🩺 Health: ")
    assert health.text.endswith(f'{MARK}"/health 08:00"')
    assert d.run_log_table.created[-1]["Step"] == "/health"
    assert "Environment: local, DRY_RUN on" in d.run_log_table.bodies[-1]

    def broken():
        raise RuntimeError("Notion is down")

    assert tb._reported("/today", broken, d)[0].text == "/today failed: Notion is down"
    assert d.run_log_table.created[-1]["Result"] == "Failed"
