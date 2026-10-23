"""/add <job link> (23 Oct, your request): a job you open from LinkedIn or another app usually
sends you to the company's own site. Paste that link and the bot reads the job page, saves the
job, screens it, builds your resume, and adds the company to Target Companies (with its job
board when the link shows one), so every /fetch checks that company from then on.

Pure parts here; the desk (screen/desk.py) does the reads and writes.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from jobengine.sweep import fulltext
from jobengine.sweep.normalize import without_region
from jobengine.sweep.sources.ats import Board, board_from_url, canon_name

COUNTRIES = ("Poland", "Netherlands", "Ireland")
# The ATS platform options of Target Companies; any other board is "Custom".
ATS_OPTIONS = {"greenhouse": "Greenhouse", "lever": "Lever", "workday": "Workday",
               "smartrecruiters": "SmartRecruiters"}
COUNTRY_CODES = {"PL": "Poland", "NL": "Netherlands", "IE": "Ireland", "POL": "Poland",
                 "NLD": "Netherlands", "IRL": "Ireland"}


@dataclass
class PageJob:
    """What a job page says about itself (schema.org JobPosting first, the page next)."""

    url: str
    title: str = ""
    company: str = ""
    location: str = ""
    description: str | None = None
    valid_through: str = ""


def _text(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("name") or ""
    return str(value or "").strip()


def _country(value: Any) -> str:
    name = _text(value)
    return COUNTRY_CODES.get(name.upper(), name)


def _locations(value: Any) -> str:
    places = []
    for place in value if isinstance(value, list) else [value]:
        if not isinstance(place, Mapping):
            continue
        address = place.get("address") or {}
        if isinstance(address, Mapping):
            parts = [_text(address.get("addressLocality")), _country(address.get("addressCountry"))]
            places.append(", ".join(p for p in parts if p))
    return " / ".join(dict.fromkeys(p for p in places if p))


def _posting(soup: BeautifulSoup) -> Mapping[str, Any] | None:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except ValueError:
            continue
        for node in fulltext._walk(data):
            if fulltext._is_job_posting(node):
                return node
    return None


def _meta(soup: BeautifulSoup, name: str) -> str:
    tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
    return str(tag.get("content") or "").strip() if tag else ""


def read_page(url: str, page: str) -> PageJob:
    """Title, company, place and description of a job page; empty fields when unknown."""
    soup = BeautifulSoup(page, "html.parser")
    job = PageJob(url=url, description=fulltext.extract(page))
    posting = _posting(soup)
    if posting:
        job.title = _text(posting.get("title"))
        job.company = _text(posting.get("hiringOrganization"))
        job.location = _locations(posting.get("jobLocation"))
        if posting.get("jobLocationType") == "TELECOMMUTE":
            job.location = f"{job.location} (Remote)".strip()
        job.valid_through = _text(posting.get("validThrough"))
    if not job.title:
        title = _meta(soup, "og:title") or (soup.title.get_text() if soup.title else "")
        job.title = title.split("|")[0].split(" - ")[0].strip()
    if not job.company:
        job.company = _meta(soup, "og:site_name")
    return job


def board_link(board: Board) -> str:
    """The job board's own list page, kept as the company's Careers URL."""
    links = {
        "greenhouse": f"https://boards.greenhouse.io/{board.token}",
        "lever": f"https://jobs{'.eu' if board.eu else ''}.lever.co/{board.token}",
        "smartrecruiters": f"https://careers.smartrecruiters.com/{board.token}",
        "workday": f"https://{board.host}/{board.site}" if board.host else "",
        "ashby": f"https://jobs.ashbyhq.com/{board.token}",
        "workable": f"https://apply.workable.com/{board.token}/",
        "recruitee": f"https://{board.token}.recruitee.com/",
        "personio": f"https://{board.token}.jobs.personio.{board.site or 'de'}/",
        "teamtailor": f"https://{board.token}.teamtailor.com/jobs",
    }
    return links.get(board.ats, "")


def careers_url(job_url: str) -> tuple[str, str]:
    """(Careers URL, ATS platform option) for the company of a job link: its job board when
    the link shows one, else the site's home page (its careers page is found from there)."""
    board = board_from_url(job_url)
    if board is not None and board_link(board):
        return board_link(board), ATS_OPTIONS.get(board.ats, "Custom")
    parts = urlsplit(job_url)
    return f"{parts.scheme}://{parts.netloc}/", "Unknown"


def known_company(name: str, companies: Iterable[str]) -> str | None:
    """The Target Companies row that is this company ("Acme Cloud (IE)" for "Acme Cloud")."""
    key = canon_name(without_region(name))
    for company in companies:
        if canon_name(without_region(company)) == key:
            return company
    return None


def company_props(company: str, job_url: str, country: str | None, today: date) -> dict[str, Any]:
    """A new Target Companies row: never more than these fields (safety allowlist)."""
    url, platform = careers_url(job_url)
    props: dict[str, Any] = {
        "Company": company,
        "Careers URL": url,
        "ATS platform": platform,
        "Active": True,
        "Last checked": today,
        "Notes": f"Added by /add on {today.isoformat()} from {job_url}",
    }
    if country in COUNTRIES:
        props["Region"] = country
    return props
