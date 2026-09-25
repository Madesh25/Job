"""CLI: python -m jobengine.resume [--fake] --job <page_id or fixture name>
[--correction "..."] [--no-write]"""

from __future__ import annotations

import argparse
import logging
import sys

from jobengine.main import banner
from jobengine.resume.builder import build_resume, fake_deps, real_deps
from jobengine.resume.master import MasterError
from jobengine.safety import SafetyError, check_startup
from jobengine.settings import ROOT_DIR, get_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobengine.resume")
    parser.add_argument("--fake", action="store_true",
                        help="fixture jobs, fake Golden Master (Alex Example), fake LLM and Drive")
    parser.add_argument("--job", required=True, help="Job Opportunities page ID or fixture name")
    parser.add_argument("--correction", help="build the next revision with this correction")
    parser.add_argument("--no-write", action="store_true",
                        help="render to out/ and print the changes, write nothing to Notion")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("fontTools", "weasyprint", "pdfminer"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    try:
        s = get_settings()
        check_startup(s)
    except (SafetyError, ValueError) as exc:
        print(f"Job Engine resume startup failed: {exc}", file=sys.stderr)
        return 1
    print(banner(s))

    write = not args.no_write
    try:
        deps = fake_deps(s, write=write) if args.fake else real_deps(s, write=write)
    except MasterError as exc:
        print(f"Job Engine resume failed: {exc}", file=sys.stderr)
        return 1
    out = build_resume(deps, args.job, correction=args.correction,
                       force=bool(args.correction))
    if not out.ok:
        print(f"{out.status}: {out.message}")
        return 1
    name = f"{(out.filename or 'resume.pdf').removesuffix('.pdf')}_r{out.revision}.pdf"
    path = deps.out_dir / name
    deps.out_dir.mkdir(parents=True, exist_ok=True)
    path.write_bytes(out.pdf or b"")
    print(out.caption)
    print("Changes:")
    for line in out.changes or ["none"]:
        print(f"- {line}")
    shown = path.relative_to(ROOT_DIR) if path.is_relative_to(ROOT_DIR) else path
    print(f"Preview PDF: {shown.as_posix()}")
    if out.log_id:
        print(f"Resume Log row: {out.log_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
