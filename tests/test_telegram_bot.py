import io
import json
import sys
import urllib.error

import pytest

from jobengine import telegram_bot as tb
from jobengine.safety import SafetyError
from jobengine.settings import load_settings

CHAT_ID = "111222333"
FAKE_TOKEN = "fake-token-for-tests"


def settings(env="local", **environ):
    base = {"TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "TELEGRAM_CHAT_ID": CHAT_ID}
    base.update(environ)
    return load_settings(env, base)


def prod_settings():
    return settings("prod")


def update(update_id, text, chat_id=CHAT_ID):
    return {"update_id": update_id, "message": {"chat": {"id": int(chat_id)}, "text": text}}


class FakeTelegram:
    """In-memory Bot API: queued update batches in, sent messages recorded."""

    def __init__(self, batches=None, fail_with=None):
        self.batches = list(batches or [])
        self.fail_with = fail_with
        self.calls = []
        self.sent = []

    def __call__(self, method, payload, timeout):
        self.calls.append((method, payload))
        if self.fail_with and method == "getUpdates":
            error, self.fail_with = self.fail_with, None
            raise error
        if method == "getUpdates":
            return {"ok": True, "result": self.batches.pop(0) if self.batches else []}
        if method == "sendMessage":
            self.sent.append((payload["chat_id"], payload["text"]))
            return {"ok": True, "result": {}}
        return {"ok": False, "description": f"unexpected {method}"}


@pytest.mark.parametrize(
    "text, expected",
    [
        ("/start", "start"),
        ("/status@MadeshDevBot", "status"),
        ("/HELP extra words", "help"),
        ("hello", None),
        ("/", None),
    ],
)
def test_parse_command(text, expected):
    assert tb.parse_command(text) == expected


def test_start_reply_has_prefix_and_help():
    chat_id, text = tb.handle_update(update(1, "/start"), settings())
    assert chat_id == CHAT_ID
    assert text.startswith("[LOCAL] Job Engine bot is running (env=local).")
    assert "/status" in text


def test_status_reply_shows_env_and_safety():
    _, text = tb.handle_update(update(1, "/status"), settings("dev"))
    assert text.startswith("[DEV] Job Engine | env=dev | dry_run=true")
    assert "sender=madeshwaranm02@gmail.com" in text
    assert "model=claude-haiku-4-5" in text


def test_prod_replies_have_no_prefix():
    _, text = tb.handle_update(update(1, "/help"), prod_settings())
    assert text == tb.HELP_TEXT


def test_plain_text_and_unknown_command():
    _, text = tb.handle_update(update(1, "hi"), settings())
    assert "/help" in text
    _, text = tb.handle_update(update(2, "/apply"), settings())
    assert "Unknown command /apply" in text


def test_other_chats_are_ignored():
    assert tb.handle_update(update(1, "/start", chat_id="999"), settings()) is None


def test_non_text_updates_are_ignored():
    assert tb.handle_update({"update_id": 1}, settings()) is None
    no_text = {"update_id": 2, "message": {"chat": {"id": int(CHAT_ID)}}}
    assert tb.handle_update(no_text, settings()) is None


def test_poll_once_answers_and_advances_offset():
    fake = FakeTelegram([[update(10, "/start"), update(11, "/help", chat_id="999")]])
    offset = tb.poll_once(tb.TelegramClient(fake), settings(), None)
    assert offset == 12
    assert len(fake.sent) == 1
    assert fake.sent[0][0] == CHAT_ID
    assert "offset" not in fake.calls[0][1]


def test_poll_once_passes_offset_and_keeps_it_when_empty():
    fake = FakeTelegram([[]])
    assert tb.poll_once(tb.TelegramClient(fake), settings(), 42) == 42
    assert fake.calls[0][1]["offset"] == 42


def test_run_announces_start_and_survives_api_errors():
    fake = FakeTelegram(
        batches=[[update(5, "/status")]],
        fail_with=tb.TelegramError("getUpdates failed: timeout"),
    )
    sleeps = []
    tb.run(settings(), tb.TelegramClient(fake), max_polls=2, sleep=sleeps.append)
    assert fake.sent[0] == (CHAT_ID, "[LOCAL] Job Engine bot started.")
    assert fake.sent[1][1].startswith("[LOCAL] Job Engine | env=local")
    assert sleeps == [5]


def test_api_not_ok_raises():
    client = tb.TelegramClient(lambda m, p, t: {"ok": False, "description": "Unauthorized"})
    with pytest.raises(tb.TelegramError, match="Unauthorized"):
        client.send_message(CHAT_ID, "x")


def test_check_bot_settings_requires_token_and_chat():
    tb.check_bot_settings(settings())
    with pytest.raises(SafetyError, match="TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID"):
        tb.check_bot_settings(load_settings("local", {}))


def test_http_errors_never_include_token(monkeypatch):
    def raise_http(req, timeout):
        body = io.BytesIO(json.dumps({"description": "Unauthorized"}).encode())
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, body)

    monkeypatch.setattr(tb.urllib.request, "urlopen", raise_http)
    with pytest.raises(tb.TelegramError) as info:
        tb.http_transport(FAKE_TOKEN)("getMe", {}, 1)
    assert FAKE_TOKEN not in str(info.value)
    assert "401" in str(info.value)

    def raise_url(req, timeout):
        raise urllib.error.URLError(f"cannot reach {req.full_url}")

    monkeypatch.setattr(tb.urllib.request, "urlopen", raise_url)
    with pytest.raises(tb.TelegramError) as info:
        tb.http_transport(FAKE_TOKEN)("getMe", {}, 1)
    assert FAKE_TOKEN not in str(info.value)
    assert "<token>" in str(info.value)


