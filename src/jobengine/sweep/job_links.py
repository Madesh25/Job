"""Direct job links for alert rows (28 Oct, your request; simpler search 30 Oct).

A LinkedIn alert gives the company, the title and the place, but the bot never opens
LinkedIn, and job boards such as JustJoin IT or Pracuj.pl refuse it. Such a row waits for
/jd, and the autopilot cannot apply from its link. After each sweep the bot looks for the same
job on the company's own site, never on LinkedIn, the way you would do it by hand (30 Oct):

1. The company's job board by its name (Greenhouse, Lever, SmartRecruiters, Ashby, Workable,
   Recruitee: the same free check as for visa sponsor companies). A posting with the same
   title in the same country is the job.
2. The company's careers page: its Careers URL in Target Companies, else the one found
   before (kept SITE_DAYS), else one AI web search for the company's website and careers
   page (Config model.fetch, else model.people; on the PC Gemini on Vertex AI). The bot opens
   that page and reads it: the job board it links to, the job links on it, the page's own
   search box (a plain search for the title, never a login or a form that sends anything)
   and up to two job list pages it links to ("Jobs", "Vacancies", ...).
3. Otherwise one AI web search for the job itself.

Every answer counts only after the bot read the page itself: an employer page (no job board
or aggregator), whose job title is the row's title, with a full description. A recruitment
agency's own page counts too: it is where that job is published.

A found job gets its own link in URL and its description in the page body, so screening reads
it and the autopilot can open its form. The old link stays on the page ("Earlier link"). The
dedupe keeps an employer link: a later alert for the same job never puts LinkedIn back.
Only an exact title counts: "Senior DevOps Engineer" is never used for "DevOps Engineer".
A row that found nothing is tried again after RETRY_DAYS; it waits for /jd until then. A row
that stopped because this sweep's searches or browser pages were used up is tried again at
the next sweep.
A form behind a login or a captcha is still yours to send: the autopilot stops there.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from jobengine.notion_repo import JobsRepo, select_filter
from jobengine.sweep import fulltext
from jobengine.sweep.crossmatch import DESCRIPTION_HEADER, plain_title, title_key
from jobengine.sweep.dedupe import DESCRIPTION_BLOCK_CHARS, DESCRIPTION_MAX_BLOCKS, link_rank
from jobengine.sweep.normalize import canon, canon_city, canon_company, without_region

log = logging.getLogger("jobengine.sweep")

# bot_state: {"tried": {page_id: date}, "no_board": {company: date},
#             "sites": {company: {"url": careers page or "", "date": date}}}
KEY = "sweep.job_links"
RETRY_DAYS = 3  # a job not found is looked up again after this (14 until 30 Oct)
NO_BOARD_DAYS = 14  # a company without a job board or careers page is asked again after
MAX_RENDERS_PER_JOB = 2  # browser pages for one job: one site never uses the whole budget
THIN_PAGE_CHARS = 3000  # a page with less text than this may fill itself with JavaScript
THIN_PAGE_LINKS = 30
MAX_LINE = 160  # longer link texts are whole cards or menus, not titles
SITE_DAYS = 60  # a careers page found is used this long before it is looked up again
DEFAULT_MAX_ROWS = 40  # rows looked at in one sweep (sweep.job_links.max_rows)
DEFAULT_MAX_SEARCHES = 40  # AI web searches in one sweep (sweep.job_links.max_searches)
DEFAULT_WORKERS = 3  # companies looked up at the same time (sweep.job_links.workers)
MAX_CANDIDATES = 4  # pages read for one AI answer
MAX_SITE_PAGES = 10  # pages of one company's site read for one row
MAX_DEPTH = 2  # clicks from the page found: website -> careers page -> job list
MAX_JOB_LINKS = 3  # job links with the row's title opened on one page
MAX_LIST_PAGES = 2  # job list pages followed from a careers page
STAGE = "fetch"  # llm.py: a web search stage, model.fetch else model.people
OPEN_STATUSES = ("New", "Screened", "Approved", "Resume built")
EARLIER = "Earlier link"
# Sites that list other companies' jobs: never the job's own page.
NOT_EMPLOYER = ("glassdoor.", "indeed.", "talent.com", "jobted.", "careerjet.", "jobrapido.",
                "simplyhired.", "ziprecruiter.", "monster.", "stepstone.", "totaljobs.",
                "welcometothejungle.", "builtin.", "wellfound.", "otta.", "eures.",
                "google.", "bing.", "jooble.", "adzuna.", "whatjobs.", "jobsora.", "jobleads.",
                "learn4good.", "trabajo.", "efinancialcareers.", "dice.com", "himalayas.",
                "remoteok.", "weworkremotely.", "nofluffjobs.", "justjoin.", "pracuj.",
                "theprotocol.", "bulldogjob.", "irishjobs.", "jobs.ie", "nationalevacaturebank.",
                "iamexpat.", "werkzoeken.", "jobbird.", "intermediair.", "linkedin.")
# Job systems that host many employers' pages: the page itself must name the company.
ATS_HOSTS = ("myworkdayjobs.com", "greenhouse.io", "lever.co", "smartrecruiters.com",
             "ashbyhq.com", "recruitee.com", "teamtailor.com", "personio.", "workable.com",
             "jobvite.com", "icims.com", "successfactors.", "oraclecloud.com", "eightfold.ai",
             "taleo.net", "breezy.hr", "bamboohr.com", "join.com", "homerun.co")
COUNTRY_WORDS = {"poland": ("poland", "polska", "pl"),
                 "netherlands": ("netherlands", "nederland", "the netherlands", "nl", "holland"),
                 "ireland": ("ireland", "ie", "eire")}
# Links to a careers page or a job list, in the languages of your countries.
CAREER_WORDS = ("career", "jobs", "job-openings", "job-ads", "joboffer", "job-offer",
                "vacanc", "vacature", "werken-bij",
                "werkenbij", "kariera", "praca", "oferty", "join-us", "joinus", "work-with-us",
                "openings", "open-positions", "positions", "opportunities")
SEARCH_FIELDS = ("q", "query", "search", "keyword", "keywords", "s", "term", "text", "k",
                 "searchtext", "search_query", "zoeken", "szukaj")
# A Workday link without its server part ("wd5"): the AI often gives these, and the address
# does not exist (Armacell, Coca-Cola, Kyndryl on 30 Oct: ConnectError). The site is asked on
# each Workday server until one answers.
SHARDLESS_WORKDAY = re.compile(
    r"^https://([a-z0-9-]+)\.myworkdayjobs\.com/(?:[a-z]{2}-[a-z]{2}/)?([\w-]+)", re.I)
WORKDAY_SHARDS = ("wd1", "wd3", "wd5", "wd12", "wd103", "wd105")
TITLE_SPLIT = re.compile(r"\s+(?:[|\u2013\u2014\-\u00b7:]|at)\s+")  # | - and long dashes

SYSTEM = """You find the official page of one job advert on the web.
Search the web, then answer with JSON only: {"url": "<link>"}.
The link must be the job's own page on the employer's careers site or on its applicant
tracking system (Workday, Greenhouse, Lever, SmartRecruiters, Ashby, Recruitee, Teamtailor,
Personio, Workable, SuccessFactors, Oracle and similar). Never a LinkedIn, Indeed, Glassdoor,
job board or aggregator page. It must be this exact job: the same title at the same company in
the same city or country. When you are not sure, answer {"url": ""}."""

SITE_SYSTEM = """You find the official website and careers page of one company.
Search the web, then answer with JSON only: {"website": "<link>", "careers": "<link>"}.
"careers" is the page that lists the company's open jobs: on its own site, or on its
applicant tracking system (Workday, Greenhouse, Lever, SmartRecruiters, Teamtailor,
Recruitee, Personio, Workable and similar). For a recruitment agency, its own vacancies page.
Never a LinkedIn, Indeed, Glassdoor, job board or aggregator page. Use the company in the
country given when it has a page per country. When you are not sure, use ""."""

Getter = Callable[[str, Any], Any]
Poster = Callable[[str, Any], Any]


@dataclass
class Deps:
    s: Any
    repo: JobsRepo
    page: fulltext.PageGetter  # url -> (final url, html)
    get: Getter | None = None  # job boards' public APIs (step 1)
    post: Poster | None = None
    llm: Any = None  # step 2; None: no AI search
    state: Any = None
    today: date = field(default_factory=date.today)
    careers: dict[str, str] = field(default_factory=dict)  # company key -> Careers URL
    render: fulltext.PageGetter | None = None  # a browser for JavaScript pages (render.py)
    feed: fulltext.PageGetter | None = None  # job feeds (RSS, XML): SuccessFactors, Personio


@dataclass
class Result:
    labels: list[str] = field(default_factory=list)  # "Company, Role (company job board)"
    tried: int = 0
    searches: int = 0
    sites: int = 0  # careers pages found by an AI search this sweep
    again: int = 0  # jobs tried again next sweep: this sweep's searches or browser ran out


@dataclass
class Found:
    url: str
    text: str
    how: str  # "company job board", "careers page", "careers page search" or "web search"


def settings(s: Any) -> dict[str, Any]:
    return dict(s.sweep.get("job_links") or {})


def enabled(s: Any) -> bool:
    return bool(settings(s).get("enabled", True))


# ---------------------------------------------------------------- matching


def same_title(found: str | None, role: str | None, city: str | None = None,
               country: str | None = None) -> bool:
    """The exact job title, as crossmatch counts it ("Remote" and the place left out)."""
    want = title_key(role, city, country)
    return bool(want) and title_key(found, city, country) == want


def page_titles(html: str) -> list[str]:
    """The job titles a page states: JSON-LD JobPosting title, the first heading, and the
    first part of the browser title ("DevOps Engineer | Acme Careers")."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[str] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except ValueError:
            continue
        for node in data if isinstance(data, list) else [data]:
            if isinstance(node, dict) and node.get("@type") == "JobPosting" and node.get("title"):
                out.append(str(node["title"]))
    heading = soup.find("h1")
    if heading is not None and heading.get_text(strip=True):
        out.append(heading.get_text(" ", strip=True))
    if soup.title is not None and soup.title.string:
        out.append(TITLE_SPLIT.split(soup.title.string.strip())[0])
    return out


