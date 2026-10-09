"""Interview prep pack and thank-you mail (flow feature 15, 9 Oct).

/prep <job>: one message to read before the interview: the tools the job asks for that you
have and the gaps to answer honestly (the free skill matcher, no AI), the company details
stored from the job page, the likely questions with an answer hint each, questions to ask
them and your adopted interview tips. The questions come from one AI call on the job
description and your Golden Master resume; a hint may only use facts from the resume, and a
hint that claims one of the job's gaps is replaced by a reminder to answer it honestly.

/thanks <job> [first name]: the thank-you mail to send the same day, from the template in
config/base.yaml `interview.thanks` (a Notion Config key `interview.thanks` wins), filled
with the role, the company, one detail from the job page and your name. No AI; nothing is
sent: you paste it into your reply in the interview thread.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("jobengine.apply")

STAGE = "tailor"
MAX_TOKENS = 1500
MAX_QUESTIONS = 8
MAX_DETAILS = 4
HONEST = "(not on your resume: say so honestly and name the closest thing you have used)"
SYSTEM_PROMPT = f"""You help a candidate prepare for a job interview.
From JOB DESCRIPTION and RESUME, list the {MAX_QUESTIONS} questions the interviewers are most
likely to ask (technical and behavioural, in the order they are likely to come). For each give
a short answer hint (at most 30 words) built ONLY from facts in RESUME: a project, a tool, a
number, an employer. Never add a skill, tool, number or claim that RESUME does not contain.
For a skill listed in GAPS the hint must say plainly that the candidate has not used it in
production and name the closest thing RESUME shows.
No em dashes or en dashes.
Answer with JSON only: {{"questions": [{{"q": "<question>", "hint": "<answer hint>"}}]}}"""
NEGATION = re.compile(r"\b(not|never|no|haven't|have not|yet to)\b", re.I)


@dataclass(frozen=True)
class Questions:
    items: list[tuple[str, str]] = field(default_factory=list)  # (question, hint)
    note: str | None = None  # why there are none


def _clean(text: Any) -> str:
    text = str(text or "").replace(chr(0x2014), ", ").replace(chr(0x2013), "-")
    return " ".join(text.split())


def _names(term: str, text: str) -> bool:
    return bool(re.search(rf"(?<![\w]){re.escape(term)}(?![\w])", text, re.I))


def checked_hint(hint: str, gaps: list[str]) -> str:
    """A hint that names a gap without saying you have not used it becomes HONEST."""
    named = [g for g in gaps if g and _names(g, hint)]
    if named and not NEGATION.search(hint):
        return HONEST
    return hint


def questions(llm: Any, *, company: str, role: str, jd: str, resume_text: str,
              gaps: list[str], key: str) -> Questions:
    """One AI call, checked. Never raises: a failure is a note."""
    if not jd.strip():
        return Questions(note="the job has no description")
    if not resume_text.strip():
        return Questions(note="your Golden Master resume could not be read")
    user = (f"COMPANY: {company}\nROLE: {role}\nGAPS: {', '.join(gaps) or 'none'}\n\n"
            f"JOB DESCRIPTION:\n{jd[:6000]}\n\nRESUME:\n{resume_text[:6000]}")
    try:
        answer = llm.complete_json(STAGE, SYSTEM_PROMPT, user, max_tokens=MAX_TOKENS,
                                   key=f"prep-{key}")
    except Exception as exc:
        log.info("interview questions not written: %s", exc)
        return Questions(note=f"the AI call failed ({type(exc).__name__})")
    items = []
    for item in answer.get("questions") or []:
        if not isinstance(item, dict):
            continue
        q, hint = _clean(item.get("q")), _clean(item.get("hint"))
        if q:
            items.append((q, checked_hint(hint, gaps) if hint else HONEST))
    if not items:
        return Questions(note="the AI gave no questions")
    return Questions(items[:MAX_QUESTIONS])


def prep_text(*, company: str, role: str, where: str, url: str, have: list[str],
              gaps: list[str], details: list[str], qs: Questions, ask: list[str],
              tips: list[str]) -> str:
    lines = [f"Interview prep: {company}, {role} ({where})", f"Job: {url or '(no URL)'}", ""]
    lines.append("They ask for and you have: " + (", ".join(have) or "(no tools matched)"))
    if gaps:
        lines.append("Gaps to answer honestly: " + ", ".join(gaps))
    if details:
        lines += ["", f"About {company} (from the job page):"]
        lines += [f"- {d}" for d in details[:MAX_DETAILS]]
    lines += ["", "Likely questions (hints use only facts from your resume):"]
    if qs.items:
        for n, (q, hint) in enumerate(qs.items, 1):
            lines += [f"{n}. {q}", f"   Hint: {hint}"]
    else:
        lines.append(f"- not written ({qs.note}).")
    if ask:
        lines += ["", "Questions to ask them:"] + [f"- {a}" for a in ask]
    if tips:
        lines += ["", "Your interview tips:"] + [f"- {t}" for t in tips]
    lines += ["", "After the interview: /thanks <job link> <interviewer first name> gives the "
              "thank-you mail to send the same day."]
    return "\n".join(lines)


def their_words(detail: str) -> str:
    """The job page speaks as "we": "moving our workloads to EKS" -> "moving your ..."."""
    for word, new in (("our", "your"), ("ours", "yours"), ("we", "you"), ("us", "you")):
        detail = re.sub(rf"\b{word}\b", new, detail)
    return detail.rstrip(".")


def thanks_text(template: str, *, first: str | None, company: str, role: str,
                detail: str | None, name: str) -> str:
    detail_line = (f"I especially enjoyed hearing about {their_words(detail)}. "
                   if detail else "")
    body = template.replace("\\n", "\n").format(
        greeting=f"Hi {first}," if first else "Hello,", company=company, role=role or "open",
        detail=detail_line, name=name)
    body = re.sub(r"[ \t]+\n", "\n", re.sub(r"[ \t]{2,}", " ", body))
    return (f"Thank-you mail for {company} (send it today as a reply in the interview "
            f"thread; edit it first, nothing is sent):\n{body.strip()}")