def test_main_fails_without_secrets(monkeypatch, capsys):
    monkeypatch.setattr(tb, "get_settings", lambda: load_settings("local", {}))
    assert tb.main([]) == 1
    assert "missing TELEGRAM_BOT_TOKEN" in capsys.readouterr().err


def test_console_transport_reads_lines_and_prints_replies():
    out = io.StringIO()
    client = tb.TelegramClient(tb.console_transport(CHAT_ID, io.StringIO("/help\n"), out))
    updates = client.get_updates(None)
    assert updates == [
        {"update_id": 1, "message": {"chat": {"id": CHAT_ID}, "text": "/help"}}
    ]
    client.send_message(CHAT_ID, "hello")
    assert out.getvalue() == "you> /help\nbot> hello\n"
    with pytest.raises(EOFError):
        client.get_updates(2)


def test_fake_mode_runs_without_token_or_network(monkeypatch, capsys):
    monkeypatch.setattr(tb, "get_settings", lambda: load_settings("local", {}))
    monkeypatch.setattr(sys, "stdin", io.StringIO("/start\n/status\nhi\n"))

    def no_network(*args, **kwargs):
        raise AssertionError("fake mode must not open network connections")

    monkeypatch.setattr(tb.urllib.request, "urlopen", no_network)
    assert tb.main(["--fake"]) == 0
    out = capsys.readouterr().out
    assert "bot> [LOCAL] Job Engine bot started." in out
    assert "bot> [LOCAL] Job Engine bot is running (env=local)." in out
    assert "bot> [LOCAL] Job Engine | env=local | dry_run=true" in out
    assert "bot> [LOCAL] I only understand commands for now." in out
    assert out.rstrip().endswith("Job Engine bot stopped.")


def test_fake_mode_uses_configured_chat_id(monkeypatch, capsys):
    monkeypatch.setattr(tb, "get_settings", lambda: settings())
    monkeypatch.setattr(sys, "stdin", io.StringIO("/help\n"))
    assert tb.main(["--fake"]) == 0
    assert "bot> [LOCAL] Job Engine commands:" in capsys.readouterr().out


def test_fetch_in_fake_mode_returns_prefixed_summary(monkeypatch, capsys):
    monkeypatch.setattr(tb, "get_settings", lambda: load_settings("local", {}))
    monkeypatch.setattr(sys, "stdin", io.StringIO("/fetch\n"))
    assert tb.main(["--fake"]) == 0
    out = capsys.readouterr().out
    assert "bot> [LOCAL] \u2705 Job search finished" in out
    assert "New jobs added to Notion: 8" in out


def test_fetch_reply_goes_through_telegram_text():
    fetcher = tb.make_fetcher(settings(), fake=True)
    _, text = tb.handle_update(update(1, "/fetch"), settings(), fetcher)
    assert text.startswith("[LOCAL] \u2705 Job search finished")
    _, dev_text = tb.handle_update(
        update(2, "/fetch"), settings("dev"), lambda progress: "Sweep done"
    )
    assert dev_text == "[DEV] Sweep done"