def in_place(location: str | None, city: str | None, country: str | None) -> bool:
    """A job board posting's place is the row's city or country (empty counts as yes:
    some boards leave it out)."""
    loc = f" {canon(location)} "
    if not loc.strip():
        return True
    cities = {canon(city), canon_city(city)} if city else set()
    words = COUNTRY_WORDS.get(canon(country), (canon(country),) if country else ())
    return any(f" {w} " in loc for w in (*cities, *words) if w)


def employer_page(url: str, company: str) -> bool:
    """An https page that is neither a job board nor an aggregator."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or link_rank(url) != 3:
        return False
    return not any(bad in host for bad in NOT_EMPLOYER)


def same_site(url: str, other: str) -> bool:
    """Both links are on one site: careers.acme.com and www.acme.com."""
    def base(link: str) -> str:
        return ".".join((urlsplit(link).hostname or "").lower().split(".")[-2:])

    return bool(other) and base(url) == base(other) != ""


def names_company(url: str, html: str, company: str, site: str = "") -> bool:
    """The company's own domain (or the site of its careers page `site`), or a job system's
    page that names the company."""
    host = (urlsplit(url).hostname or "").lower()
    if site and same_site(url, site):
        return True
    name = canon_company(without_region(company))
    first = name.split()[0] if name else ""
    if len(first) >= 3 and first in host:
        return True
    return bool(name) and any(a in host for a in ATS_HOSTS) and f" {name} " in (
        f" {canon(BeautifulSoup(html, 'html.parser').get_text(' '))} ")


# ---------------------------------------------------------------- step 1: job board


def _board_postings(deps: Deps, name: str, board: Any, role: str) -> list[Any]:
    """Every posting of a job board. SmartRecruiters lists hundreds of jobs for a big
    company in pages: it is asked for the title instead, as its own search box would be
    (ALTEN, 30 Oct: the board was found, the Polish job was not on its first pages)."""
    from jobengine import http
    from jobengine.parallel import Budget
    from jobengine.sweep.boards import WORKDAY_DETAILS, _keep_all, read_board
    from jobengine.sweep.models import TargetCompany
    from jobengine.sweep.sources import ats

    get = deps.get
    if get is not None and board.ats == "smartrecruiters":
        plain = get

        def get(url: str, params: Any) -> Any:
            ask = dict(params or {})
            if url.endswith("/postings"):
                ask["q"] = role
            return plain(url, ask)

    target = TargetCompany(name, "", "Unknown", True, None)
    try:
        if board.ats == "workday" and deps.post is not None:
            # The site's own search, for this title (not the usual DevOps words).
            postings, _, _ = ats.workday(target, board, deps.get, deps.post, _keep_all,
                                         Budget(WORKDAY_DETAILS), deps.today, (role,), 1)
            return postings
        return read_board(deps.s, target, board, get, deps.post, deps.page, deps.today,
                          feed=deps.feed)
    except http.HttpError as exc:
        log.info("job board of %s not read: %s", name, exc)
        return []


def workday_board(deps: Deps, url: str) -> Any:
    """The Workday site of a link without its server part, or None."""
    from jobengine import http
    from jobengine.sweep.sources import ats

    match = SHARDLESS_WORKDAY.match(url)
    if match is None or deps.post is None:
        return None
    tenant, site = match.group(1).lower(), match.group(2)
    for shard in WORKDAY_SHARDS:
        host = f"{tenant}.{shard}.myworkdayjobs.com"
        try:
            data = deps.post(f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                             {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
        except http.HttpError:
            continue
        if isinstance(data, dict) and "total" in data:
            log.info("Workday site for %s: %s/%s", url.split("/")[2], host, site)
            return ats.Board("workday", tenant, host=host, site=site)
    return None


def _pick(deps: Deps, postings: list[Any], role: str, city: str | None,
          country: str | None, how: str) -> Found | None:
    """The posting with the row's title in the row's place, with its full description."""
    full_min = _full_min(deps)
    for p in postings:
        if p.title:
            _note(p.title, role, city, country, p.location_text or "")
        if not p.url or not same_title(p.title, role, city, country):
            continue
        if not in_place(p.location_text, city, country):
            continue
        text = (p.description or "").strip()
        if p.description_is_snippet or len(text) < full_min:
            text = _page_text(deps, p.url) or text
        if len(text) >= full_min:
            return Found(p.url, text, how)
    return None


def from_board(deps: Deps, company: str, role: str, city: str | None, country: str | None,
               boards: dict[str, Any], no_board: dict[str, str] | None = None) -> Found | None:
    """The job on the company's job board found by its name. `no_board` (company -> date):
    companies without one, not asked again for NO_BOARD_DAYS (each guess asks 7 job systems)."""
    from jobengine.sweep.models import TargetCompany
    from jobengine.sweep.sources import ats
    from jobengine.sweep.sponsors import short_name

    if deps.get is None:
        return None
    name = short_name(without_region(company))
    key = canon_company(name)
    if no_board is not None and key in no_board:
        return None
    if key not in boards:
        target = TargetCompany(name, "", "Unknown", True, None)
        board = ats.Detector(deps.s, None, deps.get, None, deps.today).guess(target)
        if board is None and no_board is not None:
            no_board[key] = deps.today.isoformat()
        boards[key] = _board_postings(deps, name, board, role) if board is not None else []
    return _pick(deps, boards[key], role, city, country, "company job board")


def _full_min(deps: Deps) -> int:
    return int(deps.s.screening.get("full_min_chars", 600))


def _page_text(deps: Deps, url: str) -> str | None:
    from jobengine import http

    try:
        _final, html = deps.page(url)
    except http.HttpError as exc:
        log.info("job page not read %s: %s", urlsplit(url).hostname, exc)
        return None
    return fulltext.extract(html)


# ---------------------------------------------------------------- step 2: careers page


# Words an alert card shows where the company should be (30 Oct: rows with the company "1"
# and "Hybrid" from LinkedIn alerts). Such a row is not looked up: there is no company.
NOT_COMPANIES = {"hybrid", "remote", "on site", "onsite", "full time", "part time",
                 "unknown", "confidential", "company", "employer",
                 # a seniority word where the company should be (30 Oct: "Mid/Senior")
                 "junior", "mid", "regular", "medior", "senior", "mid senior", "junior mid",
                 "lead", "principal",
                 # job boards, not employers: the real company is not named
                 "solid jobs", "gamblingcareers com"}


def real_company(company: str) -> bool:
    name = canon(without_region(company))
    return len(name) >= 2 and not name.isdigit() and name not in NOT_COMPANIES


# Set on the worker's thread when a job could not be looked up fully because this sweep's
# searches or browser pages were used up: the job is then tried again at the next sweep.
_SHORT = threading.local()


def _short() -> None:
    _SHORT.hit = True


# The job titles a worker saw for its current job that come close to the row's title, for
# the log ("closest titles seen"): shows whether a job was missing or named a little
# differently (30 Oct: 14 of 40 careers pages read without the exact title).
_SEEN = threading.local()
NEAR = 0.4  # share of title words in common to count as close
MAX_NEAR = 3


def _note(seen: str, role: str, city: str | None, country: str | None,
          where: str = "") -> None:
    want = set(title_key(role, city, country).split())
    got = set(title_key(seen, city, country).split())
    if not want or not got or len(seen) > MAX_LINE:
        return
    score = len(want & got) / len(want | got)
    if score >= NEAR:
        near = getattr(_SEEN, "titles", None)
        if near is not None:
            label = f"{seen.strip()}{f' ({where})' if where else ''}"
            near[label] = max(score, near.get(label, 0.0))


def closest() -> str:
    near = getattr(_SEEN, "titles", None) or {}
    best = sorted(near, key=lambda k: -near[k])[:MAX_NEAR]
    return "; ".join(f"'{t}'" for t in best) if best else "none"


def thin_page(html: str) -> bool:
    """A page that may show its jobs only with JavaScript: little text or few links (30 Oct:
    CGI's long werken-bij pages used 5 browser pages and found nothing)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return (len(soup.get_text(" ", strip=True)) < THIN_PAGE_CHARS
            or len(soup.find_all("a", href=True)) < THIN_PAGE_LINKS)


