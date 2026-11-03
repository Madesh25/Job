"""Claude checks every form before Submit (3 Nov, your decision).

DeepSeek (or your saved answers) fills the form; right before the bot presses Submit, the
filled answers are checked. Anything wrong = not submitted: the job comes to you as "Apply by
hand" with the reason. Claude not reachable (no key, its monthly cap, an error) = not
submitted either.

Low cost by design:
1. Free code checks first: an email, phone, link, number or date in the right shape; a
   dropdown or radio answer that is one of its options; your sponsorship answer not turned
   around; years of experience not more than you said.
2. Only answers that are not already yours for sure go to Claude: your name, email, phone and
   links from your profile, the approved resume and a saved answer whose question matches the
   field word for word are never sent. Answers from your profile for other fields (salary,
   notice, city, ...) and saved answers whose question matches only partly are sent: the
   field may ask something else.
3. A short fact sheet (your profile answers, about 300 tokens) instead of the whole resume.
4. Verdict memory: a question and answer Claude said OK to is never checked again (Bot
   State `apply.checked`).
5. A tiny reply: only the wrong field numbers with a reason.

Claude Haiku answers (stage "check", never DeepSeek or Gemini, Config ai.claude_cap applies).
The half-price batch is not used: the form is open in the browser while it is checked, and a
batch answers up to a day later.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

from jobengine.apply.form_bot import RULES, Answer, _words, norm

log = logging.getLogger("jobengine.apply")

STAGE = "check"
MEMORY_KEY = "apply.checked"  # bot_state: {"items": {digest: "2026-11-03", ...}}
MAX_MEMORY = 600
MAX_FACTS = 1500  # characters, about 300 to 400 tokens
MAX_ANSWER = 400
# Profile answers that are yours word for word: never sent to Claude.
IDENTITY = ("first_name", "last_name", "full_name", "email", "phone", "linkedin", "github",
            "website")
NOT_FACTS = ("why", "cover_letter")  # long texts: the fact sheet stays short

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)
URL = re.compile(r"^https?://\S+\.\S+$", re.I)
NUMBER = re.compile(r"^\s*-?\d+([.,]\d+)?\s*$")
YEARS_FIELD = re.compile(r"\byears?\b.*\bexperience\b|\bexperience\b.*\byears?\b")
YEARS = re.compile(r"\b(\d{1,2})\b")
SPONSOR = re.compile(r"\bsponsor")

SYSTEM = """You check a filled job application form right before it is sent, for the candidate.
For each numbered field decide whether the answer is right for that question, using the
candidate's facts. An answer is WRONG when it answers a different question than the label
asks, contradicts the facts, has the wrong format or unit for the field, picks an option that
does not fit the question, or would clearly hurt the application (for example it says the
candidate needs sponsorship when the facts say no). Do not judge style or wording.
Reply with JSON only: {"wrong": [{"n": <field number>, "why": "<at most 15 words>"}]}.
Use {"wrong": []} when every answer is right."""


def digest(label: str, value: str) -> str:
    raw = f"{' '.join(norm(label).split())}\n{' '.join(norm(value).split())}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _label(a: Answer) -> str:
    return " ".join(a.field.label.split())[:60] or "a field"


def _rule_key(label: str, kind: str) -> str | None:
    text = norm(label)
    for pattern, key, unless in RULES:
        if pattern.search(text) and not (unless is not None and unless.search(text)):
            if key == "cover_letter" and kind != "textarea":
                continue
            return key
    return None


def _first(text: str) -> str:
    words = norm(text).split()
    return words[0] if words else ""


def code_checks(answers: Sequence[Answer], profile: Mapping[str, str],
                facts: Mapping[str, str]) -> list[str]:
    """The free checks: what is plainly wrong, without any AI."""
    out = []
    for a in answers:
        if a.value is None or a.note or a.source == "resume":
            continue
        f, value, label = a.field, a.value.strip(), _label(a)
        if f.kind in ("select", "radio") and (
                a.option is None or not 0 <= a.option < len(f.options)
                or norm(f.options[a.option]) != norm(value)):
            out.append(f"\"{label}\": the answer is not one of its options")
            continue
        if f.kind == "checkbox":
            continue
        if f.kind == "email" and not EMAIL.match(value):
            out.append(f"\"{label}\": not an email address")
        elif f.kind == "tel" and len(re.sub(r"\D", "", value)) < 7:
            out.append(f"\"{label}\": not a phone number")
        elif f.kind == "url" and not URL.match(value):
            out.append(f"\"{label}\": not a link")
        elif f.kind == "number" and not NUMBER.match(value):
            out.append(f"\"{label}\": not a number")
        elif f.kind == "date" and not re.search(r"\d", value):
            out.append(f"\"{label}\": not a date")
        text = norm(f.label)
        said = YEARS.findall(norm(facts.get("experience") or ""))
        if YEARS_FIELD.search(text) and said:
            given = YEARS.findall(norm(value))
            if given and int(given[0]) > max(int(n) for n in said):
                out.append(f"\"{label}\": more years than you said ({facts['experience']})")
        if SPONSOR.search(text) and profile.get("sponsorship_needed"):
            mine, theirs = _first(profile["sponsorship_needed"]), _first(value)
            if mine in ("yes", "no") and theirs in ("yes", "no") and mine != theirs:
                out.append(f"\"{label}\": your sponsorship answer is "
                           f"\"{profile['sponsorship_needed']}\"")
    return out


def _saved_exact(label: str, value: str, saved: Sequence[Mapping[str, str]]) -> bool:
    """A saved answer of yours whose every question word is in the label: yours for sure."""
    have = set(_words(label))
    for row in saved:
        want = _words(row.get("q") or "")
        if want and norm(row.get("a") or "") == norm(value) and all(w in have for w in want):
            return True
    return False


def to_check(answers: Sequence[Answer], saved: Sequence[Mapping[str, str]],
             memory: Mapping[str, Any]) -> list[Answer]:
    """The answers Claude has to look at; the others are yours for sure or already OK."""
    out = []
    for a in answers:
        if a.value is None or a.note or a.source == "resume":
            continue
        if a.field.kind == "checkbox":
            continue  # yes or no to a box, the consent rules are code (form_submit)
        if digest(a.field.label, a.value) in memory:
            continue
        if a.source == "profile" and _rule_key(a.field.label, a.field.kind) in IDENTITY:
            continue
        if a.source == "saved" and a.field.kind not in ("select", "radio") and \
                _saved_exact(a.field.label, a.value, saved):
            continue
        out.append(a)
    return out


def fact_sheet(profile: Mapping[str, str], facts: Mapping[str, str], job: str) -> str:
    lines = [f"Job: {job}"] if job else []
    lines += [f"{k}: {' '.join(str(v).split())}" for k, v in facts.items() if v]
    lines += [f"{k}: {' '.join(str(v).split())}" for k, v in profile.items()
              if v and k not in NOT_FACTS and k not in facts]
    return "\n".join(lines)[:MAX_FACTS]


def prompt(items: Sequence[Answer]) -> str:
    lines = []
    for n, a in enumerate(items, 1):
        options = f" (options: {' | '.join(a.field.options[:12])})" if a.field.options else ""
        value = " ".join(str(a.value).split())[:MAX_ANSWER]
        lines.append(f"{n}. [{a.field.kind}] {_label(a)}{options} = {value}")
    return "Fields:\n" + "\n".join(lines)


def _memory(state: Any) -> dict[str, Any]:
    value = (state.get(MEMORY_KEY) if state is not None else None) or {}
    items = value.get("items")
    return dict(items) if isinstance(items, dict) else {}


def remember(state: Any, items: Sequence[Answer], day: str) -> None:
    if state is None or not items:
        return
    memory = _memory(state)
    for a in items:
        memory[digest(a.field.label, a.value or "")] = day
    keep = sorted(memory.items(), key=lambda kv: kv[1])[-MAX_MEMORY:]
    state.set(MEMORY_KEY, {"items": dict(keep)})


class Checker:
    """What form_submit.finisher calls right before Submit: the reasons not to submit."""

    def __init__(self, llm: Any, *, profile: Mapping[str, str], facts: Mapping[str, str],
                 saved: Sequence[Mapping[str, str]], job: str, state: Any, day: str):
        self.llm, self.profile, self.facts = llm, profile, facts
        self.saved, self.job, self.state, self.day = saved, job, state, day
        self.sent = 0  # answers Claude looked at (the tests and the log)

    def __call__(self, answers: Sequence[Answer]) -> list[str]:
        wrong = code_checks(answers, self.profile, self.facts)
        if wrong:
            return wrong  # plainly wrong: no need to ask Claude
        items = to_check(answers, self.saved, _memory(self.state))
        if not items:
            return []
        self.sent = len(items)
        system = f"{SYSTEM}\n\nCandidate facts:\n{fact_sheet(self.profile, self.facts, self.job)}"
        raw = self.llm.complete_json(STAGE, system, prompt(items), max_tokens=300, key="form")
        found = raw.get("wrong") if isinstance(raw, dict) else None
        if not isinstance(found, list):
            raise ValueError(f"the form check gave no verdict: {json.dumps(raw)[:200]}")
        out, bad = [], set()
        for item in found:
            try:
                n = int(item.get("n"))
            except (AttributeError, TypeError, ValueError):
                continue
            if 1 <= n <= len(items) and n not in bad:
                bad.add(n)
                why = " ".join(str(item.get("why") or "looks wrong").split())[:120]
                out.append(f"\"{_label(items[n - 1])}\": {why} (Claude's check)")
        if found and not out:
            raise ValueError("the form check named no field it could find")
        remember(self.state, [a for n, a in enumerate(items, 1) if n not in bad], self.day)
        log.info("form check: %d answer(s) sent, %d wrong", len(items), len(out))
        return out
