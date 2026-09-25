"""Entry point: load settings, run the startup safety check, print a banner."""

from __future__ import annotations

import sys

from jobengine.safety import SafetyError, check_startup
from jobengine.settings import Settings, get_settings


def banner(s: Settings) -> str:
    return (
        f"Job Engine | env={s.app_env} | dry_run={str(s.dry_run).lower()} | "
        f"notion_write={','.join(s.notion_write)} | sender={s.gmail_sender}"
    )


def main() -> int:
    try:
        s = get_settings()
        check_startup(s)
    except (SafetyError, ValueError) as exc:
        print(f"Job Engine startup failed: {exc}", file=sys.stderr)
        return 1
    print(banner(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
