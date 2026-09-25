"""Point the Telegram bot at the Cloud Run service, or go back to long polling.

    python -m jobengine.deploy.set_webhook --url https://job-engine-dev-xxxx.a.run.app
    python -m jobengine.deploy.set_webhook --delete

Uses TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET from the environment (never printed).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from typing import Any

from jobengine.settings import Settings, get_settings
from jobengine.telegram_bot import Transport, http_transport

WEBHOOK_PATH = "/telegram/webhook"
ALLOWED_UPDATES = ["message", "callback_query"]
SHOWN = ("url", "has_custom_certificate", "pending_update_count", "last_error_date",
         "last_error_message", "max_connections", "allowed_updates")


def webhook_url(service_url: str) -> str:
    url = service_url.strip().rstrip("/")
    if not url.startswith("https://"):
        raise ValueError("the service URL must start with https://")
    return url + WEBHOOK_PATH


def set_webhook(transport: Transport, service_url: str, secret: str) -> dict[str, Any]:
    if not secret or len(secret) < 32 or not secret.isalnum():
        raise ValueError("TELEGRAM_WEBHOOK_SECRET must be 32 or more letters and digits")
    return transport("setWebhook", {
        "url": webhook_url(service_url), "secret_token": secret,
        "allowed_updates": ALLOWED_UPDATES, "drop_pending_updates": True}, 30)


def delete_webhook(transport: Transport) -> dict[str, Any]:
    return transport("deleteWebhook", {"drop_pending_updates": False}, 30)


def webhook_info(transport: Transport) -> dict[str, Any]:
    result = transport("getWebhookInfo", {}, 30).get("result") or {}
    return {k: result[k] for k in SHOWN if k in result}


def main(argv: list[str] | None = None, settings: Callable[[], Settings] = get_settings,
         transport: Transport | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobengine.deploy.set_webhook")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url", help="the Cloud Run service URL (https://...run.app)")
    group.add_argument("--delete", action="store_true",
                       help="remove the webhook (the bot can then be run by long polling)")
    args = parser.parse_args(argv)
    s = settings()
    if not s.telegram_bot_token:
        print("TELEGRAM_BOT_TOKEN is not set.", file=sys.stderr)
        return 1
    call = transport or http_transport(s.telegram_bot_token)
    try:
        if args.delete:
            result = delete_webhook(call)
        else:
            result = set_webhook(call, args.url, s.telegram_webhook_secret or "")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"{'deleteWebhook' if args.delete else 'setWebhook'}: "
          f"{result.get('description') or result.get('ok')}")
    print(json.dumps(webhook_info(call), indent=1))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
