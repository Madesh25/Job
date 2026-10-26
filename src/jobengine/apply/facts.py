"""Your own facts for the AI texts: the cover letter and the interview hints (16 Oct).

Phase 9 (P9-15, P9-23): the cover letter said "available to commence immediately" while your
notice period is 90 days, and both texts wrote "4+ years" and "3+ years specializing in AWS"
while your answer is "4 years overall, 3 years in DevOps". The AI only saw the resume, which
has neither line. Now the prompt gets your answers (Config apply.experience,
apply.notice_period, apply.earliest_start, apply.relocation, as in the Apply pack) and every
text is checked after the call:

- "N+ years" becomes "N years" (the "+" claims more than you said);
- a year count that is not in your experience answer is a problem (the caller drops or
  replaces the text);
- a sentence that says you can start immediately becomes your notice period.

Gap terms: a Gaps cell holds phrases from the job ("Dynamo, Lambda, Bedrock, Generative AI
technologies"); the checks look for each short term, not the whole phrase, and leave out
terms your resume itself shows (those are not gaps to hide).
"""

from __future__ import annotations

import re
from typing import Any

from jobengine.apply.pack import MISSING, answer

KEYS = ("experience", "notice_period", "earliest_start", "relocation")
LABELS = {"experience": "EXPERIENCE", "notice_period": "NOTICE PERIOD",
          "earliest_start": "EARLIEST START", "relocation": "RELOCATION"}
RULES = """- Years of experience: use only the numbers in EXPERIENCE, in its words. Never write
  "N+ years" and never give years for one tool, cloud or skill.
- Availability: use only NOTICE PERIOD and EARLIEST START. Never say "immediately"."""

YEARS = re.compile(r"\b(\d{1,2})\s*\+?\s*(?=(?:years?|yrs?)\b)", re.I)
PLUS = re.compile(r"\b(\d{1,2})\s*\+\s*(?=(?:years?|yrs?)\b)", re.I)
# "4 years overall, 3+ in DevOps" (/prep, 26 Oct): the years are named earlier in the sentence.
PLUS_IN = re.compile(r"\b(\d{1,2})\s*\+(?=\s+(?:in|of|on|with|as)\b)", re.I)
NOW = re.compile(r"\b(immediate(ly)?|right away|straight away|at once|without delay)\b", re.I)
SENTENCE = re.compile(r"[^.!?\n]*[.!?]?")
FILLER = re.compile(r"^(?:and|or|with|strong|solid|proven|deep|hands-on|good|excellent|"
                    r"experience|knowledge|familiarity|understanding|expertise|exposure|"
                    r"operating|working|building|using|of|in|with|on)\s+", re.I)
MAX_TERM_WORDS = 4


def facts(s: Any, config: Any) -> dict[str, str]:
    """Your answers that are set (an unset one is left out, never a placeholder)."""
    out = {}
    for key in KEYS:
        value = answer(s, config, key)
        if value != MISSING.format(key=key):
            out[key] = value
    return out


def block(values: dict[str, str]) -> str:
    """The FACTS part of an AI prompt."""
    lines = [f"{LABELS[k]}: {values[k]}" for k in KEYS if values.get(k)]
    return "FACTS (the candidate's own answers):\n" + ("\n".join(lines) or "none")


def _allowed(values: dict[str, str]) -> set[str]:
    return set(re.findall(r"\d+", values.get("experience") or ""))


def _keep_space(old: str, new: str) -> str:
    return old[:len(old) - len(old.lstrip())] + new


def fix(text: str, values: dict[str, str]) -> tuple[str, str | None]:
    """(text with "+" years and "immediately" fixed, the problem left or None)."""
    text = PLUS_IN.sub(r"\1", PLUS.sub(r"\1 ", text))
    notice = values.get("notice_period") or ""
    if notice and not NOW.search(notice):
        line = f"My notice period is {notice.rstrip('.')}."
        text = SENTENCE.sub(lambda m: (_keep_space(m.group(0), line)
                                       if NOW.search(m.group(0)) else m.group(0)), text)
    allowed = _allowed(values)
    wrong = [n for n in YEARS.findall(text) if allowed and n not in allowed]
    if wrong:
        return text, (f"it gave {wrong[0]} years, but your answer is "
                      f"\"{values.get('experience')}\"")
    return text, None


def names(term: str, text: str) -> bool:
    """The text names the term. A one-word term written with a capital ("Lambda",
    "Organizations") must keep it, so "help organizations" does not count."""
    flags = 0 if " " not in term and term[:1].isupper() else re.I
    return bool(re.search(rf"(?<![\w]){re.escape(term)}(?![\w])", text, flags))


def gap_terms(gaps: list[str], resume_text: str = "") -> list[str]:
    """Short terms from the gap phrases, without the ones the resume shows."""
    terms: list[str] = []
    for gap in gaps:
        for part in re.split(r",|;|\(|\)|\band\b|\bor\b", gap):
            term = part.strip(" .:-")
            while FILLER.match(term):
                term = FILLER.sub("", term, count=1).strip()
            if not term or len(term) < 2 or len(term.split()) > MAX_TERM_WORDS:
                continue
            if resume_text and names(term, resume_text):
                continue
            if term.casefold() not in {t.casefold() for t in terms}:
                terms.append(term)
    return terms
