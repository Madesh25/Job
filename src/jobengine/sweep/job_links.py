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

Only the company's own site counts (2 Nov, your rule): a job that a company does not put on
its own site is likely a ghost job. When the bot read the company's job list and the job is
not on it, the page gets a "Company site check" note and the job waits for /jd (10 Nov,
your rule: one look-up per job is enough, never again and again). When the company's site
could not be checked (no careers page found, the site did not answer), the note says that
and the job waits for /jd too. Job boards (JustJoin IT, Pracuj.pl, theprotocol.it,
IrishJobs, LinkedIn) are never opened.

A job whose full description is already on its page (pasted with /jd, read from another
board, or screened) is not looked up at all (10 Nov: every look-up costs Google searches),
unless you approved it: then its own link is still wanted, to apply.

Every answer counts only after the bot read the page itself: an employer page (no job board
or aggregator), whose job title is the row's title, with a full description. A recruitment
agency's own page counts too: it is where that job is published.

A found job gets its own link in URL and its description in the page body, so screening reads
it and the autopilot can open its form. The old link stays on the page ("Earlier link"). The
dedupe keeps an employer link: a later alert for the same job never puts LinkedIn back.
Only an exact title counts: "Senior DevOps Engineer" is never used for "DevOps Engineer".
A row is looked up once (kept ONCE_DAYS, longer than a posting lives). Only a row that
stopped because this sweep's searches or browser pages were used up is tried again at the
next sweep: it was not really looked up.
A form behind a login or a captcha is still yours to send: the autopilot stops there.
"""

from __future__ import annotations

import functools
import json
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from urllib.parse import unquote, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from jobengine import textblocks
from jobengine.notion_repo import JobsRepo, select_filter
from jobengine.safety import SafetyError
from jobengine.sweep import fulltext, normalize
from jobengine.sweep.crossmatch import DESCRIPTION_HEADER, plain_title, title_key
from jobengine.sweep.dedupe import DESCRIPTION_BLOCK_CHARS, DESCRIPTION_MAX_BLOCKS, link_rank
from jobengine.sweep.normalize import canon, canon_city, canon_company, without_region

log = logging.getLogger("jobengine.sweep")

# bot_state: {"tried": {page_id: date}, "no_board": {company: date},
#             "sites": {company: {"url": careers page or "", "date": date}}}
KEY = "sweep.job_links"
# Bumped when the lookup itself gets better (4 Nov: JavaScript pages, job systems, Google's
# copy): the jobs and companies not found before are looked up again once, not 3 or 14
# days later. Ghost-job counts ("misses") are kept.
LOOKUP_VERSION = 9  # 9: a job list the browser could not open is no check
RETRY_DAYS = 3  # until 10 Nov a job not found was looked up again after this
ONCE_DAYS = 120  # 10 Nov: a job is looked up once; kept this long (postings close sooner)
DESCRIBED = "described"  # rows not looked up because their description is on the page
WITH_LINK_STATUSES = ("Approved", "Resume built")  # their own link is wanted even so
MAX_BODY_READS = 200  # pages read in one sweep to see whether a description is there
NO_BOARD_DAYS = 14  # a company without a job board or careers page is asked again after
# Browser pages for one job: one site never uses the whole budget. 13 Nov: 2 -> 4 (11 jobs
# stopped at 2 while 63 of the /fetch's pages were unused); a page opened before in the same
# /fetch costs nothing (render.Renderer keeps it).
MAX_RENDERS_PER_JOB = 4
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
             "taleo.net", "breezy.hr", "bamboohr.com", "join.com", "homerun.co",
             # 4 Nov: job systems the adverts were on (UNIQA, Square One, PeopleVibe, BNG,
             # Sea Machines, Ework, Infor, Patrickbeek)
             "erecruiter.pl", "traffit.com", "recruitify.ai", "emply.com", "applytojob.com",
             "csod.com", "pinpointhq.com", "careers-page.com", "njoyn.com", "softgarden.",
             "jobs.cz", "hrlink.pl", "elevatus.io", "manatal.com")
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
# A Workday link without its server part (armacell.myworkdayjobs.com) or with a made-up host
# (4 Nov: the search gave nn.workdayjobs.com, which does not exist, for NN Group): the
# tenant and site are asked of each real Workday server.
SHARDLESS_WORKDAY = re.compile(
    r"^https://([a-z0-9-]+)\.(?:my)?workdayjobs\.com/(?:[a-z]{2}-[a-z]{2}/)?([\w-]+)", re.I)
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
Search the web, then answer with JSON only:
{"website": "<link>", "careers": "<link>", "board": "<link>"}.
"careers" is the page that lists the company's open jobs: on its own site, or on its
applicant tracking system (Workday, Greenhouse, Lever, SmartRecruiters, Teamtailor,
Recruitee, Personio, Workable and similar). For a recruitment agency, its own vacancies page.
"board" is the job list on the applicant tracking system itself when the company uses one
(for example https://<name>.wd3.myworkdayjobs.com/<site>, a successfactors.eu or
successfactors.com career page, boards.greenhouse.io/<name>, jobs.lever.co/<name>), copied
exactly from a search result, never made up. Never a LinkedIn, Indeed, Glassdoor, job board
or aggregator page. Use the company in the country given when it has a page per country.
When you are not sure, use ""."""

# 4 Nov, your decision: a job whose company site refuses the bot or could not be checked is
# read from Google's copy of the company's own page (Gemini with Google Search). The bot
# never opens the refusing site; only text from the employer's own page or job system counts.
GOOGLE_SYSTEM = """You read one job advert for a candidate, using Google Search.
Find this exact job (same title, same company, same city or country) on the employer's own
careers site or its job system (Workday, SuccessFactors, Greenhouse, Lever and similar), and
answer with JSON only:
{"found": true, "url": "<the advert's own page>", "description": "<the advert>"}.
"description" gives the advert as the search results show it: what the job does, what it
asks for (skills, years, languages, work permit), what it offers and where, in the advert's
own words as far as the results have them, at most 900 words. Only what the advert says:
never add, guess or take from another advert. An advert on the employer's careers page or
job system counts even when the search results show only part of it. Never use LinkedIn,
Indeed, Glassdoor, job boards or aggregators. When you cannot find this advert on the
employer's own page at all, answer {"found": false}."""
GOOGLE_HOW = "Google's copy of the company page"  # its site does not let the bot read it
MIN_TITLE_SHARE = 0.5  # of the role's words the text must name


