"""CLI: python -m jobengine.mail [--fake] --job <page_id or fixture> [--no-write]

Writes Gmail drafts for the job's contacts (never sends). --no-write prints every rendered
mail and creates or writes nothing. DRY_RUN (the default) also creates nothing."""

from __future__ import annotations

import argparse
import logging
import sys

from jobengine.mail.drafter import create_drafts, fake_deps, real_deps
from jobengine.mail.templates import TemplateError
from jobengine.main import banner
from jobengine.safety import SafetyError, check_startup
from jobengine.settings import get_settings
from jobengine.sweep.fakes import FAKE_TODAY


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobengine.mail")
    parser.add_argument("--fake", action="store_true",
                        help="fixture job, contacts, templates, Drive and Gmail, no network")
    parser.add_argument("--job", required=True, help="Job Opportunities page ID or fixture name")
    parser.add_argument("--no-write", action="store_true",
                        help="print every rendered mail, create and write nothing")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    try:
        s = get_settings()
        check_startup(s)
    except (SafetyError, ValueError) as exc:
        print(f"Job Engine mail startup failed: {exc}", file=sys.stderr)
        return 1
    print(banner(s))
    write = not args.no_write
    try:
        if args.fake:
            deps = fake_deps(s, write=write)
            deps.today = lambda: FAKE_TODAY
        else:
            deps = real_deps(s, write=write)
    except TemplateError as exc:
        print(f"Job Engine mail failed: {exc}", file=sys.stderr)
        return 1
    result = create_drafts(deps, args.job)
    print(result.message)
    if not write:
        for i, mail in enumerate(result.mails, 1):
            print(f"\n----- mail {i} of {len(result.mails)} -----\n{mail.preview()}")
        print("\n--no-write: no draft created, nothing written.")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
