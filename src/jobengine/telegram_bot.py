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
import faulthandler
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any, TextIO

from jobengine import gmail_renew, next_steps
from jobengine.jobctl import CONTROL, Stopped, end_text
from jobengine.main import banner
from jobengine.resume import builder as resume_builder
from jobengine.safety import SafetyError, check_startup, telegram_text
from jobengine.screen import autopilot
from jobengine.screen.desk import Desk, Reply, fake_desk, real_desk
from jobengine.settings import ROOT_DIR, Settings, get_settings
from jobengine.sweep import fakes
from jobengine.sweep.runner import fake_deps, real_deps, run_sweep

log = logging.getLogger("jobengine.telegram_bot")

API_BASE = "https://api.telegram.org"
FILE_METHOD = "_file"  # transport call for a file download (not a Bot API method)
MAX_DOWNLOAD_BYTES = 20_000_000
FAKE_CHAT_ID = "1"
FAKE_FILES_DIR = ROOT_DIR / "out" / "fake"
POLL_TIMEOUT = 30

HELP_TEXT = (
    "Job Engine commands:\n"
    "/start - check that the bot is alive\n"
    "/next - your day in order: what is done and what to do now\n"
    "/status - environment, safety settings, job and contact counts\n"
    "/fetch - search for new jobs, then screen them\n"
    "/autopilot - fetch, screen at half price, approve the best (10 a day), save resumes, "
    "find contacts and write Gmail drafts (never sent)\n"
    "/autopilot when - the schedule (07:00 and 19:30, Config schedule.autopilot), next run\n"
    "/pending - review screened jobs one at a time, one country at a time (Approve, Skip, "
    "Next); /pending poland, netherlands, ireland, remote or all\n"
    "/add <job link> - a job on a company site (where LinkedIn or another app sends you): "
    "saved, screened, resume built, company added to Target Companies\n"
    "/jd <url> - paste a job description (LinkedIn, Pracuj.pl and other blocked sites), "
    "then /done\n"
    "/jd - list jobs waiting for a description\n"
    "/linkedin - LinkedIn jobs one at a time: paste each description, tap Done (screened)\n"
    "/screen - screen jobs that are not screened yet\n"
    "/screen <url or id> - screen one job again\n"
    "/screen batch - send them at half price, answers later\n"
    "/screen collect - save the answers of that batch\n"
    "/fetchreport - where the postings of the last /fetch went, per source, and the dropped "
    "jobs by reason; /fetchreport <word> for one reason or source\n"
    "/alertcheck - the job alert emails the next /fetch reads, and the jobs found in each\n"
    "/gaps - skills jobs ask for that you do not have yet, and which certificate covers most\n"
    "/keywords - the tools your best jobs name most, and which to add to your LinkedIn profile\n"
    "/contacts <url or id> - contacts for a job (saved ones, Hunter, Prospeo, AI search) and "
    "your LinkedIn connections there\n"
    "/credits - contact credits left, asked from each provider\n"
    "/agencies - recruitment agencies by country; /agencies <name> gives the intro mail\n"
    "/outreach - this week's outreach budget (which approved jobs get contacts and cold mails)\n"
    "/fetchcontacts - contacts and Gmail drafts (resume attached) for the jobs you applied "
    "to today\n"
    "/drafts <url or id> - write Gmail drafts for a job's contacts again\n"
    "/applypack <url or id> - ready answers for a job's application form (visa, notice, salary)\n"
    "/answer <question> = <answer> - save a form answer; it is in every Apply pack after\n"
    "/answers - your saved form answers (/answer delete N removes one)\n"
    "/autofill - the browser bookmark that fills application forms from the Apply pack code\n"
    "/dryrun <url or id> - fill the job's form in a browser, never submit (screenshot)\n"
    "/prep <url or id> - interview prep: likely questions, your gaps, questions to ask them\n"
    "/thanks <url or id> <name> - the thank-you mail to send after an interview\n"
    "/mailmode - draft (only write Gmail drafts) or send (check every mail, then send it)\n"
    "/mailqueue - mails waiting for the recipient's morning (Tue to Thu, 8 to 10 their time)\n"
    "/today - run the daily check now (sent mails, replies, bounces, follow-ups)\n"
    "/followups - follow-up drafts waiting to be sent, and follow-ups due soon\n"
    "/stats - applied, replies, interviews and reply rates (30 days and all time)\n"
    "/sources - sweep sources, their secrets and the last sweep counts\n"
    "/health - Notion, Gmail tokens, Anthropic key, last runs\n"
    "/gmailtoken - your Gmail tokens; /gmailtoken m02 or mm renews one in 2 minutes\n"
    "/digest - the weekly digest now\n"
    "/spotcheck - 2 of the week's resumes to look at (notes: what you marked wrong)\n"
    "/report - today's daily report now (it also comes by itself every evening, and by email)\n"
    "/update - monthly strategy research for your review (opens /fetch); /update new again\n"
    "/connections - your LinkedIn connections table; send the LinkedIn export zip to update it\n"
    "/rules - V16 non-negotiables, thresholds, adopted tips and the /fetch gate\n"
    "/end - stop the running /fetch, /screen or /autopilot at its next step\n"
    "/restart - restart the bot (reloads its code and Notion settings)\n"
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
TAP_TOASTS = {"ap": "Checking the job link, then building your resume...",
              "sk": "Skipping...", "ab": "Approving, building your resume...",
              "xe": "Marking it expired...", "dm": "Using that domain...",
              "ln": "Writing the LinkedIn notes...",
              "nx": "Next job...", "pc": "Loading that country's jobs...",
              "fg": "Adding it and rebuilding...",
              "fc": "Adding it and rebuilding...", "ct": "Finding contacts...",
              "dr": "Writing Gmail drafts...", "fx": "Finding contacts and writing drafts...",
              "fr": "Listing those jobs...", "li": "Working on the LinkedIn job...",
              "ra": "Saving the resume...", "rb": "Building the resume again...",
              "cl": "Building the resume again with Claude...",
              "rq": "Send what to change...", "ia": "Marking as applied...",
              "na": "Not applying...", "nr": "Saving the reason...",
              "gs": "Adding it to your skills...", "sc": "Screening...",
              "gt": "Updated", "gr": "Adding them and rebuilding...", "gp": "Listing them...",
              "tu": "Undoing...", "en": "Getting your first job...",
              "nj": "Getting the next job...", "sz": "Snoozing...", "dd": "Well done...",
              "fo": "Saving your approval...", "fw": "Listing the answers..."}
# Every other button: a tap always gets a sign that the bot is working (5 Oct test notes).
TAP_DEFAULT = "Working on it..."

DESK_COMMANDS = ("next", "pending", "add", "report", "jd", "done", "screen", "gaps", "contacts",
                 "credits",
                 "outreach", "drafts", "answer", "answers", "autofill", "dryrun", "fetchcontacts",
                 "alertcheck", "fetchreport", "linkedin", "today", "followups", "stats",
                 "sources", "health", "digest", "spotcheck", "update", "rules")

# (method, payload, http_timeout) -> decoded JSON response
Transport = Callable[[str, dict[str, Any], float], dict[str, Any]]
# Runs the sweep and returns its summary text. The argument receives progress lines.
Progress = Callable[[str], None]
Fetcher = Callable[[Progress], str]
PROGRESS_EDIT_SECONDS = 4


class TelegramError(Exception):
    """A Bot API call failed. The message never contains the bot token."""


class TelegramNetworkError(TelegramError):
    """The call did not reach Telegram or its answer was lost (SSL EOF, timeout, reset)."""


def http_transport(token: str) -> Transport:
    """Return a transport that POSTs JSON to the Bot API for the given token."""

    def call(method: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        if method == FILE_METHOD:
            return _download(token, payload["file_path"], timeout)
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
            raise TelegramNetworkError(
                f"{method} failed: {type(exc).__name__} {reason}".replace(token, "<token>")
            ) from None
        except ValueError:
            message = f"{method} failed: response was not valid JSON"
        raise TelegramError(message.replace(token, "<token>"))

    return call


def _download(token: str, file_path: str, timeout: float) -> dict[str, Any]:
    """A file you sent the bot (after getFile). The token is in the address, so it never
    reaches a log or an error message."""
    try:
        with urllib.request.urlopen(f"{API_BASE}/file/bot{token}/{file_path}",
                                    timeout=timeout) as resp:
            return {"ok": True, "result": resp.read(MAX_DOWNLOAD_BYTES + 1)}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise TelegramNetworkError(
            f"file download failed: {type(exc).__name__} {reason}".replace(token, "<token>")
        ) from None


def keyboard(buttons: list[tuple[str, str]], column: bool = False) -> dict[str, Any]:
    """Inline buttons: one row, or one button per row (a list to pick from)."""
    keys = [{"text": label, "callback_data": data} for label, data in buttons]
    return {"inline_keyboard": [[k] for k in keys] if column else [keys]}


def register_menu(client: TelegramClient, s: Settings) -> None:
    """The / menu Telegram suggests while you type. Set for every chat and for your chat:
    Telegram shows a chat's own menu before the default one, so an older menu set for your
    chat hid new commands (23 Oct: /connections was not suggested). Read back and logged."""
    commands = bot_commands()
    scope = {"type": "chat", "chat_id": str(s.telegram_chat_id)}
    try:
        client.set_commands(commands)
        client.set_commands(commands, scope)
        shown = {c.get("command") for c in client.get_commands(scope)}
    except TelegramError as exc:  # the menu is a convenience: never stop the bot over it
        log.warning("could not set the / menu: %s", exc)
        return
    missing = [c["command"] for c in commands if c["command"] not in shown]
    if missing:
        log.warning("/ menu: Telegram shows %d of %d commands; missing: %s",
                    len(commands) - len(missing), len(commands), ", ".join(missing))
    else:
        log.info("/ menu: %d commands set (every chat and yours)", len(commands))


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

    `tap <data>` presses a button, `reply <message number> <text>` replies to a message
    (for resume corrections) and `file <path>` sends a file from your disk (the LinkedIn
    export). Documents are saved to out/fake/. getUpdates raises EOFError when the input
    ends, which stops the bot.
    """
    next_id = 0  # update ids
    message_id = 0  # bot message numbers, shown with documents for `reply <n> <text>`
    sent: dict[int, str] = {}  # message number -> text or caption, for replies
    menus: dict[str, Any] = {}  # the / menu per scope (setMyCommands, getMyCommands)

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
            elif any(ref in text for ref in ("Ref JOB-", "Ref ST-", "Ref SPOT-", "Ref DRY-")):
                # answer with `reply <n> <text>`
                print(f"bot [message {message_id}]> {text}", file=stdout, flush=True)
            else:
                print(f"bot> {text}", file=stdout, flush=True)
            rows = (payload.get("reply_markup") or {}).get("inline_keyboard") or []
            keys = [f"[{b['text']}: tap {b['callback_data']}]" for row in rows for b in row]
            if keys:
                print("     " + " ".join(keys), file=stdout, flush=True)
            return {"ok": True, "result": {"message_id": message_id}}
        if method == "setMyCommands":
            menus[str(payload.get("scope"))] = payload["commands"]
            return {"ok": True, "result": True}
        if method == "getMyCommands":
            return {"ok": True, "result": menus.get(str(payload.get("scope")), [])}
        if method in ("answerCallbackQuery", "sendChatAction", "deleteMessage"):
            return {"ok": True, "result": True}
        if method == "editMessageText":
            print(f"bot (updated)> {payload['text']}", file=stdout, flush=True)
            rows = (payload.get("reply_markup") or {}).get("inline_keyboard") or []
            keys = [f"[{b['text']}: tap {b['callback_data']}]" for row in rows for b in row]
            if keys:
                print("     " + " ".join(keys), file=stdout, flush=True)
            return {"ok": True, "result": {}}
        if method == "getFile":
            return {"ok": True, "result": {"file_path": payload["file_id"]}}
        if method == FILE_METHOD:
            return {"ok": True, "result": Path(payload["file_path"]).read_bytes()}
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
            if text.startswith("file "):
                path = text[5:].strip().strip('"')
                document = {"file_id": path, "file_name": Path(path).name}
                return {"ok": True, "result": [{"update_id": next_id, "message": {
                    "chat": {"id": chat_id}, "document": document}}]}
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


SEND_RETRY_SECONDS = 2  # one more try after a lost connection (16 Oct: an SSL EOF lost a reply)


class TelegramClient:
    def __init__(self, transport: Transport, sleep: Callable[[float], None] = time.sleep):
        self._transport = transport
        self._sleep = sleep

    def _call(self, method: str, payload: dict[str, Any], timeout: float = 15) -> Any:
        try:
            data = self._transport(method, payload, timeout)
        except TelegramNetworkError as exc:
            if method == "getUpdates":  # the polling loop already waits and asks again
                raise
            log.warning("%s, retrying in %ss", exc, SEND_RETRY_SECONDS)
            self._sleep(SEND_RETRY_SECONDS)
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
        self, chat_id: str, text: str, buttons: list[tuple[str, str]] | None = None,
        column: bool = False,
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
                payload["reply_markup"] = keyboard(buttons, column)
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

    def download(self, file_id: str) -> bytes:
        """The bytes of a file sent to the bot (Telegram lets a bot download up to 20 MB)."""
        info = self._call("getFile", {"file_id": file_id}) or {}
        if not info.get("file_path"):
            raise TelegramError("getFile failed: Telegram gave no file path (over 20 MB?)")
        data = self._call(FILE_METHOD, {"file_path": info["file_path"]}, timeout=60)
        if not isinstance(data, bytes):
            raise TelegramError("file download failed: no file came back")
        return data

    def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        self._call("answerCallbackQuery", payload)

    def delete_message(self, chat_id: str, message_id: int) -> None:
        """Delete a message (a bot may delete incoming messages in a private chat)."""
        self._call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    def typing(self, chat_id: str) -> None:
        """Show "typing..." in the chat (Telegram clears it after about 5 seconds)."""
        self._call("sendChatAction", {"chat_id": chat_id, "action": "typing"})

    def set_commands(self, commands: list[dict[str, str]],
                     scope: dict[str, str] | None = None) -> None:
        payload: dict[str, Any] = {"commands": commands}
        if scope:
            payload["scope"] = scope
        self._call("setMyCommands", payload)

    def get_commands(self, scope: dict[str, str] | None = None) -> list[dict[str, str]]:
        result = self._call("getMyCommands", {"scope": scope} if scope else {})
        return [c for c in result or [] if isinstance(c, dict)] if isinstance(result, list) \
            else []

    def edit_message(self, chat_id: str, message_id: int, text: str,
                     buttons: list[tuple[str, str]] | None = None, column: bool = False) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if buttons is not None:
            payload["reply_markup"] = keyboard(buttons, column)
        self._call("editMessageText", payload)


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
        CONTROL.check()  # a safe point: /end stops the run here
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
    if command == "end":
        return end_text()
    if command == "restart":
        return ("/restart restarts the bot running on your PC. On Cloud Run each deploy starts "
                "the bot again; the --fake bot has nothing to restart.")
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
STALE_COMMANDS = ("fetch", "screen", "update", "autopilot", "restart")


def _stale_command(update: dict[str, Any], started: float | None) -> str | None:
    message = update.get("message") or {}
    command = parse_command(message.get("text") or "")
    if started is None or command not in STALE_COMMANDS:
        return None
    sent = message.get("date")  # Telegram always sets it; the terminal harness does not
    return command if sent is not None and int(sent) < int(started) else None


def end_now(client: TelegramClient, s: Settings, update: dict[str, Any]) -> bool:
    """The webhook answers /end at once, before the worker queue (where it would wait behind
    the very run it should stop). True when the update was /end from the configured chat."""
    message = update.get("message") or {}
    chat = str((message.get("chat") or {}).get("id", ""))
    if chat != str(s.telegram_chat_id) or parse_command(message.get("text") or "") != "end":
        return False
    client.send_message(chat, telegram_text(end_text(), s))
    return True


class Restart(Exception):  # noqa: N818 (a request, not an error)
    """/restart was sent; `offset` confirms it to Telegram so it is not delivered again."""

    def __init__(self, offset: int) -> None:
        super().__init__("restart")
        self.offset = offset


def poll_once(
    client: TelegramClient,
    s: Settings,
    offset: int | None,
    fetch: Fetcher | None = None,
    desk: Desk | None = None,
    started: float | None = None,
    dispatch: Callable[[dict[str, Any]], None] | None = None,
    timeout: int = POLL_TIMEOUT,
) -> int | None:
    """Fetch one batch of updates, answer them, and return the next offset. With `started`
    (the bot's start time), /fetch, /screen and /update sent before it are not run. With
    `dispatch` (the bot's background thread) updates are handed over instead of answered
    here, except /end and /restart, which act at once. /restart raises Restart."""
    for update in client.get_updates(offset, timeout):
        offset = update["update_id"] + 1
        command = _stale_command(update, started)
        chat = str((update.get("message") or {}).get("chat", {}).get("id", ""))
        mine = chat == str(s.telegram_chat_id)
        now = parse_command((update.get("message") or {}).get("text") or "") if mine else None
        if now == "restart" and dispatch is not None:
            if command is None:
                raise Restart(offset)
            continue  # a /restart from before this start: already done
        if now == "end" and dispatch is not None:
            client.send_message(chat, telegram_text(end_text(), s))
            continue
        if command and mine:
            log.info("not running /%s sent before the bot started", command)
            client.send_message(chat, telegram_text(
                f"Not running /{command}: it was sent before the bot started (maybe a run you "
                f"stopped). Send /{command} again to run it.", s))
            continue
        if dispatch is not None:
            dispatch(update)
        else:
            handle_one(client, s, update, fetch, desk)
    return offset


class Background:
    """Long polling answers each update on one background thread, so the bot keeps reading
    messages while a /fetch runs and /end can stop it. Updates that come meanwhile wait their
    turn, as before; one thread at a time keeps the bot's state single-threaded."""

    def __init__(self, handle: Callable[[dict[str, Any]], None],
                 note: Callable[[dict[str, Any], str], None] | None = None) -> None:
        self.handle, self.note = handle, note
        self.waiting: deque[dict[str, Any]] = deque()
        self.thread: threading.Thread | None = None

    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def idle(self) -> bool:
        return not self.busy() and not self.waiting

    def submit(self, update: dict[str, Any]) -> None:
        if CONTROL.running and self.note is not None and "message" in update:
            self.note(update, CONTROL.running)
        self.waiting.append(update)
        self.pump()

    def pump(self) -> None:
        """Start the next waiting update when the thread is free."""
        if self.busy() or not self.waiting:
            return
        update = self.waiting.popleft()
        self.thread = threading.Thread(target=self._run, args=(update,), name="jobengine-job",
                                       daemon=True)
        self.thread.start()

    def _run(self, update: dict[str, Any]) -> None:
        try:
            self.handle(update)
        except Exception:  # one failed update never stops the bot
            log.exception("answering an update failed")

    def join(self, timeout: float | None = None) -> None:
        if self.thread is not None:
            self.thread.join(timeout)


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
        with Typing(client, str(s.telegram_chat_id)):  # "typing..." until the summary
            run_fetch(client, s, str(s.telegram_chat_id), fetch)
        return
    chat = str(s.telegram_chat_id)
    message = update.get("message") or {}
    if str((message.get("chat") or {}).get("id", "")) != chat:
        out = handle_update(update, s, fetch)  # logs and ignores other chats
        if out is not None:
            client.send_message(*out)
        return
    if message.get("document"):
        with Typing(client, chat):
            replies = _guarded("LinkedIn export", lambda: document_replies(
                client, message["document"], desk))
        send_replies(client, s, chat, replies)
        return
    text = message.get("text") or ""
    if parse_command(text) == "gmailtoken" or gmail_renew.looks_like_answer(text):
        gmail_token_message(client, s, message, desk)
        return
    if desk is not None and parse_command(text) == "autopilot":
        if text.split()[1:2] in (["when"], ["schedule"]):
            send_replies(client, s, chat, _guarded("/autopilot when", desk.autopilot_when))
        else:
            with Typing(client, chat):
                run_autopilot(client, s, chat, desk, fetch)
        return
    if desk is not None and parse_command(text) == "screen" and len(text.split()) == 1:
        choice = _guarded("/screen", lambda: [r for r in [desk.screen_choice()] if r])
        if choice:  # more jobs wait than today's limit leaves: the buttons decide (P9-19)
            send_replies(client, s, chat, choice)
            return
        with Typing(client, chat):
            run_screen(client, s, chat, desk)
        return
    with Typing(client, chat):
        replies = desk_replies(update, s, desk)
        out = None if replies is not None else handle_update(update, s, fetch)
    if replies is not None:
        send_replies(client, s, chat, with_next_step(text, replies))
    elif out is not None:
        client.send_message(*out)


EXPORT_TYPES = (".zip", ".csv")


def document_replies(client: TelegramClient, document: dict[str, Any],
                     desk: Desk | None) -> list[Reply]:
    """A file sent to the bot: a LinkedIn export (zip or Connections.csv) updates the
    🤝 LinkedIn Connections table; any other file is not read."""
    name = str(document.get("file_name") or "file")
    if not name.lower().endswith(EXPORT_TYPES):
        return [Reply(f"{name}: the bot only reads your LinkedIn export (the zip or "
                      "Connections.csv). Other files are not read.")]
    if desk is None:
        return [Reply("The LinkedIn export needs Notion (NOTION_TOKEN): not available here.")]
    if int(document.get("file_size") or 0) > MAX_DOWNLOAD_BYTES:
        return [Reply(f"{name} is over 20 MB (Telegram's limit for bots). Send only "
                      "Connections.csv from the zip.")]
    data = client.download(str(document.get("file_id") or ""))
    return desk.connections_file(name, data)


def gmail_token_message(client: TelegramClient, s: Settings, message: dict[str, Any],
                        desk: Desk | None) -> None:
    """/gmailtoken (status or a sign-in link) and the pasted sign-in address
    (gmail_renew.py). The pasted address holds a one-time code: it is deleted at once."""
    chat = str(s.telegram_chat_id)
    text = message.get("text") or ""
    now = datetime.now()
    if gmail_renew.looks_like_answer(text):
        try:
            client.delete_message(chat, int(message.get("message_id") or 0))
        except (TelegramError, ValueError) as exc:
            log.warning("could not delete the pasted sign-in address: %s", exc)
        with Typing(client, chat):
            reply = _guarded("Gmail token", lambda: [Reply(gmail_renew.finish(s, text, now))])
        send_replies(client, s, chat, reply)
        return
    args = text.split()[1:]
    if args:
        account = args[0].lower()
        notify = lambda answer: send_replies(client, s, chat, [Reply(answer)])  # noqa: E731
        listening = (s.app_env == "local" and account in gmail_renew.ACCOUNTS
                     and gmail_renew.listen(s, notify) is not None)
        reply = _guarded("Gmail token", lambda: [Reply(gmail_renew.start(
            s, account, now, listening))])
        if listening and not gmail_renew.PENDING:
            gmail_renew.stop()  # the link was refused (no OAuth client): nothing to wait for
    else:
        config: Any = desk.deps.config() if desk is not None else {}
        with Typing(client, chat):
            reply = _guarded("Gmail token", lambda: [Reply(gmail_renew.status(
                s, config, now.date()))])
    send_replies(client, s, chat, reply)


def send_replies(client: TelegramClient, s: Settings, chat_id: str, replies: list[Reply]) -> None:
    for reply in replies:
        if reply.document:
            name, data = reply.document
            client.send_document(chat_id, name, data, telegram_text(reply.text, s),
                                 reply.buttons or None)
        else:
            client.send_message(chat_id, telegram_text(reply.text, s), reply.buttons or None,
                                column=reply.column)


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


def _scheduled(name: str, action: Callable[[], list[Reply]], desk: Desk) -> list[Reply]:
    """A scheduled step: a failure is told in Telegram and mailed to you (H3, 25 Oct)."""
    try:
        return action()
    except Exception as exc:  # report the failure instead of stopping the bot
        log.exception("%s failed", name)
        desk.alert_failure(name, exc)
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
        replies = _guarded("Spot check", lambda: desk.spot_note(ref_text, text) or [])
        if replies:
            return replies
        replies = _guarded("Dry run", lambda: desk.dryrun_note(ref_text, text) or [])
        if replies:
            return replies
    if command == "next":
        return _guarded("/next", desk.next_command)
    if command == "pending":
        return _guarded("/pending", lambda: desk.pending_command(args))
    if command == "agencies":
        return _guarded("/agencies", lambda: desk.agencies_command(args))
    if command == "gaps":
        return _guarded("/gaps", desk.gaps_command)
    if command == "keywords":
        return _guarded("/keywords", desk.keywords_command)
    if command == "connections":
        return _guarded("/connections", desk.connections_command)
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
    if command == "answer":
        return _guarded("/answer", lambda: desk.answer_command(args))
    if command == "answers":
        return _guarded("/answers", desk.answers_command)
    if command == "autofill":
        return _guarded("/autofill", desk.autofill_command)
    if command == "spotcheck":
        return _guarded("/spotcheck", lambda: desk.spot_check_command(args))
    if command == "dryrun":
        return _guarded("/dryrun", lambda: desk.dryrun_command(args))
    if command == "applypack":
        return _guarded("/applypack", lambda: desk.applypack_command(args))
    if command == "prep":
        return _guarded("/prep", lambda: desk.prep_command(args))
    if command == "thanks":
        return _guarded("/thanks", lambda: desk.thanks_command(args))
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
    if command == "add":
        return _guarded("/add", lambda: desk.add_command(args))
    if command == "report":
        return _guarded("/report", desk.report_command)
    if command == "jd":
        return _guarded("/jd", lambda: desk.jd(args))
    if command == "done":
        return _guarded("/done", desk.done)
    if command == "linkedin":
        return _guarded("/linkedin", desk.linkedin_command)
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
        client.answer_callback(str(query.get("id", "")),
                               TAP_TOASTS.get(data.partition(":")[0], TAP_DEFAULT))
    except TelegramError as exc:
        log.warning("answerCallbackQuery failed: %s", exc)
    action, _, arg = data.partition(":")
    if action == "sc" and arg.isdigit() and desk is not None:  # a /screen choice button
        with Typing(client, sender):
            run_screen(client, s, sender, desk, limit=int(arg))
        return
    with Typing(client, sender), SlowWatch(client, sender, data):
        if desk is None:
            replies = [Reply("Buttons are not available in this bot.")]
        else:
            replies = _guarded("Button", lambda: desk.tap(data))
    message_id = (query.get("message") or {}).get("message_id")
    if replies and replies[0].edit and message_id:
        # A picker button changes its own message (FR-1) instead of adding a new one.
        first, *replies = replies
        try:
            client.edit_message(sender, int(message_id), telegram_text(first.text, s),
                                first.buttons, column=first.column)
        except TelegramError as exc:
            log.warning("could not edit the message: %s", exc)
            replies.insert(0, first)
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
        with CONTROL.job("/fetch"):
            summary = fetch(progress.update)
    except Stopped:
        _stopped(client, s, chat_id, "/fetch", progress)
        return
    except Exception as exc:  # report any sweep failure instead of stopping the bot
        log.exception("/fetch failed")
        progress.finish(ok=False)
        client.send_message(chat_id, telegram_text(f"/fetch failed: {exc}", s))
        return
    progress.finish(ok=True)
    client.send_message(chat_id, telegram_text(summary, s))


def _stopped(client: TelegramClient, s: Settings, chat_id: str, name: str,
             progress: ProgressMessage) -> None:
    log.info("%s stopped by /end", name)
    progress.status = "stopped by /end"
    progress.finish(ok=False)
    client.send_message(chat_id, telegram_text(
        f"{name} stopped by /end after {progress.elapsed()}. What was done before it stopped "
        "is kept.", s))


def run_screen(
    client: TelegramClient, s: Settings, chat_id: str, desk: Desk,
    clock: Callable[[], float] = time.monotonic, limit: int | None = None,
) -> None:
    """/screen without a job: it can take minutes, so show one progress message like /fetch.
    `limit`: a /screen choice button (that many now, over today's limit if needed)."""
    progress = ProgressMessage(client, chat_id, s, clock=clock, name="Screening",
                               first="Screening new jobs")
    try:
        with CONTROL.job("/screen"):
            replies = desk.screen_replies("", progress.update, limit)
    except Stopped:
        _stopped(client, s, chat_id, "/screen", progress)
        return
    except Exception as exc:  # report the failure instead of stopping the bot
        log.exception("/screen failed")
        progress.finish(ok=False)
        client.send_message(chat_id, telegram_text(f"/screen failed: {exc}", s))
        return
    progress.finish(ok=True)
    send_replies(client, s, chat_id, with_next_step("/screen", replies))


def with_next_step(text: str, replies: list[Reply]) -> list[Reply]:
    """The replies with a "👉 Next:" line at the end of the last text answer (next_steps.py),
    unless that answer already shows what comes next (buttons or its own Next)."""
    command = parse_command(text)
    args = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
    answer = "\n".join(r.text for r in replies)
    line = next_steps.hint(command, args, answer)
    if not line or not replies:
        return replies
    last = replies[-1]
    if last.document or last.buttons:
        return [*replies, Reply(f"\U0001F449 Next: {line}")]
    return [*replies[:-1], Reply(last.text + next_steps.NEXT + line)]


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
        with CONTROL.job("/autopilot"):
            replies = desk.autopilot(autopilot_fetch(fetch), progress.update)
    except Stopped:
        _stopped(client, s, chat_id, "/autopilot", progress)
        return
    except Exception as exc:  # report the failure instead of stopping the bot
        log.exception("/autopilot failed")
        progress.finish(ok=False)
        client.send_message(chat_id, telegram_text(f"/autopilot failed: {exc}", s))
        return
    progress.finish(ok=True)
    send_replies(client, s, chat_id, with_next_step("/autopilot", replies))


def autopilot_check(client: TelegramClient, s: Settings, desk: Desk | None,
                    fetch: Fetcher | None = None) -> None:
    """Long polling: carry on with an /autopilot whose half-price batch has answered, else
    start the morning's scheduled run when it is due (Config schedule.autopilot); run the
    afternoon check when it is due (Config schedule.check, screen/check.py); then send
    one queued mail whose recipient's morning has come (mail/timing.py), and ping new
    replies (track/ping.py, every tracking.reply_check_minutes)."""
    if desk is None:
        return
    _bind_notify(client, s, desk)
    replies = _scheduled("Autopilot",
                         lambda: desk.autopilot_scheduled(autopilot_fetch(fetch)) or [], desk)
    replies += _scheduled("Afternoon check",
                          lambda: desk.check_scheduled(autopilot_fetch(fetch)) or [], desk)
    replies += _scheduled("Mail queue", desk.mail_queue_tick, desk)
    replies += _scheduled("Reply check", desk.reply_ping_tick, desk)
    replies += _scheduled("Daily report", desk.daily_report_tick, desk)
    replies += _scheduled("Spot check", desk.spot_check_tick, desk)
    replies += _scheduled("Apply nudge", desk.nudge_tick, desk)
    replies += _guarded("Gmail token", lambda: [Reply(text) for text in gmail_renew.reminder(
        s, desk.state, desk.deps.config(), date.today())])
    replies += _guarded("LinkedIn connections", desk.connections_reminder)
    replies += _guarded("Leftover credits", desk.leftover_tick)
    if replies:
        send_replies(client, s, str(s.telegram_chat_id), replies)


SLOW_SECONDS = 90.0
SLOW_TEXT = ("Still working on it (over {seconds} seconds). Where it is now has been written "
             "to the bot window. If nothing comes in 5 more minutes, restart the bot and send "
             "Claude Code the bot window lines.")


class SlowWatch:
    """A button still running after SLOW_SECONDS: tell the chat once and write every thread's
    stack to the log (stderr), so a hang shows where it is (6 Oct: Approve resume showed
    "typing" for minutes with nothing in the log)."""

    def __init__(self, client: TelegramClient, chat_id: str, what: str,
                 seconds: float = SLOW_SECONDS):
        self.client, self.chat_id, self.what, self.seconds = client, chat_id, what, seconds
        self._timer: threading.Timer | None = None

    def _fire(self) -> None:
        log.warning("button %s still running after %.0f s; stacks follow", self.what,
                    self.seconds)
        try:
            faulthandler.dump_traceback(all_threads=True)
        except (RuntimeError, ValueError, OSError):  # stderr closed or not a real file
            log.exception("could not write the stacks")
        try:
            self.client.send_message(self.chat_id,
                                     SLOW_TEXT.format(seconds=int(self.seconds)))
        except TelegramError as exc:
            log.warning("could not send the slow note: %s", exc)

    def __enter__(self) -> SlowWatch:
        self._timer = threading.Timer(self.seconds, self._fire)
        self._timer.daemon = True
        self._timer.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._timer is not None:
            self._timer.cancel()


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
    background: bool = False,
    restart: Callable[[], None] | None = None,
) -> None:
    """Register the / menu, announce startup, then poll forever (or max_polls times).
    Between polls, every few minutes: a waiting /autopilot batch is checked, and the morning's
    scheduled /autopilot starts when it is due. With `background` (the real bot) updates are
    answered on a background thread so /end can stop a long run; /restart stops the running
    job, then calls `restart`."""
    register_menu(client, s)
    client.send_message(str(s.telegram_chat_id), telegram_text("Job Engine bot started.", s))
    offset: int | None = None
    started = time.time()
    polls = 0
    next_check = clock()
    chat = str(s.telegram_chat_id)
    bg = None
    if background:
        bg = Background(lambda update: handle_one(client, s, update, fetch, desk),
                        lambda update, name: client.send_message(chat, telegram_text(
                            f"Waiting: this runs after {name} (/end stops {name}).", s)))
    while max_polls is None or polls < max_polls:
        polls += 1
        try:
            offset = poll_once(client, s, offset, fetch, desk, started=started,
                               dispatch=bg.submit if bg else None,
                               timeout=WAITING_POLL if bg and bg.waiting else POLL_TIMEOUT)
            if bg is not None:
                bg.pump()
            if desk is not None and clock() >= next_check and (bg is None or bg.idle()):
                next_check = clock() + autopilot.check_seconds(desk)
                autopilot_check(client, s, desk, fetch)
        except Restart as request:
            offset = request.offset
            if _restart(client, s, bg, request.offset, restart):
                return
        except TelegramError as exc:
            log.error("%s, retrying in 5s", exc)
            sleep(5)
    while bg is not None and not bg.idle():  # max_polls reached (tests): finish the queue
        bg.join()
        bg.pump()


RESTART_WAIT = 60.0  # seconds a running job gets to reach its next step before the restart
WAITING_POLL = 2  # a short poll while updates wait for the background thread


def _restart(client: TelegramClient, s: Settings, bg: Background | None, offset: int,
             restart: Callable[[], None] | None) -> bool:
    """Stop the running job, confirm /restart, then restart. False (and the bot keeps
    running) when a job is still stuck."""
    chat = str(s.telegram_chat_id)
    name = CONTROL.request_stop()
    if bg is not None:
        if name:
            client.send_message(chat, telegram_text(f"Stopping {name} first...", s))
        bg.join(RESTART_WAIT)
        if bg.busy():
            log.warning("restart: the running job did not stop in %.0fs", RESTART_WAIT)
    try:
        client.get_updates(offset, 0)  # confirm /restart so it is not delivered again
    except TelegramError as exc:
        log.warning("could not confirm /restart: %s", exc)
    stuck = (bg is not None and bg.busy()) or bool(resume_builder.STUCK_SAVES)
    if stuck:
        # A thread waiting on a file in the shared out folder cannot be stopped, and a
        # restart in this process would wait for it for ever (7 Oct).
        log.warning("restart refused: a job is still stuck (%s)",
                    ", ".join(resume_builder.STUCK_SAVES) or "a button")
        client.send_message(chat, telegram_text(STUCK_RESTART, s))
        return False
    client.send_message(chat, telegram_text("Restarting the bot...", s))
    if restart is not None:
        restart()
    return True


STUCK_RESTART = ("Cannot restart from Telegram: a job is still stuck (it waits on a file in "
                 "the out folder or on the network). In the bot window press Ctrl+C, or run "
                 "docker ps and docker stop <id>, then start the bot again. Close any PDF "
                 "from the out folder that is open in a viewer first.")


def restart_process(argv: list[str]) -> None:
    """Start the bot again in this process: fresh code, Notion settings and state. Values
    from --env-file are read when the container starts, so a changed .env needs a new
    docker run instead."""
    logging.shutdown()
    os.execv(sys.executable, [sys.executable, "-m", "jobengine.telegram_bot", *argv])


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
        from jobengine.screen import daily

        return (f"{text}\n\n\U0001F449 Next: /screen checks the new jobs against your profile "
                f"(uses the AI, at most {daily.daily_limit(s)} jobs a day).")

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
        run(s, client, fetch=make_fetcher(s, args.fake, desk), desk=desk, background=not args.fake,
            restart=lambda: restart_process(sys.argv[1:] if argv is None else argv))
    except TelegramError as exc:
        print(f"Job Engine bot stopped: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("Job Engine bot stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
