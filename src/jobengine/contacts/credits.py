"""Credit counters in Config (`credits.<provider>` = "<used> / <limit> per month").

Parsing and the monthly reset are pure. Outside prod the counters are simulated in memory
and never written; in prod each paid call updates the Config row (allowlisted keys only).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from jobengine.config_store import ConfigStore
from jobengine.safety import check_config_write

log = logging.getLogger("jobengine.contacts.credits")

PROVIDERS = ("apollo", "hunter", "snov")
COUNTER_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)")


@dataclass
class Counter:
    used: int
    limit: int
    updated: date | None = None

    @property
    def left(self) -> int:
        return max(self.limit - self.used, 0)

    def text(self) -> str:
        return f"{self.used} / {self.limit} per month"

    def short(self) -> str:
        return f"{self.used}/{self.limit}"


def parse_counter(value: str | None, updated: date | None = None) -> Counter | None:
    match = COUNTER_RE.match(value or "")
    if not match:
        return None
    return Counter(used=int(match.group(1)), limit=int(match.group(2)), updated=updated)


def monthly_reset(counter: Counter, today: date) -> Counter:
    """Counters reset on the 1st: a counter last updated in an earlier month starts at 0."""
    if counter.updated and (counter.updated.year, counter.updated.month) < (today.year,
                                                                           today.month):
        return Counter(used=0, limit=counter.limit, updated=counter.updated)
    return counter


ConfigWriter = Callable[[str, str, date], None]


class CreditBook:
    """The providers' counters for one run. `writer` is None outside prod (simulated)."""

    def __init__(self, config: ConfigStore, today: date, writer: ConfigWriter | None = None):
        self.today = today
        self.writer = writer
        self.counters: dict[str, Counter | None] = {}
        for provider in PROVIDERS:
            key = f"credits.{provider}"
            counter = parse_counter(config.get(key), config.updated(key))
            self.counters[provider] = monthly_reset(counter, today) if counter else None

    def available(self, provider: str) -> int:
        counter = self.counters.get(provider)
        return counter.left if counter else 0

    def spend(self, provider: str, credits: int) -> None:
        counter = self.counters.get(provider)
        if counter is None or credits <= 0:
            return
        counter.used += credits
        counter.updated = self.today
        if self.writer is not None:
            key = f"credits.{provider}"
            check_config_write(key)
            self.writer(key, counter.text(), self.today)
        else:
            log.info("credits.%s is now %s (simulated, not written)", provider, counter.text())

    def line(self) -> str:
        parts = []
        for provider in PROVIDERS:
            counter = self.counters.get(provider)
            name = provider.capitalize()
            parts.append(f"{name} {counter.short()}" if counter else f"{name} n/a")
        return "Credits: " + ", ".join(parts)

