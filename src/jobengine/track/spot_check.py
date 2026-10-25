"""Weekly spot check (Autopilot H5, 25 Oct): 5 minutes on Sunday to look at the work.

With the autopilot building resumes and cover letters by itself, you no longer check each
one. So every Sunday at 20:00 India time (with the weekly digest) the bot sends 2 of the
week's approved resumes picked at random: the PDF, its keyword match and the cover letter,
with "👍 Looks fine" and "⚠️ Something is wrong". Something is wrong asks what, in a reply;
the notes are kept (Bot State `spotcheck.notes`) for Claude Code and shown by /spotcheck.
/spotcheck also sends a spot check at any time.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
from typing import Any

SENT_KEY = "spotcheck.sent"  # bot_state: {"week": "2026-W43"}
NOTES_KEY = "spotcheck.notes"  # bot_state: {"items": [{"date", "job", "note"}]}
WEEKDAY = 6  # Sunday
AT = time(20, 0)
WINDOW = timedelta(hours=3)
PICKS = 2
DAYS = 7
MAX_NOTES = 30
REF = "Ref SPOT-"
LETTER_HEAD = "Cover letter (paste it if the form asks for one"
LETTER_END = ("\nApply pack", "\nTips for", "\nAutofill:", "\nYour saved answers")
MAX_LETTER = 1500


def _week(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def due(state: Any, now: datetime) -> bool:
    """Sunday from 20:00 (local time of `now`) for 3 hours, once a week."""
    if now.weekday() != WEEKDAY:
        return False
    start = datetime.combine(now.date(), AT, tzinfo=now.tzinfo)
    if not start <= now <= start + WINDOW:
        return False
    return ((state.get(SENT_KEY) if state is not None else None) or {}).get("week") \
        != _week(now.date())


def mark_sent(state: Any, day: date) -> None:
    if state is not None:
        state.set(SENT_KEY, {**(state.get(SENT_KEY) or {}), "week": _week(day)})


def remember(state: Any, cards: Mapping[str, str]) -> None:
    """The jobs of the cards just sent, by ref (for the buttons)."""
    if state is not None:
        value = state.get(SENT_KEY) or {}
        state.set(SENT_KEY, {**value, "jobs": {**(value.get("jobs") or {}), **cards}})


def job_of(state: Any, card_ref: str) -> str:
    return (((state.get(SENT_KEY) if state is not None else None) or {}).get("jobs") or {}) \
        .get(card_ref, "this resume")


def question(job: str, card_ref: str) -> str:
    return (f"What is wrong with {job}? Reply to this message in a few words (for example: "
            f"\"says 5 years of Terraform, I have 3\").\n{REF}{card_ref}")


def _date(value: Any) -> date | None:
    text = str((value or {}).get("start") if isinstance(value, dict) else value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def pick(rows: Sequence[tuple[str, Mapping[str, Any]]], today: date,
         rng: random.Random | None = None, n: int = PICKS) -> list[tuple[str, Mapping[str, Any]]]:
    """n approved Resume Log rows of the last DAYS days, at random (one per job)."""
    since = today - timedelta(days=DAYS)
    by_job: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for log_id, row in rows:
        day = _date(row.get("Date"))
        if not row.get("Approved") or day is None or not since < day <= today:
            continue
        job = ((row.get("Job") or [None])[0]) or log_id
        if job not in by_job or (row.get("Revision") or 0) > (by_job[job][1].get("Revision") or 0):
            by_job[job] = (log_id, row)
    chosen = list(by_job.values())
    (rng or random.Random()).shuffle(chosen)
    return chosen[:n]


def letter(body: Sequence[str]) -> str | None:
    """The cover letter saved on the job's page (the newest), or None."""
    text = "\n".join(body)
    at = text.rfind(LETTER_HEAD)
    if at < 0:
        return None
    rest = text[at:].split(":\n", 1)
    if len(rest) < 2:
        return None
    out = rest[1]
    for end in LETTER_END:
        out = out.split(end, 1)[0]
    out = out.strip()
    return out[:MAX_LETTER] if out else None


def ref(log_id: str) -> str:
    return log_id.replace("-", "")[:8]


def card(job: str, match: str | None, cover_letter: str | None, log_id: str) -> str:
    lines = [f"🔍 Spot check: {job}", match or "Resume match: not known (no job description).",
             "", "Cover letter:", cover_letter or "(none written for this job)", "",
             "Does it look right? A wrong fact, an odd sentence, a skill you do not have?",
             f"{REF}{ref(log_id)}"]
    return "\n".join(lines)


def add_note(state: Any, replied_to: str, note: str, today: date) -> str | None:
    """A reply to the "what is wrong" question: kept for Claude Code."""
    if REF not in replied_to or not note.strip():
        return None
    job = replied_to.split("\n", 1)[0].removeprefix("What is wrong with ").split("?")[0]
    items = list(((state.get(NOTES_KEY) or {}).get("items")) or [])
    items.append({"date": today.isoformat(), "job": job, "note": " ".join(note.split())})
    state.set(NOTES_KEY, {"items": items[-MAX_NOTES:]})
    return (f"Saved ({len(items[-MAX_NOTES:])} note(s)). Send Claude Code the /spotcheck notes "
            "list when you have a few; it fixes the cause.")


def notes(state: Any) -> list[dict[str, str]]:
    return list(((state.get(NOTES_KEY) or {}).get("items")) or []) if state is not None else []


def notes_text(state: Any) -> str:
    items = notes(state)
    if not items:
        return "No spot check notes yet."
    lines = [f"Spot check notes ({len(items)}), for Claude Code:"]
    lines += [f"{n}. {i['date']} {i['job']}: {i['note']}" for n, i in enumerate(items, 1)]
    return "\n".join(lines)