def copy_model(s: Any) -> str:
    """sweep.job_links.copy_model: another Gemini for Google's copy. Empty (the default):
    the fetch model. 4 Nov: gemini-3.8-flash made 20 to 78 Google searches a call (Gemini
    cannot be told a maximum), timed out or answered no JSON, and found no more."""
    return str((s.sweep.get("job_links") or {}).get("copy_model") or "").strip()

# 4 Nov: big companies' own websites (Serco, PwC) refuse automated readers with HTTP 403,
# while the job system they post on is meant to be read by job sites.
BOARD_SYSTEM = """You find the job list of one company on its applicant tracking system.
Search the web, then answer with JSON only: {"board": "<link>"}.
The link must be on the job system itself: Workday (myworkdayjobs.com), SuccessFactors,
Greenhouse, Lever, SmartRecruiters, Ashby, Teamtailor, Recruitee, Personio, Workable, Oracle,
Eightfold, iCIMS or similar. Never the company's own website, LinkedIn, Indeed, Glassdoor, a
job board or an aggregator. Copy it exactly from a search result, never make one up. Use the
company in the country given when it has a page per country. When you are not sure, answer
{"board": ""}."""

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
    config: Any = None  # Notion Config (agency names) for the learned boards
    targets: list[Any] = field(default_factory=list)  # Target Companies (never learned)


@dataclass
class Result:
    labels: list[str] = field(default_factory=list)  # "Company, Role (company job board)"
    tried: int = 0
    searches: int = 0
    sites: int = 0  # careers pages found by an AI search this sweep
    again: int = 0  # jobs tried again next sweep: this sweep's searches or browser ran out
    ghosts: list[str] = field(default_factory=list)  # not on the company's site twice: skipped
    no_company: list[str] = field(default_factory=list)  # the alert showed no company: noted
    not_yet: int = 0  # not on the company's site when checked: waits for /jd
    unchecked: int = 0  # the company's site could not be checked: waits for /jd
    deleted: list[str] = field(default_factory=list)  # reposted jobs deleted (4 Nov)
    boards_learned: list[str] = field(default_factory=list)  # job boards learned (13 Nov)


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


# What a careers page adds to a job title that is not part of it (2 Nov log): a job number
# ("DevOps / Platform Engineer (ID:4116)", STAFIDE) and the button text of a job card
# ("Platform / DevOps Engineer Explore this vacancy", wolk). The title itself must still be
# the same: "Senior DevOps Engineer" is never "DevOps Engineer".
_JOB_NUMBER = re.compile(r"\s*[(\[]\s*(?:id|ref|req|job id|job no|nr)\.?\s*[:#.]?\s*[\w-]+\s*[)\]]",
                         re.I)
_BUTTON = re.compile(
    r"\s*(?:explore this vacancy|bekijk deze vacature|bekijk vacature|view (?:this )?"
    r"(?:job|vacancy|position|role)|see (?:the )?(?:job|vacancy)|apply now|apply|read more"
    r"|lees meer|more info|meer info|learn more|zobacz ofert[eę]|aplikuj)\s*[>\u2192\u203a]*\s*$",
    re.I)


# 2 Nov, second log: a team code in front ("[WBG] DevOps Engineer", Software Mind) and a
# running number behind ("Platform Engineer (18/22)", Infolet).
_TEAM_CODE = re.compile(r"^\s*\[[A-Z0-9]{2,6}\]\s*")
_RUNNING_NUMBER = re.compile(r"\s*\(\d{1,3}/\d{1,3}\)\s*$")
# A job number at the end of an alert's title: "GCP Cloud Engineer ID89421" (AgileEngine).
_ID_SUFFIX = re.compile(r"\s+(?:id|ref|req)\s?[:#]?\s?\d{3,}\s*$", re.I)


# Link text before the title (4 Nov: CIMSOLUTIONS lists "Bekijk vacature voor DevOps
# Engineer", "View vacancy for ...").
_BUTTON_BEFORE = re.compile(
    r"^\s*(?:bekijk (?:de |deze )?vacature (?:voor|van)|view (?:the )?(?:job|vacancy|position|"
    r"role)(?: for)?|apply (?:for|to)|solliciteer (?:op|voor|naar)|zobacz ofert[eę]"
    r"(?: pracy)?(?: na)?|aplikuj na)\s*:?\s+", re.I)


def card_title(text: str | None) -> str:
    """A job title as a careers page shows it, without its job number and button text."""
    value = _BUTTON_BEFORE.sub("", text or "")
    value = _JOB_NUMBER.sub("", value)
    value = _BUTTON.sub("", value).strip()
    value = _RUNNING_NUMBER.sub("", _TEAM_CODE.sub("", value)).strip()
    return _ID_SUFFIX.sub("", value).strip()


def same_title(found: str | None, role: str | None, city: str | None = None,
               country: str | None = None) -> bool:
    """The exact job title, as crossmatch counts it ("Remote" and the place left out)."""
    want = title_key(card_title(role), city, country)
    got = title_key(card_title(found), city, country)
    if city:  # "... (f/m) Warsaw" for a row in "Warszawa" (Netcompany, 2 Nov)
        home = canon_city(city)
        got = " ".join(w for w in got.split() if canon_city(w) != home)
    if got != want:  # "Site Reliability Engineer Wroclaw, Krakow, Gdansk, Warsaw, Poland"
        places = place_words() - set(want.split())
        got = " ".join(w for w in got.split() if w not in places)
    return bool(want) and got == want


@functools.lru_cache(maxsize=1)
def place_words() -> frozenset[str]:
    """The one-word names of your countries and cities (4 Nov: Grid Dynamics puts every city
    after the title)."""
    from jobengine.settings import get_settings

    words = {w for names in COUNTRY_WORDS.values() for w in names if len(w) > 2}
    try:
        locations = get_settings().sweep.get("locations") or {}
    except Exception:  # no settings (a script): the countries only
        locations = {}
    for spec in locations.values():
        for name in [*(spec.get("names") or []),
                     *(a for aliases in (spec.get("cities") or {}).values() for a in aliases)]:
            value = canon(str(name))
            if value and " " not in value and len(value) > 2:
                words.add(value)
                words.add(canon_city(value))
    return frozenset(words)