def company_key(company: str) -> str:
    return canon_company(without_region(company))


def careers_of(companies: list[Any]) -> dict[str, str]:
    """Target Companies' Careers URLs by company (the first https one per company)."""
    out: dict[str, str] = {}
    for c in companies:
        url = (c.careers_url or "").strip()
        if url.lower().startswith("https://"):
            out.setdefault(company_key(c.name), url)
    return out


def careers_links(base: str, html: str) -> list[str]:
    """Links on a page to a careers page or a job list ("Careers", "Vacancies", ...)."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        href = str(a["href"]).strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        words = f"{href} {a.get_text(' ', strip=True)}".lower().replace(" ", "-")
        if any(w in words for w in CAREER_WORDS):
            url = urljoin(base, href).split("#")[0]
            if url.startswith("https://") and url.rstrip("/") != base.rstrip("/"):
                out.append(url)
    return list(dict.fromkeys(out))


def title_links(base: str, html: str, role: str, city: str | None,
                country: str | None) -> list[str]:
    """Links on a page whose text is the row's job title: the link itself, a heading in
    it, or its title attribute ("DevOps Engineer" in a job card)."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        texts = [a.get_text(" ", strip=True), str(a.get("title") or ""),
                 str(a.get("aria-label") or "")]
        texts += [h.get_text(" ", strip=True)
                  for h in a.find_all(["h1", "h2", "h3", "h4", "h5", "strong", "span"])]
        heading = a.find(["h1", "h2", "h3", "h4", "h5"])
        shown = heading.get_text(" ", strip=True) if heading is not None else texts[0]
        if shown:
            _note(TITLE_SPLIT.split(shown)[0], role, city, country)
        if any(same_title(TITLE_SPLIT.split(x)[0], role, city, country)
               or same_title(x, role, city, country) for x in texts if x):
            url = urljoin(base, str(a["href"]).strip()).split("#")[0]
            if url.startswith("https://"):
                out.append(url)
    return list(dict.fromkeys(out))


