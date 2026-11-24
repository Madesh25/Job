"""Weekly screening check (S3, 23 Nov, your ask): the 20 Nov audit of 7 days of /screen
(58 jobs checked one by one, 4 faults), done by itself every week in 5 minutes.

Every Sunday from 19:30 India time (before the 20:00 spot check) the bot picks 10 jobs
screened in the last 7 days at random, spread over the verdicts (Apply high, Apply normal,
Apply low, Needs review, Skip in turn, so a rare verdict is not missed), and writes them to
Notion Screening Check: the job, its verdict and why (skip reason, gaps, years, sponsorship,
language, visa flags), with links to its Notion page and its posting. You tick Your check
Right or Wrong, and for a Wrong one pick what it Should be and add a Note if you like.

The next Sunday's message tells last week's result ("8 right, 2 wrong, 0 not ticked") and
names the wrong ones; each wrong one is also written to the Error Log as "Reported by you"
(Stage Screen, Status New), so Claude Code fixes it with the other reported rows. The picks of
a week are the same if the check runs twice (seeded by the week), and your ticks are never
overwritten. /screencheck runs it at any time.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from typing import Any

from jobengine.track.run_log import redact, where

SENT_KEY = "screencheck.sent"  # bot_state: {"week": "2026-W48", "reported": ["2026-W47"]}
WEEKDAY = 6  # Sunday
AT = time(19, 30)
WINDOW = timedelta(hours=3)
PICKS = 10
DAYS = 7
VERDICTS = ("Apply high", "Apply normal", "Apply low", "Needs review", "Skip")
RIGHT, WRONG = "Right", "Wrong"
WHY_CHARS = 1900  # Notion keeps 2000 characters in one text value
# LOG-04 (24 Nov): Qualtrics (Expired since 10 Nov) and EY (closed that morning) were picked.
GONE = "Expired"
SECTION = "Screening ("
TABLE_NAME = "Screening Check"


def week_of(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def due(state: Any, now: datetime) -> bool:
    """Sunday from 19:30 (local time of `now`) for 3 hours, once a week."""
    if now.weekday() != WEEKDAY:
        return False
    start = datetime.combine(now.date(), AT, tzinfo=now.tzinfo)
    if not start <= now <= start + WINDOW:
        return False
    return ((state.get(SENT_KEY) if state is not None else None) or {}).get("week") \
        != week_of(now.date())


def mark_sent(state: Any, day: date) -> None:
    if state is not None:
        state.set(SENT_KEY, {**(state.get(SENT_KEY) or {}), "week": week_of(day)})


def _day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str((value or {}).get("start") if isinstance(value, dict) else value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _list(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value if v)
    return str(value or "")


def screening_lines(body: Sequence[str]) -> list[str]:
    """The verdict and notes of the newest screening section of the job's page ("Verdict:
    Skip (Seniority)", "Note: 6 years required, your limit is 4"): LOG-04, the Why of a Skip
    showed only the ghost risk because a gate's reason is written there, not in a column."""
    start = max((i for i, line in enumerate(body) if line.startswith(SECTION)), default=None)
    if start is None:
        return []
    out = []
    for line in body[start + 1:]:
        if line.startswith(SECTION) or line.startswith("Description source:"):
            break
        if line.startswith(("Verdict:", "Note:")):
            out.append(line)
    return out


def why(values: Mapping[str, Any], body: Sequence[str] | None = None) -> str:
    """Why the verdict: the screening lines of the job's page first, then its status (a job you
    declined says so), then what screening wrote on the job row."""
    parts = screening_lines(body or [])
    if values.get("Status") and values.get("Status") != "Screened":
        parts.append(f"Status: {values.get('Status')}")
    for label, key in (("Skip reason", "Skip reason"), ("Gaps", "Gaps"),
                       ("Years required", "Years required"), ("Sponsorship", "Sponsorship"),
                       ("Language", "Language required"), ("Visa flags", "Visa flags"),
                       ("Contract", "Contract type"), ("Ghost job risk", "Ghost job risk")):
        text = _list(values.get(key)).strip()
        if text:
            parts.append(f"{label}: {text}")
    return redact("\n".join(parts) or "No reason saved on the job (only the verdict).")


def pick(rows: Sequence[tuple[str, Mapping[str, Any]]], today: date, n: int = PICKS,
         rng: random.Random | None = None) -> list[tuple[str, Mapping[str, Any]]]:
    """n jobs screened in the last DAYS days (found or seen again then), at random, the
    verdicts in turn. The same week gives the same picks."""
    since = today - timedelta(days=DAYS)
    groups: dict[str, list[tuple[str, Mapping[str, Any]]]] = defaultdict(list)
    for page_id, values in sorted(rows, key=lambda r: r[0]):
        verdict = values.get("Screen verdict")
        seen = _day(values.get("Swept date")) or _day(values.get("First seen"))
        if GONE in (values.get("Status"), values.get("Skip reason")):
            continue  # too old or closed: nothing to check (LOG-04)
        if verdict in VERDICTS and seen is not None and since < seen <= today:
            groups[str(verdict)].append((page_id, values))
    rng = rng or random.Random(week_of(today))
    for jobs in groups.values():
        rng.shuffle(jobs)
    chosen: list[tuple[str, Mapping[str, Any]]] = []
    while len(chosen) < n and any(groups.values()):
        for verdict in VERDICTS:
            if groups.get(verdict) and len(chosen) < n:
                chosen.append(groups[verdict].pop())
    return chosen


