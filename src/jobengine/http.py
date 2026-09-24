"""The one place that makes HTTP requests for the sweep (spec section 8).

Every request is checked by safety.assert_fetch_allowed first: https only, host on
safety.allowed_hosts, and never a linkedin host. Query strings are never logged or put in
error messages because they can carry API keys (Adzuna app_key).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from jobengine.safety import assert_fetch_allowed
from jobengine.settings import Settings, get_settings

log = logging.getLogger("jobengine.http")

TIMEOUT_SECONDS = 20
MAX_429_RETRIES = 3

# Tests replace these with httpx.MockTransport and a no-op sleep.
_transport: httpx.BaseTransport | None = None
_sleep: Callable[[float], None] = time.sleep


class HttpError(Exception):
    """A request failed. The message has the method, host and path, never the query."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def set_transport(transport: httpx.BaseTransport | None) -> None:
    global _transport
    _transport = transport


def safe_url(url: str) -> str:
    """scheme://host/path without the query string, for logs and errors."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname}{parts.path}"


def _retry_after(resp: httpx.Response) -> float:
    try:
        return max(0.0, float(resp.headers.get("Retry-After", "1")))
    except ValueError:
        return 1.0


def _error_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        return str(body.get("message") or body.get("error") or body.get("description") or "")
    return ""


def request_json(
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | list[tuple[str, Any]] | None = None,
    headers: Mapping[str, str] | None = None,
    json: Any = None,
    s: Settings | None = None,
) -> Any:
    """Send one request and return the decoded JSON body.

    HTTP 429 is retried up to MAX_429_RETRIES times, waiting for Retry-After.
    """
    s = s or get_settings()
    assert_fetch_allowed(url, s)
    where = f"{method} {safe_url(url)}"
    with httpx.Client(transport=_transport, timeout=TIMEOUT_SECONDS) as client:
        for attempt in range(MAX_429_RETRIES + 1):
            log.debug("%s", where)
            try:
                resp = client.request(method, url, params=params, headers=headers, json=json)
            except httpx.HTTPError as exc:
                raise HttpError(f"{where} failed: {type(exc).__name__}") from None
            if resp.status_code == 429 and attempt < MAX_429_RETRIES:
                wait = _retry_after(resp)
                log.info("%s rate limited, retrying in %.1fs", where, wait)
                _sleep(wait)
                continue
            if resp.status_code >= 400:
                detail = _error_detail(resp)
                message = f"{where} failed: HTTP {resp.status_code} {detail}".strip()
                raise HttpError(message, resp.status_code)
            try:
                return resp.json()
            except ValueError:
                raise HttpError(f"{where} failed: response was not JSON") from None
    raise HttpError(f"{where} failed: still rate limited", 429)  # pragma: no cover


def get_json(
    url: str,
    params: Mapping[str, Any] | list[tuple[str, Any]] | None = None,
    headers: Mapping[str, str] | None = None,
    s: Settings | None = None,
) -> Any:
    return request_json("GET", url, params=params, headers=headers, s=s)


def post_json(
    url: str,
    json: Any = None,
    params: Mapping[str, Any] | list[tuple[str, Any]] | None = None,
    headers: Mapping[str, str] | None = None,
    s: Settings | None = None,
) -> Any:
    return request_json("POST", url, params=params, headers=headers, json=json, s=s)


def patch_json(
    url: str,
    json: Any = None,
    headers: Mapping[str, str] | None = None,
    s: Settings | None = None,
) -> Any:
    return request_json("PATCH", url, headers=headers, json=json, s=s)
