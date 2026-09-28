"""ATS job feeds for Target Companies (spec section 3.3).

Supported: Greenhouse, Lever, SmartRecruiters, Workday and Amazon (amazon.jobs). The board
is found from the careers URL; when that is the company's own site, the careers page is read
once a week (http.get_page) and the ATS it links to is used (cached in bot_state
"ats.detected"). Companies whose board still cannot be found are only counted. Each feed is
filtered by title and location before any per-posting detail call.
"""

from __future__ import annotations

import html
import logging
import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from jobengine import http
from jobengine.settings import Settings
from jobengine.sweep.models import RawPosting, SourceResult, TargetCompany

log = logging.getLogger("jobengine.sweep")

SOURCE = "ats"
BOARD = "Company site"
SR_PAGE_SIZE = 100
SR_MAX_PAGES = 10
WD_PAGE_SIZE = 20
SUPPORTED = ("greenhouse", "lever", "smartrecruiters", "workday", "amazon", "avature")
# Searches for boards that are too big to read whole (Workday, Amazon).
DEFAULT_SEARCH_TERMS = ("devops", "site reliability", "sre", "platform engineer",
                        "cloud engineer", "kubernetes")
# bot_state key: company name -> the board found on its careers page, and when.
DETECT_KEY = "ats.detected"
DEFAULT_DETECT_EVERY_DAYS = 7
DEFAULT_MAX_DETECT_PER_RUN = 25

GREENHOUSE_URL = re.compile(r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/([\w-]+)", re.I)
LEVER_URL = re.compile(r"jobs(\.eu)?\.lever\.co/([\w-]+)", re.I)
SMARTRECRUITERS_URL = re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)", re.I)
WORKDAY_URL = re.compile(
    r"([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[a-z]{2}/)?([\w-]+)", re.I
)
AMAZON_URL = re.compile(r"(?:www\.)?amazon\.jobs\b", re.I)
# Avature career sites list jobs on a server-rendered SearchJobs page, for example
# https://apply.deloittece.com/en_US/careers/SearchJobs/?523=[5515]
AVATURE_URL = re.compile(r"^https://[^/\s]+/[a-z]{2}_[A-Z]{2}/[\w-]+/SearchJobs\b", re.I)
PAGE_URL = re.compile(r"(?:https?:)?//[^\s\"'<>\\]+", re.I)
# Path parts that are never a board token.
NOT_TOKENS = {"static", "js", "css", "assets", "embed", "wday", "api", "v1", "images"}

# SmartRecruiters reports ISO country codes; the feed states the country explicitly.
SR_COUNTRIES = {"pl": "Poland", "nl": "Netherlands", "ie": "Ireland"}

Getter = Callable[[str, Mapping[str, Any]], Any]
Poster = Callable[[str, Any], Any]  # url, JSON body -> JSON answer
PageGetter = Callable[[str], tuple[str, str]]  # url -> (final url, html)
Prefilter = Callable[[str, str], bool]


@dataclass(frozen=True)
class Board:
    ats: str  # greenhouse, lever, smartrecruiters, workday or amazon
    token: str  # Workday: the tenant
    eu: bool = False
    host: str = ""  # Workday only: <tenant>.wd3.myworkdayjobs.com
    site: str = ""  # Workday only: the career site name in the URL

    def as_dict(self) -> dict[str, Any]:
        return {"ats": self.ats, "token": self.token, "eu": self.eu, "host": self.host,
                "site": self.site}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Board:
        return cls(ats=str(value["ats"]).lower(), token=str(value.get("token") or ""),
                   eu=bool(value.get("eu", False)), host=str(value.get("host") or ""),
                   site=str(value.get("site") or ""))


def board_from_url(url: str) -> Board | None:
    """The ATS board a URL points to, or None."""
    if match := GREENHOUSE_URL.search(url):
        token = match.group(1)
        if token.lower() == "embed":  # boards.greenhouse.io/embed/job_board?for=<token>
            token = (parse_qs(urlsplit(url).query).get("for") or [""])[0]
        return Board("greenhouse", token) if token and token not in NOT_TOKENS else None
    if match := LEVER_URL.search(url):
        if match.group(2).lower() in NOT_TOKENS:
            return None
        return Board("lever", match.group(2), eu=bool(match.group(1)))
    if match := SMARTRECRUITERS_URL.search(url):
        token = match.group(1)
        return None if token.lower() in NOT_TOKENS else Board("smartrecruiters", token)
    if match := WORKDAY_URL.search(url):
        tenant, shard, site = match.groups()
        if site.lower() in NOT_TOKENS:
            return None
        return Board("workday", tenant.lower(), host=f"{tenant}.{shard}.myworkdayjobs.com".lower(),
                     site=site)
    if AMAZON_URL.search(url):
        return Board("amazon", "amazon")
    if AVATURE_URL.search(url):
        return Board("avature", url)  # the search page itself, with its filters
    return None


