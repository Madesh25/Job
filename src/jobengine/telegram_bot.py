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
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import date
from typing import Any, TextIO

from jobengine.main import banner
from jobengine.safety import SafetyError, check_startup, telegram_text
from jobengine.screen.desk import Desk, Reply, fake_desk, real_desk
from jobengine.settings import Settings, get_settings
from jobengine.sweep import fakes
from jobengine.sweep.runner import fake_deps, real_deps, run_sweep

log = logging.getLogger("jobengine.telegram_bot")

API_BASE = "https://api.telegram.org"
FAKE_CHAT_ID = "1"
POLL_TIMEOUT = 30

HELP_TEXT = (
    "Job Engine commands:\n"
    "/start - check that the bot is alive\n"
    "/status - show environment and safety settings\n"
    "/fetch - search for new jobs, then screen them\n"
    "/pending - review screened jobs one at a time (Approve, Skip, Next)\n"
    "/jd <url> - paste a job description (for LinkedIn jobs), then /done\n"
    "/jd - list jobs waiting for a description\n"
    "/screen - screen jobs that are not screened yet\n"
    "/screen <url or id> - screen one job again\n"
    "/help - show this list"
)
DESK_COMMANDS = ("pending", "jd", "done", "screen")

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
        req = urllib.request.Request(
            f"{API_BASE}/bot{token}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
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


def console_transport(chat_id: str, stdin: TextIO, stdout: TextIO) -> Transport:
    """Dummy Telegram: stdin lines become messages from chat_id, replies go to stdout.

    getUpdates raises EOFError when the input ends, which stops the bot.
    """
    next_id = 0

    def call(method: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        nonlocal next_id
        if method == "sendMessage":
            print(f"bot> {payload['text']}", file=stdout, flush=True)
            rows = (payload.get("reply_markup") or {}).get("inline_keyboard") or []
            keys = [f"[{b['text']}: tap {b['callback_data']}]" for row in rows for b in row]
            if keys:
                print("     " + " ".join(keys), file=stdout, flush=True)
            next_id += 1
            return {"ok": True, "result": {"message_id": next_id}}
        if method == "answerCallbackQuery":
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
            message = {"chat": {"id": chat_id}, "text": text}
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
        `buttons` are (label, callback data) pairs shown in one row under the message."""
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [
                [{"text": label, "callback_data": data} for label, data in buttons]
            ]}
        result = self._call("sendMessage", payload)
        return (result or {}).get("message_id") if isinstance(result, dict) else None

    def answer_callback(self, callback_id: str) -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_id})

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
    ):
        self.client = client
        self.chat_id = chat_id
        self.s = s
        self.clock = clock
        self.status = "Starting..."
        self.started = time.strftime("%H:%M")
        self.started_at = clock()
        self.message_id = client.send_message(
            chat_id,
            telegram_text(f"\U0001F50E Searching for jobs (started {self.started})...", s),
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
            self._edit(f"\U0001F50E Job search running (started {self.started})\n{status}")

    def elapsed(self) -> str:
        minutes = int((self.clock() - self.started_at) // 60)
        return "under a minute" if minutes < 1 else f"{minutes} min"

    def finish(self, ok: bool) -> None:
        if ok:
            self._edit(f"\u2705 Job search done in {self.elapsed()}. Summary below.")
        else:
            self._edit(f"\u274C Job search stopped after {self.elapsed()}: {self.status}")


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


def poll_once(
    client: TelegramClient,
    s: Settings,
    offset: int | None,
    fetch: Fetcher | None = None,
    desk: Desk | None = None,
) -> int | None:
    """Fetch one batch of updates, answer them, and return the next offset."""
    for update in client.get_updates(offset):
        offset = update["update_id"] + 1
        if "callback_query" in update:
            handle_callback(client, s, update["callback_query"], desk)
            continue
        if fetch is not None and _is_fetch(update, s):
            run_fetch(client, s, str(s.telegram_chat_id), fetch)
            continue
        replies = desk_replies(update, s, desk)
        if replies is not None:
            send_replies(client, s, str(s.telegram_chat_id), replies)
            continue
        out = handle_update(update, s, fetch)
        if out is not None:
            client.send_message(*out)
    return offset


def send_replies(client: TelegramClient, s: Settings, chat_id: str, replies: list[Reply]) -> None:
    for reply in replies:
        client.send_message(chat_id, telegram_text(reply.text, s), reply.buttons or None)


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
    if command == "pending":
        return _guarded("/pending", desk.pending)
    if command == "jd":
        return _guarded("/jd", lambda: desk.jd(args))
    if command == "done":
        return _guarded("/done", desk.done)
    if command == "screen":
        return _guarded("/screen", lambda: [Reply(desk.screen(args))])
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
    try:
        client.answer_callback(str(query.get("id", "")))
    except TelegramError as exc:
        log.warning("answerCallbackQuery failed: %s", exc)
    if desk is None:
        replies = [Reply("Buttons are not available in this bot.")]
    else:
        replies = _guarded("Button", lambda: desk.tap(str(query.get("data") or "")))
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
) -> None:
    """Announce startup, then poll forever (or max_polls times, for tests)."""
    client.send_message(str(s.telegram_chat_id), telegram_text("Job Engine bot started.", s))
    offset: int | None = None
    polls = 0
    while max_polls is None or polls < max_polls:
        polls += 1
        try:
            offset = poll_once(client, s, offset, fetch, desk)
        except TelegramError as exc:
            log.error("%s, retrying in 5s", exc)
            sleep(5)


def make_fetcher(s: Settings, fake: bool, desk: Desk | None = None) -> Fetcher:
    """/fetch runs the sweep (fixtures and an in-memory Notion with --fake, real otherwise),
    then screens the new jobs when a desk is given."""

    def fetch(progress: Progress) -> str:
        if fake:
            repo = desk.repo if desk is not None else None
            summary = run_sweep(s, fake_deps(s, repo=repo), fakes.FAKE_TODAY, progress=progress)
        else:
            summary = run_sweep(s, real_deps(s), date.today(), progress=progress)
        text = summary.friendly_text()
        if desk is None or summary.blocked:
            return text
        progress("Screening the new jobs...")
        return f"{text}\n\n{desk.screen(progress=progress)}"

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
