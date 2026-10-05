"""FETCH 1: measure job sites before a reader is built for them. Writes nothing to Notion.

`python -m jobengine.sweep --probe <site>|all` runs one search per approved title on each
site, newest first, and prints per site and title: the HTTP answer (or the block), the kind
of answer (JSON, page data JSON in a web page, plain web page), its size, and a rough count
of dates in the last 2 days. One raw answer per site and title is saved in out/probes/, so
the reader can be built against real data (Claude's environment cannot reach these sites).

The search addresses are the ones the sites' own pages use, as far as public sources tell;
a wrong one shows up as an HTTP error in the table, which is what the probe is for.

Rules, the same as for the readers that follow: robots.txt is read first and a disallowed
address is skipped; one request per second; plain requests only (no browser, no login, no
cookies, nothing that works around a site's bot protection); LinkedIn is never probed.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus, urlsplit
from urllib.robotparser import RobotFileParser

from jobengine import http
from jobengine.settings import ROOT_DIR, Settings

TITLES = ("DevOps Engineer", "Site Reliability Engineer", "Platform Engineer", "Cloud Engineer")
PAUSE_SECONDS = 1.0
DAYS = 2
OUT_DIR = ROOT_DIR / "out" / "probes"


@dataclass(frozen=True)
class Site:
    name: str
    label: str
    url: str  # {q}: the title for a query string, {path}: the title for a URL path
    body: Callable[[str], Any] | None = None  # POST with this JSON (EURES)


def _eures_body(title: str, where: str = "EVERYWHERE", quoted: bool = False,
                period: str | None = None) -> dict[str, Any]:
    keyword = f'"{title.lower()}"' if quoted else title.lower()
    return {"resultsPerPage": 50, "page": 1, "sortSearch": "MOST_RECENT",
            "keywords": [{"keyword": keyword, "specificSearchCode": where}],
            "publicationPeriod": period, "occupationUris": [], "skillUris": [],
            "requiredExperienceCodes": [], "positionScheduleCodes": [], "sectorCodes": [],
            "educationAndQualificationLevelCodes": [], "positionOfferingCodes": [],
            "locationCodes": ["pl", "nl", "ie"], "euresFlagCodes": [], "otherBenefitsCodes": [],
            "requiredLanguages": [], "minNumberPost": None, "sessionId": "job-engine-probe",
            "requestLanguage": "en"}


SITES = {site.name: site for site in (
    Site("justjoin", "JustJoin IT",
         "https://api.justjoin.it/v2/user-panel/offers?keyword={q}&sortBy=publishedAt"
         "&orderBy=DESC&perPage=100&page=1"),
    Site("nofluffjobs", "NoFluffJobs",
         "https://nofluffjobs.com/pl/?criteria=keyword%3D{q}&sort=newest"),
    Site("pracuj", "Pracuj.pl", "https://www.pracuj.pl/praca/{path};kw?sc=0"),
    Site("theprotocol", "theprotocol.it", "https://theprotocol.it/filtry/{path};kw?sort=date"),
    Site("eures", "EURES (PL, NL, IE)",
         "https://europa.eu/eures/api/jv-searchengine/public/jv-search/search", _eures_body),
    # 5 Oct: the plain search matched any word (31,000 mostly unrelated Polish jobs, not by
    # date). Two tighter searches to compare: title only, and the exact phrase; last 3 days.
    Site("eures_title", "EURES title, 3 days",
         "https://europa.eu/eures/api/jv-searchengine/public/jv-search/search",
         lambda t: _eures_body(t, "TITLE", period="LAST_THREE_DAYS")),
    Site("eures_phrase", "EURES phrase, 3 days",
         "https://europa.eu/eures/api/jv-searchengine/public/jv-search/search",
         lambda t: _eures_body(t, quoted=True, period="LAST_THREE_DAYS")),
    Site("iamexpat", "IamExpat",
         "https://www.iamexpat.nl/career/jobs-netherlands?search={q}&language=english"),
    # 5 Oct: the search word is ignored (every title gave the same newest 20 jobs); the IT
    # category page lists only IT jobs, which the reader then filters by title.
    Site("iamexpat_it", "IamExpat IT jobs",
         "https://www.iamexpat.nl/career/jobs-netherlands/it-technology-positions"),
    Site("nvb", "Nationale Vacaturebank",
         "https://www.nationalevacaturebank.nl/vacatures/zoeken?query={q}&sort=date"),
    Site("irishjobs", "IrishJobs.ie", "https://www.irishjobs.ie/jobs/{path}?sort=2"),
)}

NEXT_DATA = re.compile(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
ISO_DATE = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)")


@dataclass
class Result:
    site: str
    title: str
    status: str  # "OK", "HTTP 403", "robots.txt disallows", "failed: ..."
    kind: str = ""  # "JSON", "page data JSON", "web page"
    size: int = 0
    recent_dates: int = 0
    saved: str = ""


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)

    def text(self) -> str:
        lines = [f"Probe (last {DAYS} days, titles: {', '.join(TITLES)}). Nothing written "
                 "to Notion. Send this table and the files in out/probes to Claude.", ""]
        for r in self.results:
            if r.status != "OK":
                lines.append(f"- {r.site} | {r.title}: {r.status}")
                continue
            lines.append(f"- {r.site} | {r.title}: OK, {r.kind}, {r.size // 1024} KB, "
                         f"about {r.recent_dates} dates in the last {DAYS} days, saved "
                         f"{r.saved}")
        return "\n".join(lines)


def search_url(site: Site, title: str) -> str:
    return site.url.format(q=quote_plus(title.lower()),
                           path=quote(title.lower().replace(" ", "-")))


def recent_dates(text: str, today: date, days: int = DAYS) -> int:
    """Rough: ISO dates in the answer that fall in the last `days` days (a posted date
    usually appears once per job)."""
    first = today - timedelta(days=days)
    count = 0
    for match in ISO_DATE.finditer(text):
        try:
            found = date(int(match[1]), int(match[2]), int(match[3]))
        except ValueError:
            continue
        if first <= found <= today:
            count += 1
    return count


def kind_of(text: str) -> tuple[str, str]:
    """("JSON" | "page data JSON" | "web page", the part worth saving)."""
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            return "JSON", json.dumps(json.loads(stripped), indent=1, ensure_ascii=False)
        except ValueError:
            pass
    found = NEXT_DATA.search(text)
    if found:
        try:
            return "page data JSON", json.dumps(json.loads(found.group(1)), indent=1,
                                                ensure_ascii=False)
        except ValueError:
            pass
    return "web page", text


Getter = Callable[..., tuple[str, str]]


def allowed_by_robots(url: str, get: Getter, cache: dict[str, RobotFileParser | None]) -> bool:
    parts = urlsplit(url)
    root = f"{parts.scheme}://{parts.netloc}"
    if root not in cache:
        try:
            _, text = get(f"{root}/robots.txt")
            parser = RobotFileParser()
            parser.parse(text.splitlines())
            cache[root] = parser
        except http.HttpError:
            cache[root] = None  # no robots.txt: nothing is disallowed
    parser = cache[root]
    return parser is None or parser.can_fetch(http.PAGE_USER_AGENT, url)


def run(s: Settings, names: list[str], today: date, *, get: Getter | None = None,
        out_dir: Path = OUT_DIR, pause: Callable[[float], None] = time.sleep,
        titles: tuple[str, ...] = TITLES) -> Report:
    def default_get(url: str, **kw: Any) -> tuple[str, str]:
        return http.get_page(url, s=s, **kw)

    get = get or default_get
    robots: dict[str, RobotFileParser | None] = {}
    report = Report()
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        site = SITES[name]
        # A page without a search word in it is the same for every title: read it once.
        once = site.body is None and "{q}" not in site.url and "{path}" not in site.url
        for title in titles[:1] if once else titles:
            url = search_url(site, title)
            result = Result(site=site.label, title=title, status="OK")
            report.results.append(result)
            if not allowed_by_robots(url, get, robots):
                result.status = "robots.txt disallows this search: skipped"
                continue
            pause(PAUSE_SECONDS)
            try:
                body = site.body(title) if site.body else None
                _, text = get(url, accept_json=True, json_body=body)
            except http.HttpError as exc:
                result.status = (f"HTTP {exc.status} (blocked or wrong address)" if exc.status
                                 else f"failed: {exc}")
                continue
            result.kind, keep = kind_of(text)
            result.size = len(text)
            result.recent_dates = recent_dates(text, today)
            ext = "html" if result.kind == "web page" else "json"
            path = out_dir / f"{name}-{title.lower().replace(' ', '-')}.{ext}"
            path.write_text(keep, encoding="utf-8")
            result.saved = path.relative_to(out_dir.parent.parent).as_posix() \
                if path.is_relative_to(out_dir.parent.parent) else path.as_posix()
    return report
