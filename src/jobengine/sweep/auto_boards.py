"""Job boards learned from job links (13 Nov, your idea: read each company's own job board).

A company's own job board (Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Personio,
Recruitee, Workday ...) gives every open job with its full description and its real apply
link, for free and without AI. Until 13 Nov only the boards of Target Companies were read;
a board seen in any other job link was only a /newcompanies suggestion.

Now the bot remembers such a board (Bot State `sweep.auto_boards`) and reads it in every
/fetch, together with the Target Companies:

- learned from the jobs of every /fetch (a link on the company's board) and from the direct
  job links the bot finds for alert rows (sweep/job_links.py);
- never for a Target Company (its own row is read already), an agency or a company you
  turned down in /newcompanies ("Not interested");
- kept `days` (60) after it was last seen in a link, at most `max` (40), newest first.

Target Companies itself is not changed: it is shared with prod, and a company is added there
only with your tap in /newcompanies. Config: sweep.auto_boards {enabled, max, days}.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

from jobengine.screen.add_link import ATS_OPTIONS, short_company
from jobengine.screen.visa import agency_by_name
from jobengine.sweep.models import Job, TargetCompany
from jobengine.sweep.normalize import canon_company, without_region
from jobengine.sweep.sources import ats

log = logging.getLogger("jobengine.sweep")

KEY = "sweep.auto_boards"  # bot_state: {"items": {company key: {"name", "url", "ats", "last"}}}
DEFAULT_MAX = 40
DEFAULT_DAYS = 60
# Boards the ATS source does not read in a /fetch, or that are one search page, not a board.
NOT_LEARNED = ("amazon", "avature", "eightfold")


def settings(s: Any) -> dict[str, Any]:
    return dict((getattr(s, "sweep", None) or {}).get("auto_boards") or {})


def enabled(s: Any) -> bool:
    return bool(settings(s).get("enabled", True))


def company_key(name: str | None) -> str:
    return canon_company(without_region(name or ""))


def _items(state: Any) -> dict[str, dict[str, Any]]:
    value = (state.get(KEY) if state is not None else None) or {}
    items = value.get("items") or {}
    return {k: dict(v) for k, v in items.items() if isinstance(v, dict) and v.get("url")}


def board_of(url: str | None) -> ats.Board | None:
    """The readable job board a link is on, or None."""
    board = ats.board_from_url(url or "")
    if board is None or board.ats in NOT_LEARNED or board.ats not in ats.SUPPORTED:
        return None
    return board


def learn(state: Any, found: Iterable[tuple[str, str]], targets: list[TargetCompany],
          config: Any, skip: Iterable[str], today: date, s: Any = None) -> list[str]:
    """Remember the boards of (company, job link) pairs; the names of the new ones."""
    if state is None:
        return []
    known = {company_key(c.name) for c in targets}
    turned_down = set(skip)
    items = _items(state)
    new: list[str] = []
    for company, url in found:
        key = company_key(company)
        board = board_of(url)
        if (board is None or not key or key in ("unknown", "confidential") or key in known
                or key in turned_down
                or (config is not None and agency_by_name(company, config))):
            continue
        old = items.get(key)
        if old is None:
            name = short_company(without_region(company).strip()) or company
            items[key] = {"name": name, "url": url, "ats": board.ats,
                          "last": today.isoformat()}
            new.append(name)
        else:
            old["last"] = today.isoformat()
    if new or found:
        _save(state, items, today, s)
    if new:
        log.info("job boards learned: %s", ", ".join(new))
    return new


def from_jobs(state: Any, jobs: Iterable[Job], targets: list[TargetCompany], config: Any,
              skip: Iterable[str], today: date, s: Any = None) -> list[str]:
    """Learn the boards in this /fetch's job links (the company boards' own jobs excluded)."""
    pairs = [(j.company, j.url) for j in jobs if j.source != ats.SOURCE and j.url]
    return learn(state, pairs, targets, config, skip, today, s)


def _save(state: Any, items: dict[str, dict[str, Any]], today: date, s: Any) -> None:
    cfg = settings(s) if s is not None else {}
    days = int(cfg.get("days", DEFAULT_DAYS))
    most = int(cfg.get("max", DEFAULT_MAX))
    cutoff = (today - timedelta(days=days)).isoformat()
    kept = sorted(((k, v) for k, v in items.items() if str(v.get("last", "")) >= cutoff),
                  key=lambda kv: str(kv[1].get("last", "")), reverse=True)
    state.set(KEY, {"items": dict(kept[:most])})


def companies(state: Any, targets: list[TargetCompany], skip: Iterable[str] = ()
              ) -> list[TargetCompany]:
    """The learned boards as rows for the ATS source (not Target Companies, not turned down;
    a company added to Target Companies since is read from its own row)."""
    known = {company_key(c.name) for c in targets}
    turned_down = set(skip)
    out = []
    for key, item in _items(state).items():
        if key in known or key in turned_down:
            continue
        out.append(TargetCompany(str(item.get("name") or key), str(item["url"]),
                                 ATS_OPTIONS.get(str(item.get("ats")), "Custom"), True, None))
    return out


def board_for(state: Any, company: str) -> ats.Board | None:
    """The learned board of a company (sweep/job_links.py looks there first), or None."""
    item = _items(state).get(company_key(company))
    return board_of(item.get("url")) if item else None