def search_url(base: str, html: str, role: str) -> str | None:
    """The page's own job search box, filled with the title: a GET form with a text field
    (the same address your browser opens when you type the title and press Enter). Forms
    that send data (POST) or ask for a password are never used."""
    soup = BeautifulSoup(html, "html.parser")
    for form in soup.find_all("form"):
        if str(form.get("method") or "get").lower() != "get":
            continue
        if form.find("input", attrs={"type": "password"}) is not None:
            continue
        fields = [i for i in form.find_all("input") if i.get("name")
                  and str(i.get("type") or "text").lower() in ("text", "search")]
        named = [i for i in fields if str(i["name"]).lower() in SEARCH_FIELDS]
        if not named:
            continue
        action = urljoin(base, str(form.get("action") or base))
        if not action.startswith("https://"):
            continue
        params = {str(i["name"]): str(i.get("value") or "")
                  for i in form.find_all("input") if i.get("name")
                  and str(i.get("type") or "text").lower() == "hidden"}
        params[str(named[0]["name"])] = role
        joiner = "&" if "?" in action else "?"
        return f"{action.split('#')[0]}{joiner}{urlencode(params)}"
    return None


def _job_page(deps: Deps, url: str, company: str, role: str, city: str | None,
              country: str | None, how: str, reads: list[int], site: str) -> Found | None:
    """A job page counts when it is an employer page with the title and a description."""
    from jobengine import http

    if reads[0] <= 0 or not employer_page(url, company):
        return None
    reads[0] -= 1
    try:
        final, html = deps.page(url)
    except http.HttpError as exc:
        log.info("job page not read %s: %s", urlsplit(url).hostname, exc)
        return None
    if not employer_page(final, company) or not names_company(final, html, company, site):
        return None
    if not any(same_title(t, role, city, country) for t in page_titles(html)):
        return None
    text = fulltext.extract(html) or ""
    return Found(final, text, how) if len(text) >= _full_min(deps) else None


