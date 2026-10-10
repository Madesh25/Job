"""ATS job feeds for Target Companies (spec section 3.3).

Supported: Greenhouse, Lever, SmartRecruiters, Workday, Ashby, Avature and Amazon
(amazon.jobs). The board
is found from the careers URL; when that is the company's own site, the careers page is read
once a week (http.get_page) and the ATS it links to is used (cached in bot_state
"ats.detected"). Companies whose board still cannot be found are only counted. Each feed is
filtered by title and location before any per-posting detail call.
"""

from __future__ import annotations

import html
import json
import logging
import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from jobengine import http
from jobengine.parallel import Budget, run_all
from jobengine.settings import Settings
from jobengine.sweep.models import RawPosting, SourceResult, TargetCompany
from jobengine.sweep.normalize import without_region

log = logging.getLogger("jobengine.sweep")

SOURCE = "ats"
BOARD = "Company site"
SR_PAGE_SIZE = 100
SR_MAX_PAGES = 10
WD_PAGE_SIZE = 20
# A SuccessFactors job feed gives its newest 20 jobs unless asked for more (`rows`); 20
# newest jobs worldwide held 1 or 2 in our countries (HCLTech, Wipro, Volvo on 10 Oct).
SF_ROWS = 200
SUPPORTED = ("greenhouse", "lever", "smartrecruiters", "workday", "amazon", "avature", "ashby",
             "successfactors", "phenom", "oracle")
# Searches for boards that are too big to read whole (Workday, Amazon).
DEFAULT_SEARCH_TERMS = ("devops", "site reliability", "sre", "platform engineer",
                        "cloud engineer", "kubernetes", "infrastructure engineer", "devsecops")
# bot_state key: company name -> the board found on its careers page, and when.
DETECT_KEY = "ats.detected"
DEFAULT_DETECT_EVERY_DAYS = 7
DEFAULT_MAX_DETECT_PER_RUN = 25

GREENHOUSE_URL = re.compile(r"(?:job-)?boards(\.eu)?\.greenhouse\.io/([\w-]+)", re.I)
# Boards hosted in the EU (job-boards.eu.greenhouse.io: IMC on 10 Oct) have their own API
# host. A board's link does not always say where it is hosted (HubSpot's global API answered
# HTTP 404 on 10 Oct), so the other host is asked when the first does not know the board.
GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
GREENHOUSE_EU_API = "https://boards-api.eu.greenhouse.io/v1/boards/{token}/jobs"
LEVER_URL = re.compile(r"jobs(\.eu)?\.lever\.co/([\w-]+)", re.I)
SMARTRECRUITERS_URL = re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)", re.I)
WORKDAY_URL = re.compile(
    r"([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[a-z]{2}/)?([\w-]+)", re.I
)
AMAZON_URL = re.compile(r"(?:www\.)?amazon\.jobs\b", re.I)
ASHBY_URL = re.compile(r"jobs\.ashbyhq\.com/([\w.-]+)", re.I)
# SAP SuccessFactors career sites publish a job feed, for example
# https://careers.capgemini.com/services/rss/job/?locale=en_US&keywords=(devops)
SUCCESSFACTORS_URL = re.compile(r"^(https://[^/\s]+)/services/rss/job/?(?:\?(.*))?$", re.I)
# Phenom career sites: https://careers.allianz.com/global/en/search-results?keywords=devops
PHENOM_URL = re.compile(r"^(https://[^/\s]+/(?:global|[a-z]{2})/[a-z]{2})/search-results\b", re.I)
# Oracle Cloud HCM: https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001,
# also on a company's own domain (Dell: enterpriseplatform.dell.com/.../sites/careers). The
# host must still be allowed (safety.allowed_hosts or allowed_host_suffixes) to be read.
ORACLE_URL = re.compile(
    r"^https://([\w.-]+)/hcmUI/CandidateExperience/[a-z]{2}(?:-[A-Z]{2})?/sites/([\w-]+)",
    re.I)
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
    ats: str  # greenhouse, lever, smartrecruiters, workday, ashby, avature, successfactors,
    # phenom, oracle or amazon
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
        token = match.group(2)
        if token.lower() == "embed":  # boards.greenhouse.io/embed/job_board?for=<token>
            token = (parse_qs(urlsplit(url).query).get("for") or [""])[0]
        if not token or token in NOT_TOKENS:
            return None
        return Board("greenhouse", token, eu=bool(match.group(1)))
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
    if match := ASHBY_URL.search(url):
        token = match.group(1)
        return None if token.lower() in NOT_TOKENS else Board("ashby", token)
    if match := SUCCESSFACTORS_URL.search(url):
        locale = (parse_qs(match.group(2) or "").get("locale") or ["en_US"])[0]
        return Board("successfactors", match.group(1), site=locale)
    if match := PHENOM_URL.search(url):
        return Board("phenom", match.group(1))
    if match := ORACLE_URL.search(url):
        return Board("oracle", match.group(2), host=match.group(1).lower())
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
    urls = [GREENHOUSE_EU_API, GREENHOUSE_API] if board.eu else [GREENHOUSE_API,
                                                                   GREENHOUSE_EU_API]
    for n, url in enumerate(urls):
        try:
            data = get(url.format(token=board.token), {"content": "true"})
            break
        except http.HttpError as exc:
            if n == len(urls) - 1 or exc.status != 404:
                raise
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
    company: TargetCompany, board: Board, get: Getter, keep: Prefilter,
    detail_budget: int | Budget,
):
    budget = detail_budget if isinstance(detail_budget, Budget) else Budget(detail_budget)
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
        if budget.take():
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


