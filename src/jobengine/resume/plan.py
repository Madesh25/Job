"""The tailoring plan (LLM stage `tailor`): which skills to show and a few words per bullet.

The LLM only proposes; the integrity gate decides. The LLM never sees contacts, emails or
phone numbers: only the skills tables, the bullets, the job description and the reference
terms.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from jobengine.llm import LLMClient
from jobengine.reference import OWNED_LEVELS, Reference
from jobengine.resume.models import MasterResume, Plan, PlanRow

STAGE = "tailor"
MAX_TOKENS = 4000

SYSTEM_PROMPT = """You tailor a one-page DevOps resume to one job. Return one JSON object only.

You may change exactly two things:
1. Technical Skills tables. Remove items that do not matter for this job, reorder items and
   rows, remove whole rows, move items between the main table and "Also worked with". Rename a
   row label only to a name that still contains the original label (for example "CI/CD" to
   "CI/CD Pipelines") or to an Active Term Map JD term for that label. Add an item only if it is
   in the owned terms list. Keep at least 5 main rows and at least 1 "Also worked with" row.
   Never list an item twice.
2. Experience bullets. Insert or swap a few words inside existing bullets to use the job's
   terms, only terms from the owned terms list or words already in that bullet, joined with
   small connector words (and, with, using, for, on, in, via, to, of, across, including).
   At most 8 inserted words per bullet and 25 in total. Never change a number, a company, a
   title, a date or a claim. Never add, remove, split, merge or reorder bullets.

Never invent experience. Never use a long dash; use commas or a plain hyphen.
If the job asks for a tool that is not owned, list it in gaps_reported, do not add it.

Output schema:
{
  "skills_main": [{"label": <string>, "items": [<string>]}],
  "skills_also": [{"label": <string>, "items": [<string>]}],
  "bullet_edits": [{"id": <bullet id>, "text": <the full new bullet text>}],
  "term_mappings": [{"jd_term": <string>, "used_as": <string>, "where": "skills" or <id>}],
  "priority": {"bullet_edits": [<ids, most important first>],
               "skill_items_low_relevance": [<items, least relevant first>]},
  "focus": {"skills": <one line>, "experience": <one line>, "summary": "unchanged"},
  "gaps_reported": [<job terms you could not use>],
  "correction_refused": <null, or the reason when the correction asks for something the
                         rules above do not allow>
}
Only list bullets you changed in bullet_edits.
"""


class PlanError(Exception):
    """The LLM answer is not a valid plan."""


@dataclass(frozen=True)
class JobContext:
    page_id: str
    company: str
    role: str
    country: str | None
    jd: str
    specific_details: list[str] = field(default_factory=list)
    gaps: str | None = None


def owned_terms(reference: Reference) -> list[str]:
    """Terms the resume may use, with what backs them."""
    lines = [f"{s.name} ({s.level})" for s in reference.skills if s.level in OWNED_LEVELS]
    for row in reference.term_map:
        if row.backs:
            lines.append(f"{', '.join(row.jd_terms)} (Term Map: truthfully "
                         f"{row.truthful_equivalent})")
    return lines


def _rows(rows: Any) -> list[dict[str, Any]]:
    return [{"label": r.label, "items": list(r.items)} for r in rows]


def user_prompt(
    master: MasterResume,
    job: JobContext,
    reference: Reference,
    correction: str | None = None,
    previous: Plan | None = None,
    errors: list[str] | None = None,
) -> str:
    data: dict[str, Any] = {
        "job": {"company": job.company, "role": job.role, "country": job.country},
        "skills_main": _rows(master.skills_main),
        "skills_also": _rows(master.skills_also),
        "bullets": [{"id": b.id, "company": b.company, "text": b.text} for b in master.bullets],
        "specific_details": job.specific_details,
        "gaps_from_screening": job.gaps or "",
        "owned_terms": owned_terms(reference),
    }
    if previous is not None:
        data["previous_plan"] = previous.to_json()
    if errors:
        data["gate_errors_to_fix"] = errors
    if correction:
        data["correction_from_candidate"] = correction
    return (
        f"{json.dumps(data, indent=1, ensure_ascii=False)}\n\n"
        f"Job description:\n<<<\n{job.jd}\n>>>"
    )


def parse_plan(raw: dict[str, Any], master: MasterResume) -> Plan:
    """Validate the LLM answer. Missing tables mean "unchanged"."""
    data = dict(raw or {})
    unchanged = Plan.unchanged(master)
    for key in ("skills_main", "skills_also"):
        if not data.get(key):
            data[key] = [row.model_dump() for row in getattr(unchanged, key)]
    try:
        plan = Plan.model_validate(data)
    except ValidationError as exc:
        raise PlanError(f"the tailoring plan is not valid: {exc.errors()[0]['msg']}") from None
    # Tidy what the model may pad: stray spaces and empty items.
    for rows in (plan.skills_main, plan.skills_also):
        rows[:] = [PlanRow(label=r.label.strip(), items=[i.strip() for i in r.items if i.strip()])
                   for r in rows]
    for edit in plan.bullet_edits:
        edit.text = " ".join(edit.text.split())
    return plan


def make_plan(
    llm: LLMClient,
    master: MasterResume,
    job: JobContext,
    reference: Reference,
    *,
    correction: str | None = None,
    previous: Plan | None = None,
    errors: list[str] | None = None,
    key: str | None = None,
) -> Plan:
    raw = llm.complete_json(
        STAGE, SYSTEM_PROMPT,
        user_prompt(master, job, reference, correction, previous, errors),
        max_tokens=MAX_TOKENS, key=key,
    )
    return parse_plan(raw, master)
