"""Local-only helper that makes a Gmail and Drive token JSON (spec section 1).

    python -m jobengine.gmail_auth --client-secret <OAuth client json> --account m02|main \\
        --out .secrets/gmail-<account>-token.json

It opens the Google consent screen with exactly gmail.modify and drive.file, prints which
account was authorised and writes an authorised-user JSON (client_id, client_secret,
refresh_token, token_uri, scopes). It refuses to run when APP_ENV is not local. Needs the
optional extra: pip install -e ".[auth]".
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from jobengine.gmail_client import SENDER_SCOPES
from jobengine.settings import load_settings

NOT_LOCAL = "gmail_auth runs only with APP_ENV=local (it opens a browser on your machine)."
NO_EXTRA = 'google-auth-oauthlib is not installed. Run: pip install -e ".[auth]"'
TOKEN_URI = "https://oauth2.googleapis.com/token"


def token_info(creds: Any) -> dict[str, Any]:
    """The authorised-user JSON, without the short-lived access token."""
    return {
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri or TOKEN_URI,
        "scopes": list(creds.scopes or SENDER_SCOPES),
    }


def expected_account(account: str, environ: Mapping[str, str]) -> str:
    s = load_settings("local", environ)
    return s.dev_sender if account == "m02" else s.prod_sender


def default_flow(client_secret: Path, login_hint: str) -> Any:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        raise SystemExit(NO_EXTRA) from None
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SENDER_SCOPES)
    return flow.run_local_server(port=0, login_hint=login_hint, prompt="consent",
                                 access_type="offline")


def authorised_email(creds: Any) -> str:
    from googleapiclient.discovery import build

    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return service.users().getProfile(userId="me").execute().get("emailAddress", "")


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    flow: Callable[[Path, str], Any] = default_flow,
    whoami: Callable[[Any], str] = authorised_email,
) -> int:
    environ = dict(os.environ if environ is None else environ)
    parser = argparse.ArgumentParser(prog="python -m jobengine.gmail_auth")
    parser.add_argument("--client-secret", required=True, type=Path)
    parser.add_argument("--account", required=True, choices=("m02", "main"))
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if environ.get("APP_ENV", "local") != "local":
        print(NOT_LOCAL, file=sys.stderr)
        return 2
    out = args.out or Path(".secrets") / f"gmail-{args.account}-token.json"
    expected = expected_account(args.account, environ)
    creds = flow(args.client_secret, expected)
    missing = [scope for scope in SENDER_SCOPES if scope not in (creds.scopes or [])]
    if missing:
        print(f"Google did not grant: {', '.join(missing)}. Tick every box on the consent "
              "screen and run again.", file=sys.stderr)
        return 1
    who = whoami(creds)
    print(f"Authorised account: {who}")
    if who.casefold() != expected.casefold():
        print(f"Warning: expected {expected} for --account {args.account}.", file=sys.stderr)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(token_info(creds), indent=1) + "\n", encoding="utf-8")
    out.chmod(0o600)
    print(f"Wrote {out}. Put its content (one line) into GMAIL_SENDER_TOKEN_JSON in .env; "
          "never commit it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
