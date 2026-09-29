"""Lusha (PR 10b, step 1): only a probe of the answer's shape, no lookup yet.

The request comes from Lusha's official MCP server (npm @lusha-org/mcp 1.2.0): base URL
https://api.lusha.com, header `api_key`, `POST /prospecting/contact/search` with
`filters.companies.include.names`, then `POST /prospecting/contact/enrich` with the search's
`requestId`, the `contactIds` and `revealEmails`. That code passes Lusha's answer on as it is,
so it does not show where the people and the revealed emails are in the answer; emails are
never guessed, so the lookup waits until the answer's shape is known.

`python -m jobengine.contacts --probe lusha --domain "<Company Name>"` makes one search (no
reveal) and prints only the field names and value types of the answer: no names, no emails,
no values. Send that output to Claude to build the lookup.
"""

from __future__ import annotations

import re
from typing import Any

from jobengine.contacts.providers.base import ProviderDeps

NAME = "Lusha"
BASE = "https://api.lusha.com"
SEARCH = f"{BASE}/prospecting/contact/search"
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,40}$")
MAX_PATHS = 200


def headers(key: str) -> dict[str, str]:
    return {"api_key": key, "Content-Type": "application/json"}


def search_body(company: str) -> dict[str, Any]:
    return {"pages": {"page": 0, "size": 10},
            "filters": {"companies": {"include": {"names": [company]}}}}


def shape(value: Any, path: str = "", out: list[str] | None = None) -> list[str]:
    """Field paths and value types, never values. Keys that are not plain field names (IDs,
    emails used as keys) become <key>; lists show their first item only."""
    out = [] if out is None else out
    if len(out) >= MAX_PATHS:
        return out
    if isinstance(value, dict):
        if not value:
            out.append(f"{path or '.'}: {{}}")
        for key, item in value.items():
            name = key if isinstance(key, str) and KEY_RE.match(key) else "<key>"
            shape(item, f"{path}.{name}" if path else name, out)
    elif isinstance(value, list):
        if not value:
            out.append(f"{path}[]: empty")
        else:
            shape(value[0], f"{path}[]", out)
    else:
        kind = "null" if value is None else type(value).__name__
        out.append(f"{path}: {kind}")
    return list(dict.fromkeys(out))


def probe(company: str, deps: ProviderDeps) -> tuple[int, list[str]]:
    key = deps.s.lusha_api_key
    if not key:
        raise PermissionError("LUSHA_API_KEY is not set")
    found = deps.request("POST", SEARCH, headers=headers(key), json_body=search_body(company))
    paths = shape(found)
    lists = [len(v) for v in (found.values() if isinstance(found, dict) else [])
             if isinstance(v, list)]
    return (max(lists) if lists else 0), paths
