"""Direct job links for alert rows (28 Oct, your request).

A LinkedIn alert gives the company, the title and the place, but the bot never opens
LinkedIn, and job boards such as JustJoin IT or Pracuj.pl refuse it. Such a row waits for
/jd, and the autopilot cannot apply from its link. After each sweep the bot looks for the same
job on the company's own site, never on LinkedIn:

1. The company's job board by its name (Greenhouse, Lever, SmartRecruiters, Ashby, Workable,
   Recruitee: the same free check as for visa sponsor companies). A posting with the same
   title in the same country is the job.
2. Otherwise one web search by the AI (Config model.fetch, else model.people; on the PC Gemini
   on Vertex AI). Its answer counts only after the bot read the page itself: an employer page
   (no job board or aggregator), whose job title is the row's title, with a full description.

A found job gets its own link in URL and its description in the page body, so screening reads
it and the autopilot can open its form. The old link stays on the page ("Earlier link"). The
dedupe keeps an employer link: a later alert for the same job never puts LinkedIn back.
Only an exact title counts: "Senior DevOps Engineer" is never used for "DevOps Engineer".
A row that found nothing is tried again after RETRY_DAYS. A form behind a login or a captcha
is still yours to send: the autopilot stops there and says so.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from jobengine.notion_repo import JobsRepo, select_filter
from jobengine.sweep import fulltext
from jobengine.sweep.crossmatch import DESCRIPTION_HEADER, title_key
from jobengine.sweep.dedupe import DESCRIPTION_BLOCK_CHARS, DESCRIPTION_MAX_BLOCKS, link_rank
from jobengine.sweep.normalize import canon, canon_city, canon_company, without_region

log = logging.getLogger("jobengine.sweep")

KEY = "sweep.job_links"  # bot_state: {"tried": {page_id: date}, "no_board": {company: date}}
RETRY_DAYS = 14
DEFAULT_MAX_ROWS = 40  # rows looked at in one sweep (sweep.job_links.max_rows)
DEFAULT_MAX_SEARCHES = 20  # AI web searches in one sweep (sweep.job_links.max_searches)
MAX_CANDIDATES = 4  # pages read for one AI answer
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
TITLE_SPLIT = re.compile(r"\s+(?:[|\u2013\u2014\-\u00b7:]|at)\s+")  # | - and long dashes

SYSTEM = """You find the official page of one job advert on the web.
Search the web, then answer with JSON only: {"url": "<link>"}.
The link must be the job's own page on the employer's careers site or on its applicant
tracking system (Workday, Greenhouse, Lever, SmartRecruiters, Ashby, Recruitee, Teamtailor,
Personio, Workable, SuccessFactors, Oracle and similar). Never a LinkedIn, Indeed, Glassdoor,
job board or aggregator page. It must be this exact job: the same title at the same company in
the same city or country. When you are not sure, answer {"url": ""}."""

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


@dataclass
class Result:
    labels: list[str] = field(default_factory=list)  # "Company, Role (company job board)"
    tried: int = 0
    searches: int = 0


@dataclass
class Found:
    url: str
    text: str
    how: str  # "company job board" or "web search"


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


def names_company(url: str, html: str, company: str) -> bool:
    """The company's own domain, or a job system's page that names the company."""
    host = (urlsplit(url).hostname or "").lower()
    name = canon_company(without_region(company))
    first = name.split()[0] if name else ""
    if len(first) >= 3 and first in host:
        return True
    return bool(name) and any(a in host for a in ATS_HOSTS) and f" {name} " in (
        f" {canon(BeautifulSoup(html, 'html.parser').get_text(' '))} ")


# ---------------------------------------------------------------- step 1: job board


def from_board(deps: Deps, company: str, role: str, city: str | None, country: str | None,
               boards: dict[str, Any], no_board: dict[str, str] | None = None) -> Found | None:
    """The job on the company's job board found by its name. `no_board` (company -> date):
    companies without one, not asked again for RETRY_DAYS (each guess asks 7 job systems)."""
    from jobengine import http
    from jobengine.sweep.boards import read_board
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
        postings: list[Any] = []
        if board is None and no_board is not None:
            no_board[key] = deps.today.isoformat()
        if board is not None:
            try:
                postings = read_board(deps.s, target, board, deps.get, deps.post,
                                      deps.page, deps.today)
            except http.HttpError as exc:
                log.info("job board of %s not read: %s", company, exc)
        boards[key] = postings
    full_min = _full_min(deps)
    for p in boards[key]:
        if not p.url or not same_title(p.title, role, city, country):
            continue
        if not in_place(p.location_text, city, country):
            continue
        text = (p.description or "").strip()
        if p.description_is_snippet or len(text) < full_min:
            text = _page_text(deps, p.url) or text
        if len(text) >= full_min:
            return Found(p.url, text, "company job board")
    return None


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


# ---------------------------------------------------------------- step 2: web search


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
    """The rows ("tried") or companies ("no_board") of the last RETRY_DAYS."""
    value = (state.get(KEY) if state is not None else None) or {}
    cutoff = (today - timedelta(days=RETRY_DAYS)).isoformat()
    return {k: v for k, v in (value.get(part) or {}).items() if str(v) > cutoff}


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
    """Give each open alert row of `boards` its own link on the company's site."""
    cfg = settings(deps.s)
    max_rows = int(cfg.get("max_rows", DEFAULT_MAX_ROWS))
    searches_left = int(cfg.get("max_searches", DEFAULT_MAX_SEARCHES)) if deps.llm else 0
    result = Result()
    if max_rows <= 0 or not boards:
        return result
    rows = deps.repo.query_rows({"and": [select_filter("Board", *boards),
                                         select_filter("Status", *OPEN_STATUSES)]})
    rows = [(pid, v) for pid, v in rows if v.get("Screen verdict") != "Skip"
            and link_rank(v.get("URL")) < 3 and v.get("Company") and v.get("Role")]
    rows.sort(key=lambda r: str(r[1].get("Posted date") or ""), reverse=True)
    tried = _remembered(deps.state, deps.today, "tried")
    no_board = _remembered(deps.state, deps.today, "no_board")
    job_boards: dict[str, Any] = {}
    for page_id, values in rows:
        if result.tried >= max_rows:
            break
        if page_id in tried:
            continue
        company, role = str(values["Company"]), str(values["Role"])
        city, country = values.get("City"), values.get("Country")
        result.tried += 1
        tried[page_id] = deps.today.isoformat()
        if progress is not None:
            progress(f"Direct job links: looking for {company}, {role}")
        try:
            found = from_board(deps, company, role, city, country, job_boards, no_board)
            if found is None and searches_left > 0:
                searches_left -= 1
                result.searches += 1
                found = from_search(deps, company, role, city, country, page_id)
        except Exception:  # one row never stops the others
            log.exception("direct link for %s not looked up", company)
            continue
        if found is None:
            continue
        board = str(values.get("Board") or "alert")
        deps.repo.update(page_id, {"URL": found.url})
        deps.repo.append_body(page_id, blocks(found, board, values.get("URL")))
        if with_body is not None:
            with_body.add(page_id)
        result.labels.append(f"{company}, {role} ({found.how})")
        log.info("%s row %s: direct link %s (%s)", board, page_id, found.url, found.how)
    if deps.state is not None:
        deps.state.set(KEY, {"tried": tried, "no_board": no_board})
    return result