# Extra words a close title may have (4 Nov: JustJoin's "DevOps Engineer" is Motorola's
# "Software DevOps Engineer" in Krakow); never a level word, never more than two.
NEVER_EXTRA = {"senior", "sr", "lead", "principal", "staff", "head", "manager", "director",
               "junior", "jr", "intern", "internship", "trainee", "graduate", "student",
               "architect", "chief", "vp"}
MAX_EXTRA_WORDS = 2


def close_title(found: str | None, role: str | None, city: str | None = None,
                country: str | None = None) -> bool:
    """The board's title is the row's title with at most two more words, none a level word
    ("Software DevOps Engineer" for "DevOps Engineer", never "Senior DevOps Engineer")."""
    want = title_key(card_title(role), city, country).split()
    got = title_key(card_title(found), city, country).split()
    if city:
        home = canon_city(city)
        got = [w for w in got if canon_city(w) != home]
    places = place_words() - set(want)  # "Software DevOps Engineer (Krakow, Poland)"
    got = [w for w in got if w not in places]
    extra = [w for w in got if w not in want]
    return (len(want) >= 2 and all(w in got for w in want)
            and 0 < len(extra) <= MAX_EXTRA_WORDS and not set(extra) & NEVER_EXTRA)


# Words an alert adds to a title that a company's own list may leave out (5 Nov:
# Xccelerated lists "Cloud Engineer" for the alert's "Cloud Engineer GCP", Reperio
# "Platform Engineer" for "AWS Platform Engineer").
TECH_WORDS = {"aws", "gcp", "azure", "google", "amazon", "microsoft", "kubernetes", "k8s",
              "openshift", "linux", "terraform"}


def shorter_title(found: str | None, role: str | None, city: str | None = None,
                  country: str | None = None) -> bool:
    """The list's title is the row's title without its platform words: maybe the same
    job, so the job is never called a ghost (it is not taken as the job's link either)."""
    want = title_key(card_title(role), city, country).split()
    got = title_key(card_title(found), city, country).split()
    missing = [w for w in want if w not in got]
    return (len(got) >= 2 and all(w in want for w in got) and bool(missing)
            and all(w in TECH_WORDS for w in missing))


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


_URL = re.compile(r"https?://[^\s<>\"'`]+")


def first_url(value: object) -> str:
    """The first link in the AI's answer, alone. Phase 23 2-01 (8 Nov): the answer was two
    links joined by "or", and both went into the job's URL field."""
    match = _URL.search(str(value or ""))
    return match.group(0).rstrip(".,;:)]") if match else ""


def employer_page(url: str, company: str) -> bool:
    """An https page that is neither a job board nor an aggregator."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or link_rank(url) != 3:
        return False
    if host in ("www.google.com", "careers.google.com") and parts.path.startswith(
            ("/about/careers", "/jobs")):
        return True  # 4 Nov: Google's own careers site (google.com is otherwise a search)
    return not any(bad in host for bad in NOT_EMPLOYER)


def same_site(url: str, other: str) -> bool:
    """Both links are on one site: careers.acme.com and www.acme.com."""
    def base(link: str) -> str:
        return ".".join((urlsplit(link).hostname or "").lower().split(".")[-2:])

    return bool(other) and base(url) == base(other) != ""


def name_in_host(host: str, name: str) -> bool:
    """The company's name is in the web address: its first word of 3 letters or more, or a
    short name written together at the start of a part of the address (4 Nov: P&G is
    pgcareers.com; "p g" has no word of 3 letters)."""
    words = name.split()
    if words and len(words[0]) >= 3 and words[0] in host:
        return True
    labels = [CAREERS_PREFIX.sub("", label) for label in host.replace("-", "").split(".")]
    together = "".join(words)
    initials = "".join(w[0] for w in words) if len(words) > 1 else ""
    for short in (together, initials):  # "p g" -> "pg"; "procter gamble" -> "pg"
        if len(words) > 1 and len(short) >= 2 and any(lb.startswith(short) for lb in labels):
            return True
    # 4 Nov: a 2-letter name with a careers word next to it (NN Group is nn-careers.com)
    first = words[0] if words else ""
    return len(first) == 2 and any(
        lb == first or CAREERS_SUFFIX.fullmatch(lb[len(first):] if lb.startswith(first) else "")
        for lb in (label.replace("-", "") for label in host.split(".")))


# The part before the company's name in a careers site's address (4 Nov: a.s.r. is
# werkenbijasr.nl, BNG werkenbijbng.nl).
CAREERS_PREFIX = re.compile(r"^(?:werkenbij(?:de)?|werkenvoor|careers?at|careers?|jobsat|jobs|"
                            r"kariera|karriere|praca|joinus|workat|work)(?=[a-z0-9]{2})")


CAREERS_SUFFIX = re.compile(r"careers?|jobs|vacatures|werkenbij|kariera|karriere|praca")


def names_company(url: str, html: str, company: str, site: str = "") -> bool:
    """The company's own domain (or the site of its careers page `site`), or a job system's
    page that names the company."""
    host = (urlsplit(url).hostname or "").lower()
    if site and same_site(url, site):
        return True
    name = canon_company(without_region(company))
    if name_in_host(host, name):
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
    except SafetyError as exc:  # 2 Nov: HSBC and Qualcomm (Eightfold) stopped the whole job
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
        _seen()
        text = (p.description or "").strip()
        if p.description_is_snippet or len(text) < full_min:
            text = _page_text(deps, p.url) or text
        if len(text) >= full_min:
            return Found(p.url, text, how)
    # No exact title: one job in the row's place with the same title and at most two more
    # words is the same job (4 Nov: five jobs were skipped as ghosts this way, Motorola's
    # "Software DevOps Engineer" in Krakow among them). Two or more: not sure, none.
    close = [p for p in postings if p.url and close_title(p.title, role, city, country)
             and in_place(p.location_text, city, country)]
    if close or any(shorter_title(p.title, role, city, country)
                    and in_place(p.location_text, city, country) for p in postings):
        _seen()  # a close title on its list: never a ghost job, even when not sure which
    if len(close) == 1:
        p = close[0]
        text = (p.description or "").strip()
        if p.description_is_snippet or len(text) < full_min:
            text = _page_text(deps, p.url) or text
        if len(text) >= full_min:
            log.info("close title for %s: %s", role, p.title)
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
    # A board guessed by the name counts as the company's job list only with jobs in the
    # row's country: another company may have the same name.
    if _in_country(boards[key], city, country):
        _listed()
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
                 "solid jobs", "gamblingcareers com",
                 # 4 Nov: "Cloud & Platform Engineer (Go/Java) | Poland" gave "Poland"
                 "poland", "polska", "netherlands", "the netherlands", "nederland", "ireland",
                 "europe", "emea"}


def real_company(company: str) -> bool:
    name = canon(without_region(company))
    return len(name) >= 2 and not name.isdigit() and name not in NOT_COMPANIES


# Set on the worker's thread when a job could not be looked up fully because this sweep's
# searches or browser pages were used up: the job is then tried again at the next sweep.
_SHORT = threading.local()


_CHECK = threading.local()  # .read: this job's company job list was read (2 Nov)
MISSES_TO_SKIP = 2  # checks of the company's job list without the job before it is skipped
MISSES_KEEP_DAYS = 30
NOTE = "Company site check"
# bot_state part for the checks without the job. "misses" (2 Nov, first run) counted a
# homepage as a job list; those checks are not used.
MISSES = "misses_2"  # page body line; screening reads no description past it


INCOMPLETE_KEY = "sweep.boards_missing"  # bot_state: {"companies": {name: last iso day}}
INCOMPLETE_DAYS = 14  # a board that missed jobs is no check for this long


def note_incomplete(state: Any, companies: list[str], today: date) -> None:
    """Companies whose own job board missed jobs of this sweep (discover.py, the summary's
    "Jobs another source found that the company's own job board did not list")."""
    if state is None:
        return
    cutoff = (today - timedelta(days=INCOMPLETE_DAYS)).isoformat()
    kept = {k: v for k, v in ((state.get(INCOMPLETE_KEY) or {}).get("companies") or {}).items()
            if isinstance(v, str) and v > cutoff}
    kept.update({c: today.isoformat() for c in companies})
    state.set(INCOMPLETE_KEY, {"companies": kept})


def _incomplete(state: Any, today: date) -> set[str]:
    value = (state.get(INCOMPLETE_KEY) if state is not None else None) or {}
    cutoff = (today - timedelta(days=INCOMPLETE_DAYS)).isoformat()
    companies = value.get("companies") or {}
    if not isinstance(companies, dict):
        return set()
    return {company_key(str(c)) for c, day in companies.items() if str(day) > cutoff}


def _partial() -> None:
    """The company's job list needed the browser, and no browser page was left for it."""
    _CHECK.partial = True


