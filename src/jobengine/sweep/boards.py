"""Check every Target Company's job board link (FETCH 13, 5 Oct).

    python -m jobengine.sweep --check-boards

For each active company the link checked is the one proposed in config/board_candidates.yaml
(found by web search), else its Careers URL. A link to a job board /fetch reads (Greenhouse,
Lever, SmartRecruiters, Workday, Ashby, Avature, amazon.jobs) is read the way /fetch reads it
and its jobs are counted, all jobs and those in Poland, the Netherlands or Ireland. Any other
link is opened once: the answer (HTTP code), the platform the site runs on when it is a known
one, and a job board it links to are noted, and the page is saved in out/boards so a reader can
be built from it. Writes nothing to Notion. The table is printed and saved as
out/boards/report.csv.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from jobengine import http
from jobengine.parallel import Budget
from jobengine.settings import CONFIG_DIR, ROOT_DIR, Settings
from jobengine.sweep.models import RawPosting, TargetCompany
from jobengine.sweep.sources import ats

log = logging.getLogger("jobengine.sweep")

CANDIDATES = CONFIG_DIR / "board_candidates.yaml"
OUT_DIR = ROOT_DIR / "out" / "boards"
# Career site platforms /fetch cannot read yet, recognised by their links.
PLATFORMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Oracle Cloud HCM", re.compile(r"oraclecloud\.com/hcmUI|/sites/CX_\d+|/sites/jobsearch\b",
                                    re.I)),
    ("Eightfold", re.compile(r"/careers/job/\d{9,}|[?&]domain=[\w-]+\.[\w.]+|"
                             r"apply\.careers\.microsoft\.com", re.I)),
    ("Phenom", re.compile(r"/(?:global|[a-z]{2})/[a-z]{2}/(?:job|search-results|c)\b", re.I)),
    ("SuccessFactors", re.compile(r"/job/[^/?#\s\"']+/\d{5,}(?:-[a-z]{2}_[A-Z]{2})?/|"
                                  r"/go/[^/?#\s\"']+/\d{5,}/|"
                                  r"successfactors\.(?:com|eu)", re.I)),
    ("Workable", re.compile(r"(?:apply\.)?workable\.com/", re.I)),
    ("Recruitee", re.compile(r"\.recruitee\.com", re.I)),
    ("Personio", re.compile(r"\.jobs\.personio\.(?:de|com)", re.I)),
    ("BrassRing", re.compile(r"brassring\.com|/reqid/\d+BR\b", re.I)),
    ("Taleo", re.compile(r"taleo\.net", re.I)),
    ("iCIMS", re.compile(r"icims\.com", re.I)),
)

Getter = Callable[[str, Mapping[str, Any]], Any]
Poster = Callable[[str, Any], Any]
PageGetter = Callable[[str], tuple[str, str]]


@dataclass
class Check:
    company: str
    region: str
    link: str
    origin: str  # "proposed" (board_candidates.yaml) or "Notion" (Careers URL)
    result: str  # "OK", "HTTP 404", "no reader yet", "careers page, no job board found", ...
    platform: str = ""  # greenhouse, workday, ..., or a PLATFORMS name, or ""
    jobs: int | None = None
    here: int | None = None  # jobs in Poland, the Netherlands or Ireland
    found_link: str = ""  # a job board link found on the page
    saved: str = ""


def load_candidates(path: Path = CANDIDATES) -> dict[str, str]:
    """Company name -> proposed link; {} when the file is missing or empty."""
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    proposed = data.get("proposed") or {}
    return {str(name): str(link).strip() for name, link in proposed.items() if link}


def platform_of(url: str, page: str = "") -> str:
    """The career site platform a URL (or the links on its page) belongs to, or ""."""
    for name, pattern in PLATFORMS:
        if pattern.search(url):
            return name
    counts = {name: len(pattern.findall(page)) for name, pattern in PLATFORMS}
    best = max(counts, key=lambda name: counts[name]) if page else ""
    return best if best and counts[best] >= 3 else ""


def place_words(s: Settings) -> tuple[str, ...]:
    """Country names and city spellings of sweep.locations, lower case."""
    words: list[str] = []
    for country, spec in (s.sweep.get("locations") or {}).items():
        words.append(str(country).lower())
        words += [str(w).lower() for w in (spec or {}).get("names") or []]
        for aliases in ((spec or {}).get("cities") or {}).values():
            words += [str(w).lower() for w in aliases or []]
    return tuple(dict.fromkeys(words))


def count_here(postings: list[RawPosting], words: tuple[str, ...]) -> int:
    return sum(1 for p in postings if any(w in (p.location_text or "").lower() for w in words))


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "company"


def _keep_all(title: str, location: str) -> bool:
    return True


def read_board(s: Settings, company: TargetCompany, board: ats.Board, get: Getter,
               post: Poster, page: PageGetter, today: date,
               feed: PageGetter | None = None) -> list[RawPosting]:
    """Every posting of a supported board (no detail calls), as /fetch would see it."""
    cfg = s.sweep.get("ats") or {}
    terms = tuple(cfg.get("search_terms") or ats.DEFAULT_SEARCH_TERMS)
    if board.ats == "greenhouse":
        postings, _, _ = ats.greenhouse(company, board, get, _keep_all)
    elif board.ats == "lever":
        postings, _, _ = ats.lever(company, board, get, _keep_all)
    elif board.ats == "smartrecruiters":
        postings, _, _ = ats.smartrecruiters(company, board, get, _keep_all, Budget(0))
    elif board.ats == "workday":
        postings, _, _ = ats.workday(company, board, get, post, _keep_all, Budget(0), today,
                                     terms, 1)
    elif board.ats == "ashby":
        postings, _, _ = ats.ashby(company, board, get, _keep_all)
    elif board.ats == "avature":
        postings, _, _ = ats.avature(company, board, page, _keep_all)
    elif board.ats == "successfactors":
        postings, _, _ = ats.successfactors(company, board, feed or page, _keep_all, terms)
    elif board.ats == "phenom":
        postings, _, _ = ats.phenom(company, board, page, _keep_all, terms)
    elif board.ats == "oracle":
        postings, _, _ = ats.oracle(company, board, get, _keep_all, terms)
    else:
        postings, _, _ = ats.amazon(company, get, _keep_all, terms)
    return postings


def _problem(exc: http.HttpError) -> str:
    return f"HTTP {exc.status}" if exc.status else f"failed: {str(exc)[:80]}"


def check_company(s: Settings, company: TargetCompany, link: str, origin: str, get: Getter,
                  post: Poster, page: PageGetter, today: date, out_dir: Path,
                  words: tuple[str, ...]) -> Check:
    row = Check(company.name, company.region or "", link, origin, "")
    if not link:
        row.result = "no link"
        return row
    board = ats.board_from_url(link)
    if board is None:
        try:
            final_url, text = page(link)
        except http.HttpError as exc:
            row.result = _problem(exc)
            return row
        out_dir.mkdir(parents=True, exist_ok=True)
        saved = out_dir / f"{_slug(company.name)}.html"
        saved.write_text(text, encoding="utf-8")
        row.saved = str(saved)
        board = ats.board_in_page(final_url, text)
        if board is None:
            row.platform = platform_of(final_url, text)
            row.result = "no reader yet" if row.platform else "careers page, no job board found"
            return row
        row.found_link = board.host or board.token
    row.platform = board.ats
    answers: list[Any] = []  # the board's first JSON answer, kept when no job is local

    def get_kept(url: str, params: Mapping[str, Any]) -> Any:
        data = get(url, params)
        answers.append(data)
        return data

    def post_kept(url: str, body: Any) -> Any:
        data = post(url, body)
        answers.append(data)
        return data

    try:
        postings = read_board(s, company, board, get_kept, post_kept, page, today)
    except http.HttpError as exc:
        row.result = _problem(exc)
        return row
    row.result = "OK"
    row.jobs = len(postings)
    row.here = count_here(postings, words)
    if row.here == 0 and answers:
        # Shows the site's own filters (countries, locations) for a better search.
        out_dir.mkdir(parents=True, exist_ok=True)
        saved = out_dir / f"{_slug(company.name)}-answer.json"
        saved.write_text(json.dumps(answers[0], ensure_ascii=False, indent=1)[:2_000_000],
                         encoding="utf-8")
        row.saved = str(saved)
    return row


def run(s: Settings, companies: list[TargetCompany], candidates: Mapping[str, str],
        get: Getter, post: Poster, page: PageGetter, today: date,
        out_dir: Path = OUT_DIR) -> list[Check]:
    """One Check per active company, in Target Companies order. Companies sharing a link are
    read once."""
    words = place_words(s)
    done: dict[str, Check] = {}
    rows = []
    for company in companies:
        if not company.active:
            continue
        link = candidates.get(company.name) or company.careers_url or ""
        origin = "proposed" if company.name in candidates else "Notion"
        if link in done:
            first = done[link]
            rows.append(Check(company.name, company.region or "", link, origin, first.result,
                              first.platform, first.jobs, first.here, first.found_link,
                              first.saved))
            continue
        row = check_company(s, company, link, origin, get, post, page, today, out_dir, words)
        done[link] = row
        rows.append(row)
    unknown = sorted(set(candidates) - {c.name for c in companies})
    for name in unknown:
        rows.append(Check(name, "", candidates[name], "proposed",
                          "not in Target Companies (check the name)"))
    return rows


def _jobs(row: Check) -> str:
    return "" if row.jobs is None else f"{row.jobs} jobs, {row.here} in PL/NL/IE"


def report_text(rows: list[Check]) -> str:
    ok = sum(1 for r in rows if r.result == "OK")
    lines = [f"Job board check: {ok} of {len(rows)} companies readable by /fetch "
             "(nothing written to Notion)", ""]
    for r in rows:
        parts = [r.result, r.platform, _jobs(r), f"found {r.found_link}" if r.found_link else ""]
        lines.append(f"- {r.company} [{r.origin}]: " + " | ".join(p for p in parts if p))
        lines.append(f"    {r.link}")
    return "\n".join(lines)


def save_csv(rows: list[Check], out_dir: Path = OUT_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "report.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Company", "Region", "Checked link", "Link from", "Result", "Platform",
                         "Jobs", "In PL/NL/IE", "Job board found on page", "Saved page"])
        for r in rows:
            writer.writerow([r.company, r.region, r.link, r.origin, r.result, r.platform,
                             "" if r.jobs is None else r.jobs,
                             "" if r.here is None else r.here, r.found_link, r.saved])
    return path


def check_all(s: Settings, today: date) -> str:
    """The real run: Target Companies from Notion, the network through jobengine.http."""
    from jobengine.notion_repo import NotionClient, NotionReader

    if not s.notion_token:
        return "Board check cannot run: NOTION_TOKEN missing"
    companies = NotionReader(NotionClient(s.notion_token, s), s).target_companies()

    def get(url: str, params: Mapping[str, Any]) -> Any:
        return http.get_json(url, params=params or None, s=s)

    def post(url: str, body: Any) -> Any:
        return http.post_json(url, json=body, s=s)

    def page(url: str) -> tuple[str, str]:
        # SuccessFactors job feeds are RSS: accepted here too.
        return http.get_page(url, s=s, accept_feed=True)

    rows = run(s, companies, load_candidates(), get, post, page, today)
    path = save_csv(rows)
    return f"{report_text(rows)}\n\nSaved {path}; zip the out/boards folder and send it."
