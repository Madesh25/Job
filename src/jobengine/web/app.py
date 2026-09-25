"""The Cloud Run app (spec section 2): Telegram webhook, scheduled tasks, health check.

Every path is protected in code (the service allows unauthenticated calls so Telegram can
reach it): the webhook by the secret token header and the chat ID lock, the task endpoints
by a Cloud Scheduler OIDC token. Updates go through the same handler as long polling.
"""

from __future__ import annotations

import logging
import sys
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from jobengine import telegram_bot as tb
from jobengine.bot_state import BotState, FakeBotState
from jobengine.safety import SafetyError, check_startup, telegram_text
from jobengine.screen.desk import Desk, Reply
from jobengine.settings import Settings
from jobengine.web.auth import (
    SECRET_HEADER,
    AuthError,
    Verifier,
    check_scheduler_token,
    check_telegram_secret,
    google_verifier,
)
from jobengine.web.worker import Worker

log = logging.getLogger("jobengine.web")

RECENT_KEY = "telegram.recent_updates"
RECENT_SIZE = 200
TASK_TIMEOUT = 870  # under the 900 s Cloud Run request timeout


class RecentUpdates:
    """The last 200 update_ids, in memory and in bot_state, so Telegram retries are dropped
    even after a restart."""

    def __init__(self, state: BotState, size: int = RECENT_SIZE):
        self.state = state
        self.size = size
        try:
            saved = (state.get(RECENT_KEY) or {}).get("ids") or []
        except Exception:  # a broken bot_state must not stop the webhook
            log.exception("could not read %s", RECENT_KEY)
            saved = []
        self.ids: deque[int] = deque((int(i) for i in saved), maxlen=size)

    def seen(self, update_id: int) -> bool:
        """True for a repeat; otherwise remembers the ID and returns False."""
        if update_id in self.ids:
            return True
        self.ids.append(update_id)
        try:
            self.state.set(RECENT_KEY, {"ids": list(self.ids)})
        except Exception:
            log.exception("could not store %s", RECENT_KEY)
        return False


@dataclass
class Tasks:
    """What the scheduled endpoints run. Each returns a short summary for the JSON reply."""

    daily: Callable[[], dict[str, Any]]
    digest: Callable[[], dict[str, Any]]
    sweep: Callable[[], dict[str, Any]]


def desk_tasks(s: Settings, desk: Desk, client: tb.TelegramClient,
               fetch: tb.Fetcher | None) -> Tasks:
    """The real task bodies: they run the same desk code as the Telegram commands and send
    their reports to the chat."""
    from jobengine.strategy.runner import run_strategy_reminder
    from jobengine.track.digest import run_weekly_digest
    from jobengine.track.runner import run_daily

    chat = str(s.telegram_chat_id)

    def send(replies: list[Reply]) -> None:
        tb.send_replies(client, s, chat, replies)

    def daily() -> dict[str, Any]:
        if desk.track is None:
            raise RuntimeError("tracking is not available (see the startup log)")
        report = run_daily(desk.track, desk.tracking_now())
        send(desk.report_replies(report))
        reminder = run_strategy_reminder(desk.strategy) if desk.strategy else None
        if reminder:
            send([Reply(reminder)])
        return {"ok": report.ok, "sent": len(report.sent), "replies": len(report.replies),
                "followups": len(report.followups), "reminder": bool(reminder)}

    def digest() -> dict[str, Any]:
        if desk.track is None:
            raise RuntimeError("tracking is not available (see the startup log)")
        send([Reply(run_weekly_digest(desk.track, desk.tracking_now()))])
        return {"ok": True}

    def sweep() -> dict[str, Any]:
        config = desk.deps.config()
        if (config.get("schedule.auto_fetch") or "false").strip().lower() != "true":
            return {"ok": True, "skipped": "Config schedule.auto_fetch is not true"}
        if fetch is None:
            raise RuntimeError("the sweep is not available")
        summary = fetch(lambda line: None)  # the strategy gate is checked inside the sweep
        send([Reply(summary)])
        return {"ok": True}

    return Tasks(daily=daily, digest=digest, sweep=sweep)


