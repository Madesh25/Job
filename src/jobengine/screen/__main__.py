"""CLI: python -m jobengine.screen [--fake] [--today YYYY-MM-DD] [--row <ref>] [--no-write]"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from jobengine import http
from jobengine.main import banner
from jobengine.safety import SafetyError, check_startup
from jobengine.screen.runner import (
    FAKE_TODAY,
    ScreenError,
    fake_deps,
    real_deps,
    screen_one,
    screen_pending,
)
from jobengine.screen.tiering import rank_key
from jobengine.settings import get_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobengine.screen")
    parser.add_argument("--fake", action="store_true",
                        help="fixture rows, fake LLM and fake IND register, in-memory Notion")
    parser.add_argument("--today", type=date.fromisoformat,
                        help="run as if today were this date (YYYY-MM-DD)")
    parser.add_argument("--row", help="re-screen one row: page ID, URL or posting ID")
    parser.add_argument("--no-write", action="store_true",
                        help="print the verdicts without writing anything")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    try:
        s = get_settings()
        check_startup(s)
    except (SafetyError, ValueError) as exc:
        print(f"Job Engine screening startup failed: {exc}", file=sys.stderr)
        return 1
    print(banner(s))

    today = args.today or (FAKE_TODAY if args.fake else date.today())
    write = not args.no_write
    try:
        deps = fake_deps(s, write=write) if args.fake else real_deps(s, write=write)
        if args.row:
            summary = screen_one(s, deps, today, args.row)
        else:
            summary = screen_pending(s, deps, today)
    except (ScreenError, http.HttpError) as exc:
        print(f"Job Engine screening failed: {exc}", file=sys.stderr)
        return 1

    print(summary.text())
    ref = deps.reference()
    rows = {r.page_id: r for r in _rows(deps)}
    results = [r for r in summary.results if r.page_id in rows]
    results.sort(key=lambda r: rank_key(rows[r.page_id], ref, today, verdict=r.verdict,
                                        bottom=r.bottom, ext=r.extraction), reverse=True)
    for r in results:
        row = rows[r.page_id]
        reason = f" ({r.skip_reason})" if r.skip_reason else ""
        flags = f" | visa: {', '.join(r.visa_flags)}" if r.visa_flags else ""
        bottom = " | bottom" if r.bottom else ""
        c = r.counts
        match = f" | {c['Strong']} strong, {c['Transferable']} transferable, {c['Gap']} gaps"
        match = match if r.extraction else ""
        print(f"- {r.verdict}{reason}: {row.company}, {row.role} ({row.city or row.country})"
              f"{match}{flags}{bottom}")
    return 0


def _rows(deps):
    from jobengine.screen.runner import job_row

    if deps.repo is None:
        return []
    return [job_row(pid, v) for pid, v in deps.repo.query_rows()]


if __name__ == "__main__":
    sys.exit(main())