def _listed() -> None:
    """This job's company job list was read: a miss means the job is not on it."""
    _CHECK.read = True


def _in_country(postings: list[Any], city: str | None, country: str | None) -> bool:
    """A job list for the row: jobs that name a place in the row's country. A board without
    one is not this job's list (2 Nov: Capgemini's and Xebia's boards had no Dutch or
    Polish jobs; Link Group's guessed board had no places at all)."""
    return any(p.location_text and in_place(p.location_text, city, country) for p in postings)


def _seen() -> None:
    """The job's title is on the company's job list (its page may still be unreadable)."""
    _CHECK.seen = True


# Words of job titles, in the languages of your countries: a page with a few links like that
# is a job list (2 Nov: a homepage or a careers page that answered 404 was counted as one).
JOB_WORDS = re.compile(
    r"\b(?:engineer|developer|administrator|architect|analyst|manager|specialist|consultant|"
    r"devops|sre|tester|designer|scientist|technician|inżynier|programista|specjalista|"
    r"administrator|ontwikkelaar|beheerder|medewerker)\b", re.I)
MIN_JOB_LINKS = 3


def job_list_page(html: str) -> bool:
    """The page lists jobs: at least MIN_JOB_LINKS links whose text is a job title."""
    soup = BeautifulSoup(html, "html.parser")
    count = 0
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if text and len(text) <= MAX_LINE and JOB_WORDS.search(text):
            count += 1
            if count >= MIN_JOB_LINKS:
                return True
    return False


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


# 4 Nov: on careers.kingfisher.com every link has "careers" in it; its privacy policy was
# opened in the browser instead of the job list.
NOT_CAREERS = re.compile(r"privacy|cookie|terms|legal|gdpr|rodo|disclaimer|accessibility|"
                         r"sitemap|login|sign-in|register")