# Hub cities named in Workday location filters when a site has no country filter.
HUB_CITIES = ("warsaw", "warszawa", "krakow", "kraków", "wroclaw", "wrocław", "gdansk",
              "gdańsk", "poznan", "poznań", "lodz", "łódź", "katowice", "amsterdam",
              "rotterdam", "utrecht", "eindhoven", "the hague", "den haag", "dublin", "cork",
              "galway", "limerick")


def _facet_values(facets: Any, wanted: Callable[[str, str], bool]) -> dict[str, list[str]]:
    """{facet parameter: [value ids]} of the facet values `wanted(parameter, name)` keeps
    (a facet may sit inside a group)."""
    found: dict[str, list[str]] = {}
    for facet in facets or []:
        if not isinstance(facet, Mapping):
            continue
        param = str(facet.get("facetParameter") or "")
        values = facet.get("values") or []
        ids = [str(v.get("id")) for v in values if isinstance(v, Mapping) and v.get("id")
               and not v.get("facetParameter")
               and wanted(param, str(v.get("descriptor") or "").strip())]
        if ids:
            found.setdefault(param, []).extend(ids)
        for param_inner, inner in _facet_values(
                [v for v in values if isinstance(v, Mapping) and v.get("facetParameter")],
                wanted).items():
            found.setdefault(param_inner, []).extend(inner)
    return found


def _country_facets(facets: Any) -> dict[str, list[str]]:
    """The Workday filter for Poland, the Netherlands and Ireland: the country facet when
    the site has one, else the location facet values in those countries' hub cities
    (Accenture, HPE and Shell had no country facet on 5 Oct)."""
    countries = _facet_values(
        facets, lambda p, name: "country" in p.lower() and name in ANY_COUNTRY)
    if countries:
        return countries

    def in_hub(param: str, name: str) -> bool:
        low = name.lower()
        return "location" in param.lower() and (
            any(c.lower() in low for c in ANY_COUNTRY) or any(c in low for c in HUB_CITIES))

    return _facet_values(facets, in_hub)


