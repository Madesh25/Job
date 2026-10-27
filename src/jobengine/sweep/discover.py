"""Company discovery and the "company site missed it" check (C1, 26 Oct).

Your idea of 23 Oct: several sources find the same jobs, so one catches what another misses.
After each /fetch, from the jobs that passed the scope filters:

- New companies: a company with jobs from the alerts, Adzuna, Jooble, NoFluffJobs or IamExpat
  that is not in Target Companies (not even unticked), not an agency and not one you turned
  down is kept as a suggestion in Bot State (`discover.companies`, the last MAX_DAYS days).
  Companies from the visa sponsor lists come here too (sweep/sponsors.py, 28 Oct).
  /newcompanies shows them one at a time, the ones whose job link shows a job board the bot
  can read (Greenhouse, Lever, ...) first, with "Add to Target Companies" (as /add does, with
  Undo) and "Not interested" (never suggested again, `discover.skip`). Nothing is added
  without your tap: Target Companies is shared with prod.
- Missed by the company site: a job another source found at a Target Company whose job board
  was read in the same /fetch, but that board did not list it (same company, title and
  country). The /fetch summary names them: the Careers URL may point to the wrong board, or
  the reader may stop too early.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlsplit

from jobengine.screen.add_link import ATS_OPTIONS, board_link, short_company
from jobengine.screen.visa import agency_by_name
from jobengine.sweep.crossmatch import match_key
from jobengine.sweep.models import Job, TargetCompany
from jobengine.sweep.normalize import canon_company, without_region
from jobengine.sweep.sources.ats import SOURCE as ATS
from jobengine.sweep.sources.ats import board_from_url

KEY = "discover.companies"  # bot_state: {"items": {key: item}}
SKIP_KEY = "discover.skip"  # bot_state: {"names": [canon name, ...]}
MAX_DAYS = 30  # a suggestion not seen again for this long is dropped
MAX_ITEMS = 60
MAX_ROLES = 5
MAX_MISSED = 10
# Job boards and aggregators: their links say nothing about the company's own site.
BOARD_HOSTS = ("linkedin.", "adzuna.", "jooble.", "nofluffjobs.", "justjoin.", "pracuj.",
               "irishjobs.", "jobs.ie", "iamexpat.", "indeed.", "glassdoor.", "theprotocol.",
               "bulldogjob.", "solid.jobs", "rocketjobs.", "nationalevacaturebank.",
               "monsterboard.", "welcometothejungle.", "jobsireland.", "europa.eu",
               "google.", "lnkd.in")


def company_key(name: str | None) -> str:
    return canon_company(without_region(name or ""))


def ref(key: str) -> str:
    """A short button id for a company (Telegram allows 64 bytes of callback data)."""
    return hashlib.sha1(key.encode()).hexdigest()[:10]


def _board_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return not host or any(part in host for part in BOARD_HOSTS)


def site_of(urls: Iterable[str]) -> tuple[str, str]:
    """(Careers URL, ATS platform) from the job links: a readable job board first, then the
    company's own site; ("", "Unknown") when every link is a job board or aggregator (the next
    /fetch then looks for the board by the company's name)."""
    urls = [u for u in urls if u]
    for url in urls:
        board = board_from_url(url)
        if board is not None and board_link(board):
            return board_link(board), ATS_OPTIONS.get(board.ats, "Custom")
    for url in urls:
        if not _board_host(url):
            parts = urlsplit(url)
            return f"{parts.scheme}://{parts.netloc}/", "Unknown"
    return "", "Unknown"


@dataclass
class Found:
    new: dict[str, dict[str, Any]] = field(default_factory=dict)  # key -> item
    missed: list[str] = field(default_factory=list)  # "Company, Role (Board)"