def careers_links(base: str, html: str) -> list[str]:
    """Links on a page to a careers page or a job list ("Careers", "Vacancies", ...)."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        href = str(a["href"]).strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        words = f"{href} {a.get_text(' ', strip=True)}".lower().replace(" ", "-")
        if any(w in words for w in CAREER_WORDS) and not NOT_CAREERS.search(words):
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
            if shorter_title(TITLE_SPLIT.split(shown)[0], role, city, country):
                _seen()
        if any(same_title(TITLE_SPLIT.split(x)[0], role, city, country)
               or same_title(x, role, city, country) for x in texts if x):
            _seen()
            url = urljoin(base, str(a["href"]).strip()).split("#")[0]
            if url.startswith("http://"):  # 2 Nov: Xccelerated links its jobs with http://
                url = "https://" + url[len("http://"):]
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
    if other_country(final, html, country):
        log.info("job page %s is in another country than %s", final, country)
        return None
    text = fulltext.extract(html) or ""
    if len(text) < _full_min(deps) and deps.render is not None and (
            getattr(deps.render, "left", 1) > 0):
        # A job page built by JavaScript (Zoho Recruit, 2 Nov: Technopride)
        try:
            final, html = deps.render(final)
        except http.HttpError as exc:
            log.info("job page not opened in the browser %s: %s", urlsplit(url).hostname, exc)
        else:
            text = fulltext.extract(html) or text
    return Found(final, text, how) if len(text) >= _full_min(deps) else None


# Countries a job link names in its address (2 Nov: AgileEngine's "devops-engineer-kobie-
# india-id98058" was taken for a Poland job).
OTHER_COUNTRIES = ("india", "usa", "united-states", "canada", "mexico", "brazil",
                   "argentina", "colombia", "chile", "peru", "ukraine", "moldova", "serbia",
                   "romania", "bulgaria", "philippines", "pakistan", "egypt", "morocco",
                   "united-kingdom", "germany", "spain", "portugal", "france", "italy",
                   "czech", "hungary", "georgia", "armenia", "turkey", "vietnam", "china",
                   "singapore", "australia", "south-africa", "nigeria", "kenya", "uae")


def other_country(url: str, html: str, country: str | None) -> bool:
    """The job page is for another country than the row's: its JobPosting's country, or a
    country named in its link."""
    if not country:
        return False
    mine = {w.lower() for w in COUNTRY_WORDS.get(canon(country), (canon(country),))}
    found: set[str] = set()
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except ValueError:
            continue
        for node in data if isinstance(data, list) else [data]:
            if not isinstance(node, dict) or node.get("@type") != "JobPosting":
                continue
            places = node.get("jobLocation") or []
            for place in places if isinstance(places, list) else [places]:
                address = place.get("address") if isinstance(place, dict) else None
                value = address.get("addressCountry") if isinstance(address, dict) else None
                if isinstance(value, dict):
                    value = value.get("name")
                if isinstance(value, str) and value.strip():
                    found.add(canon(value))
    if found:
        return not any(f in mine for f in found)
    words = set(re.split(r"[^a-z0-9]+", urlsplit(url).path.lower()))
    joined = urlsplit(url).path.lower()
    named = {c for c in OTHER_COUNTRIES
             if (c in words if "-" not in c else f"-{c}-" in f"-{joined}-")}
    return bool(named) and not named & mine


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
        if _in_country(boards[key], city, country):
            _listed()
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
    if _in_country(boards[key], city, country):
        _listed()
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
        if _in_country(boards[key], city, country):
            _listed()
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
        if job_list_page(html):
            _listed()
        found = _on_page(deps, final, html, company, role, city, country, how, boards, reads,
                         careers)
        if found is not None:
            return found
        # 4 Nov: also a full-looking careers page that shows no job list (YachtCloud: big
        # menus and footer, the jobs come later by JavaScript); a long page that lists jobs
        # already is never opened again.
        if (deps.render is not None and (depth or _careers_url(final))
                and ats.board_in_page(final, html) is None
                and (thin_page(html) or not job_list_page(html))):
            has = getattr(deps.render, "has", None)
            kept = bool(has and has(url))  # opened for another job of this /fetch: free
            if not kept and (renders[0] <= 0 or getattr(deps.render, "left", 1) <= 0):
                _partial()
                log.info("careers page not opened in the browser %s: no browser pages left "
                         "(this job: %d, this /fetch: %s)", urlsplit(url).hostname,
                         renders[0], getattr(deps.render, "left", "?"))
                if getattr(deps.render, "left", 1) <= 0:
                    _short()  # the sweep's budget, not this job's: try it again next sweep
                continue
            if not kept:
                renders[0] -= 1
            try:
                final, html = deps.render(url)
            except http.HttpError as exc:
                log.info("careers page not opened in the browser %s: %s",
                         urlsplit(url).hostname, exc)
                # 5 Nov: Sii's job list needs the browser, which got HTTP 403; three of its
                # jobs were skipped as ghosts. A list the browser could not open is no check.
                _partial()
            else:
                if job_list_page(html):
                    _listed()
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


def _site_ok(deps: Deps, url: str, company: str, refused: list[str] | None = None) -> bool:
    """An answer of the website search is a page of this company (read once to check).
    `refused` gets the hosts that refused the bot (HTTP 401 or 403)."""
    from jobengine import http

    if not employer_page(url, company):
        return False
    try:
        final, html = deps.page(url)
    except http.HttpError as exc:
        log.info("careers page not read %s: %s", urlsplit(url).hostname, exc)
        if refused is not None and getattr(exc, "status", None) in (401, 403):
            refused.append(urlsplit(url).hostname or url)
        return False
    return employer_page(final, company) and names_company(final, html, company)


def _board_link(url: str) -> bool:
    """A link on a job system (Workday, SuccessFactors, Greenhouse ...), not a company site."""
    from jobengine.sweep.sources import ats

    host = (urlsplit(url).hostname or "").lower()
    return (ats.board_from_url(url) is not None or bool(SHARDLESS_WORKDAY.match(url))
            or any(a in host for a in ATS_HOSTS))


def _checked(deps: Deps, url: str, company: str, refused: list[str] | None = None) -> str:
    """The link when it really is this company's careers page or job board, else ""."""
    board = workday_board(deps, url)
    if board is not None:
        return f"https://{board.host}/{board.site}"
    return url if _site_ok(deps, url, company, refused) else ""


def find_site(deps: Deps, company: str, country: str | None, key: str,
              more: Callable[[], bool] | None = None) -> str:
    """One AI web search for the company's careers page (else its website), checked by
    reading it. "" when nothing usable was found. `more` takes one more search from the
    sweep's budget (the job system search when the site refuses the bot)."""
    from jobengine.llm import LLMError

    user = f"Company: {without_region(company)}\nCountry: {country or 'not given'}"
    try:
        raw, sources = deps.llm.complete_json_with_search(STAGE, SITE_SYSTEM, user,
                                                          max_tokens=400, max_uses=2, key=key)
    except LLMError as exc:
        log.info("careers page search failed for %s: %s", company, exc)
        return ""
    raw = raw or {}
    answers = [str(raw.get(k) or "").strip() for k in ("board", "careers", "website")]
    refused: list[str] = []
    for url in list(dict.fromkeys(u for u in [*answers, *sources] if u))[:MAX_CANDIDATES]:
        found = _checked(deps, url, company, refused)
        if found:
            log.info("careers page for %s: %s", company, found)
            return found
    if refused and (more is None or more()):  # its own site refuses the bot: its job
        found = find_board(deps, company, country, key, refused)  # system may not
        if found:
            return found
    log.info("careers page for %s not found", company)
    return ""


