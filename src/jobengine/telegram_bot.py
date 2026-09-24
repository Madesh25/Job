"""Minimal Telegram bot: /start, /status and /help, answering only the configured chat.

Run with `python -m jobengine.telegram_bot`. It long-polls the Telegram Bot API using the
standard library only. Every outgoing message goes through safety.telegram_text so local and
dev messages carry their [LOCAL] or [DEV] prefix.

Run with `--fake` to use a dummy Telegram in the terminal instead: each line you type is a
message from your chat and the replies are printed. No token and no network are needed.
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
from typing import Any, TextIO

from jobengine.main import banner
from jobengine.safety import SafetyError, check_startup, telegram_text
from jobengine.settings import Settings, get_settings

log = logging.getLogger("jobengine.telegram_bot")

API_BASE = "https://api.telegram.org"
FAKE_CHAT_ID = "1"
POLL_TIMEOUT = 30

HELP_TEXT = (
    "Job Engine commands:\n"
    "/start - check that the bot is alive\n"
    "/status - show environment and safety settings\n"
    "/help - show this list"
)

# (method, payload, http_timeout) -> decoded JSON response
Transport = Callable[[str, dict[str, Any], float], dict[str, Any]]


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
            message = {"chat": {"id": chat_id}, "text": line.rstrip("\n")}
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
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        return self._call("getUpdates", payload, timeout=timeout + 10) or []

    def send_message(self, chat_id: str, text: str) -> None:
        self._call("sendMessage", {"chat_id": chat_id, "text": text})


def parse_command(text: str) -> str | None:
    """Return the command name for "/status" or "/status@MadeshDevBot args", else None."""
    if not text.startswith("/"):
        return None
    return text.split()[0][1:].split("@")[0].lower() or None


def status_text(s: Settings) -> str:
    model = s.force_model or "from Notion Config"
    return f"{banner(s)}\nmodel={model}"


def reply_for(text: str, s: Settings) -> str:
    """Plain reply text (without the env prefix) for an incoming message."""
    command = parse_command(text)
    if command == "start":
        return f"Job Engine bot is running (env={s.app_env}).\n\n{HELP_TEXT}"
    if command == "status":
        return status_text(s)
    if command == "help":
        return HELP_TEXT
    if command is None:
        return "I only understand commands for now. Send /help to see them."
    return f"Unknown command /{command}. Send /help to see the commands."


def handle_update(update: dict[str, Any], s: Settings) -> tuple[str, str] | None:
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
    return chat_id, telegram_text(reply_for(text, s), s)


def poll_once(client: TelegramClient, s: Settings, offset: int | None) -> int | None:
    """Fetch one batch of updates, answer them, and return the next offset."""
    for update in client.get_updates(offset):
        offset = update["update_id"] + 1
        out = handle_update(update, s)
        if out is not None:
            client.send_message(*out)
    return offset


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
) -> None:
    """Announce startup, then poll forever (or max_polls times, for tests)."""
    client.send_message(str(s.telegram_chat_id), telegram_text("Job Engine bot started.", s))
    offset: int | None = None
    polls = 0
    while max_polls is None or polls < max_polls:
        polls += 1
        try:
            offset = poll_once(client, s, offset)
        except TelegramError as exc:
            log.error("%s, retrying in 5s", exc)
            sleep(5)


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

    try:
        run(s, client)
    except TelegramError as exc:
        print(f"Job Engine bot stopped: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("Job Engine bot stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