def workday(
    company: TargetCompany, board: Board, get: Getter, post: Poster, keep: Prefilter,
    detail_budget: int | Budget, today: date, terms: tuple[str, ...], max_pages: int,
):
    """Workday career site search (the JSON the site's own page uses). Detail calls give
    the full description, the start date and every location.

    Big sites list hundreds of matches from all over the world, so the first page of a
    search held no job in Poland, the Netherlands or Ireland (Accenture, HPE, Shell on
    5 Oct). The site's own country filter is therefore read from the first answer and every
    search is limited to those three countries when the site offers that filter."""
    budget = detail_budget if isinstance(detail_budget, Budget) else Budget(detail_budget)
    base = f"https://{board.host}/wday/cxs/{board.token}/{board.site}"
    listed: dict[str, Mapping[str, Any]] = {}
    failed: list[http.HttpError] = []
    countries: dict[str, list[str]] | None = None
    first: Mapping[str, Any] | None = None  # the unfiltered first answer, reused when no filter
    for term in terms:
        if countries is None:
            try:
                first = post(f"{base}/jobs", {"appliedFacets": {}, "limit": WD_PAGE_SIZE,
                                              "offset": 0, "searchText": term}) or {}
                countries = _country_facets(first.get("facets"))
            except http.HttpError:
                countries = {}  # the term's own search below reports the failure
        offset = 0
        for _ in range(max_pages):
            body = {"appliedFacets": countries or {}, "limit": WD_PAGE_SIZE, "offset": offset,
                    "searchText": term}
            try:
                if first is not None and not countries and offset == 0:
                    data, first = first, None  # the same search was just made
                else:
                    first = None
                    data = post(f"{base}/jobs", body) or {}
            except http.HttpError as exc:
                # One search word the site refuses (Dell answered HTTP 422 on 1 Oct) must
                # not lose the other words' jobs; only all words failing is an error.
                log.info("workday search %r failed for %s: %s", term, company.name, exc)
                failed.append(exc)
                break
            items = data.get("jobPostings") or []
            for item in items:
                if item.get("externalPath"):
                    listed.setdefault(item["externalPath"], item)
            offset += len(items)
            if not items or offset >= int(data.get("total") or 0):
                break
    if failed and len(failed) == len(terms):
        raise failed[-1]

    postings, dropped, calls = [], 0, 0
    for path, item in listed.items():
        title = item.get("title") or ""
        location = item.get("locationsText") or ""
        # "3 Locations", or no place at all (Accenture lists none): only the detail says where.
        many = not location.strip() or bool(MANY_LOCATIONS.match(location))
        if not (_title_ok(keep, title) if many else keep(title, location)):
            dropped += 1
            continue
        info: Mapping[str, Any] = {}
        if budget.take():
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


# ---------------------------------------------------------------- Ashby

ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/{token}"


def _ashby_location(job: Mapping[str, Any]) -> str:
    places = [job.get("location") or ""]
    places += [(extra or {}).get("location") or "" for extra in job.get("secondaryLocations") or []]
    country = (((job.get("address") or {}).get("postalAddress") or {}).get("addressCountry")
               or "")
    text = " / ".join(dict.fromkeys(p for p in places if p))
    if country and country.lower() not in text.lower():
        text = f"{text}, {country}" if text else country
    if job.get("isRemote"):
        text = f"{text} (Remote)".strip()
    return text


def _ashby_salary(job: Mapping[str, Any]) -> str | None:
    summary = (job.get("compensation") or {}).get("compensationTierSummary")
    return f"{summary} (Ashby)" if summary else None


def ashby(company: TargetCompany, board: Board, get: Getter, keep: Prefilter):
    """Ashby's public job board API (the one the hosted board page reads)."""
    data = get(ASHBY_API.format(token=board.token), {"includeCompensation": "true"}) or {}
    postings, dropped = [], 0
    for job in data.get("jobs") or []:
        if job.get("isListed") is False:
            continue
        title = job.get("title") or ""
        location = _ashby_location(job)
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
                url=job.get("jobUrl") or job.get("applyUrl") or "",
                posting_id=f"ashby-{board.token}-{job.get('id')}",
                posted_date=_iso_date(job.get("publishedAt")),
                salary_text=_ashby_salary(job),
                description=job.get("descriptionPlain") or html_to_text(job.get("descriptionHtml")),
            )
        )
    return postings, dropped, 0


