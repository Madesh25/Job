"""A draft answer under the reply ping (flow feature 3, 9 Oct).

When a recruiter or an engineer answers and asks for a call, your notice period, salary,
visa, start date or relocation, the reply ping carries a short answer to copy into Gmail.
Only the new part of their mail is read (the quoted mail you sent is cut, it names visa and
relocation itself). Every sentence comes from config/base.yaml `reply_drafts` (a Notion
Config key `reply.<name>` wins) filled with your Apply pack answers and the job's country
permit line; no AI is used and nothing is sent. You edit it and send it yourself.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from jobengine.apply.pack import MISSING, answer
from jobengine.config_store import ConfigStore
from jobengine.settings import Settings
from jobengine.track.classify import strip_quoted

# (topic, what in their mail asks for it); checked on the new part only, in this order.
TOPICS = (
    ("call", re.compile(r"\b(interview|call|chat|meet|meeting|schedule|availability|available"
                        r"|slot|calendly|teams|zoom|rozmow\w*|gesprek|sollicitatiegesprek)\b",
                        re.I)),
    ("notice", re.compile(r"\b(notice|wypowiedzeni\w*|opzegtermijn)\b", re.I)),
    ("salary", re.compile(r"\b(salary|compensation|expectations?|rate|budget|wynagrodzeni\w*"
                          r"|salaris)\b", re.I)),
    ("visa", re.compile(r"\b(visa|sponsor\w*|work permit|right to work|authori[sz]ed"
                        r"|pozwoleni\w*|werkvergunning)\b", re.I)),
    ("start", re.compile(r"\b(start date|when (could|can) you start|earliest start"
                         r"|available from|joining date)\b", re.I)),
    ("relocation", re.compile(r"\b(relocat\w*|move to|on-?site)\b", re.I)),
)
DEFAULTS = {
    "greeting": "Hi {first},",
    "greeting_unknown": "Hello,",
    "thanks": "Thank you for your reply about the {role} role at {company}.",
    "thanks_unknown": "Thank you for your reply.",
    "call": "I would be glad to talk. I am available {interviews}, for example on {days}.",
    "notice": "My notice period is {notice_period}.",
    "salary": "On salary: {expected_salary}.",
    "visa": "On the work permit: {permit}",
    "visa_unknown": "I need visa sponsorship: {sponsorship_needed}.",
    "start": "Earliest start: {earliest_start}.",
    "relocation": "On relocation: {relocation}.",
    "closing": "Kind regards,\n{name}",
}
DAYS = 3


def topics(text: str) -> list[str]:
    """What their mail asks about (the quoted part ignored)."""
    new = strip_quoted(text)
    return [name for name, pattern in TOPICS if pattern.search(new)]


def next_days(today: date, count: int = DAYS) -> str:
    """"Tuesday 13 Oct, Wednesday 14 Oct or Thursday 15 Oct": the next working days."""
    days: list[date] = []
    day = today
    while len(days) < count:
        day += timedelta(days=1)
        if day.weekday() < 5:
            days.append(day)
    names = [f"{d.strftime('%A')} {d.day} {d.strftime('%b')}" for d in days]
    return ", ".join(names[:-1]) + f" or {names[-1]}" if len(names) > 1 else names[0]


def _template(s: Settings, config: ConfigStore, key: str) -> str:
    return str(config.get(f"reply.{key}") or s.reply_drafts.get(key) or DEFAULTS[key])


def _value(s: Settings, config: ConfigStore, key: str) -> str | None:
    value = answer(s, config, key)
    return None if value == MISSING.format(key=key) else value


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text[:2] != text[:2].upper() else text


def _permit(s: Settings, country: str | None) -> str | None:
    countries = s.apply_pack.get("countries") or {}
    line = next((v.get("permit") for k, v in countries.items()
                 if k.casefold() == (country or "").strip().casefold()), None)
    return " ".join(str(line).split()) if line else None


def draft(s: Settings, config: ConfigStore, text: str, *, today: date, first: str | None,
          job: dict[str, Any] | None) -> str | None:
    """The answer to copy, or None when their mail asks nothing the draft can answer."""
    asked = topics(text)
    if not asked:
        return None
    job = job or {}
    company, role = job.get("Company") or "", job.get("Role") or ""
    fill = {"first": first or "", "company": company, "role": role or "open",
            "days": next_days(today), "name": " ".join(str(config.get("profile.name") or "")
                                                       .split())}
    lines = [_template(s, config, "greeting" if first else "greeting_unknown").format(**fill),
             "",
             _template(s, config, "thanks" if company else "thanks_unknown").format(**fill)]
    for topic in asked:
        if topic == "visa":
            permit = _permit(s, job.get("Country"))
            if permit:
                lines.append(_template(s, config, "visa").format(permit=permit))
                continue
            needed = _value(s, config, "sponsorship_needed")
            if needed:
                lines.append(_template(s, config, "visa_unknown").format(
                    sponsorship_needed=needed))
            continue
        key = {"call": "interviews", "notice": "notice_period", "salary": "expected_salary",
               "start": "earliest_start", "relocation": "relocation"}[topic]
        value = _value(s, config, key)
        if value is None:
            continue  # nothing guessed: that question stays for you
        value = _lower_first(value.rstrip("."))
        lines.append(_template(s, config, topic).format(**fill, **{key: value}))
    if len(lines) == 3:
        return None
    lines += ["", _template(s, config, "closing").format(**fill).replace("\\n", "\n")]
    return "\n".join(lines).strip()


def block(text: str) -> str:
    return ("Draft answer (copy it into your reply in Gmail, edit it, send it yourself):\n"
            f"{text}")
