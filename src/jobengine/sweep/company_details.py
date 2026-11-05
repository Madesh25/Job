"""Target Companies details (5 Nov, your ask): "it's not fully added its details like its URL,
tier, domain".

A company the bot put into Target Companies itself (/newcompanies, /add, the companies of
your applications: its Notes start with "Added by /" or "Added from your application") gets
its empty columns filled by the next /fetch, a few companies a run:

- Careers URL and ATS platform: one AI web search (Gemini with Google Search) for its careers
  page or job system, each link checked by reading it, as the direct job links pass does
  (sweep/job_links.py). A site that refuses the bot is never opened again.
- Domain: its website's address (without www.), the email domain the contact finder uses.
- Hub / City: its main office or tech hub in the row's country, from the same search.
- Tier: one of the table's tiers, from the same search.
- IND sponsor: Dutch companies from the IND register (Verified or Not listed), "N/A" for
  the other countries. No search.

Only empty columns are written, never one you filled; the rows you added by hand are never
touched. A company is looked up again after RETRY_DAYS only when something is still empty.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from jobengine.sweep import job_links

log = logging.getLogger(__name__)

KEY = "companies.details"  # bot_state: {"tried": {company name: iso day}}
RETRY_DAYS = 14
DEFAULT_MAX_PER_RUN = 20
BOT_NOTES = ("Added by /", "Added from your application")
FIELDS = ("Careers URL", "ATS platform", "Domain", "Hub / City", "Tier", "IND sponsor")
TIERS = ("Tier 1 - Indian IT", "Tier 2 - Tech/Cloud", "Tier 3 - Banks/Fintech",
         "Tier 4 - Consulting/GBS", "Tier 5 - Polish product",
         "Tier 5 - Local product (NL/IE)", "Tier 6 - Automotive/Industrial")
ATS_NAMES = {"greenhouse": "Greenhouse", "lever": "Lever", "workday": "Workday",
             "smartrecruiters": "SmartRecruiters"}
MAX_CITY = 40

DETAILS_SYSTEM = f"""You look up one company for a job seeker's list of target employers.
Search the web, then answer with JSON only:
{{"website": "<link>", "careers": "<link>", "board": "<link>", "city": "<city>",
"tier": "<tier>"}}.
"website" is the company's official website. "careers" is the page that lists its open jobs,
on its own site or its applicant tracking system. "board" is the job list on its applicant
tracking system when it uses one (Workday myworkdayjobs.com, SuccessFactors, Greenhouse,
Lever, SmartRecruiters, Teamtailor, Recruitee, Personio, Workable and similar), copied
exactly from a search result, never made up. Never a LinkedIn, Indeed, Glassdoor, job board
or aggregator page.
"city" is the city of its main office or tech hub in the country given.
"tier" is exactly one of: {"; ".join(TIERS)}. Indian IT services firms are Tier 1; software,
product and cloud companies Tier 2; banks, insurers, payments and fintech Tier 3;
consulting, outsourcing, IT services and shared service centres Tier 4; product companies
from Poland "Tier 5 - Polish product", from the Netherlands or Ireland "Tier 5 - Local
product (NL/IE)"; car makers, engineering, energy and industrial firms Tier 6.
When you are not sure of a field, use ""."""


@dataclass
class Result:
    filled: list[str] = field(default_factory=list)  # "Company (Careers URL, Tier, ...)"
    searches: int = 0


def enabled(s: Any) -> bool:
    return bool((s.sweep.get("company_details") or {}).get("enabled", True))


def bot_added(company: Any) -> bool:
    return str(getattr(company, "notes", "") or "").startswith(BOT_NOTES)


def missing(company: Any) -> list[str]:
    """The columns of this row that are still empty."""
    have = {"Careers URL": company.careers_url, "Domain": company.domain,
            "Hub / City": company.hub_city, "Tier": company.tier,
            "IND sponsor": company.ind_sponsor}
    return [name for name, value in have.items() if not str(value or "").strip()]


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _platform(url: str) -> str:
    from jobengine.sweep.sources import ats

    board = ats.board_from_url(url)
    if board is not None:
        return ATS_NAMES.get(board.ats, "Custom")
    return "Workday" if job_links.SHARDLESS_WORKDAY.match(url) else "Custom"


def _domain(name: str, website: str, careers: str) -> str:
    """The company's own web address: its website, else a careers page on its own site."""
    for url in (website, careers):
        if (url and job_links.employer_page(url, name) and not job_links._board_link(url)
                and job_links._company_host(url, name)):
            return _host(url)
    return ""


