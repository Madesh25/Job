"""ATS job feeds for Target Companies (spec section 3.3): Greenhouse, Lever, SmartRecruiters.

Workday, Custom and Unknown boards are not supported here and are only counted. Each feed
is filtered by title and location before any per-posting detail call.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from jobengine import http
from jobengine.settings import Settings
from jobengine.sweep.models import RawPosting, SourceResult, TargetCompany

SOURCE = "ats"
BOARD = "Company site"
SR_PAGE_SIZE = 100
SR_MAX_PAGES = 10

GREENHOUSE_URL = re.compile(r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/([\w-]+)", re.I)
LEVER_URL = re.compile(r"jobs(\.eu)?\.lever\.co/([\w-]+)", re.I)
SMARTRECRUITERS_URL = re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)", re.I)

# SmartRecruiters reports ISO country codes; the feed states the country explicitly.
SR_COUNTRIES = {"pl": "Poland", "nl": "Netherlands", "ie": "Ireland"}

Getter = Callable[[str, Mapping[str, Any]], Any]
Prefilter = Callable[[str, str], bool]


@dataclass(frozen=True)
class Board:
    ats: str  # greenhouse, lever or smartrecruiters
    token: str
    eu: bool = False


def detect_board(company: TargetCompany, overrides: Mapping[str, Any]) -> Board | None:
    override = overrides.get(company.name)
    if override:
        return Board(
            ats=str(override["ats"]).lower(),
            token=str(override["token"]),
            eu=bool(override.get("eu", False)),
        )
    url = company.careers_url or ""
    if match := GREENHOUSE_URL.search(url):
        token = match.group(1)
        if token.lower() == "embed":  # boards.greenhouse.io/embed/job_board?for=<token>
            token = (parse_qs(urlsplit(url).query).get("for") or [""])[0]
        return Board("greenhouse", token) if token else None
    if match := LEVER_URL.search(url):
        return Board("lever", match.group(2), eu=bool(match.group(1)))
    if match := SMARTRECRUITERS_URL.search(url):
        return Board("smartrecruiters", match.group(1))
    return None


def html_to_text(value: str | None, escaped: bool = False) -> str | None:
    if not value:
        return None
    if escaped:
        value = html.unescape(value)
    text = BeautifulSoup(value, "html.parser").get_text("\n")
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return text or None


def _iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------- Greenhouse


def greenhouse(company: TargetCompany, board: Board, get: Getter, keep: Prefilter):
    url = f"https://boards-api.greenhouse.io/v1/boards/{board.token}/jobs"
    data = get(url, {"content": "true"})
    postings, dropped = [], 0
    for job in data.get("jobs") or []:
        title = job.get("title") or ""
        location = (job.get("location") or {}).get("name") or ""
        if not keep(title, location):
            dropped += 1
            continue
        postings.append(
            RawPosting(
                source=SOURCE,
                board=BOARD,
                title=title,
                company=company.name,
                location_text=location,
                url=job.get("absolute_url") or "",
                posting_id=str(job.get("id")),
                # first_published only. updated_at changes on every edit and is never used.
                posted_date=_iso_date(job.get("first_published")),
                description=html_to_text(job.get("content"), escaped=True),
            )
        )
    return postings, dropped, 0


# ---------------------------------------------------------------- Lever


def _lever_salary(salary: Mapping[str, Any] | None) -> str | None:
    if not salary or salary.get("min") is None:
        return None
    low, high = salary.get("min"), salary.get("max")
    amount = f"{low} - {high}" if high is not None and high != low else f"{low}"
    interval = str(salary.get("interval") or "").replace("-", " ").strip()
    currency = salary.get("currency") or ""
    parts = " ".join(p for p in (amount, currency, interval) if p)
    return f"{parts} (Lever)"


def _lever_description(job: Mapping[str, Any]) -> str | None:
    parts = [job.get("descriptionPlain") or ""]
    for item in job.get("lists") or []:
        parts.append(item.get("text") or "")
        parts.append(html_to_text(item.get("content")) or "")
    parts.append(job.get("additionalPlain") or "")
    text = "\n".join(p.strip() for p in parts if p and p.strip())
    return text or None


def lever(company: TargetCompany, board: Board, get: Getter, keep: Prefilter):
    host = "api.eu.lever.co" if board.eu else "api.lever.co"
    data = get(f"https://{host}/v0/postings/{board.token}", {"mode": "json"})
    postings, dropped = [], 0
    for job in data or []:
        title = job.get("text") or ""
        categories = job.get("categories") or {}
        location = categories.get("location") or ", ".join(categories.get("allLocations") or [])
        if job.get("workplaceType") == "remote":
            location = f"{location} (Remote)".strip()
        if not keep(title, location):
            dropped += 1
            continue
        created = job.get("createdAt")
        posted = (
            datetime.fromtimestamp(created / 1000, tz=UTC).date()
            if isinstance(created, int | float) else None
        )
        postings.append(
            RawPosting(
                source=SOURCE,
                board=BOARD,
                title=title,
                company=company.name,
                location_text=location,
                url=job.get("hostedUrl") or job.get("applyUrl") or "",
                posting_id=str(job.get("id")),
                posted_date=posted,
                salary_text=_lever_salary(job.get("salaryRange")),
                description=_lever_description(job),
            )
        )
    return postings, dropped, 0


# ---------------------------------------------------------------- SmartRecruiters


def _sr_location(location: Mapping[str, Any]) -> str:
    parts = [location.get("city"), location.get("region")]
    code = str(location.get("country") or "").lower()
    parts.append(SR_COUNTRIES.get(code, code.upper() or None))
    text = ", ".join(p for p in parts if p)
    if location.get("remote"):
        text = f"{text} (Remote)".strip()
    return text


def _sr_description(detail: Mapping[str, Any]) -> str | None:
    sections = ((detail.get("jobAd") or {}).get("sections")) or {}
    parts = []
    for name in ("companyDescription", "jobDescription", "qualifications",
                 "additionalInformation"):
        section = sections.get(name) or {}
        text = html_to_text(section.get("text"))
        if text:
            parts.append(f"{section.get('title') or name}\n{text}")
    return "\n\n".join(parts) or None


def smartrecruiters(
    company: TargetCompany, board: Board, get: Getter, keep: Prefilter, detail_budget: int
):
    base = f"https://api.smartrecruiters.com/v1/companies/{board.token}/postings"
    kept, dropped = [], 0
    offset = 0
    for _ in range(SR_MAX_PAGES):
        data = get(base, {"limit": SR_PAGE_SIZE, "offset": offset})
        content = data.get("content") or []
        for job in content:
            title = job.get("name") or ""
            location = _sr_location(job.get("location") or {})
            if keep(title, location):
                kept.append((job, location))
            else:
                dropped += 1
        offset += len(content)
        if not content or offset >= int(data.get("totalFound") or 0):
            break

    postings, detail_calls = [], 0
    for job, location in kept:
        detail: Mapping[str, Any] = {}
        if detail_calls < detail_budget:
            detail_calls += 1
            detail = get(f"{base}/{job['id']}", {}) or {}
        url = detail.get("postingUrl") or f"https://jobs.smartrecruiters.com/{board.token}/{job['id']}"
        postings.append(
            RawPosting(
                source=SOURCE,
                board=BOARD,
                title=job.get("name") or "",
                company=company.name,
                location_text=location,
                url=url,
                posting_id=str(job.get("id")),
                posted_date=_iso_date(job.get("releasedDate")),
                description=_sr_description(detail) if detail else None,
            )
        )
    return postings, dropped, detail_calls


# ---------------------------------------------------------------- source


def fetch(
    s: Settings,
    companies: list[TargetCompany],
    keep: Prefilter,
    get: Getter | None = None,
) -> SourceResult:
    """ATS source over the active Target Companies. `get` is the fake."""
    result = SourceResult(name=SOURCE)
    if get is None:

        def get(url: str, params: Mapping[str, Any]) -> Any:
            return http.get_json(url, params=params or None, s=s)

    overrides = s.sweep.get("ats_boards") or {}
    detail_budget = int((s.sweep.get("ats") or {}).get("max_detail_calls", 20))
    dropped_total = 0
    for company in companies:
        if not company.active:
            continue
        board = detect_board(company, overrides)
        if board is None or board.ats not in ("greenhouse", "lever", "smartrecruiters"):
            result.not_supported.append(company.name)
            continue
        try:
            if board.ats == "greenhouse":
                postings, dropped, _ = greenhouse(company, board, get, keep)
            elif board.ats == "lever":
                postings, dropped, _ = lever(company, board, get, keep)
            else:
                postings, dropped, used = smartrecruiters(company, board, get, keep, detail_budget)
                detail_budget -= used
        except http.HttpError as exc:
            result.notes.append(f"ats {company.name}: {exc}")
            continue
        result.postings.extend(postings)
        dropped_total += dropped
    if dropped_total:
        result.notes.append(
            f"ats: {dropped_total} postings outside title or location scope were not fetched"
        )
    return result
