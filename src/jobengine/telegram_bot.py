"""Telegram bot: /start, /status, /help, /fetch, /pending, /jd, /done and /screen, answering
only the configured chat (messages and button presses alike).

Run with `python -m jobengine.telegram_bot`. It long-polls the Telegram Bot API using the
standard library only. Every outgoing message goes through safety.telegram_text so local and
dev messages carry their [LOCAL] or [DEV] prefix.

Run with `--fake` to use a dummy Telegram in the terminal instead: each line you type is a
message from your chat and the replies are printed. `tap <data>` presses a button, for
example `tap ap:pl-clean`. No token and no network are needed.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, TextIO

from jobengine.main import banner
from jobengine.safety import SafetyError, check_startup, telegram_text
from jobengine.screen import autopilot
from jobengine.screen.desk import Desk, Reply, fake_desk, real_desk
from jobengine.settings import ROOT_DIR, Settings, get_settings
from jobengine.sweep import fakes
from jobengine.sweep.runner import fake_deps, real_deps, run_sweep

log = logging.getLogger("jobengine.telegram_bot")

API_BASE = "https://api.telegram.org"
FAKE_CHAT_ID = "1"
FAKE_FILES_DIR = ROOT_DIR / "out" / "fake"
POLL_TIMEOUT = 30

HELP_TEXT = (
    "Job Engine commands:\n"
    "/start - check that the bot is alive\n"
    "/status - environment, safety settings, job and contact counts\n"
    "/fetch - search for new jobs, then screen them\n"
    "/autopilot - fetch, screen at half price, approve the best (10 a day), save resumes, "
    "find contacts and write Gmail drafts (never sent)\n"
    "/autopilot when - the morning schedule (Config schedule.autopilot) and the next run\n"
    "/pending - review screened jobs one at a time, one country at a time (Approve, Skip, "
    "Next); /pending poland, netherlands, ireland, remote or all\n"
    "/jd <url> - paste a job description (for LinkedIn jobs), then /done\n"
    "/jd - list jobs waiting for a description\n"
    "/screen - screen jobs that are not screened yet\n"
    "/screen <url or id> - screen one job again\n"
    "/screen batch - send them at half price, answers later\n"
    "/screen collect - save the answers of that batch\n"
    "/fetchreport - where the postings of the last /fetch went, per source, and the dropped "
    "jobs by reason; /fetchreport <word> for one reason or source\n"
    "/alertcheck - the job alert emails the next /fetch reads, and the jobs found in each\n"
    "/gaps - skills jobs ask for that you do not have yet (what to learn next)\n"
    "/contacts <url or id> - find contacts for a job (cache first, then Apollo, Hunter, Snov)\n"
    "/credits - show the contact providers' credit counters\n"
    "/outreach - this week's outreach budget (which approved jobs get contacts and cold mails)\n"
    "/fetchcontacts - contacts and Gmail drafts (resume attached) for the jobs you applied "
    "to today\n"
    "/drafts <url or id> - write Gmail drafts for a job's contacts again\n"
    "/applypack <url or id> - ready answers for a job's application form (visa, notice, salary)\n"
    "/mailmode - draft (only write Gmail drafts) or send (check every mail, then send it)\n"
    "/mailqueue - mails waiting for the recipient's morning (Tue to Thu, 8 to 10 their time)\n"
    "/today - run the daily check now (sent mails, replies, bounces, follow-ups)\n"
    "/followups - follow-up drafts waiting to be sent, and follow-ups due soon\n"
    "/stats - applied, replies, interviews and reply rates (30 days and all time)\n"
    "/sources - sweep sources, their secrets and the last sweep counts\n"
    "/health - Notion, Gmail tokens, Anthropic key, last runs\n"
    "/digest - the weekly digest now\n"
    "/update - monthly strategy research for your review (opens /fetch); /update new again\n"
    "/rules - V16 non-negotiables, thresholds, adopted tips and the /fetch gate\n"
    "/help - show this list"
)


MESSAGE_LIMIT = 4000  # Telegram allows 4096 characters per message


def split_text(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """`text` in parts of at most `limit` characters, cut at line ends where possible."""
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:  # a single very long line: cut it
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    parts.append(current)
    return [p for p in parts if p.strip()] or [text[:limit]]


def bot_commands() -> list[dict[str, str]]:
    """The / menu Telegram shows while typing: one entry per command in HELP_TEXT."""
    out: dict[str, str] = {}
    for line in HELP_TEXT.splitlines()[1:]:
        head, _, description = line.partition(" - ")
        name = head.split()[0].lstrip("/")
        out.setdefault(name, description[:256])
    return [{"command": name, "description": text} for name, text in out.items()]


# Shown at once on a button tap (Telegram's small popup) while the work runs.
TAP_TOASTS = {"ap": "Approving, building your resume...", "sk": "Skipping...",
              "nx": "Next job...", "pc": "Loading that country's jobs...",
              "fg": "Adding it and rebuilding...",
              "fc": "Adding it and rebuilding...", "ct": "Finding contacts...",
              "dr": "Writing Gmail drafts...", "fx": "Finding contacts and writing drafts...",
              "fr": "Listing those jobs..."}

DESK_COMMANDS = ("pending", "jd", "done", "screen", "gaps", "contacts", "credits", "outreach",
                 "drafts", "fetchcontacts", "alertcheck", "fetchreport", "today",
                 "followups", "stats", "sources", "health", "digest", "update", "rules")

# (method, payload, http_timeout) -> decoded JSON response
Transport = Callable[[str, dict[str, Any], float], dict[str, Any]]
# Runs the sweep and returns its summary text. The argument receives progress lines.
Progress = Callable[[str], None]
Fetcher = Callable[[Progress], str]
PROGRESS_EDIT_SECONDS = 4


class TelegramError(Exception):
    """A Bot API call failed. The message never contains the bot token."""


def http_transport(token: str) -> Transport:
    """Return a transport that POSTs JSON to the Bot API for the given token."""

    def call(method: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        if "_document" in payload:
            data, content_type = multipart(payload)
        else:
            data, content_type = json.dumps(payload).encode("utf-8"), "application/json"
        req = urllib.request.Request(
            f"{API_BASE}/bot{token}/{method}",
            data=data,
            headers={"Content-Type": content_type},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # The Bot API returns a JSON error body; keep its description, drop the URL.
            try:
                body = json.loads(exc.read().decode("utf-8"))
                detail = body.get("description", "")
            except (ValueError, OSError):
                detail = ""
            message = f"{method} failed: HTTP {exc.code} {detail}".strip()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            message = f"{method} failed: {type(exc).__name__} {reason}"
        except ValueError:
            message = f"{method} failed: response was not valid JSON"
        raise TelegramError(message.replace(token, "<token>"))

    return call


def multipart(payload: dict[str, Any]) -> tuple[bytes, str]:
    """multipart/form-data body for sendDocument: the file plus the other fields."""
    boundary = uuid.uuid4().hex
    name, data = payload["_document"]
    parts: list[bytes] = []
    for key, value in payload.items():
        if key == "_document":
            continue
        text = value if isinstance(value, str) else json.dumps(value)
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
                     f"{text}\r\n".encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="document"; '
                 f'filename="{name}"\r\nContent-Type: application/pdf\r\n\r\n'.encode()
                 + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def console_transport(
    chat_id: str, stdin: TextIO, stdout: TextIO, files_dir: Path = FAKE_FILES_DIR,
) -> Transport:
    """Dummy Telegram: stdin lines become messages from chat_id, replies go to stdout.

    `tap <data>` presses a button and `reply <message number> <text>` replies to a message
    (for resume corrections). Documents are saved to out/fake/. getUpdates raises EOFError
    when the input ends, which stops the bot.
    """
    next_id = 0  # update ids
    message_id = 0  # bot message numbers, shown with documents for `reply <n> <text>`
    sent: dict[int, str] = {}  # message number -> text or caption, for replies

    def call(method: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        nonlocal next_id, message_id
        if method in ("sendMessage", "sendDocument"):
            message_id += 1
            text = payload.get("text") or payload.get("caption") or ""
            sent[message_id] = text
            if method == "sendDocument":
                name, data = payload["_document"]
                files_dir.mkdir(parents=True, exist_ok=True)
                (files_dir / name).write_bytes(data)
                path = files_dir / name
                where = (path.relative_to(ROOT_DIR) if path.is_relative_to(ROOT_DIR)
                         else path).as_posix()
                print(f"bot [message {message_id}, document {where}]> {text}", file=stdout,
                      flush=True)
            elif "Ref JOB-" in text or "Ref ST-" in text:  # answer with `reply <n> <text>`
                print(f"bot [message {message_id}]> {text}", file=stdout, flush=True)
            else:
                print(f"bot> {text}", file=stdout, flush=True)
            rows = (payload.get("reply_markup") or {}).get("inline_keyboard") or []
            keys = [f"[{b['text']}: tap {b['callback_data']}]" for row in rows for b in row]
            if keys:
                print("     " + " ".join(keys), file=stdout, flush=True)
            return {"ok": True, "result": {"message_id": message_id}}
        if method in ("answerCallbackQuery", "sendChatAction", "setMyCommands"):
            return {"ok": True, "result": True}
        if method == "editMessageText":
            print(f"bot (updated)> {payload['text']}", file=stdout, flush=True)
            return {"ok": True, "result": {}}
        if method == "getUpdates":
            print("you> ", end="", file=stdout, flush=True)
            line = stdin.readline()
            if not line:
                print(file=stdout)
                raise EOFError
            if not stdin.isatty():
                # Piped input is not echoed by the terminal, so show it.
                print(line.rstrip("\n"), file=stdout, flush=True)
            next_id += 1
            text = line.rstrip("\n")
            if text.startswith("tap "):
                # A button press: "tap ap:<page id>".
                query = {"id": str(next_id), "from": {"id": chat_id},
                         "message": {"chat": {"id": chat_id}}, "data": text[4:].strip()}
                return {"ok": True, "result": [{"update_id": next_id, "callback_query": query}]}
            message: dict[str, Any] = {"chat": {"id": chat_id}, "text": text}
            reply = re.match(r"reply (\d+) (.+)", text, re.DOTALL)
            if reply:
                number = int(reply.group(1))
                message = {"chat": {"id": chat_id}, "text": reply.group(2),
                           "reply_to_message": {"message_id": number,
                                                "caption": sent.get(number, "")}}
            return {"ok": True, "result": [{"update_id": next_id, "message": message}]}
        return {"ok": False, "description": f"{method} is not supported by the fake"}

    return call


class TelegramClient:
    def __init__(self, transport: Transport):
        self._transport = transport

    def _call(self, method: str, payload: dict[str, Any], timeout: float = 15) -> Any:
        data = self._transport(method, payload, timeout)
        if not data.get("ok"):
            raise TelegramError(f"{method} failed: {data.get('description', 'unknown error')}")
        return data.get("result")

    def get_updates(self, offset: int | None, timeout: int = POLL_TIMEOUT) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout, "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        return self._call("getUpdates", payload, timeout=timeout + 10) or []

    def send_message(
        self, chat_id: str, text: str, buttons: list[tuple[str, str]] | None = None
    ) -> int | None:
        """Send a message and return its message_id (None if Telegram did not say).
        `buttons` are (label, callback data) pairs shown in one row under the message. A text
        longer than Telegram allows goes as several messages (the buttons under the last);
        before 1 Oct long answers such as /alertcheck were cut off."""
        parts = split_text(text)
        result: Any = None
        for i, part in enumerate(parts):
            payload: dict[str, Any] = {"chat_id": chat_id, "text": part}
            if buttons and i == len(parts) - 1:
                payload["reply_markup"] = {"inline_keyboard": [
                    [{"text": label, "callback_data": data} for label, data in buttons]
                ]}
            result = self._call("sendMessage", payload)
        return (result or {}).get("message_id") if isinstance(result, dict) else None

    def send_document(
        self, chat_id: str, name: str, data: bytes, caption: str,
        buttons: list[tuple[str, str]] | None = None,
    ) -> int | None:
        payload: dict[str, Any] = {"chat_id": chat_id, "caption": caption[:1024],
                                   "_document": (name, data)}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [
                [{"text": label, "callback_data": value} for label, value in buttons]
            ]}
        result = self._call("sendDocument", payload, timeout=60)
        return (result or {}).get("message_id") if isinstance(result, dict) else None

    def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        self._call("answerCallbackQuery", payload)

    def typing(self, chat_id: str) -> None:
        """Show "typing..." in the chat (Telegram clears it after about 5 seconds)."""
        self._call("sendChatAction", {"chat_id": chat_id, "action": "typing"})

    def set_commands(self, commands: list[dict[str, str]]) -> None:
        self._call("setMyCommands", {"commands": commands})

    def edit_message(self, chat_id: str, message_id: int, text: str) -> None:
        self._call(
            "editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text}
        )


class ProgressMessage:
    """One Telegram message that shows the sweep's current step in plain words.

    It is edited at most every PROGRESS_EDIT_SECONDS so the chat is not flooded and
    Telegram rate limits are respected. A failed edit never stops the sweep.
    """

    def __init__(
        self,
        client: TelegramClient,
        chat_id: str,
        s: Settings,
        clock: Callable[[], float] = time.monotonic,
        name: str = "Job search",
        first: str = "Searching for jobs",
    ):
        self.client = client
        self.chat_id = chat_id
        self.s = s
        self.clock = clock
        self.name = name
        self.status = "Starting..."
        self.started = time.strftime("%H:%M")
        self.started_at = clock()
        self.message_id = client.send_message(
            chat_id,
            telegram_text(f"\U0001F50E {first} (started {self.started})...", s),
        )
        self.last_edit = clock()

    def _edit(self, text: str) -> None:
        if self.message_id is None:
            return
        try:
            self.client.edit_message(self.chat_id, self.message_id, telegram_text(text, self.s))
        except TelegramError as exc:
            log.warning("progress update failed: %s", exc)
        self.last_edit = self.clock()

    def update(self, status: str) -> None:
        self.status = status
        if self.clock() - self.last_edit >= PROGRESS_EDIT_SECONDS:
            self._edit(f"\U0001F50E {self.name} running (started {self.started})\n{status}")

    def elapsed(self) -> str:
        minutes = int((self.clock() - self.started_at) // 60)
        return "under a minute" if minutes < 1 else f"{minutes} min"

    def finish(self, ok: bool) -> None:
        if ok:
            self._edit(f"\u2705 {self.name} done in {self.elapsed()}. Summary below.")
        else:
            self._edit(f"\u274C {self.name} stopped after {self.elapsed()}: {self.status}")


def parse_command(text: str) -> str | None:
    """Return the command name for "/status" or "/status@MadeshDevBot args", else None."""
    if not text.startswith("/"):
        return None
    return text.split()[0][1:].split("@")[0].lower() or None


def status_text(s: Settings) -> str:
    model = s.force_model or "from Notion Config"
    return f"{banner(s)}\nmodel={model}"


def reply_for(text: str, s: Settings, fetch: Fetcher | None = None) -> str:
    """Plain reply text (without the env prefix) for an incoming message."""
    command = parse_command(text)
    if command == "fetch":
        if fetch is None:
            return "/fetch is not available in this bot."
        try:
            return fetch(lambda line: None)
        except Exception as exc:  # report any sweep failure instead of stopping the bot
            log.exception("/fetch failed")
            return f"/fetch failed: {exc}"
    if command == "start":
        return f"Job Engine bot is running (env={s.app_env}).\n\n{HELP_TEXT}"
    if command == "status":
        return status_text(s)
    if command == "help":
        return HELP_TEXT
    if command is None:
        return "I only understand commands for now. Send /help to see them."
    return f"Unknown command /{command}. Send /help to see the commands."


def handle_update(
    update: dict[str, Any], s: Settings, fetch: Fetcher | None = None
) -> tuple[str, str] | None:
    """Return (chat_id, text) to send for an update, or None to ignore it.

    Messages from any chat other than TELEGRAM_CHAT_ID are ignored.
    """
    message = update.get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id", ""))
    text = message.get("text")
    if not chat_id or text is None:
        return None
    if chat_id != str(s.telegram_chat_id):
        log.warning("Ignoring message from unknown chat %s", chat_id)
        return None
    return chat_id, telegram_text(reply_for(text, s, fetch), s)


# Commands that run for minutes or cost money. Telegram delivers a message again when the bot
# stopped before confirming it (Ctrl+C in the middle of /fetch), so one sent before this bot
# started is not run again by itself.
STALE_COMMANDS = ("fetch", "screen", "update", "autopilot")


def _stale_command(update: dict[str, Any], started: float | None) -> str | None:
    message = update.get("message") or {}
    command = parse_command(message.get("text") or "")
    if started is None or command not in STALE_COMMANDS:
        return None
    sent = message.get("date")  # Telegram always sets it; the terminal harness does not
    return command if sent is not None and int(sent) < int(started) else None


def poll_once(
    client: TelegramClient,
    s: Settings,
    offset: int | None,
    fetch: Fetcher | None = None,
    desk: Desk | None = None,
    started: float | None = None,
) -> int | None:
    """Fetch one batch of updates, answer them, and return the next offset. With `started`
    (the bot's start time), /fetch, /screen and /update sent before it are not run."""
    for update in client.get_updates(offset):
        offset = update["update_id"] + 1
        command = _stale_command(update, started)
        chat = str((update.get("message") or {}).get("chat", {}).get("id", ""))
        if command and chat == str(s.telegram_chat_id):
            log.info("not running /%s sent before the bot started", command)
            client.send_message(chat, telegram_text(
                f"Not running /{command}: it was sent before the bot started (maybe a run you "
                f"stopped). Send /{command} again to run it.", s))
            continue
        handle_one(client, s, update, fetch, desk)
    return offset


def handle_one(
    client: TelegramClient,
    s: Settings,
    update: dict[str, Any],
    fetch: Fetcher | None = None,
    desk: Desk | None = None,
) -> None:
    """Answer one update. Long polling and the webhook (jobengine.web) both call this, so the
    chat ID lock and every command behave the same in both modes."""
    _bind_notify(client, s, desk)
    if "callback_query" in update:
        handle_callback(client, s, update["callback_query"], desk)
        return
    if fetch is not None and _is_fetch(update, s):
        run_fetch(client, s, str(s.telegram_chat_id), fetch)
        return
    chat = str(s.telegram_chat_id)
    message = update.get("message") or {}
    if str((message.get("chat") or {}).get("id", "")) != chat:
        out = handle_update(update, s, fetch)  # logs and ignores other chats
        if out is not None:
            client.send_message(*out)
        return
    text = message.get("text") or ""
    if desk is not None and parse_command(text) == "autopilot":
        if text.split()[1:2] in (["when"], ["schedule"]):
            send_replies(client, s, chat, _guarded("/autopilot when", desk.autopilot_when))
        else:
            run_autopilot(client, s, chat, desk, fetch)
        return
    if desk is not None and parse_command(text) == "screen" and len(text.split()) == 1:
        run_screen(client, s, chat, desk)
        return
    with Typing(client, chat):
        replies = desk_replies(update, s, desk)
        out = None if replies is not None else handle_update(update, s, fetch)
    if replies is not None:
        send_replies(client, s, chat, replies)
    elif out is not None:
        client.send_message(*out)


def send_replies(client: TelegramClient, s: Settings, chat_id: str, replies: list[Reply]) -> None:
    for reply in replies:
        if reply.document:
            name, data = reply.document
            client.send_document(chat_id, name, data, telegram_text(reply.text, s),
                                 reply.buttons or None)
        else:
            client.send_message(chat_id, telegram_text(reply.text, s), reply.buttons or None)


def _bind_notify(client: TelegramClient, s: Settings, desk: Desk | None) -> None:
    if desk is not None:
        chat = str(s.telegram_chat_id)
        desk.notify = lambda reply: send_replies(client, s, chat, [reply])


def _guarded(name: str, action: Callable[[], list[Reply]]) -> list[Reply]:
    try:
        return action()
    except Exception as exc:  # report the failure instead of stopping the bot
        log.exception("%s failed", name)
        return [Reply(f"{name} failed: {exc}")]


def desk_replies(update: dict[str, Any], s: Settings, desk: Desk | None) -> list[Reply] | None:
    """Replies for /pending, /jd, /done, /screen and pasted text, or None when the message
    is not for the desk (other commands, other chats, or plain text without a /jd paste)."""
    if desk is None:
        return None
    message = update.get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id", ""))
    text = message.get("text")
    if text is None or chat_id != str(s.telegram_chat_id):
        return None
    command = parse_command(text)
    args = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
    replied = message.get("reply_to_message") or {}
    if command is None and replied:
        ref_text = replied.get("caption") or replied.get("text") or ""
        replies = _guarded("Correction", lambda: desk.correction(ref_text, text) or [])
        if replies:
            return replies
        replies = _guarded("Domain", lambda: desk.domain_answer(ref_text, text) or [])
        if replies:
            return replies
        replies = _guarded("Note", lambda: desk.strategy_note(ref_text, text) or [])
        if replies:
            return replies
    if command == "pending":
        return _guarded("/pending", lambda: desk.pending_command(args))
    if command == "gaps":
        return _guarded("/gaps", desk.gaps_command)
    if command == "contacts":
        return _guarded("/contacts", lambda: desk.contacts_command(args))
    if command == "credits":
        return _guarded("/credits", desk.credits)
    if command == "outreach":
        return _guarded("/outreach", desk.outreach_command)
    if command == "fetchreport":
        return _guarded("/fetchreport", lambda: desk.fetchreport_command(args))
    if command == "alertcheck":
        return _guarded("/alertcheck", desk.alertcheck_command)
    if command == "fetchcontacts":
        return _guarded("/fetchcontacts", desk.fetch_contacts_command)
    if command == "drafts":
        return _guarded("/drafts", lambda: desk.drafts_command(args))
    if command == "applypack":
        return _guarded("/applypack", lambda: desk.applypack_command(args))
    if command == "mailqueue":
        return _guarded("/mailqueue", desk.mailqueue_command)
    if command == "mailmode":
        return _guarded("/mailmode", lambda: desk.mailmode_command(args))
    if command == "status" and desk.track is not None:
        return _guarded("/status", lambda: [Reply(f"{status_text(s)}\n{desk.status_lines()}")])
    tracking = {"today": desk.today_command, "followups": desk.followups_command,
                "stats": desk.stats_command, "sources": desk.sources_command,
                "health": desk.health_command, "digest": desk.digest_command}
    if command in tracking:
        return _guarded(f"/{command}", tracking[command])
    if command == "update":
        return _guarded("/update", lambda: desk.update_command(args))
    if command == "rules":
        return _guarded("/rules", desk.rules_command)
    if command == "jd":
        return _guarded("/jd", lambda: desk.jd(args))
    if command == "done":
        return _guarded("/done", desk.done)
    if command == "screen":
        return _guarded("/screen", lambda: desk.screen_replies(args))
    if command is None:
        return _guarded("Saving the pasted text", lambda: desk.text(text) or []) or None
    return None


def handle_callback(
    client: TelegramClient, s: Settings, query: dict[str, Any], desk: Desk | None
) -> None:
    """A button press. Only presses from the configured chat are acted on."""
    sender = str((query.get("from") or {}).get("id", ""))
    if sender != str(s.telegram_chat_id):
        log.warning("Ignoring button press from unknown user %s", sender)
        return
    data = str(query.get("data") or "")
    try:
        # Answer at once so the button stops spinning, with a short note of what is happening.
        client.answer_callback(str(query.get("id", "")), TAP_TOASTS.get(data.partition(":")[0]))
    except TelegramError as exc:
        log.warning("answerCallbackQuery failed: %s", exc)
    with Typing(client, sender):
        if desk is None:
            replies = [Reply("Buttons are not available in this bot.")]
        else:
            replies = _guarded("Button", lambda: desk.tap(data))
    send_replies(client, s, sender, replies)


def _is_fetch(update: dict[str, Any], s: Settings) -> bool:
    message = update.get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id", ""))
    command = parse_command(message.get("text") or "")
    return chat_id == str(s.telegram_chat_id) and command == "fetch"


def run_fetch(
    client: TelegramClient,
    s: Settings,
    chat_id: str,
    fetch: Fetcher,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """/fetch: reply at once, keep one progress message updated, then send the summary."""
    progress = ProgressMessage(client, chat_id, s, clock=clock)
    try:
        summary = fetch(progress.update)
    except Exception as exc:  # report any sweep failure instead of stopping the bot
        log.exception("/fetch failed")
        progress.finish(ok=False)
        client.send_message(chat_id, telegram_text(f"/fetch failed: {exc}", s))
        return
    progress.finish(ok=True)
    client.send_message(chat_id, telegram_text(summary, s))


def run_screen(
    client: TelegramClient, s: Settings, chat_id: str, desk: Desk,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """/screen without a job: it can take minutes, so show one progress message like /fetch."""
    progress = ProgressMessage(client, chat_id, s, clock=clock, name="Screening",
                               first="Screening new jobs")
    try:
        replies = desk.screen_replies("", progress.update)
    except Exception as exc:  # report the failure instead of stopping the bot
        log.exception("/screen failed")
        progress.finish(ok=False)
        client.send_message(chat_id, telegram_text(f"/screen failed: {exc}", s))
        return
    progress.finish(ok=True)
    send_replies(client, s, chat_id, replies)


NEXT_STEP = "\n\n\U0001F449 Next:"  # the hint /fetch ends with


def autopilot_fetch(fetch: Fetcher | None) -> Fetcher | None:
    """The /fetch sweep without its "Next: /screen" hint: autopilot does the next step."""
    if fetch is None:
        return None

    def fetch_only(update: Progress) -> str:
        return fetch(update).split(NEXT_STEP)[0]

    return fetch_only


def run_autopilot(
    client: TelegramClient, s: Settings, chat_id: str, desk: Desk, fetch: Fetcher | None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """/autopilot: one progress message, then the summary and the resumes."""
    progress = ProgressMessage(client, chat_id, s, clock=clock, name="Autopilot",
                               first="Autopilot started")
    try:
        replies = desk.autopilot(autopilot_fetch(fetch), progress.update)
    except Exception as exc:  # report the failure instead of stopping the bot
        log.exception("/autopilot failed")
        progress.finish(ok=False)
        client.send_message(chat_id, telegram_text(f"/autopilot failed: {exc}", s))
        return
    progress.finish(ok=True)
    send_replies(client, s, chat_id, replies)


def autopilot_check(client: TelegramClient, s: Settings, desk: Desk | None,
                    fetch: Fetcher | None = None) -> None:
    """Long polling: carry on with an /autopilot whose half-price batch has answered, else
    start the morning's scheduled run when it is due (Config schedule.autopilot); then send
    one queued mail whose recipient's morning has come (mail/timing.py), and ping new
    replies (track/ping.py, every tracking.reply_check_minutes)."""
    if desk is None:
        return
    _bind_notify(client, s, desk)
    replies = _guarded("Autopilot",
                       lambda: desk.autopilot_scheduled(autopilot_fetch(fetch)) or [])
    replies += _guarded("Mail queue", desk.mail_queue_tick)
    replies += _guarded("Reply check", desk.reply_ping_tick)
    if replies:
        send_replies(client, s, str(s.telegram_chat_id), replies)


class Typing:
    """Shows "typing..." until the reply is ready: Telegram clears it after about 5 seconds,
    so it is sent again every TYPING_EVERY seconds from a small background thread."""

    def __init__(self, client: TelegramClient, chat_id: str, every: float = 4.0):
        self.client, self.chat_id, self.every = client, chat_id, every
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _send(self) -> None:
        try:
            self.client.typing(self.chat_id)
        except TelegramError as exc:  # only a hint: never stop the reply over it
            log.warning("sendChatAction failed: %s", exc)

    def _loop(self) -> None:
        while not self._stop.wait(self.every):
            self._send()

    def __enter__(self) -> Typing:
        self._send()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)


def check_bot_settings(s: Settings, fake: bool = False) -> None:
    check_startup(s)
    if fake:
        return
    missing = [
        name
        for name, value in (
            ("TELEGRAM_BOT_TOKEN", s.telegram_bot_token),
            ("TELEGRAM_CHAT_ID", s.telegram_chat_id),
        )
        if not value
    ]
    if missing:
        raise SafetyError(f"missing {', '.join(missing)}")


def run(
    s: Settings,
    client: TelegramClient,
    max_polls: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    fetch: Fetcher | None = None,
    desk: Desk | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Register the / menu, announce startup, then poll forever (or max_polls times).
    Between polls, every few minutes: a waiting /autopilot batch is checked, and the morning's
    scheduled /autopilot starts when it is due."""
    try:
        client.set_commands(bot_commands())
    except TelegramError as exc:  # the menu is a convenience: never stop the bot over it
        log.warning("could not set the / menu: %s", exc)
    client.send_message(str(s.telegram_chat_id), telegram_text("Job Engine bot started.", s))
    offset: int | None = None
    started = time.time()
    polls = 0
    next_check = clock()
    while max_polls is None or polls < max_polls:
        polls += 1
        try:
            offset = poll_once(client, s, offset, fetch, desk, started=started)
            if desk is not None and clock() >= next_check:
                next_check = clock() + autopilot.check_seconds(desk)
                autopilot_check(client, s, desk, fetch)
        except TelegramError as exc:
            log.error("%s, retrying in 5s", exc)
            sleep(5)


def make_fetcher(s: Settings, fake: bool, desk: Desk | None = None) -> Fetcher:
    """/fetch runs the sweep (fixtures and an in-memory Notion with --fake, real otherwise).
    It never screens by itself: the summary ends with the next step, /screen."""

    def fetch(progress: Progress) -> str:
        if fake:
            repo = desk.repo if desk is not None else None
            state = desk.state if desk is not None else None
            summary = run_sweep(s, fake_deps(s, repo=repo), fakes.FAKE_TODAY, progress=progress,
                                state=state)
        else:
            state = desk.state if desk is not None else None
            summary = run_sweep(s, real_deps(s), date.today(), progress=progress, state=state)
        text = summary.friendly_text()
        if summary.blocked or not summary.new:
            return text
        return (f"{text}\n\n\U0001F449 Next: /screen checks the new jobs against your profile "
                "(uses the AI, at most 30 jobs a day).")

    return fetch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobengine.telegram_bot")
    parser.add_argument(
        "--fake", action="store_true", help="use a dummy Telegram in this terminal (no network)"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        s = get_settings()
        if s.bot_mode == "webhook" and not args.fake:
            raise SafetyError("BOT_MODE=webhook: the bot runs inside python -m jobengine.web "
                              "(Telegram calls it); long polling is for local runs only")
        check_bot_settings(s, fake=args.fake)
    except (SafetyError, ValueError) as exc:
        print(f"Job Engine bot startup failed: {exc}", file=sys.stderr)
        return 1
    print(banner(s))

    if args.fake:
        s = s.model_copy(update={"telegram_chat_id": s.telegram_chat_id or FAKE_CHAT_ID})
        print("Fake Telegram: type a message and press Enter. Ctrl+D or Ctrl+C to stop.")
        client = TelegramClient(console_transport(str(s.telegram_chat_id), sys.stdin, sys.stdout))
    else:
        client = TelegramClient(http_transport(s.telegram_bot_token))

    desk = fake_desk(s, fakes.FAKE_TODAY) if args.fake else real_desk(s)
    try:
        run(s, client, fetch=make_fetcher(s, args.fake, desk), desk=desk)
    except TelegramError as exc:
        print(f"Job Engine bot stopped: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("Job Engine bot stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
