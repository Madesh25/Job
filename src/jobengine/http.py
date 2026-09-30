"""The one place that makes HTTP requests for the sweep (spec section 8).

Every API request is checked by safety.assert_fetch_allowed first: https only, host on
safety.allowed_hosts, and never a linkedin host. Job pages (get_page) go through
safety.assert_page_fetch_allowed instead: any public https host, never linkedin, a plain GET
without cookies or login, every redirect checked again and a size limit.
Query strings are never logged or put in error messages because they can carry API keys
(Adzuna app_key).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from jobengine.safety import SafetyError, assert_fetch_allowed, assert_page_fetch_allowed
from jobengine.settings import Settings, get_settings

log = logging.getLogger("jobengine.http")

# httpx and httpcore log every request URL, query string included, at INFO/DEBUG.
# The Adzuna key travels in the query string, so their logs are kept at WARNING and above.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

TIMEOUT_SECONDS = 20
MAX_429_RETRIES = 3
# A short network hiccup (home Wi-Fi, a busy API) should not fail a whole resume build or
# sweep. Connection errors happen before the request reaches the server, so they are retried
# for every method; a read timeout or a 502/503/504 only for GET (a POST or PATCH may already
# have been applied, and repeating it could create a row twice).
NETWORK_WAITS = (2.0, 5.0)  # seconds before the 2nd and 3rd attempt
CONNECT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
GET_ERRORS = (httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError)
GET_RETRY_STATUS = (502, 503, 504)
PAGE_TIMEOUT_SECONDS = 12
PAGE_MAX_BYTES = 3_000_000
PAGE_MAX_REDIRECTS = 5
PAGE_USER_AGENT = "Mozilla/5.0 (compatible; JobEngine/1.0; personal job search)"
PAGE_TYPES = ("text/html", "application/xhtml+xml", "text/plain")

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
    path = parts.path
    if (parts.hostname or "").endswith("jooble.org") and path.startswith("/api/"):
        path = "/api/(key hidden)"  # the Jooble key is the last part of the path
    return f"{parts.scheme}://{parts.hostname}{path}"


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


def _send(
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | list[tuple[str, Any]] | None = None,
    headers: Mapping[str, str] | None = None,
    json: Any = None,
    s: Settings | None = None,
) -> tuple[str, httpx.Response]:
    """Send one request. HTTP 429 is retried up to MAX_429_RETRIES times (Retry-After);
    network hiccups up to twice (NETWORK_WAITS), see the comment on NETWORK_WAITS."""
    s = s or get_settings()
    assert_fetch_allowed(url, s)
    where = f"{method} {safe_url(url)}"
    network_tries = 0
    with httpx.Client(transport=_transport, timeout=TIMEOUT_SECONDS) as client:
        attempt = 0
        while attempt <= MAX_429_RETRIES:
            log.debug("%s", where)
            try:
                resp = client.request(method, url, params=params, headers=headers, json=json)
                resp.read()
            except httpx.HTTPError as exc:
                retryable = isinstance(exc, CONNECT_ERRORS) or (
                    method == "GET" and isinstance(exc, GET_ERRORS))
                if retryable and network_tries < len(NETWORK_WAITS):
                    wait = NETWORK_WAITS[network_tries]
                    network_tries += 1
                    log.info("%s %s, retrying in %.0fs", where, type(exc).__name__, wait)
                    _sleep(wait)
                    continue
                raise HttpError(f"{where} failed: {type(exc).__name__}") from None
            if (method == "GET" and resp.status_code in GET_RETRY_STATUS
                    and network_tries < len(NETWORK_WAITS)):
                wait = NETWORK_WAITS[network_tries]
                network_tries += 1
                log.info("%s HTTP %s, retrying in %.0fs", where, resp.status_code, wait)
                _sleep(wait)
                continue
            if resp.status_code == 429 and attempt < MAX_429_RETRIES:
                wait = _retry_after(resp)
                log.info("%s rate limited, retrying in %.1fs", where, wait)
                _sleep(wait)
                attempt += 1
                continue
            if resp.status_code >= 400:
                detail = _error_detail(resp)
                message = f"{where} failed: HTTP {resp.status_code} {detail}".strip()
                raise HttpError(message, resp.status_code)
            return where, resp
    raise HttpError(f"{where} failed: still rate limited", 429)  # pragma: no cover


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
    where, resp = _send(method, url, params=params, headers=headers, json=json, s=s)
    try:
        return resp.json()
    except ValueError:
        raise HttpError(f"{where} failed: response was not JSON") from None


def get_text(
    url: str,
    params: Mapping[str, Any] | list[tuple[str, Any]] | None = None,
    headers: Mapping[str, str] | None = None,
    s: Settings | None = None,
) -> str:
    """GET a page (HTML or text) and return its body."""
    return _send("GET", url, params=params, headers=headers, s=s)[1].text


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


def get_page(
    url: str, s: Settings | None = None, max_bytes: int = PAGE_MAX_BYTES, *,
    accept_json: bool = False, json_body: Any = None,
) -> tuple[str, str]:
    """Read one public job page: (final URL, text). Plain GET, no cookies, no login.

    Redirects are followed by hand (at most PAGE_MAX_REDIRECTS) so every hop is checked by
    safety.assert_page_fetch_allowed. Bodies over max_bytes and non HTML answers fail.
    Raises HttpError, never SafetyError (a refused hop is just a page that cannot be read).
    `accept_json`: a JSON answer is read too (a job board's public search). `json_body`: the
    request is a POST with that JSON (a public search form); a redirect is then a GET.
    """
    s = s or get_settings()
    types = (*PAGE_TYPES, "application/json") if accept_json else PAGE_TYPES
    accept = "application/json,text/html" if accept_json else "text/html,application/xhtml+xml"
    headers = {"User-Agent": PAGE_USER_AGENT, "Accept": accept}
    method = "GET" if json_body is None else "POST"
    with httpx.Client(transport=_transport, timeout=PAGE_TIMEOUT_SECONDS,
                      follow_redirects=False) as client:
        for _ in range(PAGE_MAX_REDIRECTS + 1):
            try:
                assert_page_fetch_allowed(url, s)
            except SafetyError as exc:
                raise HttpError(f"page not read: {exc}") from None
            where = f"{method} {safe_url(url)}"
            client.cookies.clear()
            body_kw = {"json": json_body} if method == "POST" else {}
            try:
                with client.stream(method, url, headers=headers, **body_kw) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            raise HttpError(f"{where} failed: redirect without a location")
                        url = urljoin(url, location)
                        method = "GET"
                        continue
                    if resp.status_code >= 400:
                        raise HttpError(f"{where} failed: HTTP {resp.status_code}",
                                        resp.status_code)
                    kind = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if kind and kind not in types:
                        raise HttpError(f"{where} failed: not a web page ({kind})")
                    body = bytearray()
                    for chunk in resp.iter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise HttpError(f"{where} failed: page larger than {max_bytes} bytes")
                    charset = resp.charset_encoding or "utf-8"
                    try:
                        return url, bytes(body).decode(charset, errors="replace")
                    except LookupError:  # an unknown charset name
                        return url, bytes(body).decode("utf-8", errors="replace")
            except httpx.HTTPError as exc:
                raise HttpError(f"{where} failed: {type(exc).__name__}") from None
    raise HttpError(f"GET {safe_url(url)} failed: more than {PAGE_MAX_REDIRECTS} redirects")
