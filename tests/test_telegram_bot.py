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
