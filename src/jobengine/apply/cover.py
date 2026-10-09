"""Cover letter per job and the final resume's keyword cover (flow features 4 and 11, 9 Oct).

Cover letter: many EU application forms ask for one. After Approve resume, one AI call
writes 150 to 200 words for this job from two sources only: the job description and the
text of the approved resume PDF (the resume already passed the V16 gate, so it is the truth
to stay within). The letter is checked before it is shown: it must not name any of the job's
gaps (skills you do not have), it is cut to at most MAX_WORDS, and dashes are replaced. A
letter that fails is left out with a note; nothing is sent anywhere, you paste it yourself.

Keyword cover: the tools the job names that you have (the same list as "Skill match" on the
/pending card) and how many of them the final resume shows, so a resume that leaves out a
skill you have and the job asks for is seen before Approve resume.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("jobengine.apply")

STAGE = "tailor"
MIN_WORDS, MAX_WORDS = 110, 220
MAX_TOKENS = 700
SYSTEM_PROMPT = """You write a short cover letter for a job application.
Rules (never break them):
- Use ONLY facts found in RESUME. Never add a skill, tool, employer, number, degree or claim
  that RESUME does not contain. If the job asks for something RESUME does not show, do not
  mention it.
- 150 to 200 words, English, plain and specific, first person, three short paragraphs:
  why this role and company (use COMPANY DETAILS if given), two or three matching facts from
  RESUME, a closing line about relocation and availability from RESUME.
- No greeting placeholders like [Name], no address block, no subject line. Start with
  "Dear Hiring Team," and end with "Kind regards," and the candidate's name from RESUME.
- No em dashes or en dashes.
Answer with JSON only: {"letter": "<the letter>"}"""


@dataclass(frozen=True)
class Letter:
    text: str | None
    note: str | None = None  # why there is no letter


def pdf_text(pdf: bytes | None) -> str:
    """The text of a PDF ("" when it cannot be read)."""
    if not pdf:
        return ""
    import pdfplumber

    try:
        with pdfplumber.open(io.BytesIO(pdf)) as doc:
            return "\n".join(page.extract_text() or "" for page in doc.pages)
    except Exception as exc:  # a PDF that cannot be read only loses the extras
        log.info("resume PDF text not read: %s", exc)
        return ""


def _names(term: str, text: str) -> bool:
    return bool(re.search(rf"(?<![\w]){re.escape(term)}(?![\w])", text, re.I))


def cover(terms: list[str], resume_text: str) -> tuple[list[str], list[str]]:
    """(shown, left out): the job's terms you have, split by whether the resume shows them."""
    shown = [t for t in terms if _names(t, resume_text)]
    return shown, [t for t in terms if t not in shown]


def cover_line(terms: list[str], resume_text: str, limit: int = 6) -> str | None:
    if not terms or not resume_text:
        return None
    shown, left = cover(terms, resume_text)
    line = f"Resume match: shows {len(shown)} of {len(terms)} job keywords you have"
    if left:
        more = f" and {len(left) - limit} more" if len(left) > limit else ""
        line += f" (not on it: {', '.join(left[:limit])}{more}; Rebuild to add them)"
    return line + "."


def _clean(text: str) -> str:
    text = text.replace(chr(0x2014), ", ").replace(chr(0x2013), "-")  # em, en dash
    return "\n\n".join(" ".join(p.split()) for p in text.split("\n\n") if p.strip())


def check(text: str, gaps: list[str]) -> str | None:
    """Why the letter cannot be used, or None."""
    named = [g for g in gaps if g and _names(g, text)]
    if named:
        return f"it named skills you do not have ({', '.join(named[:3])})"
    words = len(text.split())
    if words < MIN_WORDS:
        return f"it was too short ({words} words)"
    return None


def _cut(text: str) -> str:
    words = text.split(" ")
    if len(text.split()) <= MAX_WORDS:
        return text
    cut = " ".join(words[:MAX_WORDS])
    end = max(cut.rfind(". "), cut.rfind(".\n"))
    return cut[:end + 1] if end > 0 else cut


def write(llm: Any, *, company: str, role: str, country: str, jd: str, resume_text: str,
          details: list[str], gaps: list[str], key: str) -> Letter:
    """One AI call, checked. Never raises: a failure is a Letter with a note."""
    if not resume_text.strip():
        return Letter(None, "the approved resume could not be read")  # caller drops it
    if not jd.strip():
        return Letter(None, "the job has no description")
    user = (f"COMPANY: {company}\nROLE: {role}\nCOUNTRY: {country}\n"
            f"COMPANY DETAILS: {'; '.join(details) or 'none'}\n\nJOB DESCRIPTION:\n"
            f"{jd[:6000]}\n\nRESUME:\n{resume_text[:6000]}")
    try:
        answer = llm.complete_json(STAGE, SYSTEM_PROMPT, user, max_tokens=MAX_TOKENS,
                                   key=f"cover-{key}")
    except Exception as exc:
        log.info("cover letter not written: %s", exc)
        return Letter(None, f"the AI call failed ({type(exc).__name__})")
    text = _cut(_clean(str(answer.get("letter") or "")))
    problem = check(text, gaps)
    if problem:
        log.info("cover letter left out: %s", problem)
        return Letter(None, problem)
    return Letter(text)


def block(letter: Letter) -> str:
    """The cover letter part of the Apply pack."""
    if letter.text:
        return ("Cover letter (paste it if the form asks for one; only facts from your "
                f"approved resume):\n{letter.text}")
    return f"Cover letter: not written ({letter.note})."
