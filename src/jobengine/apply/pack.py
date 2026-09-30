"""The Apply pack: ready answers for the portal form of one approved job. Pure, no AI.

Every answer is yours (config/base.yaml `apply_pack`; a Notion Config key `apply.<name>`
wins). Per job it adds the country's work permit line and visa salary minimum, the salary
the posting states, the "why this company" sentence with the job's own specific detail, and
the headline and relocation line the resume used. Links come from Config profile.*.

It is sent in Telegram when a resume is approved (and by /autopilot and /applypack), and
saved on the job's Notion page under "Apply pack (<date>)".
"""

from __future__ import annotations

from datetime import date
from typing import Any

from jobengine.config_store import ConfigStore
from jobengine.resume import header
from jobengine.settings import Settings

TITLE = "Apply pack"
PROFILE_LINKS = (("LinkedIn", "profile.linkedin_url"), ("GitHub", "profile.github"),
                 ("Website", "profile.website_url"))
MISSING = "(not set: add Config apply.{key})"


def answer(s: Settings, config: ConfigStore, key: str) -> str:
    value = config.get(f"apply.{key}") or s.apply_pack.get(key)
    return " ".join(str(value).split()) if value else MISSING.format(key=key)


def _country(s: Settings, country: str | None) -> dict[str, Any]:
    countries = s.apply_pack.get("countries") or {}
    return next((v for k, v in countries.items()
                 if k.casefold() == (country or "").strip().casefold()), {})


def _named(country: str | None) -> str:
    """"the Netherlands", "Poland", "Ireland"."""
    name = (country or "").strip()
    if not name:
        return "this country"
    return f"the {name}" if name.casefold() == "netherlands" else name


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def why_line(s: Settings, config: ConfigStore, details: list[str]) -> str | None:
    template = config.get("apply.why_template") or s.apply_pack.get("why_template")
    detail = next((d.strip() for d in details if d.strip()), None)
    if not template or not detail:
        return None
    return _clean(str(template).replace("{detail}", detail))


def build(s: Settings, config: ConfigStore, values: dict[str, Any], details: list[str],
          today: date) -> str:
    """The pack as plain text (Telegram and the Notion page)."""
    company, role = values.get("Company") or "", values.get("Role") or ""
    country = values.get("Country") or ""
    where = header.place(values) or "unknown place"
    per_country = _country(s, country)
    title = header.headline_title(role, header.approved_titles(s, config))
    reloc = header.relocation_line(s, config, values)
    lines = [
        f"{TITLE} ({today.isoformat()}): {company}, {role} ({where})",
        f"Apply here: {values.get('URL') or '(no URL on the job)'}",
        "",
        "Work permit",
        f"- Authorized to work in {_named(country)} without sponsorship? "
        f"{answer(s, config, 'authorized_without_sponsorship')}",
        f"- Need visa sponsorship? {answer(s, config, 'sponsorship_needed')}",
        f"- {_clean(per_country.get('permit')) or 'No permit line for this country'}",
        "",
        "Salary",
        f"- Expected: {answer(s, config, 'expected_salary')}",
        f"- Visa minimum: {_clean(per_country.get('salary_floor')) or 'unknown for this country'}",
    ]
    if values.get("Salary"):
        lines.append(f"- The posting states: {_clean(values.get('Salary'))}")
    lines += [
        f"- Current salary: {answer(s, config, 'current_salary')}",
        "",
        "Notice and start",
        f"- Notice period: {answer(s, config, 'notice_period')}",
        f"- Earliest start: {answer(s, config, 'earliest_start')}",
        "",
        "About you",
        f"- Experience: {answer(s, config, 'experience')}",
        f"- Education: {answer(s, config, 'education')}",
        f"- English: {answer(s, config, 'english')}",
        f"- Other languages: {answer(s, config, 'other_languages')}",
        f"- Relocation: {answer(s, config, 'relocation')}",
        f"- Interview availability: {answer(s, config, 'interviews')}",
        f"- Gender: {answer(s, config, 'gender')}",
        f"- Other diversity questions: {answer(s, config, 'other_diversity')}",
    ]
    for label, key in PROFILE_LINKS:
        if config.get(key):
            lines.append(f"- {label}: {config.get(key)}")
    why = why_line(s, config, details)
    lines += ["", "Why this company",
              f"- {why}" if why else "- (no specific detail stored for this job: write your "
                                       "own line)"]
    lines += ["", "Resume for this job",
              f"- Headline: {title or 'unchanged (no approved title in this role)'}",
              f"- Location line: {' | '.join((reloc or 'unchanged').splitlines())}"]
    return "\n".join(lines)


def blocks(text: str) -> list[str]:
    """Paragraphs for the job's Notion page: the text in chunks under 2000 characters."""
    out, chunk = [], ""
    for line in text.splitlines():
        if len(chunk) + len(line) + 1 > 1900:
            out.append(chunk.rstrip("\n"))
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        out.append(chunk.rstrip("\n"))
    return out
