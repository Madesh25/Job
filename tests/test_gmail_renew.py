"""Renewing a Gmail token from Telegram (/gmailtoken, 15 Oct): the 7-day expiry of an OAuth
app in Testing."""

import base64
import hashlib
import json
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from test_telegram_bot import CHAT_ID, FakeTelegram, settings

from jobengine import gmail_renew as gr
from jobengine import telegram_bot as tb
from jobengine.bot_state import FakeBotState
from jobengine.gmail_client import SENDER_SCOPES
from jobengine.settings import load_settings, saved_gmail_env

NOW = datetime(2026, 10, 15, 10, 0)
TOKEN = json.dumps({"client_id": "cid.apps.googleusercontent.com", "client_secret": "csecret",
                    "refresh_token": "old", "token_uri": gr.TOKEN_URL,
                    "scopes": SENDER_SCOPES})
S = settings(GMAIL_ALERTS_TOKEN_JSON_m02=TOKEN, GMAIL_SENDER_TOKEN_JSON=TOKEN)


@pytest.fixture(autouse=True)
def no_pending():
    gr.PENDING.clear()
    yield
    gr.PENDING.clear()


def link(text):
    url = next(word for word in text.split() if word.startswith(gr.AUTH_URL))
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def answer_for(query, code="4/one-time-code"):
    return f"{gr.REDIRECT}?state={query['state']}&code={code}&scope=x"


def test_the_sign_in_link_uses_your_client_pkce_and_the_right_account():
    text = gr.start(S, "m02", NOW)
    assert "Renew the Gmail token for m02 (madeshwaranm02@gmail.com)" in text
    assert "This site can't be reached" in text and "deletes your message" in text
    q = link(text)
    assert q["client_id"] == "cid.apps.googleusercontent.com"
    assert q["redirect_uri"] == gr.REDIRECT and q["login_hint"] == "madeshwaranm02@gmail.com"
    assert q["access_type"] == "offline" and q["prompt"] == "consent"
    assert q["scope"] == " ".join(SENDER_SCOPES) and q["code_challenge_method"] == "S256"
    verifier = gr.PENDING[q["state"]].verifier
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert q["code_challenge"] == digest.decode()
    # mm has no token of its own yet: the m02 token's client is used, with mm's address.
    assert "madeshwaran.manikam@gmail.com" in gr.start(S, "mm", NOW)
    assert len(gr.PENDING) == 2
    gr.start(S, "m02", NOW)  # a new link replaces the old one of that account
    assert len(gr.PENDING) == 2 and q["state"] not in gr.PENDING


def test_start_refuses_outside_local_and_without_a_client():
    assert gr.start(settings("dev", GMAIL_SENDER_TOKEN_JSON=TOKEN), "m02", NOW) == gr.NOT_LOCAL
    assert gr.start(S, "work", NOW) == gr.USAGE
    assert "jobengine.gmail_auth" in gr.start(settings(), "m02", NOW)
    assert not gr.PENDING


def test_a_pasted_address_is_recognised():
    assert gr.looks_like_answer("http://127.0.0.1:8765/?state=abc&code=4/x")
    assert gr.looks_like_answer("  127.0.0.1:8765/?state=abc&code=4/x ")
    assert not gr.looks_like_answer("http://example.com/?state=abc")
    assert not gr.looks_like_answer("/gmailtoken")


def test_finish_saves_the_new_token_for_the_right_account(tmp_path):
    q = link(gr.start(S, "m02", NOW))
    posted = []

    def post(url, data):
        posted.append((url, data))
        return {"access_token": "fake-access", "refresh_token": "1//new",
                "scope": " ".join(SENDER_SCOPES), "token_type": "Bearer"}

    text = gr.finish(S, answer_for(q), NOW + timedelta(minutes=2), tmp_path, post,
                     lambda token, access: "madeshwaranm02@gmail.com")
    assert text.startswith("✅ Saved the new Gmail token for m02 (madeshwaranm02@gmail.com)")
    assert "/restart" in text and "22 Oct" in text
    url, data = posted[0]
    assert url == gr.TOKEN_URL and data["code"] == "4/one-time-code"
    assert data["grant_type"] == "authorization_code" and data["redirect_uri"] == gr.REDIRECT
    assert data["code_verifier"] and data["client_secret"] == "csecret"
    saved = json.loads((tmp_path / "gmail-m02.json").read_text())
    assert saved["issued"] == "2026-10-15" and saved["email"] == "madeshwaranm02@gmail.com"
    assert saved["token"]["refresh_token"] == "1//new" and "access_token" not in saved["token"]
    assert not gr.PENDING  # a code works once
    env = saved_gmail_env(tmp_path)
    assert set(env) == {"GMAIL_ALERTS_TOKEN_JSON_m02", "GMAIL_SENDER_TOKEN_JSON"}
    s = load_settings("local", {"GMAIL_SENDER_TOKEN_JSON": TOKEN, **env})
    assert json.loads(s.gmail_sender_token_json)["refresh_token"] == "1//new"  # file wins


