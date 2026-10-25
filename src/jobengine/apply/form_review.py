"""Your review of a dry run (Autopilot H8 step 3, 25 Oct).

Under each /dryrun report: "✅ Answers OK" and "❌ Something is wrong".

- Answers OK: the run counts as approved. Each AI answer becomes one of your saved answers
  (so the next form gets the same answer without AI) and a row of the Notion "Answer bank"
  (Source "AI approved"). Its screenshot is deleted APPROVED_KEEP_DAYS (3) days later.
- Something is wrong: the bot lists the answers with numbers; you reply "3 = the right
  answer" (or a sentence about what went wrong). A numbered answer goes to your saved
  answers and the Answer bank (Source "Dry run correction"), and every reply becomes a row
  of the Notion "Auto-apply issues" table (Status "To fix") for the weekend fix session.

The runs are kept in Bot State `dryrun.runs` (the last MAX_RUNS). Screenshots stay on the
machine (out/dryrun), never in Notion.
"""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jobengine.apply.form_bot import Answer, filled

log = logging.getLogger("jobengine.apply")

KEY = "dryrun.runs"  # bot_state: {"items": [run, ...]}
MAX_RUNS = 30
APPROVED_KEEP_DAYS = 3
REF = "Ref DRY-"
FORMS = (("greenhouse", "Greenhouse"), ("lever.co", "Lever"), ("ashbyhq", "Ashby"),
         ("workable", "Workable"), ("recruitee", "Recruitee"), ("personio", "Personio"),
         ("teamtailor", "Teamtailor"), ("myworkdayjobs", "Workday"))
NUMBERED = re.compile(r"^\s*(\d{1,2})\s*[=:.)-]\s*(.+)$", re.S)


