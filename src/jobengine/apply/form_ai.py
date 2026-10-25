"""AI answers for the open form questions (Autopilot H8 step 2, 25 Oct).

The dry run (apply/form_bot.py) fills what your profile, Apply pack and saved answers cover.
What is left ("Why are you a good fit?", "Describe your Kubernetes experience", "How many
years of Terraform?") goes to the AI in ONE call per form (Config `model.apply`, plan C:
DeepSeek; Haiku when not set or when it fails). It gets:

- the question, its kind and the options of a dropdown or radio question;
- your facts: the Apply pack answers, your saved answers (they show your own voice), your
  Skills Inventory (skill and level) and your resume text;
- the job: company, role and the start of the description.

Rules for the AI: answer in your voice (first person, plain and short like your saved
answers), only true facts from what it was given; when the facts do not answer the question
it says so (`null`), and the question stays for you. Nothing is invented to fill a form.

Then code checks every AI answer before it is typed (no AI):
- a year count must be in your experience answer and "N+ years" becomes "N years"
  (apply/facts.py);
- "immediately" becomes your notice period;
- a skill the job asks for that is neither in your resume nor in your Skills Inventory drops
  the answer (a gap is never claimed);
- an option must be one of the question's options.
A dropped answer is listed with the reason, and the field stays empty for you.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from jobengine.apply import facts as facts_mod
from jobengine.apply.form_bot import Answer, Field, choice

log = logging.getLogger("jobengine.apply")

STAGE = "apply"
MAX_QUESTIONS = 25
MAX_RESUME = 6000
MAX_JD = 3000
MAX_ANSWER = 1200
SOURCE = "ai"

SYSTEM = """You fill a job application form for the candidate, in the candidate's own voice.
Answer only from the FACTS, SAVED ANSWERS, SKILLS and RESUME given. Never invent a fact,
a number, a skill, a certificate, a project or an employer. When they do not answer a
question, give null for it: the candidate answers it.
Write like the SAVED ANSWERS: first person, plain, short, friendly, no buzzwords, no
"I am excited", no exaggeration. A yes/no question gets the option text only.
For a question with OPTIONS, the answer must be exactly one of the options.
""" + facts_mod.RULES + """
Return JSON: {"answers": [{"n": <question number>, "answer": <text or null>}]}"""


@dataclass(frozen=True)
class Context:
    """What the AI may use: your facts, voice and skills, and the job."""

    facts: Mapping[str, str]  # apply/facts.facts: experience, notice_period, ...
    profile: Mapping[str, str]  # apply/autofill.fields: the Apply pack answers
    saved: Sequence[Mapping[str, str]]  # your /answer list
    skills: Sequence[str]  # "Kubernetes (Advanced)"
    resume: str
    company: str = ""
    role: str = ""
    jd: str = ""


def open_questions(answers: Sequence[Answer]) -> list[Answer]:
    """The fields the AI may try: no answer yet, not a file, not a consent box."""
    return [a for a in answers if a.value is None and a.note == "no answer yet"
            and a.field.kind not in ("file", "checkbox")][:MAX_QUESTIONS]


def prompt(questions: Sequence[Answer], ctx: Context) -> str:
    lines = [facts_mod.block(dict(ctx.facts)), "", "APPLY PACK ANSWERS:"]
    lines += [f"- {k}: {v}" for k, v in ctx.profile.items() if k not in ("cover_letter",)]
    lines += ["", "SAVED ANSWERS (the candidate's own words):"]
    lines += [f"- {r.get('q')} -> {r.get('a')}" for r in ctx.saved] or ["- none"]
    lines += ["", "SKILLS (Skills Inventory):", ", ".join(ctx.skills) or "none", "",
              "RESUME:", ctx.resume[:MAX_RESUME], "",
              f"JOB: {ctx.company}, {ctx.role}", ctx.jd[:MAX_JD], "", "QUESTIONS:"]
    for n, a in enumerate(questions, 1):
        f = a.field
        line = f"{n}. {f.label} ({f.kind}{', required' if f.required else ''})"
        options = [o for o in f.options if o.strip() and not o.lower().startswith("select")]
        if options:
            line += " OPTIONS: " + " | ".join(options)
        lines.append(line)
    return "\n".join(lines)


def _known(term: str, ctx: Context) -> bool:
    if facts_mod.names(term, ctx.resume):
        return True
    return any(term.casefold() in s.casefold() for s in ctx.skills)


def check(answer: str, field: Field, ctx: Context, gaps: Sequence[str]) -> tuple[str, str | None]:
    """(fixed answer, why it is dropped or None): the code fact check, no AI."""
    text = " ".join(answer.split())[:MAX_ANSWER]
    text, problem = facts_mod.fix(text, dict(ctx.facts))
    if problem:
        return text, problem
    for term in facts_mod.gap_terms(list(gaps), ctx.resume):
        if facts_mod.names(term, text) and not _known(term, ctx):
            return text, f"it claims {term}, which is not in your resume or Skills Inventory"
    if field.kind in ("select", "radio") and choice(field.options, text) is None:
        return text, f"\"{text}\" is not one of the options"
    return text, None


def fill(llm: Any, answers: list[Answer], ctx: Context, gaps: Sequence[str] = (),
         key: str | None = None) -> int:
    """Ask the AI for the open questions and put the checked answers in. Returns how many
    were filled. An AI failure leaves them open (logged)."""
    questions = open_questions(answers)
    if not questions or llm is None:
        return 0
    try:
        got = llm.complete_json(STAGE, SYSTEM, prompt(questions, ctx), max_tokens=3000,
                                key=key)
    except Exception as exc:  # the dry run still shows what the code filled
        log.warning("form answers by AI failed: %s", exc)
        for a in questions:
            a.note = "no answer yet (the AI call failed)"
        return 0
    by_n = {}
    for item in got.get("answers") or []:
        try:
            by_n[int(item.get("n"))] = item.get("answer")
        except (TypeError, ValueError, AttributeError):
            continue
    done = 0
    for n, a in enumerate(questions, 1):
        raw = by_n.get(n)
        if not isinstance(raw, str) or not raw.strip():
            a.note = "no answer yet (not in your facts)"
            continue
        text, problem = check(raw, a.field, ctx, gaps)
        if problem:
            a.note = f"AI answer dropped: {problem}"
            continue
        if a.field.kind in ("select", "radio"):
            index = choice(a.field.options, text)
            a.value, a.option = a.field.options[index], index
        else:
            a.value = text
        a.source, a.note = SOURCE, ""
        done += 1
    return done


def skills_lines(skills: Sequence[Any]) -> list[str]:
    """Skills Inventory rows as "Name (Level)"."""
    return [f"{s.name} ({s.level})" if getattr(s, "level", None) else s.name for s in skills]

