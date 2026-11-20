"""Error Log (18 Nov, your request): one Notion row per kind of problem per day, written at the
end of each run, and one Telegram message for a new kind of error. Made-up log lines."""

import logging
import sys
from datetime import date, datetime, timedelta, timezone

import pytest
from test_telegram_bot import Clock, settings

from jobengine import telegram_bot as tb
from jobengine.notion_repo import FakeTable
from jobengine.screen.desk import Reply, fake_desk
from jobengine.sweep.fakes import FAKE_TODAY
from jobengine.track import error_log, run_log

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 11, 18, 10, 0, tzinfo=IST)
LINKS = "/app/src/jobengine/sweep/job_links.py"
MAIL = "/app/src/jobengine/mail/sender.py"


@pytest.fixture
def table(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    root = logging.getLogger()
    collector = error_log.install(root)
    rows = FakeTable()
    error_log.bind(rows, lambda: NOW)
    yield rows
    root.removeHandler(collector)


@pytest.fixture
def pings():
    sent = []
    error_log.set_notify(sent.append)
    return sent


def emit(level, msg, *args, path=LINKS, name="jobengine.sweep", exc=None, when=NOW):
    record = logging.LogRecord(name, level, path, 1, msg, args, exc)
    record.created = when.timestamp()
    logging.getLogger(name).handle(record)


def test_site_problems_of_one_kind_are_one_row_with_the_sites(table, pings):
    emit(logging.INFO, "careers page not read %s: %s", "jobs.alpha.com",
         "GET https://jobs.alpha.com/x?token=abc failed: HTTP 403")
    emit(logging.INFO, "careers page not read %s: %s", "careers.beta.nl",
         "GET https://careers.beta.nl/y failed: HTTP 403")
    emit(logging.INFO, "careers page not read %s: %s", "gamma.io", "HTTP 404")
    emit(logging.INFO, "careers page read %s", "fine.com")  # no problem
    emit(logging.INFO, "workday site of %s is busy, retrying", "Acme")  # a retry: no row
    assert error_log.flush() == 2
    blocked, gone = table.created
    assert blocked["Count"] == 2 and blocked["Level"] == "Site problem"
    assert blocked["Stage"] == "Job links" and blocked["Likely cause"] == "Refuses the bot (403)"
    assert blocked["Problem"] == "careers page not read …: …"
    assert "Sites: jobs.alpha.com, careers.beta.nl" in blocked["Example"]
    assert "token=abc" not in blocked["Example"]
    assert blocked["Status"] == "New" and blocked["Day"] == date(2026, 11, 18)
    assert blocked["Key"].startswith("2026-11-18|") and blocked["Where"] == "PC"
    assert gone["Likely cause"] == "Page gone (404)" and gone["Count"] == 1
    assert pings == []  # site problems never ping


def test_a_new_error_pings_once_and_counts_go_on_in_the_same_row(table, pings):
    emit(logging.ERROR, "mail to %s failed: %s", "jane.doe@example.com",
         "invalid_grant: Token has been expired", path=MAIL, name="jobengine.mail")
    error_log.flush()
    [row] = table.created
    assert row["Level"] == "Error" and row["Stage"] == "Mail"
    assert row["Likely cause"] == "Key expired or wrong (401)"
    assert "jane.doe" not in row["Problem"] + row["Example"]
    [text] = pings
    assert "New kind of problem" in text and "Error, Mail" in text
    emit(logging.ERROR, "mail to %s failed: %s", "jane.doe@example.com",
         "invalid_grant: Token has been expired", path=MAIL, name="jobengine.mail",
         when=NOW + timedelta(hours=1))
    error_log.flush()
    assert len(table.created) == 1 and table.created[0]["Count"] == 2
    assert table.created[0]["Last seen"] == (NOW + timedelta(hours=1)).replace(microsecond=0)
    assert len(pings) == 1  # the same day: no second message


def test_a_problem_seen_in_the_last_7_days_is_not_new(table, pings):
    emit(logging.WARNING, "llm call refused: HTTP %s%s", 503, "", name="jobengine.llm",
         path="/app/src/jobengine/llm.py", when=NOW - timedelta(days=3))
    error_log.flush()
    assert len(pings) == 1 and table.created[0]["Likely cause"] == "Server error (5xx)"
    table.created[0]["Status"] = "Known"
    emit(logging.WARNING, "llm call refused: HTTP %s%s", 503, "", name="jobengine.llm",
         path="/app/src/jobengine/llm.py")
    error_log.flush()
    assert len(table.created) == 2  # a row for today
    assert table.created[1]["Stage"] == "AI" and table.created[1]["Status"] == "New"
    assert len(pings) == 1  # 3 days ago counts as seen
    assert table.created[0]["Status"] == "Known"  # your Status stays


def test_an_exception_is_a_bot_bug_with_its_type(table, pings):
    try:
        {}["missing"]
    except KeyError:
        emit(logging.ERROR, "%s failed", "Autopilot", exc=sys.exc_info(),
             path="/app/src/jobengine/telegram_bot.py", name="jobengine.telegram_bot")
    error_log.flush()
    [row] = table.created
    assert row["Likely cause"] == "Bot bug" and row["Stage"] == "Telegram"
    assert "KeyError" in row["Example"]


def test_other_libraries_and_debug_lines_do_not_count(table):
    emit(logging.ERROR, "boom", name="urllib3.connectionpool")
    emit(logging.DEBUG, "HTTP 403", name="jobengine.sweep")
    emit(logging.INFO, "saved 3 jobs", name="jobengine.sweep")
    assert error_log.pending() == 0 and error_log.flush() == 0


def test_a_broken_table_keeps_the_problems_for_the_next_write(table):
    class Broken(FakeTable):
        def recent(self, since):
            raise RuntimeError("Notion is down")

    error_log.bind(Broken(), lambda: NOW)
    emit(logging.WARNING, "strategy tips not read: %s", "HTTP 502")
    assert error_log.flush() == 0
    assert error_log.pending() == 1  # kept, the run goes on
    error_log.bind(table, lambda: NOW)
    assert error_log.flush() == 1 and table.created[0]["Count"] == 1


def test_not_writable_here_writes_nothing(table):
    error_log.bind(None, lambda: NOW)
    emit(logging.WARNING, "strategy tips not read: %s", "HTTP 502")
    assert error_log.flush() == 0


def test_the_ping_names_at_most_5_and_counts_the_rest(table, pings):
    for n in range(7):
        emit(logging.ERROR, f"problem number {'x' * n} happened")
    error_log.flush()
    [text] = pings
    assert "(7)" in text and "... and 2 more." in text


def test_likely_causes():
    assert error_log.cause_of("HTTP 429 (still rate limited)") == "Rate limited (429)"
    assert error_log.cause_of("ReadTimeout") == "Timeout"
    assert error_log.cause_of("browser page did not answer in 60 s: https://x.com/") == "Timeout"
    assert error_log.cause_of("page not rendered: no answer in 60 s") == "Timeout"
    assert error_log.cause_of("ConnectionError: reset") == "Network"
    assert error_log.cause_of("something odd") == "Other"


def test_a_run_ends_with_the_error_rows_and_its_sites_blocked(table, pings):
    run_rows = FakeTable()
    with run_log.recorded("Fetch", run_rows, lambda: NOW):
        emit(logging.INFO, "workday site of %s is busy (HTTP 429): left for this /fetch",
             "Acme", path="/app/src/jobengine/sweep/sources/ats.py")
        emit(logging.INFO, "job page not read %s: %s", "x.com", "HTTP 403")
        emit(logging.INFO, "job page not read %s: %s", "y.com", "HTTP 404")
    [run] = run_rows.created
    assert run["Sites blocked"] == 2  # the 429 and the 403, not the gone page
    assert len(table.created) == 3  # written when the run ended
    assert table.created[0]["Stage"] == "Fetch"


def test_scheduled_steps_flush_the_error_log(table, pings):
    d = fake_desk(settings(), FAKE_TODAY, now=Clock())
    d._track_now = lambda: NOW

    def broken():
        logging.getLogger("jobengine.mail").error("send failed: HTTP 500")
        return [Reply("Mail queue: 0 sent")]

    tb._scheduled("Mail queue", broken, d)
    [row] = table.created
    assert row["Likely cause"] == "Server error (5xx)" and pings


def test_the_notion_table_reads_only_recent_rows_and_patches_by_id():
    from jobengine.notion_repo import ERROR_LOG_TYPES, ErrorLogTable

    class Client:
        def __init__(self):
            self.calls = []

        def query(self, data_source_id, body=None):
            self.calls.append(("query", data_source_id, body))
            return [{"id": "p1", "properties": {
                "Key": {"type": "rich_text", "rich_text": [{"plain_text": "2026-11-18|abc"}]},
                "Count": {"type": "number", "number": 4}}}]

        def request(self, method, path, body):
            self.calls.append((method, path, body))
            return {"id": "new"}

    client = Client()
    rows = ErrorLogTable(client, "ds", ERROR_LOG_TYPES)
    [(page_id, values)] = rows.recent(date(2026, 11, 11))
    assert page_id == "p1" and values["Count"] == 4 and values["Key"] == "2026-11-18|abc"
    assert client.calls[0] == ("query", "ds", {"filter": {"property": "Day", "date": {
        "on_or_after": "2026-11-11"}}})
    rows.update("p1", {"Count": 5, "Last seen": NOW})
    method, path, body = client.calls[1]
    assert (method, path) == ("PATCH", "/pages/p1")
    assert body["properties"]["Count"] == {"number": 5}
    assert body["properties"]["Last seen"]["date"]["start"] == NOW.isoformat()


def test_a_count_with_500_in_it_is_no_server_error(table):
    """18 Nov /fetch: "... 500 of 1079 postings processed" became a Server error row."""
    emit(logging.INFO, "... %d of %d postings processed", 500, 1079,
         path="/app/src/jobengine/sweep/runner.py")
    emit(logging.INFO, "adzuna pl page %d: %d postings", 4, 404,
         path="/app/src/jobengine/sweep/runner.py")
    assert error_log.pending() == 0
    assert error_log.cause_of("Synechron: 429 jobs read") == "Other"
    assert error_log.cause_of("POST x failed: HTTP 503") == "Server error (5xx)"
    assert error_log.cause_of("HTTP/1.1 401 Unauthorized") == "Key expired or wrong (401)"
    assert error_log.cause_of("busy (429)") == "Rate limited (429)"


def test_titles_have_no_format_codes_and_names_the_companies(table):
    """18 Nov /fetch: "... every Workday site waits %.0fs" in the title; the 30 Workday rows
    showed one company only."""
    emit(logging.INFO, "%s rate limited: every Workday site waits %.0fs",
         "POST https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/jobs", 30.0,
         path="/app/src/jobengine/http.py", name="jobengine.http")
    for company in ("Synechron", "Red Hat", "Synechron"):
        emit(logging.INFO, "workday site of %s is busy (HTTP 429): left for this /fetch",
             company, path="/app/src/jobengine/sweep/sources/ats.py")
    emit(logging.INFO, "workday site of %s is busy (HTTP 429): left for this /fetch",
         "jane.doe@example.com", path="/app/src/jobengine/sweep/sources/ats.py")
    error_log.flush()
    gate, busy = table.created
    assert gate["Problem"] == "… rate limited: every Workday site waits …s"
    assert "Sites: acme.wd1.myworkdayjobs.com" in gate["Example"]
    assert busy["Count"] == 4 and busy["Likely cause"] == "Rate limited (429)"
    assert busy["Example"].endswith("Seen for: Synechron, Red Hat")


def test_a_telegram_retry_that_works_is_no_problem(table, pings):
    """19 Nov /fetch: three typing signals got through on the second try, and the Error Log
    pinged "New kind of problem" for them."""
    calls = []

    def transport(method, payload, timeout):
        calls.append(method)
        if len(calls) == 1:
            raise tb.TelegramNetworkError("sendChatAction failed: URLError timed out")
        return {"ok": True, "result": True}

    tb.TelegramClient(transport, sleep=lambda seconds: None)._call("sendChatAction", {})
    assert calls == ["sendChatAction", "sendChatAction"]
    assert error_log.pending() == 0 and pings == []


def test_lost_polls_are_a_warning_only_when_they_go_on(table, pings, monkeypatch):
    """18 Nov: one lost getUpdates (SSL EOF) was an Error row and a ping."""

    class Lost:
        sent = []

        def __init__(self, failures):
            self.failures = failures

        def __call__(self, method, payload, timeout):
            if method == "getUpdates" and self.failures:
                self.failures -= 1
                raise tb.TelegramNetworkError("getUpdates failed: URLError EOF")
            if method == "getUpdates":
                return {"ok": True, "result": []}
            return {"ok": True, "result": {"message_id": 1} if method == "sendMessage" else True}

    monkeypatch.setattr(tb, "register_menu", lambda client, s: None)
    tb.run(settings(), tb.TelegramClient(Lost(2)), max_polls=3, sleep=lambda seconds: None)
    assert error_log.pending() == 0
    tb.run(settings(), tb.TelegramClient(Lost(tb.POLL_LOST_WARN)),
           max_polls=tb.POLL_LOST_WARN, sleep=lambda seconds: None)
    error_log.flush()
    [row] = table.created
    assert row["Level"] == "Warning" and row["Stage"] == "Telegram"
    assert row["Problem"] == "Telegram not reached … times in a row: …"


def test_dry_run_lines_are_no_problem(table):
    """18 Nov: "DRY RUN: would draft a follow-up for ..." was a Warning row, Count 10."""
    emit(logging.WARNING, "DRY RUN: would draft a follow-up for %s", "jane.doe@example.com",
         path="/app/src/jobengine/track/runner.py", name="jobengine.track")
    assert error_log.pending() == 0
    run_rows = FakeTable()
    with run_log.recorded("Reply check", run_rows, lambda: NOW) as run:
        emit(logging.WARNING, "DRY RUN: would write to %s", "contacts")
        run.note(["checked"])
    assert run_rows.created[0]["Warnings"] == 0


def test_a_browser_error_is_no_site(table):
    """19 Nov: "Page.goto: Timeout 20000ms exceeded." gave the site "page.goto"."""
    emit(logging.INFO, "careers page not opened in the browser %s: %s", "www.jumbo.com",
         "page not rendered: Page.goto: Timeout 20000ms exceeded.")
    error_log.flush()
    assert table.created[0]["Example"].endswith("Sites: www.jumbo.com")


def test_a_command_writes_its_problems_when_it_ends(table, pings, monkeypatch):
    """20 Nov: the Error Log was written only after scheduled steps, so the problems of a
    /fetch stayed in memory and were lost when the bot was stopped."""
    from test_telegram_bot import FakeTelegram, update

    def fetch(progress):
        emit(logging.INFO, "careers page not read %s: %s", "jobs.alpha.com", "HTTP 403")
        return "Sweep done"

    fake = FakeTelegram()
    tb.handle_one(tb.TelegramClient(fake), settings(), update(1, "/fetch"), fetch)
    [row] = [r for r in table.created if r["Level"] == "Site problem"]
    assert row["Likely cause"] == "Refuses the bot (403)" and error_log.pending() == 0


def test_a_long_command_writes_on_the_way_and_a_stop_writes_the_rest(table, pings,
                                                                     monkeypatch):
    """A /fetch that hangs or is stopped still leaves its rows: the poll loop writes every
    ERROR_FLUSH_SECONDS, and once more when the bot stops (Ctrl+C, docker stop)."""
    from test_telegram_bot import FakeTelegram

    now = [0.0]
    polls = []

    def poll(client, s, offset, fetch, desk, **kwargs):
        polls.append(now[0])
        emit(logging.INFO, "job page not read %s: %s", f"site{len(polls)}.com", "HTTP 403")
        now[0] += 200  # each poll is 200 s here
        if len(polls) == 3:
            raise KeyboardInterrupt  # you stop the bot
        return offset

    monkeypatch.setattr(tb, "register_menu", lambda client, s: None)
    monkeypatch.setattr(tb, "poll_once", poll)
    with pytest.raises(KeyboardInterrupt):
        tb.run(settings(), tb.TelegramClient(FakeTelegram()), clock=lambda: now[0])
    [row] = table.created
    assert row["Count"] == 3  # 2 written at 400 s (one row), the third at the stop
    assert row["Example"].endswith("site3.com") or "site3.com" in row["Example"]
