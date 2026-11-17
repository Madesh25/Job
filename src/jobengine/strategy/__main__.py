"""CLI: python -m jobengine.strategy update|ind|remind [--fake] [--today YYYY-MM-DD]
       [--no-write]

update researches and prints the tip cards (review them in Telegram); ind refreshes the IND
sponsor status of the Dutch target companies; remind prints the monthly reminder, if due."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from jobengine.bot_state import FakeBotState
from jobengine.main import banner
from jobengine.safety import SafetyError, check_startup
from jobengine.settings import get_settings
from jobengine.strategy import ind_refresh
from jobengine.strategy.runner import (
    StrategyError,
    fake_deps,
    real_deps,
    run_strategy_reminder,
    run_update,
)
from jobengine.sweep.fakes import FAKE_TODAY


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobengine.strategy")
    parser.add_argument("action", choices=("update", "ind", "remind"))
    parser.add_argument("--fake", action="store_true",
                        help="fixture research, Strategy rows, V16 page and IND register")
    parser.add_argument("--today", type=date.fromisoformat, help="YYYY-MM-DD")
    parser.add_argument("--no-write", action="store_true", help="show the result, write nothing")
    parser.add_argument("--new", action="store_true", help="update: research even when tips "
                        "from the last run are still undecided")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    try:
        s = get_settings()
        check_startup(s)
    except (SafetyError, ValueError) as exc:
        print(f"Job Engine strategy startup failed: {exc}", file=sys.stderr)
        return 1
    print(banner(s))
    write = not args.no_write
    today = args.today or (FAKE_TODAY if args.fake else date.today())
    try:
        if args.fake:
            deps = fake_deps(s, write=write)
        else:
            from jobengine.bot_state import bot_state_for
            from jobengine.notion_repo import NotionClient

            if not s.notion_token:
                raise StrategyError("NOTION_TOKEN missing")
            state = bot_state_for(s, NotionClient(s.notion_token, s)) or FakeBotState()
            deps = real_deps(s, state, write=write)
    except (StrategyError, ValueError) as exc:
        print(f"Job Engine strategy failed: {exc}", file=sys.stderr)
        return 1
    deps.today = lambda: today
    deps.ind_deps.today = lambda: today  # type: ignore[attr-defined]
    if args.action == "ind":
        report = ind_refresh.refresh(deps.ind_deps)  # type: ignore[attr-defined]
        print(report.text())
        return 1 if report.error else 0
    if args.action == "remind":
        print(run_strategy_reminder(deps, today) or "No reminder today.")
        return 0
    result = run_update(deps, force=args.new)
    for message in result.messages:
        print(message)
    for card in result.cards:
        buttons = " ".join(f"[{label}: {data}]" for label, data in card.buttons)
        print(f"\n{card.text}\n{buttons}")
    print("\nReview the tips in Telegram with /update; the gate opens when every tip is decided.")
    if not write:
        print("--no-write: nothing was written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