def test_fetch_respects_strategy_gate(monkeypatch):
    monkeypatch.setattr(tb.fakes, "FAKE_TODAY", tb.date(2026, 11, 30))
    _, text = tb.handle_update(update(1, "/fetch"), settings(), tb.make_fetcher(settings(), True))
    assert text.startswith("[LOCAL] /fetch is blocked: strategy last updated 2026-09-23")


def test_fetch_without_fetcher_and_on_failure():
    _, text = tb.handle_update(update(1, "/fetch"), settings())
    assert text == "[LOCAL] /fetch is not available in this bot."

    def boom(progress):
        raise RuntimeError("notion down")

    _, text = tb.handle_update(update(2, "/fetch"), settings(), boom)
    assert text == "[LOCAL] /fetch failed: notion down"


def test_help_lists_fetch():
    assert "/fetch" in tb.HELP_TEXT


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_run_fetch_replies_at_once_updates_progress_and_sends_summary():
    fake = FakeTelegram()
    edits = []

    def transport(method, payload, timeout):
        if method == "editMessageText":
            edits.append(payload)
            return {"ok": True, "result": {}}
        if method == "sendMessage":
            fake.sent.append((payload["chat_id"], payload["text"]))
            return {"ok": True, "result": {"message_id": len(fake.sent)}}
        return fake(method, payload, timeout)

    clock = FakeClock()

    def fetch(progress):
        progress("Searching Adzuna... (0 jobs found so far)")  # within 4s: no edit yet
        clock.now = 5
        progress("Saving to your Notion: 25 of 300 jobs checked")  # 5s later: edit
        clock.now = 130
        return "Job search finished"

    tb.run_fetch(tb.TelegramClient(transport), settings(), CHAT_ID, fetch, clock=clock)
    assert fake.sent[0][1].startswith("[LOCAL] \U0001F50E Searching for jobs (started ")
    assert fake.sent[-1] == (CHAT_ID, "[LOCAL] Job search finished")
    assert len(edits) == 2  # one throttled progress edit, one final
    assert edits[0]["message_id"] == 1
    assert edits[0]["text"].endswith("\nSaving to your Notion: 25 of 300 jobs checked")
    assert edits[1]["text"] == "[LOCAL] \u2705 Job search done in 2 min. Summary below."


def test_run_fetch_reports_failure():
    sent = []

    def transport(method, payload, timeout):
        if method == "sendMessage":
            sent.append(payload["text"])
            return {"ok": True, "result": {"message_id": 7}}
        return {"ok": True, "result": {}}

    def fetch(progress):
        raise RuntimeError("notion down")

    edits = []
    real_transport = transport

    def recording(method, payload, timeout):
        if method == "editMessageText":
            edits.append(payload["text"])
        return real_transport(method, payload, timeout)

    tb.run_fetch(tb.TelegramClient(recording), settings(), CHAT_ID, fetch)
    assert sent[-1] == "[LOCAL] /fetch failed: notion down"
    assert edits[-1].startswith("[LOCAL] \u274C Job search stopped after under a minute")


def test_progress_edit_errors_do_not_stop_the_sweep():
    def transport(method, payload, timeout):
        if method == "editMessageText":
            return {"ok": False, "description": "Too Many Requests"}
        return {"ok": True, "result": {"message_id": 1}}

    clock = FakeClock()
    progress = tb.ProgressMessage(tb.TelegramClient(transport), CHAT_ID, settings(), clock)
    clock.now = 10
    progress.update("line")  # the failed edit is only logged
    progress.finish(ok=True)


def test_fake_bot_fetch_shows_start_progress_and_summary(monkeypatch, capsys):
    monkeypatch.setattr(tb, "get_settings", lambda: load_settings("local", {}))
    monkeypatch.setattr(sys, "stdin", io.StringIO("/fetch\n"))
    assert tb.main(["--fake"]) == 0
    out = capsys.readouterr().out
    assert "bot> [LOCAL] \U0001F50E Searching for jobs (started " in out
    assert "bot (updated)> [LOCAL] \u2705 Job search done in under a minute." in out
    assert "\U0001F1F5\U0001F1F1 Poland: 4" in out
    assert "\U0001F1F3\U0001F1F1 Netherlands: 2" in out
    assert "\U0001F1EE\U0001F1EA Ireland: 2" in out
    assert "Already in Notion, seen again: 6" in out
    assert "Not a match (skipped): 3" in out