# ---------------------------------------------------------------- SuccessFactors

SF_LOCATION = re.compile(r"\(([^()]*)\)\s*$")
SF_COUNTRY_CODES = {"PL": "Poland", "NL": "Netherlands", "IE": "Ireland"}


def _sf_location(text: str) -> str:
    """"Utrecht, NL, 3542 AB" -> "Utrecht, Netherlands" (the feed gives country codes)."""
    parts = [p.strip() for p in text.split(",") if p.strip()]
    out = []
    for part in parts:
        if re.fullmatch(r"[A-Z]{2}", part):
            out.append(SF_COUNTRY_CODES.get(part, part))
        elif not re.search(r"\d", part):
            out.append(part)
    return ", ".join(dict.fromkeys(out))


def feed_items(text: str) -> list[dict[str, str]]:
    """The <item>s of an RSS feed as {tag: text}; [] when it is not a readable feed."""
    try:
        root = ElementTree.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    except ElementTree.ParseError:
        return []
    items = []
    for item in root.iter("item"):
        items.append({child.tag: (child.text or "").strip() for child in item})
    return items


def _sf_date(text: str | None) -> date | None:
    try:
        return datetime.strptime((text or "").strip()[:16], "%a, %d %b %Y").date()
    except ValueError:
        return None


def successfactors(company: TargetCompany, board: Board, page: PageGetter, keep: Prefilter,
                   terms: tuple[str, ...]):
    """A SuccessFactors career site's job feed, one search per term (newest first). The
    title carries the place in brackets: "DevOps Engineer (Utrecht, NL, 3542 AB)"."""
    postings, dropped, seen = [], 0, set()
    for term in terms:
        query = urlencode({"locale": board.site or "en_US", "keywords": f"({term})",
                           "sortColumn": "referencedate", "sortDirection": "desc",
                           "rows": SF_ROWS})
        _, text = page(f"{board.token}/services/rss/job/?{query}")
        for item in feed_items(text):
            link = item.get("link") or item.get("guid") or ""
            raw_title = " ".join((item.get("title") or "").split())
            if not link or not raw_title or link in seen:
                continue
            seen.add(link)
            place = SF_LOCATION.search(raw_title)
            title = SF_LOCATION.sub("", raw_title).strip() if place else raw_title
            location = _sf_location(place.group(1)) if place else ""
            if not keep(title, location):
                dropped += 1
                continue
            job_id = re.search(r"/(\d{5,})/?$", link)
            postings.append(RawPosting(
                source=SOURCE, board=BOARD, title=title, company=company.name,
                location_text=location, url=link,
                posting_id=f"sf-{job_id.group(1) if job_id else link}",
                posted_date=_sf_date(item.get("pubDate")),
                description=html_to_text(item.get("description")),
            ))
    return postings, dropped, 0


# ---------------------------------------------------------------- Phenom

PHENOM_DATA = '"eagerLoadRefineSearch":'
PHENOM_PAGE_SIZE = 10
PHENOM_MAX_PAGES = 2


def phenom_jobs(text: str) -> list[Mapping[str, Any]]:
    """The jobs a Phenom search results page carries in its page data, or []."""
    start = text.find(PHENOM_DATA)
    if start < 0:
        return []
    try:
        data, _ = json.JSONDecoder().raw_decode(text, start + len(PHENOM_DATA))
    except ValueError:
        return []
    jobs = ((data or {}).get("data") or {}).get("jobs") if isinstance(data, dict) else None
    return [j for j in jobs or [] if isinstance(j, Mapping)]


