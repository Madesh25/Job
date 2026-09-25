"""Placeholder filling for the approved templates (spec section 4). Pure, except the one LLM
call (stage `email`) that picks the index of a stored specific detail.

Only placeholders change; the template text around them is used word for word. The LLM never
writes mail text.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from jobengine.config_store import ConfigStore
from jobengine.llm import LLMClient, LLMError
from jobengine.mail.templates import TYPE_TEMPLATE, Template, approved

log = logging.getLogger("jobengine.mail")

STAGE = "email"
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.]*)\}")
# Gender markers such as (m/f/d), (m/w/d), (f/m/x), (k/m), [m/f/d], (all genders).
GENDER_RE = re.compile(
    r"\s*[(\[]\s*(?:[mfwdxk*]\s*(?:[/|,]\s*[mfwdxk*]\s*)+|all genders|m/f/d/x)\s*[)\]]",
    re.IGNORECASE,
)
GREETING_KEY = "mail.generic_greeting_name"
PERMIT_PREFIX = "mail.permit_word."

PICK_SYSTEM = """You choose one phrase for a cold email. The sentence reads:
"I saw you're hiring a <role>, and the part about <phrase> is very close to my day-to-day work."
You get a numbered list of phrases taken from the job description. Pick the one that reads
most naturally after "the part about" and says most about the team's actual work.
Return one JSON object only: {"index": <number from the list>}. Never write or change a phrase.
"""


class FillError(Exception):
    """The template cannot be filled. That draft is aborted."""


@dataclass(frozen=True)
class MailJob:
    page_id: str
    company: str
    role: str
    city: str
    country: str
    details: tuple[str, ...] = ()
    url: str = ""


@dataclass
class Choice:
    template: Template | None
    note: str = ""  # e.g. "no specific detail, recruiter template"
    skipped: str = ""  # why this contact gets no mail


@dataclass
class Filled:
    subject: str
    body: str
    values: dict[str, str] = field(default_factory=dict)


def clean_role(role: str) -> str:
    """The job Role without gender markers like (m/f/d)."""
    return " ".join(GENDER_RE.sub("", role or "").split())


def city_for(job: MailJob) -> str:
    city = (job.city or "").strip()
    return job.country if not city or city.casefold() == "remote" else city


def first_name(name: str) -> str:
    words = (name or "").split()
    return words[0] if words else ""


def permit_word(config: ConfigStore, country: str) -> str | None:
    return config.get(f"{PERMIT_PREFIX}{(country or '').strip().casefold()}")


def choose_template(templates: dict[str, Template], contact_type: str, has_detail: bool,
                    generic: bool = False) -> Choice:
    """The template for a contact Type. Hiring without a specific detail, and a generic
    mailbox (only when it may be mailed), get the recruiter template."""
    if generic:
        key, note = "recruiter", "generic mailbox, recruiter template"
    else:
        key = TYPE_TEMPLATE.get(contact_type)
        if key is None:
            return Choice(None, skipped=f"Type {contact_type or 'empty'} has no template")
        note = ""
        if key == "hiring" and not has_detail:
            key, note = "recruiter", "no specific detail, recruiter template"
    template = approved(templates, key)
    if template is None:
        return Choice(None, skipped=f"the {key} template is missing or not APPROVED")
    return Choice(template, note=note)


def pick_detail(details: tuple[str, ...] | list[str], job: MailJob, llm: LLMClient | None,
                key: str | None = None) -> str | None:
    """One of the stored details, exactly as stored. One detail: that one. Several: the LLM
    returns an index (first one when it cannot answer)."""
    details = [d for d in details if d.strip()]
    if not details:
        return None
    if len(details) == 1 or llm is None:
        return details[0]
    numbered = "\n".join(f"{i}. {d}" for i, d in enumerate(details, 1))
    user = f"Role: {clean_role(job.role)} at {job.company}\nPhrases:\n{numbered}"
    try:
        raw = llm.complete_json(STAGE, PICK_SYSTEM, user, max_tokens=50, key=key)
        index = int(raw.get("index"))
    except (LLMError, TypeError, ValueError) as exc:
        log.warning("specific detail choice failed (%s), using the first detail", exc)
        return details[0]
    if not 1 <= index <= len(details):
        log.warning("specific detail index %s out of range, using the first detail", index)
        return details[0]
    return details[index - 1]


def placeholder_values(job: MailJob, name: str, config: ConfigStore,
                       detail: str | None) -> dict[str, str]:
    values = {
        "first_name": first_name(name),
        "company": job.company.strip(),
        "role": clean_role(job.role),
        "city": city_for(job),
        "country": job.country.strip(),
        "permit_word": permit_word(config, job.country) or "",
    }
    if detail:
        values["specific_detail"] = detail
    return values


def fill_text(text: str, values: dict[str, str]) -> str:
    """Replace every {placeholder} in one pass. An unknown or empty one aborts."""
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        value = values.get(match.group(1))
        if not value:
            missing.append(match.group(0))
            return match.group(0)
        return value

    out = PLACEHOLDER_RE.sub(replace, text)
    if missing:
        raise FillError(f"unfilled placeholder {', '.join(dict.fromkeys(missing))}")
    return out


def fill(template: Template, values: dict[str, str]) -> Filled:
    return Filled(subject=fill_text(template.subject, values),
                  body=fill_text(template.body, values), values=dict(values))


def generic_greeting(config: ConfigStore) -> str | None:
    return config.get(GREETING_KEY)


def job_from_values(page_id: str, values: dict[str, Any], details: list[str]) -> MailJob:
    return MailJob(page_id=page_id, company=values.get("Company") or "",
                   role=values.get("Role") or "", city=values.get("City") or "",
                   country=values.get("Country") or "", details=tuple(details),
                   url=values.get("URL") or "")