def job_name(values: Mapping[str, Any]) -> str:
    return f"{values.get('Company') or '?'}, {values.get('Role') or '?'}"


def key(week: str, page_id: str) -> str:
    return f"{week}|{page_id}"


def row(page_id: str, values: Mapping[str, Any], week: str, today: date,
        body: Sequence[str] | None = None) -> dict[str, Any]:
    """The Screening Check row of one picked job (without Your check: that one is yours)."""
    url = str(values.get("URL") or "").split()[0] if values.get("URL") else None
    return {
        "Job": job_name(values)[:150], "Week": week, "Picked": today,
        "Verdict": values.get("Screen verdict"), "Country": values.get("Country"),
        "Why": why(values, body)[:WHY_CHARS],
        "Job page": f"https://www.notion.so/{page_id.replace('-', '')}",
        "Job link": url if url and url.startswith("http") else None,
        "Key": key(week, page_id),
    }


def week_rows(rows: Sequence[Mapping[str, Any]], week: str) -> list[Mapping[str, Any]]:
    return [r for r in rows if str(r.get("Week") or "") == week]


def result_line(rows: Sequence[Mapping[str, Any]], week: str) -> str | None:
    """Last week's result: "Last week (2026-W47): 8 right, 2 wrong, 0 not ticked." and the
    wrong ones, or None when that week had no check."""
    if not rows:
        return None
    right = sum(1 for r in rows if r.get("Your check") == RIGHT)
    wrong = [r for r in rows if r.get("Your check") == WRONG]
    open_ = len(rows) - right - len(wrong)
    lines = [f"Last week ({week}): {right} right, {len(wrong)} wrong, {open_} not ticked."]
    for r in wrong:
        should = f", should be {r['Should be']}" if r.get("Should be") else ""
        lines.append(f"❌ {r.get('Job')}: {r.get('Verdict')}{should}")
    if wrong:
        lines.append("The wrong ones are in the Error Log (Reported by you) for Claude Code.")
    return "\n".join(lines)


def error_rows(rows: Sequence[Mapping[str, Any]], week: str,
               now: datetime) -> list[dict[str, Any]]:
    """One Error Log row per wrong verdict of `week` (Level Reported by you, Stage Screen)."""
    from jobengine.track import reports

    out = []
    for r in rows:
        if r.get("Your check") != WRONG:
            continue
        should = f", should be {r['Should be']}" if r.get("Should be") else ""
        said = f"Screening wrong: {r.get('Job')}: {r.get('Verdict')}{should}"
        example = (f"{said}\nYour note: {r.get('Note') or '-'}\nJob page: {r.get('Job page')}"
                   f"\n\nWhy the bot said {r.get('Verdict')}:\n{r.get('Why') or '-'}")
        out.append({
            "Problem": said[:150], "Day": now.date(), "First seen": now, "Last seen": now,
            "Count": 1, "Stage": "Screen", "Level": reports.LEVEL, "Likely cause": reports.CAUSE,
            "Where": where(), "Status": "New", "Example": redact(example)[:reports.EXAMPLE_CHARS],
            "Key": f"{week}|screencheck-{str(r.get('Key') or '').split('|')[-1]}",
        })
    return out


def message(week: str, picked: int, last: str | None) -> str:
    if picked:
        head = (f"🧪 Weekly screening check ({week}): {picked} of this week's verdicts are in "
                f"Notion, {TABLE_NAME}. Tick Your check Right or Wrong (for a wrong one, pick "
                "Should be): about 5 minutes.")
    else:
        head = (f"🧪 Weekly screening check ({week}): no job was screened in the last {DAYS} "
                "days, nothing to check.")
    return head + (f"\n\n{last}" if last else "")


def run(table: Any, error_table: Any, state: Any, jobs: Sequence[tuple[str, Mapping[str, Any]]],
        now: datetime, body: Callable[[str], Sequence[str]] | None = None) -> str:
    """This week's picks into the table, last week's result in the message, its wrong ones
    into the Error Log once. Returns the Telegram message."""
    today = now.date()
    week, last_week = week_of(today), week_of(today - timedelta(days=7))
    picks = pick(jobs, today)
    for page_id, values in picks:
        try:
            lines = body(page_id) if body is not None else []
        except Exception:  # the row still comes, with the columns only
            lines = []
        props = row(page_id, values, week, today, lines)
        table.upsert({"Key": props.pop("Key")}, props)
    previous = week_rows(table.rows(), last_week)
    sent = (state.get(SENT_KEY) if state is not None else None) or {}
    reported = list(sent.get("reported") or [])
    if previous and last_week not in reported and error_table is not None:
        for values in error_rows(previous, last_week, now):
            error_table.create(values)
        if state is not None:
            state.set(SENT_KEY, {**sent, "reported": (reported + [last_week])[-8:]})
    return message(week, len(picks), result_line(previous, last_week))
