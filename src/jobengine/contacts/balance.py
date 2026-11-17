"""The real credits left, asked from the providers themselves (22 Oct, Phase 11 O-07).

On the PC the Config counters are only simulated, and Snov answered "out of credits" while
the bot showed 3 of 50 used: the bot's own count does not match what the providers charge.
These calls are free (they spend no credit) and run only when look-ups are real.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from jobengine import http
from jobengine.contacts.credits import account_label
from jobengine.contacts.providers import snov, tomba
from jobengine.contacts.providers.base import ProviderDeps, Request
from jobengine.settings import Settings

HUNTER_ACCOUNT = "https://api.hunter.io/v2/account"
SNOV_BALANCE = "https://api.snov.io/v1/get-balance"
PROSPEO_ACCOUNT = "https://api.prospeo.io/account-information"
# Tomba's account call (tomba-io/python, tomba/services/account.py: GET /v1/me).
TOMBA_ACCOUNT = "https://api.tomba.io/v1/me"


def _find(data: Any, *names: str) -> Any:
    """The first value of one of these keys, at any depth."""
    if isinstance(data, Mapping):
        for name in names:
            if name in data and data[name] not in (None, ""):
                return data[name]
        for value in data.values():
            found = _find(value, *names)
            if found is not None:
                return found
    return None


def hunter(s: Settings, request: Request) -> str:
    data = request("GET", HUNTER_ACCOUNT, params={"api_key": s.hunter_api_key}) or {}
    searches = ((data.get("data") or {}).get("requests") or {}).get("searches") or {}
    used, available = searches.get("used"), searches.get("available")
    if not isinstance(used, int) or not isinstance(available, int):
        return "Hunter did not say how many searches are left"
    return f"{max(0, available - used)} of {available} searches left this month"


def snov_left(s: Settings, request: Request) -> str:
    tok = snov.token(ProviderDeps(s=s, request=request, search_titles={}))
    auth = {"Authorization": f"Bearer {tok}"}
    data = request("GET", SNOV_BALANCE, headers=auth) or {}
    balance = _find(data, "balance")
    resets = _find(data, "limit_resets_in")
    if balance is None:
        return "Snov did not say how many credits are left"
    text = f"{float(balance):g} credits left"
    return text + (f" (new credits in {resets} days)" if resets is not None else "")


def prospeo_left(s: Settings, request: Request) -> str:
    data = request("POST", PROSPEO_ACCOUNT, headers={"X-KEY": s.prospeo_api_key or "",
                                                      "Content-Type": "application/json"},
                   json_body={}) or {}
    left = _find(data, "remaining_credits", "credits_remaining")
    if left is None:
        return "Prospeo did not say how many credits are left"
    renewal = _find(data, "next_quota_renewal_days")
    text = f"{left} credits left"
    return text + (f" (new credits in {renewal} days)" if renewal is not None else "")


def tomba_left(s: Settings, request: Request) -> str:
    """Domain searches left. Tomba's answer was not seen yet (23 Oct): when it has no
    used/available pair, the line still shows that the key works."""
    data = request("GET", TOMBA_ACCOUNT,
                   headers=tomba.headers(s.tomba_api_key or "", s.tomba_api_secret or "")) or {}
    pool = _find(data, "domains", "searches")
    used = pool.get("used") if isinstance(pool, Mapping) else None
    available = pool.get("available") if isinstance(pool, Mapping) else None
    if not isinstance(used, int) or not isinstance(available, int):
        return "the key works; Tomba did not say how many searches are left"
    return f"{max(0, available - used)} of {available} searches left this month"


def prospeo_account(s: Settings, request: Request) -> tuple[int | None, Any]:
    """(credits left, days until new credits) for the leftover run."""
    data = request("POST", PROSPEO_ACCOUNT, headers={"X-KEY": s.prospeo_api_key or "",
                                                      "Content-Type": "application/json"},
                   json_body={}) or {}
    left = _find(data, "remaining_credits", "credits_remaining")
    return (int(left) if isinstance(left, int | float) else None,
            _find(data, "next_quota_renewal_days"))


def hunter_account(s: Settings, request: Request) -> tuple[int | None, Any]:
    """(credits left, reset date) for the leftover run. Hunter counts credits (23 Oct:
    hunter.io/usage shows "30 remaining (20 used of 50)"); older answers count searches."""
    data = request("GET", HUNTER_ACCOUNT, params={"api_key": s.hunter_api_key}) or {}
    requests = (data.get("data") or {}).get("requests") or {}
    pool = requests.get("credits") or requests.get("searches") or {}
    used, available = pool.get("used"), pool.get("available")
    left = available - used if isinstance(used, int) and isinstance(available, int) else None
    return left, _find(data, "reset_date")


CHECKS: dict[str, tuple[str, Callable[[Settings], Any], Callable[[Settings, Request], str]]] = {
    "hunter": ("Hunter", lambda s: s.hunter_api_key, hunter),
    "snov": ("Snov", lambda s: s.snov_client_id and s.snov_client_secret, snov_left),
    "prospeo": ("Prospeo", lambda s: s.prospeo_api_key, prospeo_left),
    "tomba": ("Tomba", lambda s: s.tomba_api_key and s.tomba_api_secret, tomba_left),
}


def lines(s: Settings, request_for: Callable[[str], Request]) -> list[str]:
    """One line per account with a key (second accounts too, 23 Oct): what the provider
    itself says is left."""
    from jobengine.contacts.finder import accounts

    out = []
    for name, (label, has_key, check) in CHECKS.items():
        for account, settings in accounts(name, s):
            if not has_key(settings):
                continue  # 23 Oct: a second account is asked even without a first key
            shown = account_label(account)
            try:
                out.append(f"{shown}: {check(settings, request_for(name))} (from {label} "
                           "itself)")
            except (http.HttpError, ValueError, TypeError) as exc:
                out.append(f"{shown}: could not ask ({exc})")
    return out