def create_app(
    s: Settings,
    client: tb.TelegramClient,
    desk: Desk | None,
    tasks: Tasks,
    *,
    fetch: tb.Fetcher | None = None,
    state: BotState | None = None,
    worker: Worker | None = None,
    verify: Verifier = google_verifier,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    worker = worker or Worker()
    recent = RecentUpdates(state or (desk.state if desk is not None else FakeBotState()))
    app.state.worker = worker

    def denied(exc: AuthError) -> JSONResponse:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status)

    @app.get("/healthz")
    def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.post("/telegram/webhook")
    async def webhook(request: Request) -> JSONResponse:
        try:
            check_telegram_secret(request.headers.get(SECRET_HEADER), s)
        except AuthError as exc:
            return denied(exc)
        try:
            update = await request.json()
            update_id = int(update["update_id"])
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"ok": True, "ignored": "not an update"})
        if recent.seen(update_id):
            return JSONResponse({"ok": True, "duplicate": True})
        # Answer Telegram at once; the worker runs the (possibly long) handler.
        worker.submit(lambda: tb.handle_one(client, s, update, fetch, desk))
        return JSONResponse({"ok": True})

    def run_task(name: str, body: Callable[[], dict[str, Any]],
                 authorization: str | None) -> JSONResponse:
        try:
            check_scheduler_token(authorization, s, verify)
        except AuthError as exc:
            return denied(exc)
        started = datetime.now().astimezone().isoformat(timespec="seconds")
        try:
            summary = worker.call(body, timeout=TASK_TIMEOUT)
        except Exception as exc:  # Scheduler records the 500; you get a Telegram message
            log.exception("scheduled task %s failed", name)
            try:
                client.send_message(str(s.telegram_chat_id), telegram_text(
                    f"Scheduled task {name} failed: {exc}", s))
            except Exception:
                log.exception("could not report the failed task %s", name)
            return JSONResponse({"ok": False, "task": name, "error": str(exc)},
                                status_code=500)
        return JSONResponse({"task": name, "started": started, **summary})

    @app.post("/tasks/daily")
    def task_daily(request: Request) -> JSONResponse:
        return run_task("daily", tasks.daily, request.headers.get("Authorization"))

    @app.post("/tasks/digest")
    def task_digest(request: Request) -> JSONResponse:
        return run_task("digest", tasks.digest, request.headers.get("Authorization"))

    @app.post("/tasks/sweep")
    def task_sweep(request: Request) -> JSONResponse:
        return run_task("sweep", tasks.sweep, request.headers.get("Authorization"))

    return app


def check_web_settings(s: Settings) -> None:
    """Startup refuses a half-configured service (Cloud Run marks the revision failed)."""
    check_startup(s)
    missing = [name for name, value in (
        ("TELEGRAM_BOT_TOKEN", s.telegram_bot_token),
        ("TELEGRAM_CHAT_ID", s.telegram_chat_id),
        ("TELEGRAM_WEBHOOK_SECRET", s.telegram_webhook_secret),
        ("SERVICE_URL", s.service_url),
        ("SCHEDULER_SA_EMAIL", s.scheduler_sa_email),
    ) if not value]
    if missing:
        raise SafetyError(f"missing {', '.join(missing)}")


def build_app(s: Settings) -> FastAPI:
    """The real app. Exits the process on an unsafe or incomplete configuration."""
    from jobengine.screen.desk import real_desk

    try:
        check_web_settings(s)
    except (SafetyError, ValueError) as exc:
        log.error("Job Engine web startup failed: %s", exc)
        sys.exit(1)
    client = tb.TelegramClient(tb.http_transport(s.telegram_bot_token or ""))
    desk = real_desk(s)
    fetch = tb.make_fetcher(s, False, desk)
    if desk is None:
        log.error("Job Engine web startup failed: NOTION_TOKEN missing")
        sys.exit(1)
    return create_app(s, client, desk, desk_tasks(s, desk, client, fetch), fetch=fetch)