def find_board(deps: Deps, company: str, country: str | None, key: str,
               refused: list[str]) -> str:
    """One more AI web search, for the company's job list on its job system (4 Nov: Serco's
    and PwC's own sites answered HTTP 403). Only a job system's link counts, checked by
    reading it."""
    from jobengine.llm import LLMError

    user = f"Company: {without_region(company)}\nCountry: {country or 'not given'}"
    try:
        raw, sources = deps.llm.complete_json_with_search(STAGE, BOARD_SYSTEM, user,
                                                          max_tokens=300, max_uses=2,
                                                          key=f"{key}-board")
    except LLMError as exc:
        log.info("job system search failed for %s: %s", company, exc)
        return ""
    answer = str((raw or {}).get("board") or "").strip()
    links = [u for u in dict.fromkeys([answer, *sources]) if u and _board_link(u)]
    for url in links[:MAX_CANDIDATES]:
        found = _checked(deps, url, company)
        if found:
            log.info("job system for %s (its site %s refuses the bot): %s", company,
                     ", ".join(dict.fromkeys(refused)), found)
            return found
    log.info("job system for %s not found (its site %s refuses the bot)", company,
             ", ".join(dict.fromkeys(refused)))
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
    answer = first_url((raw or {}).get("url"))
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
            if _company_host(url, company) and _url_names_role(url, role):
                # 5 Nov: Coolblue's own page for the job answered HTTP 405 and the job was
                # skipped as a ghost: the job is there, its page is only unreadable
                _seen()
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
    """The rows ("tried" and "described", ONCE_DAYS) or companies ("no_board",
    NO_BOARD_DAYS) still kept."""
    value = (state.get(KEY) if state is not None else None) or {}
    days = ONCE_DAYS if part in ("tried", DESCRIBED) else NO_BOARD_DAYS
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


def no_company_note(deps: Deps, page_id: str, values: dict[str, Any]) -> None:
    """A row whose alert showed no company ("Hybrid", "1", a job site's name): its company's
    site cannot be checked. Noted once; an Unscreened row is skipped (2 Nov: the same 7 rows
    came back in every /fetch log)."""
    company = values.get("Company")
    text = (f"{NOTE} ({deps.today.isoformat()}): the alert showed no company (it showed "
            f"'{company}'), so the company's own site cannot be checked")
    if str(values.get("Screen verdict") or "Unscreened") == "Unscreened":
        deps.repo.update(page_id, {"Screen verdict": "Skip"})
        text += ": skipped (Screen verdict Skip). /add the job's company link to bring it back."
    else:
        text += "."
    deps.repo.append_body(page_id, [text])
    log.info("no company for %s (%r): noted%s", values.get("Role"), company,
             " and skipped" if "Skip" in text else "")


def _company_host(url: str, company: str) -> bool:
    """The employer's own site (its name in the host) or a job system's page."""
    if not employer_page(url, company):
        return False
    if _board_link(url):
        return True
    host = (urlsplit(url).hostname or "").lower().replace("-", "")
    key = company_key(company)
    words = [w for w in key.split() if len(w) >= 3]
    return any(w in host for w in words) or name_in_host(host, key)


# Title words as job addresses in your countries write them (4 Nov: ASTEK's
# "/devops-inzynier-km-2/" for "DevOps Engineer").
URL_WORDS = {"inzynier": "engineer", "inżynier": "engineer", "ingenieur": "engineer",
             "inzynierka": "engineer"}
JOB_NUMBER = re.compile(r"\d{5,}")


def _url_names_role(url: str, role: str) -> bool:
    """The address itself names the job ("/cloud-engineer-noordwijk-313360" for "Cloud
    Engineer"): every word of the title of 3 letters or more is in its path."""
    path = canon(unquote(urlsplit(url).path).replace("-", " ").replace("_", " "))
    words = [w for w in canon(plain_title(role)).split() if len(w) > 2]
    have = {URL_WORDS.get(w, w) for w in path.split()}
    joined = "".join(path.split())  # 5 Nov: Recruitee's "/o/devopsplatform-engineer"
    return bool(words) and all(w in have or w in joined for w in words)


def _one_job_address(url: str, role: str) -> bool:
    """The address is one job's page, not a careers page or a homepage: it names the job,
    or it carries a job number of 5 digits or more (4 Nov: Serco's "?jobId=313360", Sony's
    Greenhouse ".../jobs/7478061002", Brunel's "...-it003509")."""
    parts = urlsplit(url)
    where = f"{parts.path}?{parts.query}"
    # 4 Nov: CGI's Njoyn link "?J1026-2636": on a job system 4 digits are a job number too
    return (_url_names_role(url, role) or bool(JOB_NUMBER.search(where))
            or (_board_link(url) and bool(re.search(r"\d{4,}", where))))


def _resolved(deps: Deps, url: str) -> str:
    """Gemini sometimes answers with Google's redirect link (4 Nov: ChipSoft): its target."""
    from jobengine import http
    from jobengine.llm import GROUNDING_REDIRECT

    if not url.startswith(GROUNDING_REDIRECT):
        return url
    try:
        return http.redirect_location(url, deps.s) or ""
    except http.HttpError as exc:
        log.info("Google's answer link not resolved: %s", exc)
        return ""


def _names_role(text: str, role: str) -> bool:
    words = [w for w in canon(plain_title(role)).split() if len(w) > 2]
    have = set(canon(text).split())
    return bool(words) and sum(w in have for w in words) >= MIN_TITLE_SHARE * len(words)