def test_poll_once_routes_fetch_to_run_fetch():
    fake = FakeTelegram([[update(1, "/fetch")]])
    tb.poll_once(tb.TelegramClient(fake), settings(), None, lambda progress: "Sweep done")
    texts = [text for _, text in fake.sent]
    assert texts[0].startswith("[LOCAL] \U0001F50E Searching for jobs")
    assert texts[-1] == "[LOCAL] Sweep done"


# ---------------------------------------------------------------- Module 03: screening desk

from datetime import datetime, timedelta  # noqa: E402

from jobengine.screen.desk import APPROVED_TEXT, fake_desk  # noqa: E402
from jobengine.sweep.fakes import FAKE_TODAY  # noqa: E402


class ButtonTelegram(FakeTelegram):
    """FakeTelegram that also records buttons and answers callback queries."""

    def __init__(self, batches=None):
        super().__init__(batches)
        self.buttons = []
        self.documents = []

    def __call__(self, method, payload, timeout):
        if method == "answerCallbackQuery":
            self.calls.append((method, payload))
            return {"ok": True, "result": True}
        if method in ("sendMessage", "sendDocument"):
            rows = (payload.get("reply_markup") or {}).get("inline_keyboard") or []
            self.buttons.append([b["callback_data"] for row in rows for b in row])
        if method == "sendDocument":
            self.calls.append((method, payload))
            self.sent.append((payload["chat_id"], payload["caption"]))
            self.documents.append(payload["_document"])
            return {"ok": True, "result": {"message_id": len(self.sent)}}
        return super().__call__(method, payload, timeout)


def tap(update_id, data, user=CHAT_ID):
    return {"update_id": update_id, "callback_query": {
        "id": f"cb{update_id}", "from": {"id": int(user)},
        "message": {"chat": {"id": int(user)}}, "data": data}}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 1, 9, 0)

    def __call__(self):
        return self.now


LINKS = ["mailto:alex@example.com", "tel:+15550100000", "https://alex.example.com",
         "https://github.com/alex-example", "https://www.linkedin.com/in/alex-example/"]


def fake_rendering(d, tmp_path):
    """Resume rendering without WeasyPrint: tests run anywhere."""
    from jobengine.resume.models import Measure

    d.resume.render = lambda html: b"%PDF-fake"
    d.resume.measure = lambda html: Measure(pages=1, fill=91.0, text="", links=LINKS,
                                            fonts={"Lato"})
    d.resume.out_dir = tmp_path


@pytest.fixture
def desk(tmp_path):
    s = settings()
    d = fake_desk(s, FAKE_TODAY, now=Clock())
    fake_rendering(d, tmp_path)
    d.screen()  # screen the fixture rows first
    return d


def talk(desk, *items):
    fake = ButtonTelegram([[item if isinstance(item, dict) else update(i, item)]
                           for i, item in enumerate(items, 1)])
    client = tb.TelegramClient(fake)
    offset = None
    for _ in items:
        offset = tb.poll_once(client, settings(), offset, desk=desk)
    return fake


def test_pending_shows_one_card_in_rank_order(desk):
    fake = talk(desk, "/pending")
    assert len(fake.sent) == 1
    text = fake.sent[0][1]
    assert text.startswith("[LOCAL] [1/")
    assert "Apply high\nTulip Data B.V., Cloud Engineer" in text
    assert "Match: 1 strong, 0 transferable, 1 gaps" in text
    assert "1 LinkedIn job needs the JD: /jd" in text
    assert fake.buttons[0] == ["ap:nl-sponsor-yes", "sk:nl-sponsor-yes", "nx:1"]
    order = [r.page_id for r in desk.ranked()]
    assert order.index("pl-clean") < order.index("ie-5-years")  # exactly 5 years ranks lower
    assert order.index("ie-5-years") < order.index("pl-polish-plus")  # tier first
    assert order.index("nl-not-register") > order.index("pl-polish-plus")


