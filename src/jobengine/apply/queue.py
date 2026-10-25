"""The auto-apply queue (Autopilot H8 step 4, 25 Oct): which jobs get a dry run, where the
browser runs, and how many forms at once.

- `/autoapply` (or the autopilot with Config `apply.auto` = true) puts the jobs whose resume
  is built (Status "Resume built") in the queue, at most DAILY_LIMIT (30) a day (your
  decision of 25 Oct, Config `apply.daily_limit`).
- Where the browser runs (your idea of 25 Oct, to keep Cloud Run cheap): a laptop worker
  (the bot on your laptop with APPLY_WORKER=laptop) marks itself as seen at every tick and
  takes the queue for free. The bot on Cloud Run asks "Is your laptop on?" when jobs wait
  and the laptop was not seen lately: Yes leaves them to the laptop for LAPTOP_WAIT (15
  minutes); No, no answer, or the laptop not taking them in time: Cloud Run fills them.
- One worker fills PARALLEL (3) forms at a time (one browser each), never more.
- Each job is still a dry run: filled, never submitted. Auto-submit stays off until you have
  approved enough dry runs (`submit_status`; Config `apply.submit` and
  `apply.approvals_needed`, default 10 with no Wrong in the last 10) and then needs its own
  step and your OK.

Bot State `apply.queue` holds the jobs: {"items": [{"job", "added", "state", "by", "at"}],
"day": {"date", "count"}, "laptop": "<last seen>", "asked": "<when>", "answer": "yes|no"}.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

log = logging.getLogger("jobengine.apply")

KEY = "apply.queue"
DAILY_LIMIT = 30
PARALLEL = 3
LAPTOP_SEEN = timedelta(minutes=10)  # a laptop worker seen this recently takes the queue
LAPTOP_WAIT = timedelta(minutes=15)  # after "Yes", Cloud Run waits this long
LEASE = timedelta(minutes=20)  # a job taken but not finished in this time is free again
APPROVALS_NEEDED = 10
WAITING, TAKEN, DONE = "waiting", "taken", "done"


def _get(state: Any) -> dict[str, Any]:
    return dict((state.get(KEY) if state is not None else None) or {})


def _set(state: Any, value: dict[str, Any]) -> None:
    if state is not None:
        state.set(KEY, value)


def _limit(config: Any) -> int:
    try:
        return max(0, int(str(config.get("apply.daily_limit") or DAILY_LIMIT).strip()))
    except ValueError:
        return DAILY_LIMIT


def add(state: Any, config: Any, job_ids: Sequence[str], now: datetime) -> tuple[int, int]:
    """Queue jobs not queued yet, within today's limit. (added, left today)."""
    value = _get(state)
    items = list(value.get("items") or [])
    day = value.get("day") or {}
    count = int(day.get("count") or 0) if day.get("date") == now.date().isoformat() else 0
    room = max(_limit(config) - count, 0)
    known = {i["job"] for i in items if i.get("state") != DONE}
    added = 0
    for job in job_ids:
        if added >= room:
            break
        if job in known:
            continue
        items.append({"job": job, "added": now.isoformat(timespec="seconds"),
                      "state": WAITING})
        known.add(job)
        added += 1
    value.update(items=items[-200:], day={"date": now.date().isoformat(),
                                         "count": count + added})
    _set(state, value)
    return added, room - added


def waiting(state: Any, now: datetime) -> list[str]:
    """Jobs to fill: waiting, or taken longer than LEASE ago (a worker that stopped)."""
    out = []
    for item in _get(state).get("items") or []:
        if item.get("state") == WAITING:
            out.append(item["job"])
        elif item.get("state") == TAKEN:
            try:
                if now - datetime.fromisoformat(item.get("at") or "") > LEASE:
                    out.append(item["job"])
            except (ValueError, TypeError):
                out.append(item["job"])
    return out


def take(state: Any, by: str, now: datetime, n: int = PARALLEL) -> list[str]:
    """Claim up to n jobs for this worker ("laptop" or "cloud")."""
    free = waiting(state, now)[:n]
    if not free:
        return []
    value = _get(state)
    value["items"] = [{**i, "state": TAKEN, "by": by, "at": now.isoformat(timespec="seconds")}
                      if i["job"] in free and i.get("state") != DONE else i
                      for i in value.get("items") or []]
    _set(state, value)
    return free


def finish(state: Any, job: str) -> None:
    value = _get(state)
    value["items"] = [{**i, "state": DONE} if i["job"] == job else i
                      for i in value.get("items") or []]
    _set(state, value)


def laptop_seen(state: Any, now: datetime) -> None:
    value = _get(state)
    value["laptop"] = now.isoformat(timespec="seconds")
    _set(state, value)


def _since(value: dict[str, Any], key: str, now: datetime) -> timedelta | None:
    try:
        return now - datetime.fromisoformat(value.get(key) or "")
    except (ValueError, TypeError):
        return None


def cloud_decision(state: Any, now: datetime) -> str:
    """What the Cloud Run bot does with waiting jobs: "none" (nothing waits), "laptop" (the
    laptop takes them), "ask" (send "Is your laptop on?"), "wait" (asked, waiting for the
    answer or the laptop), "cloud" (fill them here)."""
    if not waiting(state, now):
        return "none"
    value = _get(state)
    seen = _since(value, "laptop", now)
    if seen is not None and seen <= LAPTOP_SEEN:
        return "laptop"
    asked = _since(value, "asked", now)
    if asked is None or asked > LAPTOP_WAIT * 4:  # a new question for a new batch
        return "ask"
    if value.get("answer") == "no" or asked > LAPTOP_WAIT:
        return "cloud"
    return "wait"


def asked(state: Any, now: datetime) -> None:
    value = _get(state)
    value.update(asked=now.isoformat(timespec="seconds"), answer="")
    _set(state, value)


def answer(state: Any, yes: bool) -> None:
    value = _get(state)
    value["answer"] = "yes" if yes else "no"
    _set(state, value)


def run_parallel(jobs: Sequence[str], fill: Callable[[str], Any],
                 parallel: int = PARALLEL) -> list[Any]:
    """Fill the jobs, at most `parallel` browsers at once; a failed job gives its error."""
    def safe(job: str) -> Any:
        try:
            return fill(job)
        except Exception as exc:  # one broken form never stops the others
            log.warning("auto-apply dry run of %s failed: %s", job, exc)
            return exc

    if not jobs:
        return []
    with ThreadPoolExecutor(max_workers=max(1, min(parallel, len(jobs)))) as pool:
        return list(pool.map(safe, jobs))


def submit_status(config: Any, approved: int, recent_wrong: int) -> str:
    """Whether auto-submit could be turned on: it stays a dry run until then."""
    try:
        needed = int(str(config.get("apply.approvals_needed") or APPROVALS_NEEDED).strip())
    except ValueError:
        needed = APPROVALS_NEEDED
    on = (config.get("apply.submit") or "false").strip().lower() == "true"
    if approved < needed or recent_wrong:
        why = f"{approved} of {needed} dry runs approved"
        if recent_wrong:
            why += f", {recent_wrong} marked wrong in the last 10"
        return f"Auto-submit: off (dry runs only): {why}."
    if not on:
        return (f"Auto-submit: ready to discuss ({approved} dry runs approved, none wrong "
                "lately). It needs its own step and your OK; until then every form is a dry "
                "run.")
    return "Auto-submit: Config apply.submit is true, but submitting is not built yet: dry runs."
