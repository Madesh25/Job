"""IamExpat (FETCH 7): English-speaking IT jobs in the Netherlands, read from the IT jobs page.

The search box on IamExpat ignores the search word (probe of 5 Oct: every title gave the same
newest 20 jobs), so the reader opens the IT and technology category page instead. That page
carries its jobs as Next.js page data (`self.__next_f.push(...)` chunks, the `initialJobAds`
list): title, company, city, posted date and salary, newest first, about 20 a page. Titles
outside DevOps, SRE, platform and cloud are dropped later by normalize, like every source.
One page a sweep (about 5 new IT jobs a week), robots.txt respected, plain GET, no login.
Built from the probe sample of 5 Oct (out/probes/iamexpat_it-*.html).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from datetime import date, datetime
from typing import Any
from urllib.robotparser import RobotFileParser

from jobengine import http
from jobengine.settings import Settings
from jobengine.sweep.models import RawPosting, SourceResult

log = logging.getLogger("jobengine.sweep")

SOURCE = "iamexpat"
BOARD = "IamExpat"
SITE = "https://www.iamexpat.nl"
IT_PATH = "/career/jobs-netherlands/it-technology-positions"
LIST_URL = SITE + IT_PATH
COUNTRY = "Netherlands"
# The page is large (2.7 MB on 5 Oct, mostly site menus), so it may be read up to 6 MB.
MAX_BYTES = 6_000_000
PAUSE_SECONDS = 1.0
FLIGHT = re.compile(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', re.S)
ADS_KEY = '"initialJobAds":'
# Job links end in the job's id written in base 58 (the short-uuid alphabet).
BASE58 = "123456789abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ"
LINK = re.compile(r'href="(' + re.escape(IT_PATH) + r'/[^"/]+/([1-9A-Za-z]+))"')

Getter = Callable[[str], tuple[str, str]]  # url -> (final url, page text)


def short_id(uuid: str) -> str:
    """The id as it appears at the end of a job link (base 58 of the uuid's number)."""
    try:
        number = int(uuid.replace("-", ""), 16)
    except ValueError:
        return ""
    out = ""
    while number:
        number, rest = divmod(number, 58)
        out = BASE58[rest] + out
    return out


def _flight(text: str) -> str:
    parts = []
    for chunk in FLIGHT.findall(text):
        try:
            parts.append(json.loads(f'"{chunk}"'))
        except ValueError:
            continue
    return "".join(parts)


def ads_from_page(text: str) -> list[dict[str, Any]]:
    """The job list in the IT jobs page, or [] when the page has none (layout changed)."""
    data = _flight(text)
    start = data.find(ADS_KEY)
    if start < 0:
        return []
    try:
        ads, _ = json.JSONDecoder().raw_decode(data, start + len(ADS_KEY))
    except ValueError:
        return []
    return [ad for ad in ads if isinstance(ad, dict)] if isinstance(ads, list) else []


def links_by_id(text: str) -> dict[str, str]:
    """Short id -> full job link, from the job cards on the page."""
    return {short: SITE + path for path, short in LINK.findall(text)}


def _text(value: Any) -> str:
    """Page text, or "" for a reference to another chunk ("$a6") or no value."""
    if not isinstance(value, str) or value.startswith("$"):
        return ""
    return " ".join(value.split())


def _day(value: Any) -> date | None:
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def _salary(value: Any) -> str | None:
    # A salary may be followed by a whole paragraph; the first line is the range.
    first = str(value).strip().splitlines()[0].strip() if _text(value) else ""
    return f"{first} EUR per month (IamExpat)" if first else None


def to_posting(ad: dict[str, Any], links: dict[str, str]) -> RawPosting | None:
    title = _text(ad.get("JobTitle"))
    uuid = str(ad.get("id") or "")
    url = links.get(short_id(uuid)) if uuid else None
    if not title or not url:
        return None
    city = _text((ad.get("Location") or {}).get("Title"))
    place = "Remote" if ad.get("Remote") and not city else city
    description = " ".join(t for t in (_text(ad.get("AboutThisRole")),
                                       _text(ad.get("Requirements"))) if t)
    return RawPosting(
        source=SOURCE,
        board=BOARD,
        title=title,
        company=_text((ad.get("JobProvider") or {}).get("CompanyName")) or "(unknown)",
        location_text=", ".join(p for p in (place, COUNTRY) if p),
        url=url,
        posting_id=uuid,
        posted_date=_day(ad.get("PostedDate")),
        salary_text=_salary(ad.get("Salary")),
        description=description or None,
    )


def _robots(get: Getter) -> RobotFileParser | None:
    try:
        _, text = get(SITE + "/robots.txt")
    except http.HttpError:
        return None
    parser = RobotFileParser()
    parser.parse(text.splitlines())
    return parser


def fetch(s: Settings, get: Getter | None = None,
          pause: Callable[[float], None] = time.sleep) -> SourceResult:
    """IamExpat source. `get` is the fake; the default goes through jobengine.http."""
    cfg = s.sweep.get("iamexpat") or {}
    result = SourceResult(name=SOURCE)
    if not cfg.get("enabled", True):
        result.skipped_reason = "iamexpat skipped: disabled in sweep.iamexpat"
        return result
    if get is None:
        def get(url: str) -> tuple[str, str]:
            return http.get_page(url, s=s, max_bytes=MAX_BYTES)

    robots = _robots(get)
    if robots is not None and not robots.can_fetch(http.PAGE_USER_AGENT, LIST_URL):
        result.notes.append("iamexpat: robots.txt disallows the IT jobs page, skipped")
        return result
    pause(PAUSE_SECONDS)
    try:
        _, text = get(LIST_URL)
    except http.HttpError as exc:
        result.notes.append(f"iamexpat: failed (HTTP {exc.status or '?'})")
        return result
    ads = ads_from_page(text)
    log.info("iamexpat IT jobs page: %d jobs", len(ads))
    if not ads:
        result.notes.append("iamexpat: no jobs found on the page (the page layout may have "
                            "changed)")
        return result
    links = links_by_id(text)
    seen: set[str] = set()
    for ad in ads:
        posting = to_posting(ad, links)
        if posting and posting.posting_id not in seen:
            seen.add(posting.posting_id)
            result.postings.append(posting)
    return result
