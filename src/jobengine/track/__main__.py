"""CLI: python -m jobengine.track daily|digest [--fake] [--now 2026-10-15T08:00:00+05:30]
       [--no-write]

daily runs the daily check and prints the Telegram report; digest prints the weekly digest.
Nothing is sent: follow-ups are drafts, and none are made in DRY_RUN or with --no-write."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from jobengine.bot_state import FakeBotState
from jobengine.main import banner
from jobengine.safety import SafetyError, check_startup
from jobengine.settings import get_settings
from jobengine.track.digest import run_weekly_digest
from jobengine.track.fakes import FAKE_NOW
from jobengine.track.runner import fake_deps, real_deps, run_daily


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobengine.track")
    parser.add_argument("action", choices=("daily", "digest"))
    parser.add_argument("--fake", action="store_true",
                        help="fixture jobs, contacts and Gmail threads, no network")
    parser.add_argument("--now", help="ISO time with offset, e.g. 2026-10-15T08:00:00+05:30")
    parser.add_argument("--no-write", action="store_true",
                        help="report only: no Notion, label, draft or marker writes")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    try:
        s = get_settings()
        check_startup(s)
        now = datetime.fromisoformat(args.now) if args.now else (
            FAKE_NOW if args.fake else datetime.now().astimezone())
    except (SafetyError, ValueError) as exc:
        print(f"Job Engine tracking startup failed: {exc}", file=sys.stderr)
        return 1
    if now.tzinfo is None:
        print("--now needs a UTC offset, e.g. 2026-10-15T08:00:00+05:30", file=sys.stderr)
        return 1
    print(banner(s))
    write = not args.no_write
    try:
        if args.fake:
            deps = fake_deps(s, write=write)
        else:
            from jobengine.bot_state import bot_state_for
            from jobengine.notion_repo import NotionClient

            if not s.notion_token:
                raise ValueError("NOTION_TOKEN missing")
            state = bot_state_for(s, NotionClient(s.notion_token, s)) or FakeBotState()
            deps = real_deps(s, state, write=write)
    except ValueError as exc:
        print(f"Job Engine tracking failed: {exc}", file=sys.stderr)
        return 1
    if args.action == "digest":
        print(run_weekly_digest(deps, now))
        return 0
    report = run_daily(deps, now)
    print(report.text())
    for card in [*report.cards, *([report.retention] if report.retention else [])]:
        buttons = " ".join(f"[{label}: {data}]" for label, data in card.buttons)
        print(f"\n{card.text}\n{buttons}")
    if not write:
        print("\n--no-write: nothing was written.")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