def _on_page(deps: Deps, final: str, html: str, company: str, role: str, city: str | None,
             country: str | None, how: str, boards: dict[str, Any], reads: list[int],
             site: str) -> Found | None:
    """The job on one read page: the job board it uses, else a link with the title."""
    from jobengine.sweep.sources import ats

    board = ats.board_in_page(final, html)
    if board is not None:
        key = f"{board.ats}:{board.token}:{board.host}"
        if key not in boards:
            boards[key] = _board_postings(deps, without_region(company), board, role)
        found = _pick(deps, boards[key], role, city, country, how)
        if found is not None:
            return found
    for url in title_links(final, html, role, city, country)[:MAX_JOB_LINKS]:
        found = _job_page(deps, url, company, role, city, country, how, reads, site)
        if found is not None:
            return found
    return None


def _careers_url(url: str) -> bool:
    """A link that is itself a careers page or a job list (not a home page)."""
    parts = urlsplit(url)
    host, path = (parts.hostname or "").lower(), parts.path.lower()
    return host.startswith(("careers.", "career.", "jobs.", "werkenbij.", "kariera.")) or any(
        w in path for w in CAREER_WORDS) or any(a in host for a in ATS_HOSTS)


def _ours(link: str, careers: str, company: str) -> bool:
    """A link the company's careers page may lead to: its own site, a job system, or a
    site named after it (werkenbijcgi.nl). Never another company's careers page."""
    host = (urlsplit(link).hostname or "").lower()
    name = canon_company(without_region(company))
    first = name.split()[0] if name else ""
    return (same_site(link, careers) or any(a in host for a in ATS_HOSTS)
            or (len(first) >= 3 and first in host))