def test_next_approve_builds_resume_and_second_tap_shows_it_again(desk):
    fake = talk(desk, tap(1, "nx:1"), tap(2, "ap:pl-clean"), tap(3, "ap:pl-clean"),
                tap(4, "sk:nl-ind"), tap(5, "sk:nl-ind"))
    texts = [t for _, t in fake.sent]
    assert "Vistula Cloud, DevOps Engineer" in texts[0]
    assert texts[1] == "[LOCAL] " + APPROVED_TEXT.format(job="Vistula Cloud, DevOps Engineer")
    assert texts[2] == "[LOCAL] Building resume for Vistula Cloud..."
    assert texts[3].startswith("[LOCAL] Resume r1 for Vistula Cloud, DevOps Engineer\nFill 91.0%")
    assert fake.documents[0] == ("Alex_Devops_VistulaCloud_r1.pdf", b"%PDF-fake")
    assert fake.buttons[3] == ["ra:00000001000040008000000000000001", "rb:pl-clean"]
    # Second Approve tap: the same preview again, no second build.
    assert texts[6].startswith("[LOCAL] Resume r1 for Vistula Cloud")
    assert len(desk.resume.resume_log.rows) == 1
    assert desk.repo.rows["pl-clean"]["Status"] == "Resume built"
    assert desk.repo.rows["nl-ind"]["Status"] == "Declined"
    assert "[LOCAL] Already handled" in texts
    assert [m for m, _ in fake.calls if m == "answerCallbackQuery"] == ["answerCallbackQuery"] * 5


def test_buttons_from_other_users_are_ignored(desk):
    fake = talk(desk, tap(1, "ap:pl-clean", user="999"))
    assert fake.sent == []
    assert desk.repo.rows["pl-clean"]["Status"] == "Screened"


def test_jd_two_messages_then_done_screens_the_row(desk):
    fake = talk(desk, "/jd https://www.linkedin.com/jobs/view/4012345678",
                "Northwind Cloud is hiring a Platform Engineer in Cork.",
                "You run Kubernetes and Terraform on AWS.", "/done")
    texts = [t for _, t in fake.sent]
    assert "Collecting the job description for Northwind Cloud, Platform Engineer" in texts[0]
    assert "characters so far" in texts[1]
    body = desk.repo.rows["li-no-jd"]["body"]
    assert body[0] == "Description source: pasted (full)"
    assert body[1] == ("Northwind Cloud is hiring a Platform Engineer in Cork.\n"
                       "You run Kubernetes and Terraform on AWS.")
    assert desk.repo.rows["li-no-jd"]["Screen verdict"] == "Needs review"  # short paste
    assert any(t.startswith("[LOCAL] Screening done: 1 screened") for t in texts)
    assert desk.captures.get() is None


def test_jd_for_a_new_url_creates_the_row(desk):
    talk(desk, "/jd https://www.irishjobs.ie/job/77\nCompany: Harbour Soft\nRole: SRE",
         "We run Kubernetes.", "/done")
    url = "https://www.irishjobs.ie/job/77"
    created = [r for r in desk.repo.rows.values() if r.get("URL") == url]
    assert len(created) == 1
    row = created[0]
    assert (row["Company"], row["Role"], row["Board"]) == ("Harbour Soft", "SRE", "IrishJobs.ie")
    assert (row["Status"], row["Times seen"], row["First seen"]) == ("Screened", 1, FAKE_TODAY)


def test_jd_asks_for_company_and_role(desk):
    fake = talk(desk, "/jd https://jobs.example.com/unknown", "We run Kubernetes.",
                "Company: Acme Cloud\nRole: DevOps Engineer", "/done")
    texts = [t for _, t in fake.sent]
    assert "Send the company and role first" in texts[0]
    assert "Still need the company and role" in texts[1]
    assert any("Acme Cloud" == r.get("Company") for r in desk.repo.rows.values())


def test_old_capture_is_discarded(desk):
    talk(desk, "/jd https://www.linkedin.com/jobs/view/4012345678")
    desk.now.now += timedelta(minutes=21)
    fake = talk(desk, "pasted text")
    assert "older than 20 minutes and was discarded" in fake.sent[0][1]
    assert desk.captures.get() is None
    assert "body" not in desk.repo.rows["li-no-jd"] or not desk.repo.rows["li-no-jd"]["body"]


def test_jd_without_url_lists_waiting_rows(desk):
    fake = talk(desk, "/jd")
    assert "1 job waits for a description" in fake.sent[0][1]
    assert "https://www.linkedin.com/jobs/view/4012345678" in fake.sent[0][1]


def test_plain_text_without_capture_gets_the_usual_answer(desk):
    fake = talk(desk, "hello")
    assert fake.sent[0][1] == "[LOCAL] I only understand commands for now. Send /help to see them."


def test_screen_command(desk):
    fake = talk(desk, "/screen", "/screen pl-clean")
    assert fake.sent[0][1].startswith("[LOCAL] Screening done: 0 screened")
    assert fake.sent[1][1].startswith("[LOCAL] Screening done: 1 screened (1 high")
    assert "ready to review: /pending" in fake.sent[1][1]