def form_type(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return next((name for part, name in FORMS if part in host), "Other")


def _runs(state: Any) -> list[dict[str, Any]]:
    return list(((state.get(KEY) or {}).get("items")) or []) if state is not None else []


def _save(state: Any, runs: list[dict[str, Any]]) -> None:
    if state is not None:
        state.set(KEY, {"items": runs[-MAX_RUNS:]})


def record(state: Any, *, job: str, job_id: str, url: str, shot: Path | None,
           answers: Sequence[Answer], now: datetime) -> str:
    """Keep a finished dry run for the buttons; returns its id."""
    run_id = secrets.token_hex(4)
    items = [{"q": a.field.label, "a": a.value, "source": a.source} for a in filled(answers)]
    _save(state, [*_runs(state), {
        "id": run_id, "at": now.isoformat(timespec="seconds"), "job": job, "job_id": job_id,
        "url": url, "form": form_type(url), "shot": str(shot) if shot else "",
        "answers": items, "status": "new"}])
    return run_id


def get(state: Any, run_id: str) -> dict[str, Any] | None:
    return next((r for r in _runs(state) if r.get("id") == run_id), None)


def _update(state: Any, run_id: str, **changes: Any) -> None:
    _save(state, [{**r, **changes} if r.get("id") == run_id else r for r in _runs(state)])


def buttons(run_id: str) -> list[tuple[str, str]]:
    return [("✅ Answers OK", f"fo:{run_id}"), ("❌ Something is wrong", f"fw:{run_id}")]


def approve(state: Any, run_id: str, now: datetime, bank: Any = None) -> str:
    """Answers OK: AI answers become saved answers and Answer bank rows."""
    from jobengine.apply import answers as answers_mod

    run = get(state, run_id)
    if run is None:
        return "That dry run is no longer kept. Send /dryrun again."
    if run.get("status") == "approved":
        return "Already approved."
    learned = 0
    for item in run.get("answers") or []:
        if item.get("source") != "ai":
            continue
        answers_mod.save(state, item["q"], item["a"])
        learned += 1
        _bank(bank, item["q"], item["a"], "AI approved", now.date())
    _update(state, run_id, status="approved", decided=now.isoformat(timespec="seconds"))
    done = approved_count(state)
    more = f" {learned} AI answer(s) saved as yours for the next forms." if learned else ""
    return (f"✅ Approved: {run.get('job') or run.get('url')}.{more} Approved dry runs so far: "
            f"{done}. The screenshot is deleted in {APPROVED_KEEP_DAYS} days.")


def wrong_question(state: Any, run_id: str) -> str:
    run = get(state, run_id)
    if run is None:
        return "That dry run is no longer kept. Send /dryrun again."
    lines = [f"What is wrong in {run.get('job') or run.get('url')}? Reply to this message:",
             "<number> = <the right answer>, for example 2 = 3 years in production, or one "
             "sentence about what went wrong (a field it missed, a form it could not read).",
             ""]
    lines += [f"{n}. {i['q']}: {i['a']}" for n, i in enumerate(run.get("answers") or [], 1)]
    lines.append(f"{REF}{run_id}")
    return "\n".join(lines)


def correct(state: Any, replied_to: str, text: str, today: date, bank: Any = None,
            issues: Any = None) -> str | None:
    """A reply to the "what is wrong" question: an issue row, and the right answer saved."""
    from jobengine.apply import answers as answers_mod

    match = re.search(rf"{re.escape(REF)}([0-9a-f]{{8}})", replied_to)
    if not match or not text.strip():
        return None
    run_id = match.group(1)
    run = get(state, run_id)
    if run is None:
        return "That dry run is no longer kept. Send /dryrun again."
    items = run.get("answers") or []
    numbered = NUMBERED.match(text)
    question = bot_answer = ""
    right = " ".join(text.split())
    kind = "Other"
    if numbered and 1 <= int(numbered.group(1)) <= len(items):
        item = items[int(numbered.group(1)) - 1]
        question, bot_answer = item["q"], item["a"]
        right = " ".join(numbered.group(2).split())
        kind = "Wrong answer"
        answers_mod.save(state, question, right)
        _bank(bank, question, right, "Dry run correction", today)
    elif re.search(r"\b(miss|missed|empty|skipped|not filled)\b", text, re.I):
        kind = "Missed field"
    elif re.search(r"\b(could not read|not read|broken|blank page|no form)\b", text, re.I):
        kind = "Form not read"
    if issues is not None:
        try:
            issues.create({"Issue": (question or right)[:100], "Job": run.get("job") or "",
                           "Form URL": run.get("url") or None, "Form type": run.get("form"),
                           "Question": question, "Bot answer": bot_answer,
                           "Correct answer": right, "Issue type": kind, "Status": "To fix",
                           "Date": today})
        except Exception as exc:  # the answer is still saved in Bot State
            log.warning("could not write the auto-apply issue: %s", exc)
    _update(state, run_id, status="wrong")
    saved = f" Saved as your answer to \"{question}\": the next form uses it." if question else ""
    where = " It is in Auto-apply issues for the weekend fix." if issues is not None else ""
    return f"Noted ({kind}).{saved}{where}"


def _bank(bank: Any, question: str, answer: str, source: str, today: date) -> None:
    if bank is None:
        return
    try:
        bank.create({"Question": question[:200], "Answer": answer, "Source": source,
                     "Approved": True, "Country": "Any", "Last used": today, "Times used": 1})
    except Exception as exc:  # the answer is still saved in Bot State
        log.warning("could not write the answer bank: %s", exc)


def approved_count(state: Any) -> int:
    return sum(1 for r in _runs(state) if r.get("status") == "approved")


def bank_answers(bank: Any) -> list[dict[str, str]]:
    """Approved Answer bank rows as saved answers ({"q", "a"}); [] when not readable."""
    if bank is None:
        return []
    try:
        rows = bank.rows()
    except Exception as exc:  # the saved answers in Bot State still work
        log.info("answer bank not read: %s", exc)
        return []
    return [{"q": str(r.get("Question")), "a": str(r.get("Answer"))} for r in rows
            if r.get("Approved") and r.get("Question") and r.get("Answer")]


def prune_approved(state: Any, now: datetime) -> int:
    """Delete the screenshots of runs approved more than APPROVED_KEEP_DAYS ago."""
    gone = 0
    for run in _runs(state):
        if run.get("status") != "approved" or not run.get("shot") or not run.get("decided"):
            continue
        try:
            decided = datetime.fromisoformat(run["decided"])
            if now - decided < timedelta(days=APPROVED_KEEP_DAYS):
                continue
            path = Path(run["shot"])
            if path.exists():
                path.unlink()
                gone += 1
        except (ValueError, TypeError, OSError) as exc:
            log.info("screenshot not deleted: %s", exc)
    return gone