def detect_board(company: TargetCompany, overrides: Mapping[str, Any]) -> Board | None:
    override = overrides.get(company.name)
    if override:
        return Board.from_dict(override)
    return board_from_url(company.careers_url or "")


def board_in_page(final_url: str, page: str) -> Board | None:
    """The ATS a careers page redirects to or links to most often, or None."""
    board = board_from_url(final_url)
    if board is not None:
        return board
    found = Counter(
        board for url in PAGE_URL.findall(html.unescape(page))
        if (board := board_from_url(url)) is not None
    )
    return found.most_common(1)[0][0] if found else None


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


# ---------------------------------------------------------------- Workday

MANY_LOCATIONS = re.compile(r"^\s*\d+\s+locations?\s*$", re.I)
WD_POSTED = re.compile(r"posted\s+(today|yesterday|(\d+)\s+days?\s+ago)", re.I)
# Titles are checked alone (for "3 Locations") against every country the sweep knows.
ANY_COUNTRY = ("Poland", "Netherlands", "Ireland")


def workday_posted(text: str | None, today: date) -> date | None:
    """"Posted Today" / "Posted Yesterday" / "Posted 3 Days Ago"; "30+ Days Ago" is None."""
    match = WD_POSTED.search(text or "")
    if not match or "+" in (text or ""):
        return None
    word = match.group(1).lower()
    days = 0 if word == "today" else 1 if word == "yesterday" else int(match.group(2))
    return today - timedelta(days=days)


def _title_ok(keep: Prefilter, title: str) -> bool:
    return any(keep(title, country) for country in ANY_COUNTRY)


def _wd_location(info: Mapping[str, Any]) -> str:
    """The primary location and its country (other locations would confuse the country)."""
    text = info.get("location") or ""
    country = (info.get("country") or {}).get("descriptor")
    if country and country.lower() not in text.lower():
        text = f"{text}, {country}" if text else country
    return text


def workday(
    company: TargetCompany, board: Board, get: Getter, post: Poster, keep: Prefilter,
    detail_budget: int, today: date, terms: tuple[str, ...], max_pages: int,
):
    """Workday career site search (the JSON the site's own page uses). Detail calls give
    the full description, the start date and every location."""
    base = f"https://{board.host}/wday/cxs/{board.token}/{board.site}"
    listed: dict[str, Mapping[str, Any]] = {}
    for term in terms:
        offset = 0
        for _ in range(max_pages):
            body = {"appliedFacets": {}, "limit": WD_PAGE_SIZE, "offset": offset,
                    "searchText": term}
            data = post(f"{base}/jobs", body) or {}
            items = data.get("jobPostings") or []
            for item in items:
                if item.get("externalPath"):
                    listed.setdefault(item["externalPath"], item)
            offset += len(items)
            if not items or offset >= int(data.get("total") or 0):
                break

    postings, dropped, calls = [], 0, 0
    for path, item in listed.items():
        title = item.get("title") or ""
        location = item.get("locationsText") or ""
        many = bool(MANY_LOCATIONS.match(location))
        if not (_title_ok(keep, title) if many else keep(title, location)):
            dropped += 1
            continue
        info: Mapping[str, Any] = {}
        if calls < detail_budget:
            calls += 1
            try:
                info = (get(f"{base}{path}", {}) or {}).get("jobPostingInfo") or {}
            except http.HttpError as exc:
                log.info("workday detail failed for %s: %s", company.name, exc)
        if info:
            location = _wd_location(info) or location
        if many and not (info and keep(title, location)):
            dropped += 1  # where it is stays unknown without the detail
            continue
        postings.append(
            RawPosting(
                source=SOURCE,
                board=BOARD,
                title=info.get("title") or title,
                company=company.name,
                location_text=location,
                url=info.get("externalUrl") or f"https://{board.host}/{board.site}{path}",
                posting_id=f"workday-{board.token}-{path.rsplit('/', 1)[-1]}",
                posted_date=_iso_date(info.get("startDate"))
                or workday_posted(item.get("postedOn"), today),
                description=html_to_text(info.get("jobDescription")),
            )
        )
    return postings, dropped, calls