def test_fetch_chains_screening():
    s = settings()
    d = fake_desk(s, FAKE_TODAY)
    text = tb.make_fetcher(s, True, d)(lambda line: None)
    assert "New jobs added to Notion: 8" in text
    assert "Screening done: 20 screened" in text
    assert text.endswith("ready to review: /pending")


def test_desk_failures_are_reported_not_raised(desk):
    def boom(index=0):
        raise RuntimeError("notion down")

    desk.pending = boom
    fake = talk(desk, "/pending")
    assert fake.sent[0][1] == "[LOCAL] /pending failed: notion down"


def test_fake_harness_taps(monkeypatch, capsys):
    monkeypatch.setattr(tb, "get_settings", lambda: load_settings("local", {}))
    monkeypatch.setattr(sys, "stdin", io.StringIO("/screen\n/pending\ntap ap:nl-sponsor-yes\n"))
    assert tb.main(["--fake"]) == 0
    out = capsys.readouterr().out
    assert "[Approve: tap ap:nl-sponsor-yes]" in out
    assert "bot> [LOCAL] Approved: Tulip Data B.V., Cloud Engineer." in out
    assert "bot> [LOCAL] Building resume for Tulip Data B.V...." in out


def test_help_lists_screening_commands():
    for command in ("/pending", "/jd", "/done", "/screen"):
        assert command in tb.HELP_TEXT


def test_missing_llm_key_is_a_clear_reply(desk):
    from jobengine.llm import LLMError

    def no_key(config):
        raise LLMError("ANTHROPIC_API_KEY is not set, so LLM screening cannot run")

    desk.deps.llm = no_key
    desk.repo.rows["pl-clean"]["Screen verdict"] = "Unscreened"
    fake = talk(desk, "/screen")
    assert fake.sent[0][1] == (
        "[LOCAL] Screening could not run: ANTHROPIC_API_KEY is not set, so LLM screening "
        "cannot run"
    )


# ---------------------------------------------------------------- Module 04: resume buttons


def preview_ref(fake):
    return next(t for _, t in fake.sent if "Ref RL-" in t)


def test_reply_to_preview_builds_revision_two(desk):
    fake = talk(desk, tap(1, "ap:pl-clean"))
    caption = preview_ref(fake)
    reply = {"update_id": 2, "message": {
        "chat": {"id": int(CHAT_ID)}, "text": "drop Oracle",
        "reply_to_message": {"message_id": 3, "caption": caption.removeprefix("[LOCAL] ")}}}
    fake = talk(desk, reply)
    texts = [t for _, t in fake.sent]
    assert texts[0] == "[LOCAL] Building resume for Vistula Cloud..."
    assert texts[1].startswith("[LOCAL] Resume r2 for Vistula Cloud")
    assert "-Oracle" in texts[1] and "Ref RL-00000002" in texts[1]
    assert fake.documents[0][0] == "Alex_Devops_VistulaCloud_r2.pdf"


def test_reply_to_unrelated_message_is_not_a_correction(desk):
    talk(desk, tap(1, "ap:pl-clean"))
    reply = {"update_id": 2, "message": {
        "chat": {"id": int(CHAT_ID)}, "text": "drop Oracle",
        "reply_to_message": {"message_id": 1, "text": "[1/17] Apply high ..."}}}
    fake = talk(desk, reply)
    assert fake.sent[0][1] == "[LOCAL] I only understand commands for now. Send /help to see them."
    assert len(desk.resume.resume_log.rows) == 1


def test_approve_resume_old_revision_rebuild_and_i_applied(desk):
    talk(desk, tap(1, "ap:pl-clean"))
    fake = talk(desk, tap(1, "rb:pl-clean"))
    assert fake.sent[1][1].startswith("[LOCAL] Resume r2 for Vistula Cloud")
    old, new = "00000001000040008000000000000001", "00000002000040008000000000000002"
    fake = talk(desk, tap(1, f"ra:{old}"))
    assert fake.sent[0][1] == "[LOCAL] A newer revision exists"
    assert fake.sent[1][1].startswith("[LOCAL] Resume r2")
    fake = talk(desk, tap(1, f"ra:{new}"))
    assert fake.sent[0][1] == ("[LOCAL] Resume approved and saved. Apply here: "
                               "https://jobs.example.com/pl-clean")
    assert fake.buttons[0] == ["ia:pl-clean"]
    row = desk.resume.resume_log.get(new)
    assert row["Approved"] is True and row["File"].startswith("DRY RUN: ")
    fake = talk(desk, tap(1, "ia:pl-clean"), tap(2, "ia:pl-clean"))
    assert fake.sent[0][1].startswith("[LOCAL] Marked as applied on 2026-10-01")
    assert fake.sent[1][1] == "[LOCAL] Already handled (Status is Applied)."
    assert desk.repo.rows["pl-clean"]["Status"] == "Applied"


