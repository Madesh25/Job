"""Adzuna search API (spec section 3.2). Poland and the Netherlands only.

Salaries are stored only when Adzuna does not mark them as predicted. Adzuna results carry
no currency field; each country endpoint reports salaries in that country's currency, which
config sweep.adzuna.currency lists.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

from jobengine import http
from jobengine.settings import Settings
from jobengine.sweep.models import RawPosting, SourceResult

SOURCE = "adzuna"
BOARD = "Adzuna"
SEARCH_URL = "https://api.adzuna.com/v1/api/jobs/{cc}/search/{page}"

Getter = Callable[[str, Mapping[str, Any]], Any]


def _strip_tags(text: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def _number(value: Any) -> str:
    return str(int(round(float(value))))


def salary_text(result: Mapping[str, Any], currency: str) -> str | None:
    """"{min} - {max} {currency} (Adzuna)", or None when predicted or missing."""
    if str(result.get("salary_is_predicted", "0")) == "1":
        return None
    low, high = result.get("salary_min"), result.get("salary_max")
    if low is None and high is None:
        return None
    if low is not None and high is not None and float(low) != float(high):
        amount = f"{_number(low)} - {_number(high)}"
    else:
        amount = _number(low if low is not None else high)
    return f"{amount} {currency} (Adzuna)".strip()


def _posted(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def to_posting(result: Mapping[str, Any], currency: str) -> RawPosting:
    location = result.get("location") or {}
    description = _strip_tags(result.get("description"))
    return RawPosting(
        source=SOURCE,
        board=BOARD,
        title=_strip_tags(result.get("title")),
        company=((result.get("company") or {}).get("display_name") or "(unknown)").strip(),
        location_text=location.get("display_name") or "",
        location_area=tuple(location.get("area") or ()),
        url=result.get("redirect_url") or "",
        posting_id=str(result.get("id")),
        posted_date=_posted(result.get("created")),
        salary_text=salary_text(result, currency),
        description=description or None,
        description_is_snippet=True,
    )


def fetch(s: Settings, get: Getter | None = None) -> SourceResult:
    """Adzuna source. `get` is the fake; the default goes through jobengine.http."""
    cfg = s.sweep.get("adzuna") or {}
    result = SourceResult(name=SOURCE)
    if get is None:
        if not s.adzuna_app_id or not s.adzuna_app_key:
            result.skipped_reason = "adzuna skipped: ADZUNA_APP_ID or ADZUNA_APP_KEY missing"
            return result

        def get(url: str, params: Mapping[str, Any]) -> Any:
            return http.get_json(url, params=params, s=s)

    per_page = int(cfg.get("results_per_page", 50))
    max_calls = int(cfg.get("max_calls_per_run", 6))
    calls = 0
    limited = False
    for cc in cfg.get("countries") or []:
        currency = (cfg.get("currency") or {}).get(cc, "")
        page = 1
        more = True
        while more:
            if calls >= max_calls:
                limited = True
                break
            params = {
                "app_id": s.adzuna_app_id or "",
                "app_key": s.adzuna_app_key or "",
                "results_per_page": per_page,
                "max_days_old": int(cfg.get("max_days_old", 3)),
                "what_or": cfg.get("what_or", ""),
                "content-type": "application/json",
            }
            calls += 1
            try:
                data = get(SEARCH_URL.format(cc=cc, page=page), params)
            except http.HttpError as exc:
                result.notes.append(f"adzuna {cc}: {exc}")
                break
            results = data.get("results") or []
            result.postings.extend(to_posting(item, currency) for item in results)
            more = len(results) >= per_page
            page += 1
    if limited:
        result.notes.append(f"adzuna: stopped at the limit of {max_calls} calls")
    return result