# ---------------------------------------------------------------- Amazon

AMAZON_SEARCH_URL = "https://www.amazon.jobs/en/search.json"
AMAZON_COUNTRIES = {"IRL": "Ireland", "POL": "Poland", "NLD": "Netherlands"}
AMAZON_PAGE_SIZE = 100


def _amazon_date(value: str | None) -> date | None:
    try:
        return datetime.strptime((value or "").strip(), "%B %d, %Y").date()
    except ValueError:
        return None


def _amazon_description(job: Mapping[str, Any]) -> str | None:
    parts = []
    for key, heading in (("description", "Description"),
                         ("basic_qualifications", "Basic qualifications"),
                         ("preferred_qualifications", "Preferred qualifications")):
        text = html_to_text(job.get(key))
        if text:
            parts.append(f"{heading}\n{text}")
    return "\n\n".join(parts) or None


def amazon(company: TargetCompany, get: Getter, keep: Prefilter, terms: tuple[str, ...]):
    """amazon.jobs search (the JSON its own search page uses), in Ireland, Poland and
    the Netherlands. Every result carries the full description."""
    listed: dict[str, Mapping[str, Any]] = {}
    for term in terms:
        params = {"base_query": term, "country[]": list(AMAZON_COUNTRIES),
                  "result_limit": AMAZON_PAGE_SIZE, "offset": 0, "sort": "recent"}
        data = get(AMAZON_SEARCH_URL, params) or {}
        for job in data.get("jobs") or []:
            key = str(job.get("id_icims") or job.get("id") or "")
            if key:
                listed.setdefault(key, job)
    postings, dropped = [], 0
    for key, job in listed.items():
        title = job.get("title") or ""
        code = str(job.get("country_code") or "").upper()
        location = ", ".join(p for p in (job.get("city"), AMAZON_COUNTRIES.get(code, code)) if p)
        if not keep(title, location):
            dropped += 1
            continue
        path = job.get("job_path") or f"/en/jobs/{key}"
        postings.append(
            RawPosting(
                source=SOURCE,
                board=BOARD,
                title=title,
                company=company.name,
                location_text=location,
                url=f"https://www.amazon.jobs{path}",
                posting_id=f"amazon-{key}",
                posted_date=_amazon_date(job.get("posted_date")),
                description=_amazon_description(job),
            )
        )
    return postings, dropped, 0


# ---------------------------------------------------------------- Avature (list pages)

AVATURE_PAGE_SIZE = 50
AVATURE_MAX_PAGES = 6
AVATURE_JOB = re.compile(r"/JobDetail/(?:[^/?#]*/)?(\d+)")


def _page_url(url: str, offset: int) -> str:
    parts = urlsplit(url)
    query = {k: v for k, v in parse_qs(parts.query, keep_blank_values=True).items()
             if k not in ("jobRecordsPerPage", "jobOffset")}
    query["jobRecordsPerPage"] = [str(AVATURE_PAGE_SIZE)]
    query["jobOffset"] = [str(offset)]
    return urlunsplit(parts._replace(query=urlencode(query, doseq=True)))


def _card_location(anchor: Any, title: str) -> str:
    """The place line next to a job link: "Gdansk, Warsaw - Poland"."""
    node = anchor
    for _ in range(4):
        node = node.parent
        if node is None:
            break
        jobs = {m.group(1) for a in node.find_all("a", href=True)
                if (m := AVATURE_JOB.search(a["href"]))}
        if len(jobs) > 1:  # this block holds other jobs too
            break
        lines = [line.strip() for line in node.get_text("\n").splitlines() if line.strip()]
        if len(lines) > 12:
            break
        for line in lines:
            if line != title and len(line) < 200 and (
                re.search(r"poland|netherlands|ireland|polska|nederland", line, re.I)
            ):
                return line
    return ""


def avature(company: TargetCompany, board: Board, page: PageGetter, keep: Prefilter):
    """Jobs listed on an Avature SearchJobs page (the page's own filters, for example the
    country, are kept). Descriptions are read later from each job page (sweep/fulltext.py)."""
    postings, dropped, seen = [], 0, set()
    for n in range(AVATURE_MAX_PAGES):
        final_url, html_text = page(_page_url(board.token, n * AVATURE_PAGE_SIZE))
        soup = BeautifulSoup(html_text, "html.parser")
        found = 0
        for anchor in soup.find_all("a", href=True):
            href = urljoin(final_url, anchor["href"])
            match = AVATURE_JOB.search(href)
            title = anchor.get_text(" ", strip=True)
            if not match or match.group(1) in seen or not title or len(title) > 150:
                continue
            seen.add(match.group(1))
            found += 1
            location = _card_location(anchor, title) or company.region or ""
            if not keep(title, location):
                dropped += 1
                continue
            postings.append(RawPosting(
                source=SOURCE, board=BOARD, title=title, company=company.name,
                location_text=location, url=href, posting_id=f"avature-{match.group(1)}",
            ))
        if found < AVATURE_PAGE_SIZE:
            break
    return postings, dropped, 0


