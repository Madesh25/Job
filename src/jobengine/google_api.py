"""Google API clients (Gmail, Drive) with a time limit on every request.

googleapiclient's default HTTP client waits for ever: on 30 Oct a dropped Wi-Fi connection
left /fetch hanging on the alert mailbox for 43 minutes, and /end could not stop it. Each
request now gives up after TIMEOUT_SECONDS; the caller's usual error handling takes over.
"""

from __future__ import annotations

from typing import Any

TIMEOUT_SECONDS = 60


def service(api: str, version: str, creds: Any, **kwargs: Any) -> Any:
    """googleapiclient.discovery.build(api, version) for `creds`, with the time limit."""
    import google_auth_httplib2
    import httplib2
    from googleapiclient.discovery import build

    http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=TIMEOUT_SECONDS))
    return build(api, version, http=http, cache_discovery=False, **kwargs)
