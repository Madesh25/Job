"""What the filters dropped this week, for the weekly audit (28 Oct, your request).

The filters stay as strict as you set them; nothing here changes them. Each /fetch only
remembers, for the week, the jobs a filter may have got wrong, so Opus can look at them:

- near: the title is not one of your titles (sweep.title_include) but has a near word
  (cloud, infrastructure, platform, kubernetes, terraform ...): "Cloud Infrastructure
  Specialist", "Platform Developer". A few changed words can hide a fitting job;
- excluded: a DevOps-type title (one of yours or a near word) with an excluded word
  (sweep.title_exclude): mostly seniority on purpose ("Lead DevOps Engineer"), sometimes a
  wrong hit ("DevOps Engineer, Staff Augmentation");
- low: too few of your skills in the description (sweep.min_skill_match).

Only jobs in a place you take are counted (other countries and remote-only jobs are not near
misses). Plus the week's totals: postings read and saved, per source, and the drop reasons.
Kept in Bot State per week (the last WEEKS weeks), at most MAX_TITLES titles a list; the
counts are complete. Pure apart from the Bot State calls in `save`.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

KEY = "sweep.filter_weeks"  # bot_state: {"weeks": {iso Monday: {...}}}
WEEKS = 2  # an audit that runs late still finds its week
MAX_TITLES = 60
LISTS = ("near", "excluded", "low")
NEAR = re.compile(
    r"\b(cloud|infrastructure|infra|platform|site reliability|reliability|sre|kubernetes|k8s|"
    r"openshift|release engineer|build engineer|systems engineer|"
    r"dev[ -]?ops|devsecops|mlops|gitops|ci ?/ ?cd|cicd|aws|azure|gcp|google cloud|terraform|"
    r"ansible|linux|sysadmin|system administrator|systems administrator|observability|"
    r"production engineer|operations engineer|automation engineer|deployment)\b", re.I)


def near_word(title: str) -> str | None:
    found = NEAR.search(title or "")
    return found.group(1).lower() if found else None


@dataclass
class Watch:
    """One /fetch's dropped titles (it may be filled from several threads)."""

    lists: dict[str, dict[str, list[Any]]] = field(
        default_factory=lambda: {name: {} for name in LISTS})
    totals: dict[str, int] = field(default_factory=lambda: {name: 0 for name in LISTS})
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, kind: str, title: str, why: str, where: str) -> None:
        """`why`: the near word, the excluded word or ""; `where`: company and place."""
        title = " ".join((title or "").split())[:100]
        if not title:
            return
        with self._lock:
            self.totals[kind] += 1
            items = self.lists[kind]
            key = title.lower()
            if key in items:
                items[key][0] += 1
            elif len(items) < MAX_TITLES:
                items[key] = [1, title, why, where[:80]]

    def title_drop(self, reason: str, title: str, where: str) -> None:
        """A title the title filter dropped ("title not in scope" or "title excluded (x)")."""
        if reason.startswith("title excluded"):
            if near_word(title):  # "Sales Manager" is not one of yours
                self.add("excluded", title, reason.partition("(")[2].rstrip(")"), where)
        elif reason.startswith("title not in scope"):
            word = near_word(title)
            if word:
                self.add("near", title, word, where)


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _merge_list(old: dict[str, list[Any]], new: dict[str, list[Any]]) -> dict[str, list[Any]]:
    out = {k: list(v) for k, v in old.items()}
    for key, item in new.items():
        if key in out:
            out[key][0] += item[0]
        elif len(out) < MAX_TITLES:
            out[key] = list(item)
    return out


def save(state: Any, today: date, watch: Watch, by_source: Mapping[str, Mapping[str, int]],
         labels: Mapping[str, str], kept: Iterable[str], saved: str) -> None:
    """Add one /fetch to its week in Bot State. `by_source`: the loss report's buckets per
    source; `kept`: the buckets that are not drops; `saved`: the "saved as new" bucket."""
    if state is None:
        return
    value = state.get(KEY) or {}
    weeks = dict(value.get("weeks") or {})
    monday = monday_of(today).isoformat()
    week = dict(weeks.get(monday) or {})
    week["runs"] = int(week.get("runs") or 0) + 1
    sources = {k: list(v) for k, v in (week.get("sources") or {}).items()}
    reasons = dict(week.get("reasons") or {})
    kept = set(kept)
    for source, counts in by_source.items():
        name = labels.get(source, source)
        read_saved = sources.setdefault(name, [0, 0])
        read_saved[0] += sum(counts.values())
        read_saved[1] += counts.get(saved, 0)
        for reason, n in counts.items():
            if reason not in kept:
                reasons[reason] = reasons.get(reason, 0) + n
    week["sources"], week["reasons"] = sources, reasons
    totals = dict(week.get("totals") or {})
    for name in LISTS:
        totals[name] = int(totals.get(name) or 0) + watch.totals[name]
        week[name] = _merge_list(week.get(name) or {}, watch.lists[name])
    week["totals"] = totals
    weeks[monday] = week
    keep = sorted(weeks, reverse=True)[:WEEKS]
    state.set(KEY, {"weeks": {k: weeks[k] for k in keep}})


def week_of(state_value: Mapping[str, Any] | None, monday: date) -> dict[str, Any]:
    return dict(((state_value or {}).get("weeks") or {}).get(monday.isoformat()) or {})

