"""What works (S6, 23 Nov, your ask): where to spend your time, every week in the digest.

From every job in Notion, found > approved > applied > answered (> interview), split by
source (Board), country and screening verdict, and the speed: days from posting to applying
(applying in the first 1 to 2 days gets far more replies). A short tip names the source that
gives the most approvals for what it finds, and the big sources that gave none (candidates to
drop, FETCH 11). In the Sunday weekly digest and with /stats works. Pure.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime
from statistics import median
from typing import Any

from jobengine.track.stats import BoardRow, board_results, pct

SPLITS = (("source", "Board"), ("country", "Country"), ("verdict", "Screen verdict"))
TOP = 6  # groups shown per split; the rest go into "others"
MIN_FOUND = 10  # a group needs this many found jobs for a tip
DROP_FOUND = 30  # a source with this many found and none approved is a candidate to drop
FAST_DAYS = 2


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


def groups(jobs: list[Mapping[str, Any]], key: str) -> dict[str, BoardRow]:
    """found > approved > applied > answered > interview per value of `key`."""
    return board_results({**values, "Board": values.get(key) or "Unknown"} for values in jobs)


def _line(name: str, row: BoardRow) -> str:
    rate = pct(row.approved / row.found) if row.found else "-"
    out = (f"- {name}: {row.found} > {row.approved} > {row.applied} > {row.answered} "
           f"({rate} approved)")
    return out + (f", {row.interview} interview(s)" if row.interview else "")


def split_lines(rows: dict[str, BoardRow], top: int = TOP) -> list[str]:
    order = sorted(rows.items(), key=lambda kv: (-kv[1].applied, -kv[1].approved,
                                                  -kv[1].found, kv[0]))
    lines = [_line(name, row) for name, row in order[:top]]
    rest = [row for _name, row in order[top:]]
    if rest:
        total = BoardRow(*(sum(getattr(r, f) for r in rest)
                           for f in ("found", "approved", "applied", "answered", "interview")))
        lines.append(_line(f"others ({len(rest)})", total))
    return lines


def speed(jobs: Iterable[Mapping[str, Any]]) -> tuple[int, float | None, int]:
    """(applied jobs with both dates, median days from posting to applying, how many within
    FAST_DAYS)."""
    days = []
    for values in jobs:
        applied = _day(values.get("Applied date"))
        posted = _day(values.get("Posted date")) or _day(values.get("First seen"))
        if applied and posted and applied >= posted:
            days.append((applied - posted).days)
    if not days:
        return 0, None, 0
    return len(days), float(median(days)), sum(1 for d in days if d <= FAST_DAYS)


def tips(sources: dict[str, BoardRow]) -> list[str]:
    out = []
    fair = {k: r for k, r in sources.items() if r.found >= MIN_FOUND and k != "Unknown"}
    best = max(fair.items(), key=lambda kv: (kv[1].approved / kv[1].found, kv[1].approved),
               default=None)
    if best and best[1].approved:
        out.append(f"Most approvals for what it finds: {best[0]} "
                   f"({best[1].approved} of {best[1].found}, "
                   f"{pct(best[1].approved / best[1].found)}).")
    drop = sorted(k for k, r in sources.items() if r.found >= DROP_FOUND and not r.approved)
    if drop:
        out.append(f"Many found, none approved: {', '.join(drop)}: candidates to drop "
                   "(FETCH 11).")
    return out


def text(jobs: Iterable[Mapping[str, Any]]) -> str:
    """The "What works" block."""
    jobs = list(jobs)
    if not jobs:
        return "What works: no jobs in Notion yet."
    lines = ["📈 What works (all jobs in Notion: found > approved > applied > answered)"]
    sources = groups(jobs, "Board")
    for title, key in SPLITS:
        rows = sources if key == "Board" else groups(jobs, key)
        lines += [f"By {title}:", *split_lines(rows)]
    count, middle, fast = speed(jobs)
    if count:
        lines.append(f"Speed: {count} applied job(s), median {middle:g} day(s) from posting to "
                     f"applying; {fast} of {count} within {FAST_DAYS} days (the first 1 to 2 "
                     "days get far more replies).")
    else:
        lines.append("Speed: no applied job with a posting date yet.")
    lines += tips(sources)
    return "\n".join(lines)