def from_google(deps: Deps, company: str, role: str, city: str | None,
                country: str | None, key: str) -> Found | None:
    """The advert's text from Google's copy of the employer's page (4 Nov). Accepted only
    when Google's sources include the employer's own site or job system, the text is long
    enough and names the role; the refusing site itself is never opened."""
    from jobengine.llm import LLMError

    user = (f"Company: {without_region(company)}\nJob title: {role}\n"
            f"City: {city or 'not given'}\nCountry: {country or 'not given'}")
    try:
        model = copy_model(deps.s)
        extra = {"gemini_model": model} if model else {}
        raw, sources = deps.llm.complete_json_with_search(STAGE, GOOGLE_SYSTEM, user,
                                                          max_tokens=2400, max_uses=3,
                                                          key=f"{key}-google", fallback=False,
                                                          **extra)
    except LLMError as exc:
        log.info("Google's copy not read for %s: %s", company, exc)
        return None
    raw = raw or {}
    text = " ".join(str(raw.get("description") or "").split()) if raw.get("found") else ""
    own = [u for u in sources if _company_host(u, company)]
    answer = first_url(raw.get("url"))
    if text and not own:
        answer = _resolved(deps, answer)
    if text and not own and _company_host(answer, company) and _one_job_address(answer, role):
        # 4 Nov: Serco, BNG, IVO ... Google listed no source on the company's site, but its
        # answer was the advert's own address there: the job's title or its job number in
        # it (a site that refuses the bot: Google has the page, the bot may not open it).
        own = [answer]
    if not text or not own:
        hosts = sorted({urlsplit(u).hostname or "" for u in sources} - {""})[:6]
        log.info("Google's copy for %s, %s: %s", company, role,
                 "not found" if not text else "no source on the company's own site "
                 f"(sources: {', '.join(hosts) or 'none'}; answer: {answer or 'none'})")
        return None
    if len(text) < _full_min(deps) or not _names_role(text, role):
        log.info("Google's copy for %s, %s not used: too short or not this job", company, role)
        return None
    url = answer if answer and any(urlsplit(answer).hostname == urlsplit(u).hostname
                                   for u in own) else own[0]
    log.info("Google's copy for %s, %s: %s (%d characters)", company, role, url, len(text))
    return Found(url, text, GOOGLE_HOW)