# ---------------------------------------------------------------- finding the board


# Words dropped from a company name before it is tried as a board name.
NAME_NOISE = {"sp", "z", "o", "oo", "s", "a", "sa", "bv", "b", "v", "nv", "n", "ltd", "limited",
              "inc", "gmbh", "plc", "llc", "group", "the", "ireland", "poland", "netherlands",
              "polska", "nederland", "europe", "emea", "technology", "technologies"}


def name_slugs(name: str) -> list[str]:
    """Board names a company may use: "Acme Cloud (IE)" gives acmecloud and acme-cloud."""
    base = re.sub(r"\(.*?\)", " ", name.lower())
    words = [w for w in re.split(r"[^a-z0-9]+", base) if w and w not in NAME_NOISE]
    if not words:
        return []
    return list(dict.fromkeys(["".join(words), "-".join(words)]))


def _problem(exc: http.HttpError) -> str:
    if exc.status:
        return f"HTTP {exc.status}"
    return "blocked" if "page not read" in str(exc) else "no answer"


class Detector:
    """Finds the job board of companies whose careers URL is their own site.

    1. Read the careers page and use the ATS it redirects to or links to.
    2. Otherwise try the company name on the public Greenhouse, Lever and SmartRecruiters
       APIs (many career sites block robots, but their ATS answers).
    Results, also "nothing found" and the reason, are kept in bot_state for a week. Sites that
    refused us are listed so their job board link can be put in Careers URL."""

    def __init__(self, s: Settings, page: PageGetter | None, get: Getter | None, state: Any,
                 today: date):
        cfg = s.sweep.get("ats") or {}
        self.page = page
        self.get = get
        self.state = state
        self.today = today
        self.every = timedelta(days=int(cfg.get("detect_every_days", DEFAULT_DETECT_EVERY_DAYS)))
        self.left = int(cfg.get("max_detect_per_run", DEFAULT_MAX_DETECT_PER_RUN))
        self.guess_names = bool(cfg.get("guess_by_name", True))
        self.cache: dict[str, Any] = {}
        if state is not None:
            try:
                self.cache = dict(state.get(DETECT_KEY) or {})
            except Exception:  # a missing cache only means pages are read again
                log.exception("could not read %s", DETECT_KEY)
        self.changed = False
        self.found = 0
        self.blocked: list[str] = []  # "Company (HTTP 403)"
        self.no_board: list[str] = []  # page read, no known board linked, name not found

    def _note(self, company: TargetCompany, entry: Mapping[str, Any]) -> None:
        if entry.get("ats"):
            return
        if entry.get("problem"):
            self.blocked.append(f"{company.name} ({entry['problem']})")
        else:
            self.no_board.append(company.name)

    def board(self, company: TargetCompany) -> Board | None:
        entry = self.cache.get(company.name)
        checked = _iso_date(entry.get("checked")) if entry else None
        if entry and checked and self.today - checked < self.every:
            self._note(company, entry)
            return Board.from_dict(entry) if entry.get("ats") else None
        if self.left <= 0:
            return Board.from_dict(entry) if entry and entry.get("ats") else None
        self.left -= 1
        board, problem = None, None
        url = company.careers_url or ""
        if self.page is not None and url.lower().startswith("https://"):
            try:
                final_url, page = self.page(url)
                board = board_in_page(final_url, page)
            except http.HttpError as exc:
                log.info("careers page not read for %s: %s", company.name, exc)
                problem = _problem(exc)
        if board is None and self.guess_names:
            board = self.guess(company)
        entry = {**(board.as_dict() if board else {"ats": None}),
                 "checked": self.today.isoformat()}
        if board is None and problem:
            entry["problem"] = problem
        self.cache[company.name] = entry
        self.changed = True
        if board is not None:
            self.found += 1
        self._note(company, entry)
        return board

    def guess(self, company: TargetCompany) -> Board | None:
        """The company name as a Greenhouse, Lever or SmartRecruiters board, when one exists
        and lists at least one job."""
        if self.get is None:
            return None
        for slug in name_slugs(company.name):
            probes = (
                (Board("greenhouse", slug), f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                 {}),
                (Board("lever", slug), f"https://api.lever.co/v0/postings/{slug}",
                 {"mode": "json", "limit": 1}),
                (Board("lever", slug, eu=True), f"https://api.eu.lever.co/v0/postings/{slug}",
                 {"mode": "json", "limit": 1}),
                (Board("smartrecruiters", slug),
                 f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", {"limit": 1}),
            )
            for board, url, params in probes:
                try:
                    data = self.get(url, params)
                except http.HttpError:
                    continue
                if _has_jobs(data):
                    log.info("board for %s found by name: %s %s", company.name, board.ats, slug)
                    return board
        return None

    def save(self) -> None:
        if self.changed and self.state is not None:
            try:
                self.state.set(DETECT_KEY, self.cache)
            except Exception:  # never fail a sweep over the cache
                log.exception("could not store %s", DETECT_KEY)


def _has_jobs(data: Any) -> bool:
    if isinstance(data, list):  # Lever
        return bool(data)
    if isinstance(data, dict):
        if "jobs" in data:  # Greenhouse
            return bool(data["jobs"])
        if "totalFound" in data:  # SmartRecruiters answers for any name
            return int(data.get("totalFound") or 0) > 0
    return False


# ---------------------------------------------------------------- source


def fetch(
    s: Settings,
    companies: list[TargetCompany],
    keep: Prefilter,
    get: Getter | None = None,
    post: Poster | None = None,
    page: PageGetter | None = None,
    state: Any = None,
    today: date | None = None,
) -> SourceResult:
    """ATS source over the active Target Companies. `get`, `post` and `page` are the fakes;
    `state` (bot_state) caches the boards found on careers pages."""
    result = SourceResult(name=SOURCE)
    if get is None:

        def get(url: str, params: Mapping[str, Any]) -> Any:
            return http.get_json(url, params=params or None, s=s)

    if post is None:

        def post(url: str, body: Any) -> Any:
            return http.post_json(url, json=body, s=s)

    if page is None:

        def page(url: str) -> tuple[str, str]:
            return http.get_page(url, s=s)

    today = today or date.today()
    cfg = s.sweep.get("ats") or {}
    overrides = s.sweep.get("ats_boards") or {}
    detail_budget = int(cfg.get("max_detail_calls", 20))
    wd_budget = int(cfg.get("workday_detail_calls", 40))
    terms = tuple(cfg.get("search_terms") or DEFAULT_SEARCH_TERMS)
    wd_pages = int(cfg.get("workday_pages_per_term", 1))
    detector = Detector(s, page, get, state, today)
    dropped_total = 0
    amazon_done = False
    for company in companies:
        if not company.active:
            continue
        board = detect_board(company, overrides) or detector.board(company)
        if board is None or board.ats not in SUPPORTED:
            result.not_supported.append(company.name)
            continue
        try:
            if board.ats == "greenhouse":
                postings, dropped, _ = greenhouse(company, board, get, keep)
            elif board.ats == "lever":
                postings, dropped, _ = lever(company, board, get, keep)
            elif board.ats == "workday":
                postings, dropped, used = workday(company, board, get, post, keep, wd_budget,
                                                  today, terms, wd_pages)
                wd_budget -= used
            elif board.ats == "avature":
                postings, dropped, _ = avature(company, board, page, keep)
            elif board.ats == "amazon":
                if amazon_done:  # Amazon and AWS share one board: searched once
                    continue
                amazon_done = True
                postings, dropped, _ = amazon(company, get, keep, terms)
            else:
                postings, dropped, used = smartrecruiters(company, board, get, keep, detail_budget)
                detail_budget -= used
        except http.HttpError as exc:
            result.notes.append(f"ats {company.name}: {exc}")
            # A wrong job board link (HTTP 404) shows in the summary, like a blocked site.
            result.blocked.append(f"{company.name} (job board {_problem(exc)})")
            continue
        result.postings.extend(postings)
        dropped_total += dropped
    detector.save()
    if detector.found:
        result.notes.append(f"ats: found the job board of {detector.found} companies "
                            "on their careers page or by their name")
    result.blocked = detector.blocked + result.blocked
    result.no_board = detector.no_board
    if dropped_total:
        result.notes.append(
            f"ats: {dropped_total} postings outside title or location scope were not fetched"
        )
    return result