def test_finish_refuses_the_wrong_account_and_missing_permissions(tmp_path):
    good = {"access_token": "a", "refresh_token": "r", "scope": " ".join(SENDER_SCOPES)}
    q = link(gr.start(S, "mm", NOW))
    text = gr.finish(S, answer_for(q), NOW, tmp_path, lambda u, d: good,
                     lambda t, a: "madeshwaranm02@gmail.com")
    assert "but mm is madeshwaran.manikam@gmail.com. Nothing was saved" in text
    q = link(gr.start(S, "m02", NOW))
    text = gr.finish(S, answer_for(q), NOW, tmp_path,
                     lambda u, d: {**good, "scope": SENDER_SCOPES[0]}, lambda t, a: "x")
    assert "did not give every permission" in text and SENDER_SCOPES[1] in text
    assert not list(tmp_path.iterdir())


def test_finish_explains_old_cancelled_and_unknown_sign_ins(tmp_path):
    assert "not known" in gr.finish(S, f"{gr.REDIRECT}?state=nope&code=x", NOW, tmp_path)
    q = link(gr.start(S, "m02", NOW))
    assert "older than 15 minutes" in gr.finish(S, answer_for(q), NOW + timedelta(minutes=16),
                                                tmp_path)
    q = link(gr.start(S, "m02", NOW))
    text = gr.finish(S, f"{gr.REDIRECT}?state={q['state']}&error=access_denied", NOW, tmp_path)
    assert "Google answered access_denied" in text and not gr.PENDING
    q = link(gr.start(S, "m02", NOW))

    def refused(url, data):
        raise gr.http.HttpError("POST failed: HTTP 400 invalid_grant", 400)

    assert "Google refused the code" in gr.finish(S, answer_for(q), NOW, tmp_path, refused)


def _save(folder, account, issued):
    folder.mkdir(parents=True, exist_ok=True)
    token = {**json.loads(TOKEN), "refresh_token": f"{account}-r"}
    (folder / f"gmail-{account}.json").write_text(json.dumps(
        {"account": account, "issued": issued, "token": token}))


def test_status_shows_each_token_and_when_it_stops(tmp_path):
    _save(tmp_path, "m02", "2026-10-15")
    text = gr.status(S, {}, date(2026, 10, 16), tmp_path,
                     check=lambda token: None if token and "m02-r" in token else "not set")
    assert ("- m02 (madeshwaranm02@gmail.com): OK; renewed in Telegram on 15 Oct, works until "
            "22 Oct while the Google app is in Testing") in text
    assert "- mm (madeshwaran.manikam@gmail.com): NOT working (not set); from .env" in text
    text = gr.status(S, {"gmail.token_days": "0"}, date(2026, 10, 16), tmp_path,
                     check=lambda token: None)
    assert "renewed in Telegram on 15 Oct;" not in text and "works until" not in text
    assert gr.status(settings("dev"), {}, date(2026, 10, 16), tmp_path) == gr.NOT_LOCAL


def test_reminder_comes_once_a_day_from_the_day_before(tmp_path):
    _save(tmp_path, "m02", "2026-10-15")
    state = FakeBotState()
    assert gr.reminder(S, state, {}, date(2026, 10, 20), tmp_path) == []
    first = gr.reminder(S, state, {}, date(2026, 10, 21), tmp_path)
    assert first == ["⏰ The Gmail token for m02 (madeshwaranm02@gmail.com) stops working "
                     "tomorrow: the Google app is in Testing. Renew it in 2 minutes: "
                     "/gmailtoken m02"]
    assert gr.reminder(S, state, {}, date(2026, 10, 21), tmp_path) == []  # once a day
    assert "now (it has expired)" in gr.reminder(S, state, {}, date(2026, 10, 23), tmp_path)[0]
    assert gr.reminder(S, FakeBotState(), {"gmail.token_days": "0"}, date(2026, 10, 23),
                       tmp_path) == []  # the app is published


class DeletingTelegram(FakeTelegram):
    def __call__(self, method, payload, timeout):
        if method in ("deleteMessage", "sendChatAction"):
            self.calls.append((method, payload))
            return {"ok": True, "result": True}
        return super().__call__(method, payload, timeout)


def test_telegram_routes_the_command_and_deletes_the_pasted_address(monkeypatch):
    fake = DeletingTelegram()
    client = tb.TelegramClient(fake)
    s = settings()
    monkeypatch.setattr(gr, "start", lambda s, account, now: f"link for {account}")
    monkeypatch.setattr(gr, "finish", lambda s, text, now: "saved it")
    monkeypatch.setattr(gr, "status", lambda s, config, today: "status")
    message = {"chat": {"id": int(CHAT_ID)}, "message_id": 77}
    tb.handle_one(client, s, {"update_id": 1, "message": {**message, "text": "/gmailtoken MM"}})
    tb.handle_one(client, s, {"update_id": 2, "message": {**message, "text": "/gmailtoken"}})
    tb.handle_one(client, s, {"update_id": 3, "message": {
        **message, "text": "http://127.0.0.1:8765/?state=s&code=c"}})
    assert [text for _, text in fake.sent] == ["[LOCAL] link for mm", "[LOCAL] status",
                                               "[LOCAL] saved it"]
    deleted = [p for m, p in fake.calls if m == "deleteMessage"]
    assert deleted == [{"chat_id": CHAT_ID, "message_id": 77}]
    assert any(c["command"] == "gmailtoken" for c in tb.bot_commands())


def test_saved_token_file_problems_are_ignored(tmp_path):
    (tmp_path / "gmail-m02.json").write_text("not json")
    (tmp_path / "gmail-mm.json").write_text(json.dumps({"token": {"client_id": "x"}}))
    assert saved_gmail_env(tmp_path) == {}
    assert saved_gmail_env(tmp_path / "missing") == {}