def _phenom_slug(title: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-") or "job"


def phenom(company: TargetCompany, board: Board, page: PageGetter, keep: Prefilter,
           terms: tuple[str, ...]):
    """Jobs on a Phenom career site's search results pages (10 a page, two pages a term)."""
    postings, dropped, seen = [], 0, set()
    for term in terms:
        for n in range(PHENOM_MAX_PAGES):
            query = urlencode({"keywords": term, "from": n * PHENOM_PAGE_SIZE})
            _, text = page(f"{board.token}/search-results?{query}")
            jobs = phenom_jobs(text)
            for job in jobs:
                job_id = str(job.get("jobId") or job.get("reqId") or job.get("jobSeqNo") or "")
                title = str(job.get("title") or "")
                if not job_id or not title or job_id in seen:
                    continue
                seen.add(job_id)
                location = str(job.get("cityStateCountry") or job.get("location") or "")
                if job.get("country") and str(job["country"]) not in location:
                    location = f"{location}, {job['country']}" if location else str(job["country"])
                if not keep(title, location):
                    dropped += 1
                    continue
                postings.append(RawPosting(
                    source=SOURCE, board=BOARD, title=title, company=company.name,
                    location_text=location,
                    url=f"{board.token}/job/{job_id}/{_phenom_slug(title)}",
                    posting_id=f"phenom-{job.get('jobSeqNo') or job_id}",
                    posted_date=_iso_date(job.get("postedDate")),
                    description=job.get("descriptionTeaser") or None,
                ))
            if len(jobs) < PHENOM_PAGE_SIZE:
                break
    return postings, dropped, 0


# ---------------------------------------------------------------- Oracle Cloud HCM

ORACLE_PAGE_SIZE = 25
ORACLE_MAX_PLACES = 6  # cities searched one by one when a site names no country


def oracle(company: TargetCompany, board: Board, get: Getter, keep: Prefilter,
           terms: tuple[str, ...]):
    """Oracle Cloud HCM candidate site search (the public REST call its own page makes),
    newest first, one search per term."""
    api = f"https://{board.host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    params = {"onlyData": "true", "expand": "requisitionList.secondaryLocations"}

    def search(term: str, location_id: str = "") -> Mapping[str, Any]:
        finder = (f'findReqs;siteNumber={board.token},facetsList=LOCATIONS,'
                  f'limit={ORACLE_PAGE_SIZE},keyword="{term}",sortBy=POSTING_DATES_DESC')
        if location_id:
            finder += f",selectedLocationsFacet={location_id}"
        return get(api, {**params, "finder": finder}) or {}

    postings, dropped, seen = [], 0, set()
    # Big sites list jobs from everywhere (JPMorgan: 1 of 128 in Poland, the Netherlands or
    # Ireland on 5 Oct), so each search is made once per country the site's own location
    # filter names; without that filter, once for all places.
    first = search(terms[0]) if terms else {}
    facets = [f for item in first.get("items") or [] for f in item.get("locationsFacet") or []
              if isinstance(f, Mapping) and f.get("Id")]
    country_ids = [str(f["Id"]) for f in facets if str(f.get("Name") or "").strip() in ANY_COUNTRY]
    if not country_ids:  # places named with their country ("Warsaw, Poland")
        country_ids = [str(f["Id"]) for f in facets
                       if any(str(f.get("Name") or "").strip().endswith(f", {c}")
                              for c in ANY_COUNTRY)][:ORACLE_MAX_PLACES]
    if not country_ids and terms:
        # The filter lists only the biggest places (JPMorgan: 40, all in the US on 5 Oct):
        # a search for the country's name brings that country into the list, with its id.
        for country in ANY_COUNTRY:
            answer = search(country)
            country_ids += [str(f["Id"]) for item in answer.get("items") or []
                            for f in item.get("locationsFacet") or []
                            if isinstance(f, Mapping) and f.get("Id")
                            and str(f.get("Name") or "").strip() == country]
    answers = ((search(term, cid) for term in terms for cid in country_ids) if country_ids
               else (first if n == 0 else search(term) for n, term in enumerate(terms)))
    for data in answers:
        for found in data.get("items") or []:
            for job in found.get("requisitionList") or []:
                job_id = str(job.get("Id") or "")
                title = str(job.get("Title") or "")
                if not job_id or not title or job_id in seen:
                    continue
                seen.add(job_id)
                places = [str(job.get("PrimaryLocation") or "")]
                places += [str(x.get("Name") or "") for x in job.get("secondaryLocations") or []
                           if isinstance(x, Mapping)]
                location = " / ".join(dict.fromkeys(p for p in places if p))
                if not keep(title, location):
                    dropped += 1
                    continue
                postings.append(RawPosting(
                    source=SOURCE, board=BOARD, title=title, company=company.name,
                    location_text=location,
                    url=(f"https://{board.host}/hcmUI/CandidateExperience/en/sites/"
                         f"{board.token}/job/{job_id}"),
                    posting_id=f"oracle-{board.host.split('.')[0]}-{job_id}",
                    posted_date=_iso_date(job.get("PostedDate")),
                    description=job.get("ShortDescriptionStr") or None,
                ))
    return postings, dropped, 0


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


def canon_name(name: str) -> str:
    """"Infosys (IE)" -> "infosys": the company without its country suffix, lower case."""
    return " ".join(re.sub(r"\(.*?\)", " ", name).lower().split())


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
    2. Otherwise try the company name on the public Greenhouse, Lever, SmartRecruiters and
       Ashby APIs (many career sites block robots, but their ATS answers).
    A company whose Careers URL changed since is checked again at once.
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

    def _cached(self, company: TargetCompany) -> tuple[bool, Board | None]:
        """(True, board) when the cache or the run budget decides; (False, None) when the
        careers page must be read."""
        entry = self.cache.get(company.name)
        if entry and entry.get("url") != (company.careers_url or ""):
            entry = None  # the Careers URL changed since (or was never noted): check again
        checked = _iso_date(entry.get("checked")) if entry else None
        if entry and checked and self.today - checked < self.every:
            self._note(company, entry)
            return True, Board.from_dict(entry) if entry.get("ats") else None
        if self.left <= 0:
            return True, Board.from_dict(entry) if entry and entry.get("ats") else None
        self.left -= 1
        return False, None

    def _check(self, company: TargetCompany) -> tuple[Board | None, str | None]:
        """Network only (safe on a worker thread): read the careers page, then guess."""
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
        return board, problem

    def _record(self, company: TargetCompany, board: Board | None,
                problem: str | None) -> Board | None:
        entry = {**(board.as_dict() if board else {"ats": None}),
                 "checked": self.today.isoformat(), "url": company.careers_url or ""}
        if board is None and problem:
            entry["problem"] = problem
        self.cache[company.name] = entry
        self.changed = True
        if board is not None:
            self.found += 1
        self._note(company, entry)
        return board

    def board(self, company: TargetCompany) -> Board | None:
        decided, board = self._cached(company)
        if decided:
            return board
        return self._record(company, *self._check(company))

    def resolve(self, companies: list[TargetCompany], workers: int) -> dict[str, Board | None]:
        """board() for many companies: the careers pages are read `workers` at a time; the
        cache, the budget and the lists are updated here, in company order."""
        out: dict[str, Board | None] = {}
        to_check = []
        for company in companies:
            decided, board = self._cached(company)
            if decided:
                out[company.name] = board
            else:
                to_check.append(company)
        for company, found in zip(to_check, run_all(self._check, to_check, workers),
                                  strict=True):
            out[company.name] = self._record(company, *found)
        return out

    def guess(self, company: TargetCompany) -> Board | None:
        """The company name as a Greenhouse, Lever, SmartRecruiters or Ashby board, when one
        exists and lists at least one job."""
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
                (Board("ashby", slug), ASHBY_API.format(token=slug), {}),
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

        def feed(url: str) -> tuple[str, str]:
            return http.get_page(url, s=s, accept_feed=True)
    else:
        feed = page

    today = today or date.today()
    cfg = s.sweep.get("ats") or {}
    overrides = s.sweep.get("ats_boards") or {}
    sr_budget = Budget(int(cfg.get("max_detail_calls", 20)))
    wd_budget = Budget(int(cfg.get("workday_detail_calls", 40)))
    terms = tuple(cfg.get("search_terms") or DEFAULT_SEARCH_TERMS)
    wd_pages = int(cfg.get("workday_pages_per_term", 1))
    workers = max(1, int(s.sweep.get("workers", 8)))
    detector = Detector(s, page, get, state, today)
    active = [c for c in companies if c.active]
    # 1. Which board each company uses (careers pages are read several at a time).
    known = {c.name: detect_board(c, overrides) for c in active}
    # Companies with their own job site and no public job list (your decision of 5 Oct):
    # kept for contacts and referrals, covered by your alerts, never looked up here.
    own_sites = {canon_name(n) for n in cfg.get("own_site_companies") or []}
    for company in active:
        if known[company.name] is None and canon_name(company.name) in own_sites:
            result.own_site.append(company.name)
    skip = set(result.own_site)
    found = detector.resolve([c for c in active
                              if known[c.name] is None and c.name not in skip], workers)
    tasks: list[tuple[TargetCompany, Board]] = []
    # A board shared by several rows (Capgemini, Capgemini (IE) and Capgemini (NL); Amazon and
    # AWS) is read once: every row gave the same jobs again on 6 Oct, three reads for one.
    read: set[tuple[Any, ...]] = set()
    for company in active:
        if company.name in skip:
            continue
        board = known[company.name] or found.get(company.name)
        if board is None or board.ats not in SUPPORTED:
            result.not_supported.append(company.name)
            continue
        key = ("amazon",) if board.ats == "amazon" else (
            board.ats, board.token.lower(), board.host, board.site, board.eu)
        if key in read:
            continue
        read.add(key)
        name = without_region(company.name).strip() or company.name
        tasks.append((replace(company, name=name), board))

    # 2. Every board's feed, several at a time; results are kept in company order.
    def read_board(task: tuple[TargetCompany, Board]) -> tuple[list[RawPosting], int] | str:
        company, board = task
        try:
            if board.ats == "greenhouse":
                postings, dropped, _ = greenhouse(company, board, get, keep)
            elif board.ats == "lever":
                postings, dropped, _ = lever(company, board, get, keep)
            elif board.ats == "workday":
                postings, dropped, _ = workday(company, board, get, post, keep, wd_budget,
                                               today, terms, wd_pages)
            elif board.ats == "avature":
                postings, dropped, _ = avature(company, board, page, keep)
            elif board.ats == "amazon":
                postings, dropped, _ = amazon(company, get, keep, terms)
            elif board.ats == "ashby":
                postings, dropped, _ = ashby(company, board, get, keep)
            elif board.ats == "successfactors":
                postings, dropped, _ = successfactors(company, board, feed, keep, terms)
            elif board.ats == "phenom":
                postings, dropped, _ = phenom(company, board, page, keep, terms)
            elif board.ats == "oracle":
                postings, dropped, _ = oracle(company, board, get, keep, terms)
            else:
                postings, dropped, _ = smartrecruiters(company, board, get, keep, sr_budget)
        except http.HttpError as exc:
            return str(exc)
        return postings, dropped

    dropped_total = 0
    for (company, _board), outcome in zip(tasks, run_all(read_board, tasks, workers),
                                          strict=True):
        if isinstance(outcome, str):
            result.notes.append(f"ats {company.name}: {outcome}")
            # A wrong job board link (HTTP 404) shows in the summary, like a blocked site.
            status = re.search(r"HTTP (\d{3})", outcome)
            problem = f"HTTP {status.group(1)}" if status else "no answer"
            result.blocked.append(f"{company.name} (job board {problem})")
            continue
        postings, dropped = outcome
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