def _from_workday(deps: Deps, url: str, company: str, role: str, city: str | None,
                  country: str | None, boards: dict[str, Any], how: str) -> Found | None:
    board = workday_board(deps, url)
    if board is None:
        return None
    key = f"workday:{board.host}:{board.site}"
    if key not in boards:
        boards[key] = _board_postings(deps, without_region(company), board, role)
    return _pick(deps, boards[key], role, city, country, how)


def from_site(deps: Deps, careers: str, company: str, role: str, city: str | None,
              country: str | None, boards: dict[str, Any]) -> Found | None:
    """Open the careers page and look for the job like you would: its job board, its job
    links, its search box, then the pages it links to ("Careers", "Vacancies", ...), up to
    MAX_DEPTH clicks and MAX_SITE_PAGES pages. A careers page that shows its jobs only with
    JavaScript is opened in the browser when one is installed (render.py)."""
    from jobengine import http
    from jobengine.sweep.sources import ats

    if SHARDLESS_WORKDAY.match(careers):
        return _from_workday(deps, careers, company, role, city, country, boards,
                             "careers page")
    board = ats.board_from_url(careers)
    if board is not None:  # a job system's link (HCLTech's job feed, 30 Oct: HTTP 406)
        key = f"{board.ats}:{board.token}:{board.host}"
        if key not in boards:
            boards[key] = _board_postings(deps, without_region(company), board, role)
        return _pick(deps, boards[key], role, city, country, "company job board")
    reads = [MAX_SITE_PAGES]
    renders = [MAX_RENDERS_PER_JOB]
    queue: list[tuple[str, str, int]] = [(careers, "careers page", 0)]
    seen: set[str] = set()
    while queue and reads[0] > 0:
        url, how, depth = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        reads[0] -= 1
        try:
            final, html = deps.page(url)
        except http.HttpError as exc:
            log.info("careers page not read %s: %s", urlsplit(url).hostname, exc)
            continue
        found = _on_page(deps, final, html, company, role, city, country, how, boards, reads,
                         careers)
        if found is not None:
            return found
        if (deps.render is not None and (depth or _careers_url(final))
                and ats.board_in_page(final, html) is None and thin_page(html)):
            if renders[0] <= 0 or getattr(deps.render, "left", 1) <= 0:
                log.info("careers page not opened in the browser %s: no browser pages left "
                         "(this job: %d, this /fetch: %s)", urlsplit(url).hostname,
                         renders[0], getattr(deps.render, "left", "?"))
                if getattr(deps.render, "left", 1) <= 0:
                    _short()  # the sweep's budget, not this job's: try it again next sweep
                continue
            renders[0] -= 1
            try:
                final, html = deps.render(url)
            except http.HttpError as exc:
                log.info("careers page not opened in the browser %s: %s",
                         urlsplit(url).hostname, exc)
            else:
                found = _on_page(deps, final, html, company, role, city, country, how, boards,
                                 reads, careers)
                if found is not None:
                    return found
        if depth >= MAX_DEPTH:
            continue
        ask = search_url(final, html, role)
        if ask:
            queue.append((ask, "careers page search", MAX_DEPTH))
        for link in careers_links(final, html)[:MAX_LIST_PAGES + 1]:
            if employer_page(link, company) and _ours(link, careers, company):
                queue.append((link, "careers page", depth + 1))
    return None


def _site_ok(deps: Deps, url: str, company: str) -> bool:
    """An answer of the website search is a page of this company (read once to check)."""
    from jobengine import http

    if not employer_page(url, company):
        return False
    try:
        final, html = deps.page(url)
    except http.HttpError as exc:
        log.info("careers page not read %s: %s", urlsplit(url).hostname, exc)
        return False
    return employer_page(final, company) and names_company(final, html, company)


