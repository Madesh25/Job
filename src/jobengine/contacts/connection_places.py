"""Where your LinkedIn connections work (LOG-18, 25 Nov, your rules), without paid credits.

The LinkedIn export has no location, and Prospeo and Hunter credits are kept for emails. So
for the people at a job's company whose country is unknown, the bot asks the AI web search
(Config model.people: Gemini with Google Search, the official API) once per company: it
searches "name + company" and reads the result lines, like "India · Senior Associate ·
Xerago". LinkedIn and google.com are never opened by the bot.

An answer counts only when the AI says a result names that person together with this company
(a common name can match someone else); then the country is saved in the 🤝 LinkedIn
Connections table (Location source "Google search") and never asked again. A company is
searched again after RECHECK_DAYS (people who were not found, or new people). At most
MAX_PEOPLE people per search, the most likely helpers first (connections.at_company order).
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date, timedelta
from typing import Any

from jobengine.contacts import connections as conn
from jobengine.llm import LLMError
from jobengine.sweep.normalize import canon_company, without_region

log = logging.getLogger("jobengine.contacts")

STAGE = "people"
KEY = "connections.located"  # bot_state: {company key: ISO date of the last search}
SWITCH = "connections.locate"  # Notion Config: "off" stops the search
SOURCE = "Google search"
RECHECK_DAYS = 90
MAX_PEOPLE = 15
MAX_SEARCHES = 8
MAX_TOKENS = 1500

SYSTEM = """You find the country where people work now, from web search results only. For
each person (name and job title) at the company given, search the person's name with the
company and read the result titles and snippets (a LinkedIn result shows "Country · Title ·
Company"). Do not guess from the name. Return JSON only:
{"people": [{"name": <the name as given>, "country": <the country in English, or "">,
"company_seen": <true only when a result names this person together with this company>}]}.
Use "" for the country when no result says it."""


def company_key(company: str) -> str:
    return canon_company(without_region(company))


def due(state: Any, company: str, today: date) -> bool:
    if state is None:
        return False
    when = ((state.get(KEY) or {}).get(company_key(company)) or "")
    try:
        return date.fromisoformat(when) <= today - timedelta(days=RECHECK_DAYS)
    except ValueError:
        return True


def unknown_people(rows: list[conn.StoredRow], company: str,
                   country: str) -> list[conn.StoredRow]:
    """The people at the company without a country, in the order they would be shown."""
    return [m.row for m in conn.at_company(rows, company, country)
            if not m.country][:MAX_PEOPLE]


def user_prompt(company: str, people: list[conn.StoredRow]) -> str:
    lines = [f"Company: {without_region(company)}", "People:"]
    lines += [f"- {row.person.name} | {row.person.position or 'title unknown'}"
              for row in people]
    return "\n".join(lines)


def locate(llm: Any, store: Any, state: Any, rows: list[conn.StoredRow], company: str,
           country: str, today: date) -> list[conn.StoredRow]:
    """`rows` with the countries found today filled in (and saved); unchanged when nobody's
    country is unknown, the company was searched in the last RECHECK_DAYS, or the search
    fails."""
    people = unknown_people(rows, company, country)
    if not people or llm is None or not due(state, company, today):
        return rows
    try:
        raw, _urls = llm.complete_json_with_search(
            STAGE, SYSTEM, user_prompt(company, people), max_tokens=MAX_TOKENS,
            max_uses=MAX_SEARCHES, key=f"places-{company_key(company)}", titles={})
    except LLMError as exc:
        log.info("where connections at %s work: search failed: %s", company, exc)
        return rows
    saved = dict(state.get(KEY) or {})
    saved[company_key(company)] = today.isoformat()
    state.set(KEY, saved)
    found: dict[str, str] = {}
    for item in (raw or {}).get("people") or []:
        if not isinstance(item, dict) or item.get("company_seen") is not True:
            continue
        where = str(item.get("country") or "").strip()
        row = next((r for r in people if conn.same_person(r.person.name, item.get("name"))),
                   None)
        if where and row is not None and row.page_id not in found:
            found[row.page_id] = where
            try:
                store.update(row.page_id, {"Country": ("rich_text", where),
                                           "Location source": ("select", SOURCE)})
            except Exception as exc:  # the order still uses it for this answer
                log.warning("Country of %s not saved: %s", row.person.name, exc)
    log.info("where connections at %s work: %d of %d placed by Google search", company,
             len(found), len(people))
    return [replace(r, info={**r.info, "Country": found[r.page_id], "Location source": SOURCE})
            if r.page_id in found else r for r in rows]
