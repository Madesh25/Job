"""Half-price screening with the Message Batches API: send now, save the answers later.

`/screen batch` runs the free checks on the Unscreened rows (skips are written at once, no AI
used), then sends the rest, up to what is left of today's limit, as one batch. The batch ID
and its rows are kept in Bot State (`screen.batch`). Anthropic answers most batches within an
hour (at most 24 hours), at half the price of normal calls.

`/screen collect` checks the batch; once it has ended, each answer goes through the same
quote check, gates, tier and Notion writes as a normal screening. A row whose answer failed
stays Unscreened and is screened by the next /screen. Plain /screen leaves rows that are in
a waiting batch alone, so no job is paid for twice.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from jobengine import http
from jobengine.llm import LLMError
from jobengine.parallel import run_all
from jobengine.screen import daily
from jobengine.screen.extract import MAX_OUTPUT_TOKENS, SYSTEM_PROMPT, check_quotes, request_text
from jobengine.screen.extract import STAGE as SCORE_STAGE
from jobengine.screen.models import JobRow, ScreenResult, ScreenSummary
from jobengine.screen.runner import (
    BATCH_STATE_KEY,
    Prepared,
    ScreenDeps,
    _batch_rows,
    _label,
    _start,
    _usage_line,
    job_row,
    unscreened_rows,
)
from jobengine.settings import Settings

log = logging.getLogger("jobengine.screen")

STATE_KEY = BATCH_STATE_KEY
NO_BATCH = "No batch is waiting. /screen batch sends the Unscreened jobs at half price."


def waiting_rows(deps: ScreenDeps) -> set[str]:
    """Page IDs of the rows in a batch that has not been collected yet."""
    return _batch_rows(deps)


def _batch_llm(llm: Any) -> Any:
    if not all(hasattr(llm, name) for name in ("submit_batch", "batch_status", "batch_results")):
        raise LLMError("this LLM client cannot send batches")
    return llm


def submit(s: Settings, deps: ScreenDeps, today: date, limit: int | None = None) -> ScreenSummary:
    """Free checks now, then one batch for the rest (up to today's limit)."""
    summary = ScreenSummary()
    if deps.repo is None:
        summary.errors.append("DRY RUN: no Job Opportunities target here, nothing screened")
        return summary
    if deps.state is None or not deps.write:
        summary.errors.append("Batch screening needs Bot State and writes: use /screen instead.")
        return summary
    if waiting_rows(deps):
        summary.errors.append("A batch is already waiting: send /screen collect first.")
        return summary
    used = daily.used_today(deps.state, today)
    room = limit if limit is not None else max(0, daily.daily_limit(s) - used)
    if room <= 0:
        summary.errors.append(f"Today's screening limit is reached ({used} screened).")
        return summary
    run = _start(s, deps, today)
    llm = _batch_llm(run.llm)
    rows = unscreened_rows(deps)
    workers = max(1, int(s.screening.get("workers", 4)))
    prepared = run_all(lambda row: run.prepare(row, ("New",)), rows, workers)
    send: list[tuple[JobRow, Prepared]] = []
    for row, item in zip(rows, prepared, strict=True):
        if isinstance(item, ScreenResult):  # no description yet, or skipped for free
            if item.verdict == "Unscreened":
                summary.waiting_for_jd += 1
            summary.add(item)
        elif len(send) < room:
            send.append((row, item))
    if not send:
        summary.errors.append("Nothing left for the AI: no batch sent.")
        return summary
    items = [(row.page_id, request_text(row.role, row.company, row.country or "", p.jd))
             for row, p in send]
    batch_id = llm.submit_batch(SCORE_STAGE, SYSTEM_PROMPT, items, max_tokens=MAX_OUTPUT_TOKENS)
    deps.state.set(STATE_KEY, {"id": batch_id, "rows": [row.page_id for row, _ in send],
                               "sent": today.isoformat()})
    daily.record(deps.state, today, used + len(send))
    summary.errors.append(
        f"Sent {len(send)} jobs to the half-price batch. Anthropic usually answers within an "
        "hour (at most 24 hours). Send /screen collect to save the results."
    )
    left = sum(1 for item in prepared if not isinstance(item, ScreenResult)) - len(send)
    if left > 0:
        summary.errors.append(f"{left} more jobs wait for tomorrow's limit.")
    return summary


def collect(s: Settings, deps: ScreenDeps, today: date) -> ScreenSummary:
    """Save the answers of the waiting batch once it has ended."""
    summary = ScreenSummary()
    if deps.repo is None or deps.state is None:
        summary.errors.append(NO_BATCH)
        return summary
    pending = deps.state.get(STATE_KEY)
    if not pending or not pending.get("id"):
        summary.errors.append(NO_BATCH)
        return summary
    run = _start(s, deps, today)
    llm = _batch_llm(run.llm)
    status = llm.batch_status(pending["id"])
    if not status.ended:
        summary.errors.append(f"The batch is still working: {status.done} of {status.total} "
                              "answered. Send /screen collect again later.")
        return summary
    answers = llm.batch_results(pending["id"], SCORE_STAGE)
    for page_id in pending.get("rows") or []:
        try:
            values = deps.repo.get_values(page_id)
            if not values or values.get("Screen verdict") != "Unscreened":
                continue  # deleted, or screened another way meanwhile
            row = job_row(page_id, values)
            item = run.prepare(row, ("New",))
            if isinstance(item, ScreenResult):
                summary.add(item)
                continue
            answer = answers.get(page_id)
            if not isinstance(answer, dict):
                summary.errors.append(f"Could not screen {_label(row)}: "
                                      f"{answer or 'no answer in the batch'} (still Unscreened)")
                continue
            ext, dropped = check_quotes(answer, item.jd)
            summary.add(run.finish(row, item, ext, dropped, ("New",)))
        except http.HttpError as exc:
            summary.errors.append(f"Could not save {page_id}: {exc}")
    deps.state.delete(STATE_KEY)
    summary.usage_line = _usage_line(run)
    summary.errors.extend(dict.fromkeys(run.notes))
    return summary
