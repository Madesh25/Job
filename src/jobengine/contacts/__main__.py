"""CLI: python -m jobengine.contacts [--fake] --job <page_id or fixture> [--no-write]
       python -m jobengine.contacts --probe apollo|hunter|snov --domain <domain>"""

from __future__ import annotations

import argparse
import logging
import sys

from jobengine import http
from jobengine.bot_state import FakeBotState
from jobengine.contacts.finder import fake_deps, find_contacts, real_deps
from jobengine.contacts.probe import PROVIDERS, run_probe
from jobengine.main import banner
from jobengine.safety import SafetyError, check_startup
from jobengine.settings import get_settings
from jobengine.sweep.fakes import FAKE_TODAY


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobengine.contacts")
    parser.add_argument("--fake", action="store_true",
                        help="fixture jobs, Contacts cache and provider answers, no network")
    parser.add_argument("--job", help="Job Opportunities page ID or fixture name")
    parser.add_argument("--no-write", action="store_true", help="show the result, write nothing")
    parser.add_argument("--probe", choices=sorted(PROVIDERS),
                        help="one real search call to check API access (prod, DRY_RUN=false)")
    parser.add_argument("--domain", help="company email domain for --probe")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    try:
        s = get_settings()
        check_startup(s)
    except (SafetyError, ValueError) as exc:
        print(f"Job Engine contacts startup failed: {exc}", file=sys.stderr)
        return 1
    print(banner(s))

    if args.probe:
        if not args.domain:
            parser.error("--probe needs --domain")
        try:
            print(run_probe(args.probe, args.domain, s))
        except PermissionError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except http.HttpError as exc:
            print(f"{args.probe} probe failed: {exc}", file=sys.stderr)
            return 1
        return 0

    if not args.job:
        parser.error("--job is required (or use --probe)")
    write = not args.no_write
    try:
        if args.fake:
            deps = fake_deps(s, write=write)
            deps.today = lambda: FAKE_TODAY
        else:
            deps = real_deps(s, FakeBotState(), write=write)
    except ValueError as exc:
        print(f"Job Engine contacts failed: {exc}", file=sys.stderr)
        return 1
    result = find_contacts(deps, args.job)
    print(result.message)
    if not write:
        print("--no-write: nothing was written.")
    return 0 if result.status in ("done", "waiting_domain") else 1


if __name__ == "__main__":
    sys.exit(main())
