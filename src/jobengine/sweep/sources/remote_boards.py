"""Remote job boards (23 Oct): Remotive and We Work Remotely, for remote jobs open in Europe.

Remotive: its public API (https://remotive.com/api/remote-jobs, github.com/remotive-com/
remote-jobs-api). Its rules: at most 4 calls a day and 2 a minute, link back to the job's
Remotive page and name Remotive as the source (Board "Remotive", the row's link is the
Remotive page). Jobs show 24 hours after Remotive posts them. One call per /fetch, and none
when the last one was less than `remotive.min_hours` (6) hours ago.

We Work Remotely: the public RSS feed of one category (weworkremotely.com/remote-job-rss-feed:
free to use with the links pointing back to WWR). Titles read "Company: Role"; the region is
the item's <region> when the feed has one. Board "We Work Remotely".

The place is written as "Remote, <region>": the usual rules keep a job open in Europe, EMEA
or one of your countries (Country Other or that country, City Remote) and drop the rest.
"Worldwide" jobs count as open in Europe only with `worldwide: true`.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

from jobengine import http
from jobengine.settings import Settings
from jobengine.sweep.models import RawPosting, SourceResult
from jobengine.sweep.sources.ats import html_to_text

log = logging.getLogger("jobengine.sweep")

SOURCE = "remote"
REMOTIVE_URL = "https://remotive.com/api/remote-jobs"
REMOTIVE_BOARD = "Remotive"
WWR_URL = "https://weworkremotely.com/categories/{category}.rss"
WWR_BOARD = "We Work Remotely"
LAST_CALL_KEY = "sweep.remotive_last"  # bot_state: {"at": ISO time of the last Remotive call}
DEFAULT_MIN_HOURS = 6
WORLDWIDE = re.compile(r"\b(worldwide|anywhere|global)\b", re.I)

JsonGetter = Callable[[str, dict[str, Any]], Any]  # url, params -> JSON answer
TextGetter = Callable[[str], str]  # url -> body


def region_text(region: str | None, worldwide: bool) -> str:
    """The place as the normalising rules read it: "Remote, Europe", "Remote, Poland"."""
    region = (region or "").strip()
    if WORLDWIDE.search(region):
        region = "Europe" if worldwide else "Worldwide"
    return f"Remote, {region}" if region else "Remote"


def _day(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


def remotive_posting(job: dict[str, Any], worldwide: bool) -> RawPosting | None:
    if not job.get("id") or not job.get("url"):
        return None
    return RawPosting(
        source=SOURCE,
        board=REMOTIVE_BOARD,
        title=(job.get("title") or "").strip(),
        company=(job.get("company_name") or "(unknown)").strip() or "(unknown)",
        location_text=region_text(job.get("candidate_required_location"), worldwide),
        url=job["url"],
        posting_id=f"remotive-{job['id']}",
        posted_date=_day(job.get("publication_date")),
        salary_text=f"{job['salary']} (Remotive)" if job.get("salary") else None,
        description=html_to_text(job.get("description")) or None,
    )


def _called_recently(state: Any, now: datetime, hours: float) -> bool:
    last = (state.get(LAST_CALL_KEY) or {}).get("at") if state is not None else None
    try:
        return bool(last) and now - datetime.fromisoformat(last) < timedelta(hours=hours)
    except (TypeError, ValueError):
        return False


def fetch_remotive(cfg: dict[str, Any], get: JsonGetter, state: Any, now: datetime,
                   worldwide: bool, result: SourceResult) -> None:
    hours = float(cfg.get("min_hours", DEFAULT_MIN_HOURS))
    if _called_recently(state, now, hours):
        result.notes.append(f"remotive: asked less than {hours:g} hours ago, skipped this time "
                            "(its rule: at most 4 calls a day)")
        return
    params = {"category": cfg.get("category", "devops")}
    if state is not None:
        state.set(LAST_CALL_KEY, {"at": now.isoformat(timespec="seconds")})
    try:
        data = get(REMOTIVE_URL, params) or {}
    except http.HttpError as exc:
        result.notes.append(f"remotive: failed (HTTP {exc.status or '?'})")
        return
    jobs = data.get("jobs") or []
    log.info("remotive %s: %d postings", params["category"], len(jobs))
    if not jobs:
        result.notes.append(f"remotive: no jobs in category {params['category']!r} (check "
                            "sweep.remote_boards.remotive.category)")
    for job in jobs:
        posting = remotive_posting(job, worldwide)
        if posting:
            result.postings.append(posting)


def _child(item: ET.Element, name: str) -> str:
    found = item.find(name)
    return (found.text or "").strip() if found is not None else ""


def wwr_postings(xml: str, worldwide: bool) -> list[RawPosting]:
    """The jobs in a We Work Remotely RSS feed ([] when it is not a feed)."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    out = []
    for item in root.iter("item"):
        link = _child(item, "link")
        full = _child(item, "title")
        if not link or not full:
            continue
        company, _, role = full.partition(":")
        if not role:
            company, role = "(unknown)", full
        try:
            posted = parsedate_to_datetime(_child(item, "pubDate")).date()
        except (TypeError, ValueError):
            posted = None
        out.append(RawPosting(
            source=SOURCE,
            board=WWR_BOARD,
            title=role.strip(),
            company=company.strip() or "(unknown)",
            location_text=region_text(_child(item, "region"), worldwide),
            url=link,
            posting_id="wwr-" + (_child(item, "guid") or link).rstrip("/").rsplit("/", 1)[-1],
            posted_date=posted,
            description=html_to_text(_child(item, "description")) or None,
        ))
    return out


def fetch_wwr(cfg: dict[str, Any], get: TextGetter, worldwide: bool,
              result: SourceResult) -> None:
    url = WWR_URL.format(category=cfg.get("category", "remote-devops-sysadmin-jobs"))
    try:
        xml = get(url)
    except http.HttpError as exc:
        result.notes.append(f"we work remotely: failed (HTTP {exc.status or '?'})")
        return
    postings = wwr_postings(xml, worldwide)
    log.info("we work remotely: %d postings", len(postings))
    if not postings:
        result.notes.append("we work remotely: no jobs in the feed (the feed may have changed)")
    result.postings.extend(postings)


def fetch(s: Settings, state: Any = None, get_json: JsonGetter | None = None,
          get_text: TextGetter | None = None, now: datetime | None = None) -> SourceResult:
    """Both boards. `get_json` and `get_text` are the fakes; the defaults go through
    jobengine.http (allowed hosts only)."""
    cfg = s.sweep.get("remote_boards") or {}
    result = SourceResult(name=SOURCE)
    if not cfg.get("enabled", True):
        result.skipped_reason = "remote boards skipped: disabled in sweep.remote_boards"
        return result
    if get_json is None:
        def get_json(url: str, params: dict[str, Any]) -> Any:
            return http.get_json(url, params=params, s=s)
    if get_text is None:
        def get_text(url: str) -> str:
            return http.get_text(url, s=s)
    worldwide = bool(cfg.get("worldwide", False))
    remotive = cfg.get("remotive") or {}
    if remotive.get("enabled", True):
        fetch_remotive(remotive, get_json, state, now or datetime.now(), worldwide, result)
    wwr = cfg.get("weworkremotely") or {}
    if wwr.get("enabled", True):
        fetch_wwr(wwr, get_text, worldwide, result)
    return result