def find(seen: list[Job], companies: list[TargetCompany], read: Iterable[str], config: Any,
         skip: Iterable[str] = (), today: date | None = None) -> Found:
    """New companies and jobs the company's own board missed, from one sweep's jobs."""
    known = {company_key(c.name) for c in companies}
    read_keys = {company_key(n) for n in read}
    turned_down = set(skip)
    on_boards = {match_key(j.company, j.role, j.country, j.city) for j in seen
                 if j.source == ATS}
    found = Found()
    day = (today or date.today()).isoformat()
    for job in seen:
        if job.source == ATS:
            continue
        key = company_key(job.company)
        if not key or key in ("unknown", "confidential"):
            continue
        if key in known:
            label = f"{job.company}, {job.role} ({job.board})"
            if (key in read_keys and label not in found.missed
                    and match_key(job.company, job.role, job.country, job.city) not in on_boards):
                found.missed.append(label)
            continue
        if key in turned_down or agency_by_name(job.company, config):
            continue
        name = short_company(without_region(job.company).strip())
        item = found.new.setdefault(key, {"name": name,
                                          "roles": [], "countries": [], "urls": [],
                                          "boards": [], "first": day, "last": day})
        if job.role not in item["roles"]:
            item["roles"].append(job.role)
        for name, value in (("countries", job.country), ("urls", job.url),
                            ("boards", job.board)):
            if value and value not in item[name]:
                item[name].append(value)
    return found


def record(state: Any, found: Found, today: date) -> int:
    """Merge this sweep's new companies into Bot State; returns how many are new there."""
    if state is None or not found.new:
        return 0
    items: dict[str, dict[str, Any]] = dict(((state.get(KEY) or {}).get("items")) or {})
    added = 0
    for key, item in found.new.items():
        old = items.get(key)
        if old is None:
            items[key] = item
            added += 1
            continue
        for name in ("roles", "countries", "urls", "boards"):
            old[name] = list(dict.fromkeys([*old.get(name, []), *item[name]]))
        old["last"] = item["last"]
        if item.get("sponsor"):  # found on a visa sponsor list (sweep/sponsors.py)
            old["sponsor"] = item["sponsor"]
    cutoff = (today - timedelta(days=MAX_DAYS)).isoformat()
    kept = {k: v for k, v in items.items() if v.get("last", "") >= cutoff}
    newest = sorted(kept.items(), key=lambda kv: kv[1].get("last", ""), reverse=True)
    state.set(KEY, {"items": dict(newest[:MAX_ITEMS])})
    return added


def skipped(state: Any) -> list[str]:
    return list(((state.get(SKIP_KEY) if state is not None else None) or {}).get("names") or [])


def suggestions(state: Any, companies: list[TargetCompany]) -> list[tuple[str, dict[str, Any]]]:
    """The open suggestions, best first: a readable job board, then more roles, then newest."""
    if state is None:
        return []
    known = {company_key(c.name) for c in companies}
    turned_down = set(skipped(state))
    items = ((state.get(KEY) or {}).get("items")) or {}
    open_items = [(k, v) for k, v in items.items() if k not in known and k not in turned_down]

    def order(kv: tuple[str, dict[str, Any]]) -> tuple[int, int, int]:
        _url, platform = site_of(kv[1].get("urls") or [])
        readable = platform not in ("Unknown", "Custom")
        sponsor = bool(kv[1].get("sponsor"))  # on a visa sponsor list: they hire from abroad
        return (0 if readable else 1, 0 if sponsor else 1, -len(kv[1].get("roles") or []))

    open_items.sort(key=lambda kv: kv[1].get("last", ""), reverse=True)  # newest first, then
    return sorted(open_items, key=order)  # stable: newest stays first within the same order


def find_ref(state: Any, short: str) -> tuple[str, dict[str, Any]] | None:
    if state is None:
        return None
    items = ((state.get(KEY) or {}).get("items")) or {}
    return next(((k, v) for k, v in items.items() if ref(k) == short), None)


def drop(state: Any, key: str, never_again: bool = False) -> None:
    """Remove a suggestion (added or turned down); "Not interested" also remembers it."""
    if state is None:
        return
    items = dict(((state.get(KEY) or {}).get("items")) or {})
    items.pop(key, None)
    state.set(KEY, {"items": items})
    if never_again:
        names = skipped(state)
        if key not in names:
            state.set(SKIP_KEY, {"names": [*names, key]})