def find_site(deps: Deps, company: str, country: str | None, key: str) -> str:
    """One AI web search for the company's careers page (else its website), checked by
    reading it. "" when nothing usable was found."""
    from jobengine.llm import LLMError

    user = f"Company: {without_region(company)}\nCountry: {country or 'not given'}"
    try:
        raw, sources = deps.llm.complete_json_with_search(STAGE, SITE_SYSTEM, user,
                                                          max_tokens=400, max_uses=2, key=key)
    except LLMError as exc:
        log.info("careers page search failed for %s: %s", company, exc)
        return ""
    raw = raw or {}
    answers = [str(raw.get("careers") or "").strip(), str(raw.get("website") or "").strip()]
    for url in list(dict.fromkeys(u for u in [*answers, *sources] if u))[:MAX_CANDIDATES]:
        board = workday_board(deps, url)
        if board is not None:
            url = f"https://{board.host}/{board.site}"
            log.info("careers page for %s: %s", company, url)
            return url
        if _site_ok(deps, url, company):
            log.info("careers page for %s: %s", company, url)
            return url
    log.info("careers page for %s not found", company)
    return ""


# ---------------------------------------------------------------- step 3: web search


def from_search(deps: Deps, company: str, role: str, city: str | None,
                country: str | None, key: str) -> Found | None:
    from jobengine import http
    from jobengine.llm import LLMError

    user = (f"Company: {without_region(company)}\nJob title: {role}\n"
            f"City: {city or 'not given'}\nCountry: {country or 'not given'}")
    try:
        raw, sources = deps.llm.complete_json_with_search(STAGE, SYSTEM, user, max_tokens=600,
                                                          max_uses=3, key=key)
    except LLMError as exc:
        log.info("job link search failed for %s: %s", company, exc)
        return None
    answer = str((raw or {}).get("url") or "").strip()
    candidates = list(dict.fromkeys(u for u in [answer, *sources] if u))
    full_min = _full_min(deps)
    for url in [u for u in candidates if employer_page(u, company)][:MAX_CANDIDATES]:
        if SHARDLESS_WORKDAY.match(url):
            found = _from_workday(deps, url, company, role, city, country, {}, "web search")
            if found is not None:
                return found
            continue
        try:
            final, html = deps.page(url)
        except http.HttpError as exc:
            log.info("search result not read %s: %s", urlsplit(url).hostname, exc)
            continue
        if not employer_page(final, company) or not names_company(final, html, company):
            continue
        if not any(same_title(t, role, city, country) for t in page_titles(html)):
            continue
        text = fulltext.extract(html) or ""
        if len(text) >= full_min:
            return Found(final, text, "web search")
    return None


# ---------------------------------------------------------------- the pass


def _remembered(state: Any, today: date, part: str) -> dict[str, str]:
    """The rows ("tried", RETRY_DAYS) or companies ("no_board", NO_BOARD_DAYS) still kept."""
    value = (state.get(KEY) if state is not None else None) or {}
    days = RETRY_DAYS if part == "tried" else NO_BOARD_DAYS
    cutoff = (today - timedelta(days=days)).isoformat()
    return {k: v for k, v in (value.get(part) or {}).items() if str(v) > cutoff}


def _sites(state: Any, today: date) -> dict[str, dict[str, str]]:
    """Careers pages found (kept SITE_DAYS) and companies without one (NO_BOARD_DAYS)."""
    value = (state.get(KEY) if state is not None else None) or {}
    out = {}
    for key, item in (value.get("sites") or {}).items():
        if not isinstance(item, dict):
            continue
        days = SITE_DAYS if item.get("url") else NO_BOARD_DAYS
        if str(item.get("date") or "") > (today - timedelta(days=days)).isoformat():
            out[key] = item
    return out


def blocks(found: Found, board: str, old_url: str | None) -> list[str]:
    """The earlier link, then the description as screening reads it (newest last)."""
    out = [f"{EARLIER} ({board}): {old_url}"] if old_url else []
    out.append(f"{DESCRIPTION_HEADER} company site (direct link for this {board} job, found "
               f"by {found.how}: {found.url})")
    text = found.text.strip()
    while text and len(out) < DESCRIPTION_MAX_BLOCKS:
        out.append(text[:DESCRIPTION_BLOCK_CHARS])
        text = text[DESCRIPTION_BLOCK_CHARS:]
    return out