def test_resume_build_failure_is_a_message(desk):
    desk.resume.blocks = lambda: []
    fake = talk(desk, tap(1, "ap:pl-clean"))
    assert fake.sent[-2][1] == "[LOCAL] Golden Master not found in Notion. Nothing built."
    assert desk.repo.rows["pl-clean"]["Status"] == "Approved"


def test_send_document_is_multipart():
    body, content_type = tb.multipart({"chat_id": "1", "caption": "c",
                                       "reply_markup": {"inline_keyboard": []},
                                       "_document": ("a.pdf", b"%PDF-1")})
    assert content_type.startswith("multipart/form-data; boundary=")
    assert b'name="document"; filename="a.pdf"' in body and b"%PDF-1" in body
    assert b'name="reply_markup"' in body and b'{"inline_keyboard": []}' in body


def test_console_reply_command_carries_the_caption(tmp_path):
    out = io.StringIO()
    transport = tb.console_transport("1", io.StringIO("reply 1 drop Oracle\n"), out, tmp_path)
    transport("sendDocument", {"chat_id": "1", "caption": "Ref RL-00000001",
                               "_document": ("a.pdf", b"%PDF")}, 5)
    update = transport("getUpdates", {}, 5)["result"][0]["message"]
    assert update["text"] == "drop Oracle"
    assert update["reply_to_message"]["caption"] == "Ref RL-00000001"
    assert (tmp_path / "a.pdf").read_bytes() == b"%PDF"


# ---------------------------------------------------------------- Module 05: contacts


def test_resume_approval_runs_the_contact_lookup(desk):
    talk(desk, tap(1, "ap:pl-clean"))
    fake = talk(desk, tap(1, "ra:00000001000040008000000000000001"))
    texts = [t for _, t in fake.sent]
    assert texts[0].startswith("[LOCAL] Resume approved and saved.")
    assert texts[1] == "[LOCAL] Finding contacts for Vistula Cloud..."
    summary = texts[2]
    assert summary.startswith("[LOCAL] Contacts for Vistula Cloud, DevOps Engineer (Poland)")
    assert "Peer engineer: Kasia Example (Platform Engineer) [cache]" in summary
    assert "found. Credits: Apollo" in summary
    assert desk.repo.rows["pl-clean"]["Contacts"]
    # Module 06: then the Gmail drafts (DRY_RUN here: nothing created, a preview instead).
    assert texts[3].startswith("[LOCAL] Writing Gmail drafts for 4 contacts")
    drafts = texts[4]
    assert drafts.startswith("[LOCAL] Drafts for Vistula Cloud, DevOps Engineer")
    assert "DRY RUN: 4 drafts not created." in drafts
    assert "Subject: [LOCAL] to piotr.example@vistula.example.com | " in drafts
    assert "Attachment: Alex_Devops_VistulaCloud.pdf" in drafts


def test_drafts_command(desk):
    talk(desk, tap(1, "ap:pl-clean"))
    talk(desk, tap(1, "ra:00000001000040008000000000000001"))
    fake = talk(desk, "/drafts pl-clean")
    assert fake.sent[-1][1].startswith("[LOCAL] Drafts for Vistula Cloud, DevOps Engineer")
    assert talk(desk, "/drafts").sent[0][1] == "[LOCAL] Send /drafts <job URL or page id>."
    fake = talk(desk, "/drafts https://jobs.example.com/nl-ind")
    assert fake.sent[0][1].startswith("[LOCAL] No approved resume for this job")
    assert "/drafts" in tb.HELP_TEXT


def test_credits_command(desk):
    fake = talk(desk, "/credits")
    text = fake.sent[0][1]
    # Fake today is 2026-10-01: September counters start again at 0.
    assert "Apollo: 0 / 75 per month (reset for this month), updated 2026-09-28, no key" in text
    assert "Snov: 0 / 50 per month, updated 2026-08-20, no key" in text
    assert "Paid calls are off here" in text


