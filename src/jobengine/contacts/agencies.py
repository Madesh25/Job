"""Recruitment agencies channel (flow feature 7, 10 Oct).

The agencies in config/base.yaml `agencies` are established recruiters for IT and DevOps roles
in Poland, the Netherlands and Ireland (each website checked on 10 Oct). /agencies lists them
by country with their website and when you last contacted them; /agencies <name> gives the
intro mail for that agency, word for word from `agency_intro` (a Notion Config key
`agency.intro` wins), filled with your Apply pack answers and the country's permit line. The
mail goes only to an address you put in Notion Config `agency_email.<key>` (an email is never
guessed); without one, you register on the agency's website and paste the text there.
/agencies sent <name> records the date; the weekly digest names the agencies not contacted in
the last FOLLOW_UP_DAYS days. No AI; nothing is sent by the bot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from jobengine.apply.pack import MISSING, answer
from jobengine.config_store import ConfigStore
from jobengine.settings import Settings

STATE_KEY = "agencies.contacted"  # bot_state: {key: ISO date of the last contact}
FOLLOW_UP_DAYS = 30
COUNTRIES = ("Poland", "Netherlands", "Ireland")
USAGE = ("/agencies lists the recruitment agencies; /agencies <name> gives the intro mail for "
         "one; /agencies sent <name> records that you contacted it today.")


@dataclass(frozen=True)
class Agency:
    name: str
    country: str
    website: str
    focus: str

    @property
    def key(self) -> str:
        return re.sub(r"[^a-z0-9]+", "_", self.name.casefold()).strip("_")


def load(s: Settings) -> list[Agency]:
    out = []
    for item in s.agencies.get("list") or []:
        if isinstance(item, dict) and item.get("name") and item.get("country"):
            out.append(Agency(str(item["name"]), str(item["country"]),
                              str(item.get("website") or ""), str(item.get("focus") or "")))
    return out


def find(agencies: list[Agency], text: str) -> list[Agency]:
    """Agencies whose name contains every word typed ("hays ireland", "sigmar")."""
    words = text.casefold().split()
    return [a for a in agencies if words and all(w in a.name.casefold() for w in words)]


def contacted(state: Any) -> dict[str, str]:
    value = state.get(STATE_KEY) if state is not None else None
    return dict(value) if isinstance(value, dict) else {}


def mark(state: Any, agency: Agency, today: date) -> None:
    seen = contacted(state)
    seen[agency.key] = today.isoformat()
    state.set(STATE_KEY, seen)


def email(config: ConfigStore, agency: Agency) -> str | None:
    value = " ".join(str(config.get(f"agency_email.{agency.key}") or "").split())
    return value if "@" in value else None


def due(agencies: list[Agency], state: Any, today: date,
        days: int = FOLLOW_UP_DAYS) -> list[Agency]:
    """Agencies never contacted, or not in the last `days` days."""
    seen = contacted(state)
    out = []
    for a in agencies:
        last = seen.get(a.key)
        try:
            if last and (today - date.fromisoformat(last)).days < days:
                continue
        except ValueError:
            pass
        out.append(a)
    return out


def listing(agencies: list[Agency], state: Any, config: ConfigStore) -> str:
    if not agencies:
        return "No agencies in config/base.yaml agencies.list."
    seen = contacted(state)
    lines = ["Recruitment agencies (established IT recruiters; you send everything yourself):"]
    for country in COUNTRIES:
        group = [a for a in agencies if a.country == country]
        if not group:
            continue
        lines += ["", f"{country}:"]
        for a in group:
            mail = email(config, a)
            how = f"mail {mail}" if mail else f"register on {a.website}"
            last = seen.get(a.key)
            when = f"contacted {last}" if last else "not contacted yet"
            lines.append(f"- {a.name} ({a.focus}): {how}; {when}")
    lines += ["", USAGE]
    return "\n".join(lines)


def _permit(s: Settings, country: str) -> str:
    countries = s.apply_pack.get("countries") or {}
    line = next((v.get("permit") for k, v in countries.items()
                 if k.casefold() == country.casefold()), None)
    return " ".join(str(line).split()) if line else ""


def intro(s: Settings, config: ConfigStore, agency: Agency) -> str:
    """The intro mail for one agency: the template filled, word for word."""
    template = str(config.get("agency.intro") or s.agencies.get("intro") or "")
    values = {key: answer(s, config, key) for key in ("experience", "notice_period",
                                                      "relocation")}
    values = {k: (None if v == MISSING.format(key=k) else v.rstrip(".")) for k, v in
              values.items()}
    links = [str(config.get(k)) for k in ("profile.linkedin_url", "profile.github")
             if config.get(k)]
    filled = template.replace("\\n", "\n").format(
        agency=agency.name, country=agency.country,
        experience=values["experience"] or "several years",
        notice=values["notice_period"] or "to be agreed",
        relocation=values["relocation"] or "Open to relocate",
        permit=_permit(s, agency.country),
        name=" ".join(str(config.get("profile.name") or "").split()),
        phone=" ".join(str(config.get("profile.phone") or "").split()),
        links=" | ".join(links))
    filled = re.sub(r"[ \t]+\n", "\n", re.sub(r"[ \t]{2,}", " ", filled)).strip()
    mail = email(config, agency)
    head = (f"Intro mail for {agency.name} ({agency.country}). " +
            (f"Send it to {mail} with your resume attached." if mail else
             f"No email set: register on {agency.website} and paste it there, or add Notion "
             f"Config agency_email.{agency.key} with an address from their website."))
    return (f"{head}\n\n{filled}\n\nWhen it is sent: /agencies sent {agency.name}")


def digest_line(agencies: list[Agency], state: Any, today: date) -> str | None:
    waiting = due(agencies, state, today)
    if not waiting:
        return None
    return (f"Agencies to contact or follow up ({len(waiting)} not in the last "
            f"{FOLLOW_UP_DAYS} days): /agencies")