def run(deps: Deps, boards: tuple[str, ...], with_body: set[str] | None = None,
        progress: Callable[[str], None] | None = None) -> Result:
    """Give each open alert row of `boards` its own link on the company's site.

    Companies are looked up `workers` at a time (30 Oct, your idea: the searches and page
    reads are mostly waiting). All jobs of one company go to the same worker, one after the
    other, so its careers page is searched for once; the Notion writes go one at a time."""
    from jobengine.jobctl import CONTROL
    from jobengine.parallel import Budget, run_all

    cfg = settings(deps.s)
    max_rows = int(cfg.get("max_rows", DEFAULT_MAX_ROWS))
    workers = max(1, int(cfg.get("workers", DEFAULT_WORKERS)))
    result = Result()
    if max_rows <= 0 or not boards:
        return result
    rows = deps.repo.query_rows({"and": [select_filter("Board", *boards),
                                         select_filter("Status", *OPEN_STATUSES)]})
    rows = [(pid, v) for pid, v in rows if v.get("Screen verdict") != "Skip"
            and link_rank(v.get("URL")) < 3 and v.get("Company") and v.get("Role")]
    for _pid, v in [r for r in rows if not real_company(str(r[1]["Company"]))]:
        log.info("not looked up: the company of %s is %r (the alert did not show it)",
                 v.get("Role"), v.get("Company"))
    rows = [r for r in rows if real_company(str(r[1]["Company"]))]
    rows.sort(key=lambda r: str(r[1].get("Posted date") or ""), reverse=True)
    tried = _remembered(deps.state, deps.today, "tried")
    no_board = _remembered(deps.state, deps.today, "no_board")
    sites = _sites(deps.state, deps.today)
    job_boards: dict[str, Any] = {}
    allowed = int(cfg.get("max_searches", DEFAULT_MAX_SEARCHES)) if deps.llm else 0
    searches = Budget(allowed)
    lock = threading.Lock()  # counters and Notion writes

    groups: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for page_id, values in rows:
        if result.tried >= max_rows:
            break
        if page_id in tried:
            continue
        result.tried += 1
        tried[page_id] = deps.today.isoformat()
        groups.setdefault(company_key(str(values["Company"])), []).append((page_id, values))

    def search() -> bool:
        if not searches.take():
            if allowed > 0:
                _short()  # the searches ran out: try this job again next sweep
            return False
        with lock:
            result.searches += 1
        return True

    def careers_page(company: str, country: str | None, page_id: str) -> str:
        key = company_key(company)
        url = deps.careers.get(key) or (sites.get(key) or {}).get("url") or ""
        if url or key in sites or not search():
            return url
        url = find_site(deps, company, country, page_id)
        sites[key] = {"url": url, "date": deps.today.isoformat()}
        if url:
            with lock:
                result.sites += 1
        return url

    def lookup(page_id: str, values: dict[str, Any]) -> str | None:
        """One job: its link on the company's site, written to Notion. The label or None."""
        # "DevOps Engineer @ Scalo" (Adzuna) is searched for as "DevOps Engineer"
        company, role = str(values["Company"]), plain_title(str(values["Role"]))
        city, country = values.get("City"), values.get("Country")
        careers = ""
        found = from_board(deps, company, role, city, country, job_boards, no_board)
        if found is None:
            careers = careers_page(company, country, page_id)
            if careers:
                found = from_site(deps, careers, company, role, city, country, job_boards)
        # With a careers page read, a search for the exact job finds no more (30 Oct:
        # 0 of 20), so the searches go to the next companies instead.
        if found is None and not careers and search():
            found = from_search(deps, company, role, city, country, page_id)
        if found is None:
            again = " (looked up again next /fetch)" if getattr(_SHORT, "hit", False) else ""
            log.info("no direct link for %s, %s: it waits for /jd%s; closest titles seen: %s",
                     company, role, again, closest())
            return None
        board = str(values.get("Board") or "alert")
        with lock:
            deps.repo.update(page_id, {"URL": found.url})
            deps.repo.append_body(page_id, blocks(found, board, values.get("URL")))
            if with_body is not None:
                with_body.add(page_id)
        log.info("%s row %s: direct link %s (%s)", board, page_id, found.url, found.how)
        return f"{company}, {role} ({found.how})"

    def company_jobs(group: list[tuple[str, dict[str, Any]]]) -> list[str]:
        labels = []
        for page_id, values in group:
            CONTROL.check()  # after /end a worker stops before its next job
            _SHORT.hit = False
            _SEEN.titles = {}
            try:
                label = lookup(page_id, values)
            except Exception:  # one job never stops the others
                log.exception("direct link for %s not looked up", values.get("Company"))
                continue
            if label:
                labels.append(label)
            elif _SHORT.hit:
                with lock:
                    tried.pop(page_id, None)
                    result.again += 1
        return labels

    def done(finished: int, total: int) -> None:
        if progress is not None:
            progress(f"Direct job links: {finished} of {total} companies looked up")

    if progress is not None and groups:
        progress(f"Direct job links: looking up {result.tried} jobs at {len(groups)} "
                 f"companies, {min(workers, len(groups))} at a time")
    for labels in run_all(company_jobs, list(groups.values()), workers, done):
        result.labels.extend(labels)
    if deps.state is not None:
        deps.state.set(KEY, {"tried": tried, "no_board": no_board, "sites": sites})
    return result
