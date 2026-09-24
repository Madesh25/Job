"""CLI: python -m jobengine.sweep [--fake] [--source ...] [--today YYYY-MM-DD] [--parse-report]"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from jobengine import http
from jobengine.main import banner
from jobengine.safety import SafetyError, check_startup
from jobengine.settings import Settings, get_settings
from jobengine.sweep import fakes
from jobengine.sweep.runner import SOURCES, SweepError, fake_deps, real_deps, run_sweep
from jobengine.sweep.sources import gmail_alerts


def parse_report(s: Settings, fake: bool) -> int:
    """Print what the Gmail parser extracts. Writes nothing."""
    result = gmail_alerts.fetch(s, load_messages=fakes.gmail_messages if fake else None)
    if result.skipped_reason:
        print(result.skipped_reason)
        return 1
    print(f"Parsed {len(result.postings)} postings from Gmail alerts (nothing written):")
    for p in result.postings:
        print(f"- board={p.board} | title={p.title} | company={p.company} | "
              f"location={p.location_text or '(none)'} | url={p.url}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobengine.sweep")
    parser.add_argument("--fake", action="store_true",
                        help="use fixtures for every source and an in-memory Notion")
    parser.add_argument("--source", choices=[*SOURCES, "all"], default="all")
    parser.add_argument("--today", type=date.fromisoformat,
                        help="run as if today were this date (YYYY-MM-DD)")
    parser.add_argument("--parse-report", action="store_true",
                        help="print what the Gmail alert parser extracts, write nothing")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    try:
        s = get_settings()
        check_startup(s)
    except (SafetyError, ValueError) as exc:
        print(f"Job Engine sweep startup failed: {exc}", file=sys.stderr)
        return 1
    print(banner(s))

    if args.parse_report:
        return parse_report(s, args.fake)

    today = args.today or (fakes.FAKE_TODAY if args.fake else date.today())
    sources = SOURCES if args.source == "all" else (args.source,)
    try:
        deps = fake_deps(s) if args.fake else real_deps(s)
        summary = run_sweep(s, deps, today, sources)
    except (SweepError, http.HttpError) as exc:
        print(f"Job Engine sweep failed: {exc}", file=sys.stderr)
        return 1
    print(summary.text())
    return 2 if summary.blocked else 0


if __name__ == "__main__":
    sys.exit(main())
