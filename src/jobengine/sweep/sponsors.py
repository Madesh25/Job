"""Companies from the visa sponsor lists (28 Oct, your OK): suggestions for Target Companies.

The bot already reads two official lists, only to rank jobs until now:
- the Dutch IND public register of recognised sponsors (Config `ind_register.url`);
- the Irish "Permits issued to companies" list (ie_permits.py), with how many permits each
  company received last year.

Once a week (Saturday from START, India time, or the first day after when the bot was off)
the bot takes up to MAX_CHECKS companies from these lists that are not in Target Companies,
not an agency, not one you turned down and not checked in the last RECHECK_DAYS:
the Irish companies with the most permits first, then the Dutch register names that sound
like IT (software, cloud, data, digital ...). For each it looks for a public job board by the
company's name (Greenhouse, Lever, SmartRecruiters, Ashby, Workable, Recruitee: the same
check /fetch uses, sweep/sources/ats.py `Detector.guess`) and reads it. A company with at
least one job for your titles in your countries right now becomes a suggestion in
/newcompanies (sweep/discover.py), marked with the list it is on. Nothing is added without
your tap: Target Companies is shared with prod.

Plain public job board APIs, as /fetch reads them; no login, never LinkedIn. Notion Config
`discover.sponsors` = `off` stops it (default on).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from jobengine.ind_register import ind_name, names_match

SWITCH = "discover.sponsors"
DONE_KEY = "sponsors.done"  # bot_state: {"date": <local iso date of the last run>}
CHECKED_KEY = "sponsors.checked"  # bot_state: {"names": {ind name: iso date}}
SATURDAY = 5
START = time(11, 0)
MAX_CHECKS = 40  # companies a week (up to 14 small API calls each)
RECHECK_DAYS = 90
IND_LABEL = "On the Dutch IND sponsor register"
IT_WORDS = re.compile(
    r"\b(software|soft|tech|techn\w*|digital|data|cloud|it|ict|systems?|solutions?|labs?|"
    r"ai|analytics|cyber|security|networks?|computing|online|internet|games?|gaming|"
    r"fintech|payments?|platforms?|apps?|web|saas|devops|automation|engineering|"
    r"informatics?|informatica|computer|consult\w*|innovations?|io)\b", re.I)


def is_on(config: Any) -> bool:
    return (config.get(SWITCH) or "on").strip().lower() != "off"


def due(config: Any, state: Any, now: datetime) -> bool:
    """From Saturday START; a week after the last run when the bot missed a Saturday."""
    if not is_on(config) or now.time() < START:
        return False
    raw = ((state.get(DONE_KEY) if state is not None else None) or {}).get("date")
    try:
        last = date.fromisoformat(str(raw)) if raw else None
    except ValueError:
        last = None
    if last is None:
        return now.weekday() == SATURDAY
    return (now.date() - last).days >= 7


def mark_done(state: Any, day: date) -> None:
    if state is not None:
        state.set(DONE_KEY, {"date": day.isoformat()})


@dataclass(frozen=True)
class Candidate:
    name: str  # as on the list
    label: str  # "Irish permits: 120 last year" or IND_LABEL


def it_like(name: str) -> bool:
    return bool(IT_WORDS.search(name or ""))


def checked(state: Any, today: date) -> dict[str, str]:
    """The companies checked in the last RECHECK_DAYS: ind name -> iso date."""
    names = dict((((state.get(CHECKED_KEY) if state is not None else None) or {})
                  .get("names")) or {})
    cutoff = (today - timedelta(days=RECHECK_DAYS)).isoformat()
    return {k: v for k, v in names.items() if v >= cutoff}


def candidates(ind_names: Iterable[str], permits: Mapping[str, int], known: Iterable[str],
               skip: Iterable[str], done: Mapping[str, str], is_agency: Callable[[str], bool],
               limit: int = MAX_CHECKS) -> list[Candidate]:
    """Up to `limit` list companies to check: Irish permits first (the most permits first),
    then IT-sounding IND register names. `known` and `skip` are company names (Target
    Companies, open suggestions, turned down)."""
    known_names = [ind_name(n) for n in [*known, *skip] if ind_name(n)]
    out: list[Candidate] = []
    seen: set[str] = set()

    def take(name: str, label: str) -> None:
        key = ind_name(name)
        if (not key or key in seen or key in done or is_agency(name)
                or any(names_match(k, key) or names_match(key, k) for k in known_names)):
            return
        seen.add(key)
        out.append(Candidate(name, label))

    for name, count in sorted(permits.items(), key=lambda kv: (-kv[1], kv[0])):
        if len(out) >= limit:
            break
        take(name, f"Irish permits: {count} last year")
    for name in ind_names:
        if len(out) >= limit:
            break
        if it_like(name):
            take(name, IND_LABEL)
    return out


@dataclass
class Checked:
    candidate: Candidate
    board_link: str = ""
    platform: str = ""
    roles: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)


def item(found: Checked, today: date) -> dict[str, Any]:
    """A /newcompanies suggestion (sweep/discover.py items)."""
    day = today.isoformat()
    return {"name": short_name(found.candidate.name), "roles": found.roles,
            "countries": found.countries,
            "urls": [u for u in [found.board_link, *found.urls] if u][:6],
            "boards": ["visa sponsor list"], "sponsor": found.candidate.label,
            "first": day, "last": day}


def short_name(name: str) -> str:
    """"Tulip Data Nederland B.V." -> "Tulip Data Nederland": the legal form left out."""
    text = re.sub(r"[\s,]+(b\.?\s?v\.?|n\.?\s?v\.?|limited|ltd\.?|dac|uc|plc|inc\.?|"
                  r"unlimited company|designated activity company)\s*$", "", name.strip(),
                  flags=re.I)
    return " ".join(text.split()) or name


def place_check(s: Any, config: Any) -> Callable[[str, str], str | None]:
    """(title, location) -> the country, when the job is for your titles in a place you
    take (the /fetch rules: sweep.title_include and _exclude, Config countries.active, office
    or hybrid), else None."""
    from jobengine.sweep.normalize import (
        Rules,
        parse_active_countries,
        place,
        place_allowed,
        remote_only,
        title_scope,
    )

    rules = Rules.from_config(s.sweep)
    active = parse_active_countries(config.get("countries.active"))

    def fits(title: str, location: str) -> str | None:
        if title_scope(title, rules) is not None:
            return None
        country, city = place(location, rules)
        if not rules.remote_jobs and remote_only(location, title, city):
            return None
        return country if place_allowed(country, city, active, rules) else None

    return fits


def check(s: Any, candidate: Candidate, get: Any, post: Any, page: Any, today: date,
          fits: Callable[[str, str], str | None]) -> Checked:
    """Look for the company's job board by its name and read it (network only)."""
    from jobengine import http
    from jobengine.screen.add_link import ATS_OPTIONS, board_link
    from jobengine.sweep.boards import read_board
    from jobengine.sweep.models import TargetCompany
    from jobengine.sweep.sources import ats

    result = Checked(candidate)
    company = TargetCompany(short_name(candidate.name), "", "Unknown", True, None)
    board = ats.Detector(s, None, get, None, today).guess(company)
    if board is None:
        return result
    result.board_link = board_link(board)
    result.platform = ATS_OPTIONS.get(board.ats, "Custom")
    try:
        postings = read_board(s, company, board, get, post, page, today)
    except http.HttpError:
        return result
    for p in postings:
        country = fits(p.title, p.location_text or "")
        if country is None:
            continue
        if p.title not in result.roles:
            result.roles.append(p.title)
        if country not in result.countries:
            result.countries.append(country)
        if p.url and p.url not in result.urls:
            result.urls.append(p.url)
    return result


def remember(state: Any, today: date, names: Iterable[str]) -> None:
    if state is None:
        return
    done = checked(state, today)
    for name in names:
        if ind_name(name):
            done[ind_name(name)] = today.isoformat()
    state.set(CHECKED_KEY, {"names": done})


def text(checked_n: int, found: list[Checked], left_note: str = "") -> str:
    """The Telegram message after a run ("" when nothing was found)."""
    if not found:
        return ""
    lines = [f"🛂 Visa sponsor companies: checked {checked_n} from the sponsor lists; "
             f"{len(found)} have jobs for your titles in your countries now:"]
    for f in found[:10]:
        lines.append(f"- {short_name(f.candidate.name)} ({f.candidate.label}): "
                     + "; ".join(f.roles[:3]))
    lines.append("/newcompanies shows them one by one: Add to Target Companies, or Not "
                 "interested.")
    if left_note:
        lines.append(left_note)
    return "\n".join(lines)