def blocks(found: Found, board: str, old_url: str | None) -> list[str]:
    """The earlier link, then the description as screening reads it (newest last)."""
    out = [f"{EARLIER} ({board}): {old_url}"] if old_url else []
    out.append(f"{DESCRIPTION_HEADER} company site (direct link for this {board} job, found "
               f"by {found.how}: {found.url})")
    out += textblocks.chunks(textblocks.tidy(found.text), DESCRIPTION_BLOCK_CHARS,
                             DESCRIPTION_MAX_BLOCKS - len(out))
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
    saved = (deps.state.get(KEY) if deps.state is not None else None) or {}
    no_company: dict[str, str] = dict(saved.get("no_company") or {})
    for pid, v in [r for r in rows if not real_company(str(r[1]["Company"]))]:
        if pid not in no_company:
            no_company_note(deps, pid, v)
            no_company[pid] = deps.today.isoformat()
            result.no_company.append(f"{v.get('Role')} ('{v.get('Company')}')")
    rows = [r for r in rows if real_company(str(r[1]["Company"]))]
    rules = normalize.Rules.from_config(deps.s.sweep)
    for pid, v in [r for r in rows if normalize.reposter(str(r[1]["Company"]), rules)]:
        # 4 Nov, your decision: a reposted job (Jobgether ...) is deleted from the list
        # (Notion keeps it in its trash for 30 days). One you approved or built a resume
        # for stays: that was your choice.
        if v.get("Status") not in ("New", "Screened"):
            continue
        try:
            deps.repo.archive(pid)
        except Exception as exc:  # 4 Nov: one failed delete stopped every job's lookup
            log.warning("could not delete %s, %s: %s", v.get("Company"), v.get("Role"), exc)
            continue
        result.deleted.append(f"{v.get('Company')}, {v.get('Role')}")
        log.info("deleted %s, %s: a reposting site, not the employer", v.get("Company"),
                 v.get("Role"))
    rows = [r for r in rows if not normalize.reposter(str(r[1]["Company"]), rules)]
    rows.sort(key=lambda r: str(r[1].get("Posted date") or ""), reverse=True)
    tried = _remembered(deps.state, deps.today, "tried")
    no_board = _remembered(deps.state, deps.today, "no_board")
    sites = _sites(deps.state, deps.today)
    described = _remembered(deps.state, deps.today, DESCRIBED)
    # 10 Nov: a better lookup no longer looks up the jobs and companies not found before
    # again (5 and 7 Nov: that cost most of the month's Google searches). One look-up a job.
    cutoff = (deps.today - timedelta(days=MISSES_KEEP_DAYS)).isoformat()
    misses: dict[str, list[str]] = {
        k: [d for d in v if str(d) > cutoff] for k, v in (saved.get(MISSES) or {}).items()
        if isinstance(v, list)}
    misses = {k: v for k, v in misses.items() if v}
    noted: dict[str, str] = {k: v for k, v in (saved.get("noted") or {}).items()
                             if str(v) > cutoff}
    incomplete = _incomplete(deps.state, deps.today)
    job_boards: dict[str, Any] = {}
    allowed = int(cfg.get("max_searches", DEFAULT_MAX_SEARCHES)) if deps.llm else 0
    searches = Budget(allowed)
    lock = threading.Lock()  # counters and Notion writes

    body_reads = 0

    def has_description(page_id: str, values: dict[str, Any]) -> bool:
        """The full description is on the page already (a screened row had one)."""
        nonlocal body_reads
        if page_id in described:
            return True
        if str(values.get("Screen verdict") or "Unscreened") != "Unscreened":
            return True
        if body_reads >= MAX_BODY_READS:
            return False
        from jobengine.screen.runner import description

        body_reads += 1
        try:
            texts = [text for _id, text in deps.repo.body_blocks(page_id)]
        except Exception as exc:  # unreadable page: look it up as before
            log.info("direct job links: page %s not read: %s", page_id, exc)
            return False
        return description(texts)[1] == "full"  # as screening reads it

    groups: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for page_id, values in rows:
        if result.tried >= max_rows:
            break
        if page_id in tried:
            continue
        if (values.get("Status") not in WITH_LINK_STATUSES
                and has_description(page_id, values)):
            described.setdefault(page_id, deps.today.isoformat())
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
        url = find_site(deps, company, country, page_id, more=search)
        sites[key] = {"url": url, "date": deps.today.isoformat()}
        if url:
            with lock:
                result.sites += 1
        return url

    def learned_board(company: str, role: str) -> None:
        """13 Nov (fetch idea 2): a job board learned from an earlier job link
        (sweep/auto_boards.py) is the company's job list: read it first, free, no AI."""
        from jobengine.sweep import auto_boards
        from jobengine.sweep.sponsors import short_name

        name = short_name(without_region(company))
        key = canon_company(name)
        if key in job_boards:
            return
        board = auto_boards.board_for(deps.state, company)
        if board is not None:
            job_boards[key] = _board_postings(deps, name, board, role)
            log.info("job board of %s learned before: %s", company, board.ats)

    def learn(company: str, urls: list[str]) -> None:
        """A job board this lookup found (its job link or careers page): read in every
        /fetch from now on (sweep/auto_boards.py)."""
        from jobengine.sweep import auto_boards

        pairs = [(company, u) for u in urls if u and auto_boards.board_of(u) is not None]
        if not pairs or not auto_boards.enabled(deps.s):
            return
        with lock:
            try:
                learned = auto_boards.learn(deps.state, pairs[:1], deps.targets, deps.config,
                                            (), deps.today, deps.s)
            except Exception:  # never fail a lookup over the learned boards
                log.exception("learning a job board failed")
                return
            result.boards_learned.extend(learned)

    def lookup(page_id: str, values: dict[str, Any]) -> str | None:
        """One job: its link on the company's site, written to Notion. The label or None."""
        # "DevOps Engineer @ Scalo" (Adzuna) is searched for as "DevOps Engineer"
        company, role = str(values["Company"]), plain_title(str(values["Role"]))
        city, country = values.get("City"), values.get("Country")
        careers = ""
        learned_board(company, role)
        found = from_board(deps, company, role, city, country, job_boards, no_board)
        if found is None:
            careers = careers_page(company, country, page_id)
            if careers:
                found = from_site(deps, careers, company, role, city, country, job_boards)
        # With a careers page read, a search for the exact job finds no more (30 Oct:
        # 0 of 20), so the searches go to the next companies instead.
        if found is None and not careers and search():
            found = from_search(deps, company, role, city, country, page_id)
        # Its site could not be checked, or the job's page could not be read: Google's copy
        # (never when its job list was read without this job: that is a ghost job).
        # 4 Nov: also when its job list needed the browser and no browser page was left for
        # this job (TNO, CGI, SURF: the list was never really read).
        if (found is None and deps.llm is not None and not getattr(_SHORT, "hit", False)
                and (getattr(_CHECK, "seen", False) or not getattr(_CHECK, "read", False)
                     or getattr(_CHECK, "partial", False))
                and search()):
            found = from_google(deps, company, role, city, country, page_id)
        board = str(values.get("Board") or "alert")
        if found is None:
            if getattr(_SHORT, "hit", False):
                log.info("no direct link for %s, %s yet (looked up again next /fetch); "
                         "closest titles seen: %s", company, role, closest())
                return None
            log.info("no direct link for %s, %s: %s; closest titles seen: %s", company, role,
                     "its job list needs the browser, no browser page was left"
                     if getattr(_CHECK, "partial", False)
                     else "not on its job list" if getattr(_CHECK, "read", False)
                     else "its site could not be checked", closest())
            missing(page_id, values, company, role, board)
            return None
        learn(company, [found.url, careers])  # its job board is read in every /fetch now
        # The same text may be on the page already: the cross-match of this sweep read the
        # job on the company's job board (2 Nov: Snowflake's description twice).
        has_it = any(found.url in text for _id, text in deps.repo.body_blocks(page_id))
        with lock:
            deps.repo.update(page_id, {"URL": found.url})
            if not has_it:
                deps.repo.append_body(page_id, blocks(found, board, values.get("URL")))
                if with_body is not None:
                    with_body.add(page_id)
        log.info("%s row %s: direct link %s (%s)%s", board, page_id, found.url, found.how,
                 ", its description was on the page already" if has_it else "")
        return f"{company}, {role} ({found.how})"

    def missing(page_id: str, values: dict[str, Any], company: str, role: str,
                board: str) -> None:
        """A note on the page: not on the company's job list (skipped at the second check),
        or the company's site could not be checked (waits for /jd)."""
        today = deps.today.isoformat()
        unscreened = str(values.get("Screen verdict") or "Unscreened") == "Unscreened"
        seen = getattr(_CHECK, "seen", False)
        why = ""
        if seen:
            why = "the job is on its job list, but its page could not be read"
        elif not getattr(_CHECK, "read", False):
            why = "no job list found on its site, or the site did not answer"
        elif getattr(_CHECK, "partial", False):  # 2 Nov: Infor's list needs the browser
            why = "its job list needs the browser, and no browser page was left for it"
        elif company_key(company) in incomplete:  # 2 Nov: Capgemini's board, Dutch jobs
            why = ("its job board does not list all its jobs (the summary's 'job board did not "
                   "list' line): put the right Careers URL in Target Companies")
        if why:
            with lock:
                result.unchecked += 1
                if page_id in noted:
                    return
                noted[page_id] = today
                deps.repo.append_body(page_id, [
                    f"{NOTE} ({today}): {company}'s own site could not be checked ({why}). "
                    "The job waits for /jd."])
            return
        with lock:
            days = misses.setdefault(page_id, [])
            if today not in days:
                days.append(today)
            if len(days) < MISSES_TO_SKIP:
                result.not_yet += 1
                deps.repo.append_body(page_id, [
                    f"{NOTE} ({today}): not on {company}'s own job list (likely a ghost job, "
                    "or not there yet). It is not looked up again: it waits for /jd (paste "
                    "it or send its saved page), or skip it."])
                return
            text = (f"{NOTE} ({today}): not on {company}'s own job list (checked "
                    f"{', '.join(days)}). It is only on {board}: likely a ghost job or one the "
                    "company does not publish itself")
            if unscreened:
                deps.repo.update(page_id, {"Screen verdict": "Skip"})
                text += ", so it is skipped (Screen verdict Skip)."
            else:
                text += "."
            deps.repo.append_body(page_id, [text])
            result.ghosts.append(f"{company}, {role}")

    def company_jobs(group: list[tuple[str, dict[str, Any]]]) -> list[str]:
        labels = []
        for page_id, values in group:
            CONTROL.check()  # after /end a worker stops before its next job
            _SHORT.hit = False
            _CHECK.read = False
            _CHECK.seen = False
            _CHECK.partial = False
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
        deps.state.set(KEY, {"tried": tried, DESCRIBED: described, "no_board": no_board,
                             "sites": sites,
                             MISSES: misses, "noted": noted, "no_company": no_company,
                             "version": LOOKUP_VERSION})
    return result