def test_contacts_command_asks_for_an_unknown_domain_and_resumes_on_reply(desk):
    fake = talk(desk, "/contacts https://jobs.example.com/nl-ind")
    question = fake.sent[-1][1]
    assert question == ("[LOCAL] What is the email domain for Canal Payments? Reply to this "
                        "message with the domain, e.g. example.com. Ref JOB-nlind")
    reply = {"update_id": 2, "message": {
        "chat": {"id": int(CHAT_ID)}, "text": "canal.example.com",
        "reply_to_message": {"message_id": 2, "text": question.removeprefix("[LOCAL] ")}}}
    fake = talk(desk, reply)
    texts = [text for _, text in fake.sent]
    assert texts[-1].startswith("[LOCAL] Contacts for Canal Payments")
    bad = dict(reply, message=dict(reply["message"], text="canal.greenhouse.io"))
    fake = talk(desk, bad)
    assert "no longer open" in fake.sent[-1][1]  # already answered


def test_contacts_command_needs_a_job(desk):
    assert talk(desk, "/contacts").sent[0][1] == "[LOCAL] Send /contacts <job URL or page id>."
    assert "No Job Opportunities row" in talk(desk, "/contacts nothing-here").sent[0][1]


def test_help_lists_contacts_and_credits():
    assert "/contacts" in tb.HELP_TEXT and "/credits" in tb.HELP_TEXT


# ---------------------------------------------------------------- Module 07: tracking


def texts(fake):
    return [text for _, text in fake.sent]


def test_today_sends_the_report_then_the_cards(desk):
    fake = talk(desk, "/today")
    sent = texts(fake)
    assert sent[0] == "[LOCAL] Running the daily check..."
    assert sent[1].startswith("[LOCAL] Daily check 2026-10-15 (since 2026-10-01 08:00)")
    assert "Replies: Piotr Example (Vistula Cloud) -> Interview" in sent[1]
    assert "Tokens: alerts OK, sender OK" in sent[1]
    assert any("Reply from marek.example@vistula.example.com" in t for t in sent)
    assert ["lk:jobfjord:t-fjord-a", "lk:jobfjord:-"] in fake.buttons
    assert "rd:20261015" in [b for row in fake.buttons for b in row]


def test_card_taps(desk):
    talk(desk, "/today")
    fake = talk(desk, tap(1, "rc:c:cmarek:positive"))
    assert fake.sent[-1][1] == "[LOCAL] Applied: Marek Example (Vistula Cloud) -> Replied"
    fake = talk(desk, tap(2, "lk:jobfjord:t-fjord-a"))
    assert fake.sent[-1][1] == "[LOCAL] Linked Fjord Data."
    fake = talk(desk, tap(3, "rd:20261015"))
    assert fake.sent[-1][1] == "[LOCAL] DRY RUN: would delete 1 old contacts (only prod deletes)."


def test_status_is_extended(desk):
    text = talk(desk, "/status").sent[0][1]
    assert text.startswith("[LOCAL] Job Engine | env=local")
    assert "Jobs by Status: Applied 5" in text
    assert "Contacts by Status: Contacted 8" in text
    assert "Drafts waiting: 3 cold mails, 2 follow-ups" in text
    assert "Last daily check: never" in text


def test_stats_followups_health_sources_digest(desk):
    stats = talk(desk, "/stats").sent[0][1]
    assert "Last 30 days: applied 5, replied 0" in stats  # before /today
    assert "All time: applied 8, replied 1, screening 1, interview 1, offer 1" in stats
    follow = talk(desk, "/followups").sent[0][1]
    assert "- Ola Example (Vistula Cloud): due 2026-10-17" in follow
    health = talk(desk, "/health").sent[0][1]
    assert "Notion: OK" in health and "Gmail sender token: OK" in health
    assert "Environment: local, DRY_RUN on" in health
    tb.make_fetcher(settings(), True, desk)(lambda line: None)  # a fake sweep stores counts
    sources = talk(desk, "/sources").sent[0][1]
    assert "Sweep sources (last sweep: 2026-10-01)" in sources
    assert "ats: enabled, no secret needed, last sweep" in sources
    digest = talk(desk, "/digest").sent[0][1]
    assert digest.startswith("[LOCAL] Weekly digest 2026-10-15")
    assert "Strategy gate:" in digest


def test_help_lists_tracking_commands():
    for command in ("/today", "/followups", "/stats", "/sources", "/health", "/digest"):
        assert command in tb.HELP_TEXT