def look_up(deps: job_links.Deps, company: Any, register: Any) -> tuple[dict[str, Any], bool]:
    """(the props for this row's empty columns, whether a web search was made)."""
    want = set(missing(company))
    props: dict[str, Any] = {}
    if "IND sponsor" in want:
        if company.region == "Netherlands":
            if register is not None:
                props["IND sponsor"] = ("Verified" if register.match(company.name)
                                        else "Not listed")
        elif company.region:
            props["IND sponsor"] = "N/A"  # the IND register is the Dutch one
    if not want & {"Careers URL", "Domain", "Hub / City", "Tier"} or deps.llm is None:
        return props, False
    from jobengine.llm import LLMError

    name = job_links.without_region(company.name)
    user = f"Company: {name}\nCountry: {company.region or 'not given'}"
    try:
        raw, sources = deps.llm.complete_json_with_search(
            job_links.STAGE, DETAILS_SYSTEM, user, max_tokens=500, max_uses=2,
            key=f"company-{job_links.company_key(name)}")
    except LLMError as exc:
        log.info("company details not looked up for %s: %s", company.name, exc)
        return props, True
    raw = raw or {}
    website = str(raw.get("website") or "").strip()
    if "Careers URL" in want:
        answers = [str(raw.get(k) or "").strip() for k in ("board", "careers")]
        for url in list(dict.fromkeys(u for u in [*answers, *sources] if u))[
                :job_links.MAX_CANDIDATES]:
            found = job_links._checked(deps, url, name)
            if found:
                props["Careers URL"] = found
                if str(getattr(company, "ats_platform", "") or "Unknown") in ("", "Unknown"):
                    props["ATS platform"] = _platform(found)
                break
    if "Domain" in want:
        domain = _domain(name, website, str(props.get("Careers URL") or raw.get("careers")
                                            or ""))
        if domain:
            props["Domain"] = domain
    city = " ".join(str(raw.get("city") or "").split())
    if "Hub / City" in want and city and len(city) <= MAX_CITY:
        props["Hub / City"] = city
    tier = str(raw.get("tier") or "").strip()
    if "Tier" in want and tier in TIERS:
        props["Tier"] = tier
    return props, True


def fill(deps: job_links.Deps, companies: list[Any], writer: Callable[[str, dict[str, Any]], None],
         register: Any = None, max_per_run: int = DEFAULT_MAX_PER_RUN,
         progress: Callable[[str], None] | None = None) -> Result:
    """Fill the empty columns of the companies the bot added, at most `max_per_run` a run."""
    result = Result()
    tried: dict[str, str] = dict(((deps.state.get(KEY) if deps.state is not None else None)
                                  or {}).get("tried") or {})
    cutoff = (deps.today - timedelta(days=RETRY_DAYS)).isoformat()
    todo = [c for c in companies if c.active and bot_added(c) and missing(c)
            and str(tried.get(c.name) or "") <= cutoff]
    for company in todo[:max(0, max_per_run)]:
        if progress:
            progress(f"Target Companies details: {company.name}")
        try:
            props, searched = look_up(deps, company, register)
        except Exception:  # one company's lookup never stops the others
            log.exception("company details failed for %s", company.name)
            continue
        result.searches += int(searched)
        tried[company.name] = deps.today.isoformat()
        if not props:
            log.info("company details for %s: nothing found", company.name)
            continue
        try:
            writer(company.row_id, props)
        except Exception:
            log.exception("company details not written for %s", company.name)
            continue
        log.info("company details for %s: %s", company.name, props)
        result.filled.append(f"{company.name} ({', '.join(p for p in FIELDS if p in props)})")
    if deps.state is not None and todo:
        deps.state.set(KEY, {"tried": tried})
    return result


def max_per_run(s: Any) -> int:
    return int((s.sweep.get("company_details") or {}).get("max_per_run", DEFAULT_MAX_PER_RUN))
