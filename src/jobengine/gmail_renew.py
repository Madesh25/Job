"""Renew a Gmail token from Telegram (/gmailtoken), with a reminder before it expires.

While the Google OAuth app is in Testing, Google cancels its tokens 7 days after they are made
(invalid_grant). The real fix is to publish the app; until then a person still has to click
Allow, but nothing else: no command window, no .env edit.

    /gmailtoken          which token is OK and when it stops working
    /gmailtoken m02      a Google sign-in link for madeshwaranm02 (mm: madeshwaran.manikam)

You open the link on your PC, choose the account and allow. Google then sends the browser to
http://127.0.0.1:8765/?state=...&code=...: the bot answers that page itself (`listen`, with
docker run -p 127.0.0.1:8765:8765), so the page says "Done" and nothing is copied (15 Oct:
without it the page did not load and the copy step was missed). Without the port the page
does not load: you copy that address and paste it into the chat instead. Either way the bot
exchanges the one-time code (with PKCE) for a token, checks that it is the right account and
has both scopes, and saves it in out/tokens (on your PC, ignored by git, never in Notion); a
pasted address is deleted from the chat. /restart then uses it: a saved token wins over the one
in .env (settings.saved_gmail_env).

A token renewed here carries its date, so the bot reminds you once a day from the day before
it stops working (Config gmail.token_days, 7; set 0 once the app is published). Only the bot
on your PC (APP_ENV local) renews tokens; on Cloud Run they live in Secret Manager.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from jobengine import http
from jobengine.gmail_client import SENDER_SCOPES, refresh_error
from jobengine.settings import GMAIL_TOKEN_DIR, Settings

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
# Desktop OAuth clients accept any loopback address. Nothing listens there: you copy the
# address the browser lands on.
LISTEN_PORT = 8765
REDIRECT = f"http://127.0.0.1:{LISTEN_PORT}/"
ANSWER_STARTS = ("http://127.0.0.1:8765", "127.0.0.1:8765", "http://localhost:8765")
PENDING_MINUTES = 15
DEFAULT_TOKEN_DAYS = 7  # tokens of an app in Testing; Config gmail.token_days (0: published)
REMINDER_KEY = "gmail_token_reminder"  # bot_state: {account: day of the last reminder}
ACCOUNTS = ("m02", "mm")
USAGE = ("/gmailtoken shows your Gmail tokens; /gmailtoken m02 or /gmailtoken mm renews one "
         "(a Google sign-in link, then you paste the address you land on).")
NOT_LOCAL = ("/gmailtoken works on the bot on your PC only. On Cloud Run the Gmail tokens are "
             "in Secret Manager (docs/deploy.md B2).")


PASTE_STEP = ("3. The browser then shows \"This site can't be reached\" (127.0.0.1). That is "
              "expected: copy the whole address from the address bar and paste it here within "
              f"{PENDING_MINUTES} minutes (the bot deletes your message).\n")
LISTENING_STEP = ("3. Google then opens a page at 127.0.0.1:8765 that says \"Done\": the bot "
                  "saves the token by itself and tells you here. If that page says \"This site "
                  "can't be reached\" instead, the bot was started without "
                  "-p 127.0.0.1:8765:8765: copy the whole address from the address bar and "
                  f"paste it here within {PENDING_MINUTES} minutes.\n")


@dataclass
class Pending:
    account: str
    verifier: str
    started: datetime


PENDING: dict[str, Pending] = {}  # OAuth state -> the sign-in waiting for its pasted address


def expected_email(s: Settings, account: str) -> str:
    return s.dev_sender if account == "m02" else s.prod_sender


def current_token(s: Settings, account: str) -> str | None:
    if account == "m02":
        return s.gmail_alerts_token_json_m02 or s.gmail_alerts_token_json or \
            s.gmail_sender_token_json
    return s.gmail_alerts_token_json_mm


def _client(s: Settings, account: str) -> tuple[str, str] | None:
    """The OAuth client (id, secret) of the account's token, else of any token you have:
    both tokens were made with the same client (docs/gmail.md)."""
    for name in (account, *[a for a in ACCOUNTS if a != account]):
        try:
            info = json.loads(current_token(s, name) or "")
        except ValueError:
            continue
        if isinstance(info, dict) and info.get("client_id") and info.get("client_secret"):
            return str(info["client_id"]), str(info["client_secret"])
    return None


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def start(s: Settings, account: str, now: datetime, listening: bool = False) -> str:
    """The message with the Google sign-in link for one account. `listening`: the bot answers
    the page Google returns to (`listen`), so nothing has to be copied."""
    if s.app_env != "local":
        return NOT_LOCAL
    if account not in ACCOUNTS:
        return USAGE
    client = _client(s, account)
    if client is None:
        return ("No Gmail token with an OAuth client id is set yet, so there is nothing to "
                "renew from. Make the first one with python -m jobengine.gmail_auth "
                "(docs/gmail.md).")
    state = secrets.token_urlsafe(16)
    verifier = secrets.token_urlsafe(48)
    for old in [k for k, p in PENDING.items() if p.account == account]:
        del PENDING[old]  # only the newest link of an account works
    PENDING[state] = Pending(account, verifier, now)
    email = expected_email(s, account)
    url = AUTH_URL + "?" + urlencode({
        "client_id": client[0], "redirect_uri": REDIRECT, "response_type": "code",
        "scope": " ".join(SENDER_SCOPES), "access_type": "offline", "prompt": "consent",
        "login_hint": email, "state": state, "code_challenge": _challenge(verifier),
        "code_challenge_method": "S256"})
    return (f"Renew the Gmail token for {account} ({email}):\n"
            f"1. Open this link on your PC: {url}\n"
            f"2. Choose {email}. If Google says the app is not verified: Advanced, then Go to "
            "Job (unsafe). Tick every box and press Continue.\n"
            + (LISTENING_STEP if listening else PASTE_STEP) +
            "The bot saves the new token on your PC (out/tokens, never in Notion or git).")


def looks_like_answer(text: str) -> bool:
    text = text.strip()
    return text.startswith(ANSWER_STARTS) and "state=" in text


def _token_info(answer: Mapping[str, Any], client: tuple[str, str]) -> dict[str, Any]:
    return {"client_id": client[0], "client_secret": client[1],
            "refresh_token": answer.get("refresh_token"), "token_uri": TOKEN_URL,
            "scopes": str(answer.get("scope") or "").split()}


def whoami(token: Mapping[str, Any], access_token: str) -> str:
    """The Gmail address a token belongs to (users.getProfile)."""
    from google.oauth2.credentials import Credentials

    from jobengine.gmail_auth import authorised_email

    creds = Credentials(token=access_token, **{k: token[k] for k in (
        "refresh_token", "client_id", "client_secret", "token_uri", "scopes")})
    return str(authorised_email(creds))


def finish(s: Settings, text: str, now: datetime, folder: Path = GMAIL_TOKEN_DIR,
           post: Callable[[str, Mapping[str, str]], Any] | None = None,
           who: Callable[[Mapping[str, Any], str], str] = whoami) -> str:
    """Exchange the pasted address for a token and save it; the answer for the chat."""
    if s.app_env != "local":
        return NOT_LOCAL
    query = parse_qs(urlsplit(text.strip()).query)
    state = (query.get("state") or [""])[0]
    pending = PENDING.get(state)
    if pending is None:
        return ("This sign-in is not known (the link is old, or the bot restarted since). "
                "Send /gmailtoken m02 or /gmailtoken mm for a new link.")
    if now - pending.started > timedelta(minutes=PENDING_MINUTES):
        del PENDING[state]
        return (f"That link was older than {PENDING_MINUTES} minutes. Send /gmailtoken "
                f"{pending.account} for a new one.")
    if query.get("error"):
        del PENDING[state]
        return (f"Google answered {query['error'][0]} (you pressed Cancel or a box was not "
                f"ticked). Send /gmailtoken {pending.account} to try again.")
    code = (query.get("code") or [""])[0]
    if not code:
        return "The address has no code=... part. Copy the whole address from the browser."
    client = _client(s, pending.account)
    if client is None:
        return "No OAuth client id is set any more; make a token with jobengine.gmail_auth."
    del PENDING[state]  # a code works once, whatever happens next
    send = post or (lambda url, data: http.post_form(url, data, s=s))
    try:
        answer = send(TOKEN_URL, {
            "code": code, "client_id": client[0], "client_secret": client[1],
            "redirect_uri": REDIRECT, "grant_type": "authorization_code",
            "code_verifier": pending.verifier})
    except http.HttpError as exc:
        return (f"Google refused the code ({exc}). Send /gmailtoken {pending.account} for a "
                "new link and paste the address within a minute or two.")
    token = _token_info(answer, client)
    missing = [scope for scope in SENDER_SCOPES if scope not in token["scopes"]]
    if not token["refresh_token"] or missing:
        return ("Google did not give every permission (" + (", ".join(missing) or
                "no refresh token") + "). Send /gmailtoken " + pending.account +
                " again and tick every box.")
    expected = expected_email(s, pending.account)
    email = who(token, str(answer.get("access_token") or ""))
    if email.casefold() != expected.casefold():
        return (f"You signed in as {email or 'an unknown account'}, but {pending.account} is "
                f"{expected}. Nothing was saved. Send /gmailtoken {pending.account} and "
                f"choose {expected}.")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"gmail-{pending.account}.json"
    path.write_text(json.dumps({"account": pending.account, "email": email,
                                "issued": now.date().isoformat(), "token": token}, indent=1)
                    + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:  # a Windows folder mounted into Docker may refuse; the file is local
        pass
    until = now.date() + timedelta(days=DEFAULT_TOKEN_DAYS)
    return (f"✅ Saved the new Gmail token for {pending.account} ({email}). Send /restart "
            "now to use it (about 10 seconds); no .env change is needed. While the Google "
            f"app is in Testing it works until {until:%d %b}: the bot reminds you the day "
            "before. Publishing the app stops this for good (Phase 9, P9-37).")


def saved(account: str, folder: Path = GMAIL_TOKEN_DIR) -> dict[str, Any] | None:
    try:
        data = json.loads((folder / f"gmail-{account}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _issued(data: Mapping[str, Any] | None) -> date | None:
    try:
        return date.fromisoformat(str((data or {}).get("issued")))
    except ValueError:
        return None


def token_left(s: Settings, config: Any, today: date,
               folder: Path = GMAIL_TOKEN_DIR) -> list[tuple[str, int]]:
    """(account, days left) for each token renewed in Telegram (local only)."""
    days = token_days(config)
    if s.app_env != "local" or days <= 0:
        return []
    out = []
    for account in ACCOUNTS:
        issued = _issued(saved(account, folder))
        if issued is not None:
            out.append((account, (issued + timedelta(days=days) - today).days))
    return out


def days_left(s: Settings, config: Any, today: date,
              folder: Path = GMAIL_TOKEN_DIR) -> list[str]:
    """For the daily report: how many days each token renewed in Telegram still works."""
    return [f"Gmail token {account}: "
            + ("expired: /gmailtoken " + account if left < 0 else f"{left} day(s) left")
            for account, left in token_left(s, config, today, folder)]


def token_days(config: Any) -> int:
    try:
        return int(str(config.get("gmail.token_days") or DEFAULT_TOKEN_DAYS).strip())
    except ValueError:
        return DEFAULT_TOKEN_DAYS


def status(s: Settings, config: Any, today: date, folder: Path = GMAIL_TOKEN_DIR,
           check: Callable[[str | None], str | None] = refresh_error) -> str:
    """/gmailtoken: each token, whether Google accepts it now and when it stops working."""
    if s.app_env != "local":
        return NOT_LOCAL
    days = token_days(config)
    lines = ["Gmail tokens:"]
    for account in ACCOUNTS:
        data = saved(account, folder)
        token = json.dumps(data["token"]) if data and data.get("token") else \
            current_token(s, account)
        problem = check(token)
        state = "OK" if problem is None else f"NOT working ({problem})"
        issued = _issued(data)
        if issued is None:
            where = "from .env (its date is unknown)"
        elif days > 0:
            where = (f"renewed in Telegram on {issued:%d %b}, works until "
                     f"{issued + timedelta(days=days):%d %b} while the Google app is in Testing")
        else:
            where = f"renewed in Telegram on {issued:%d %b}"
        lines.append(f"- {account} ({expected_email(s, account)}): {state}; {where}")
    lines += ["", "Renew one: /gmailtoken m02 or /gmailtoken mm (2 minutes). After the Google "
              "app is published (Phase 9, P9-37) tokens stop expiring: then set Config "
              "gmail.token_days = 0 to end the reminders."]
    return "\n".join(lines)


def reminder(s: Settings, state: Any, config: Any, today: date,
             folder: Path = GMAIL_TOKEN_DIR) -> list[str]:
    """Once a day from the day before a renewed token stops working: the renew command."""
    days = token_days(config)
    if s.app_env != "local" or days <= 0 or state is None:
        return []
    sent = dict(state.get(REMINDER_KEY) or {})
    out = []
    for account in ACCOUNTS:
        issued = _issued(saved(account, folder))
        if issued is None or sent.get(account) == today.isoformat():
            continue
        ends = issued + timedelta(days=days)
        if today < ends - timedelta(days=1):
            continue
        when = "tomorrow" if today < ends else "now (it has expired)"
        out.append(f"⏰ The Gmail token for {account} ({expected_email(s, account)}) stops "
                   f"working {when}: the Google app is in Testing. Renew it in 2 minutes: "
                   f"/gmailtoken {account}")
        sent[account] = today.isoformat()
    if out:
        state.set(REMINDER_KEY, sent)
    return out


_SERVER: dict[str, Any] = {}  # the page Google returns to, while a sign-in waits
PAGE = ("<!doctype html><html><head><meta charset=\"utf-8\"><title>Job Engine</title></head>"
        "<body style=\"font-family:sans-serif;max-width:40em;margin:3em auto\"><h1>{head}</h1>"
        "<p>{text}</p><p>You can close this tab and go back to Telegram.</p></body></html>")


def listen(s: Settings, notify: Callable[[str], None],
           now: Callable[[], datetime] = datetime.now, port: int = LISTEN_PORT,
           host: str = "0.0.0.0", folder: Path = GMAIL_TOKEN_DIR) -> int | None:
    """Answer the page Google returns to after Allow (REDIRECT): the code is exchanged at once,
    the page shows the result and `notify` sends it to the chat. It stops when no sign-in waits
    any more, or after PENDING_MINUTES. Inside Docker it needs -p 127.0.0.1:8765:8765. The port
    it listens on, or None when it cannot open it (then the address is pasted instead)."""
    if _SERVER.get("server") is not None:
        return int(_SERVER["server"].server_address[1])

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server's name)
            if "state=" not in self.path:  # the browser's favicon and other requests
                self.send_response(404)
                self.end_headers()
                return
            text = finish(s, f"{REDIRECT.rstrip('/')}{self.path}", now(), folder)
            ok = text.startswith("\u2705")
            body = PAGE.format(head="Done" if ok else "Not saved",
                               text=html.escape(text)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            notify(text)
            if not PENDING:
                threading.Thread(target=stop, daemon=True).start()

        def log_message(self, *args: Any) -> None:  # the address holds the one-time code
            return

    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError:
        return None
    _SERVER["server"] = server
    threading.Thread(target=server.serve_forever, daemon=True).start()
    timer = threading.Timer(PENDING_MINUTES * 60, stop)
    timer.daemon = True
    timer.start()
    _SERVER["timer"] = timer
    return int(server.server_address[1])


def stop() -> None:
    """Close the page Google returns to (after the sign-in, or when it timed out)."""
    server = _SERVER.pop("server", None)
    timer = _SERVER.pop("timer", None)
    if timer is not None:
        timer.cancel()
    if server is not None:
        server.shutdown()
        server.server_close()
