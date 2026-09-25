"""Application confirmation matching (spec section 2 step 5). Pure apart from the search."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time

from jobengine.sweep.normalize import canon, canon_company
from jobengine.track.models import Message


@dataclass
class LinkResult:
    thread_id: str | None  # exactly one candidate
    candidates: list[Message]  # one message per candidate thread


def query(domains: list[str], since: date) -> str:
    epoch = int(datetime.combine(since, time.min, tzinfo=UTC).timestamp())
    return f"after:{epoch} from:({' OR '.join(domains)}) -in:chats"


def mentions(message: Message, company: str) -> bool:
    name = canon_company(company)
    return bool(name) and name in canon(f"{message.subject} {message.snippet}")


def candidates(messages: list[Message], company: str, domains: list[str]) -> LinkResult:
    """Messages from the company domain or a known ATS whose subject or snippet names the
    company, grouped by thread."""
    by_thread: dict[str, Message] = {}
    for m in messages:
        domain = m.sender_domain
        if not any(domain == d or domain.endswith("." + d) for d in domains):
            continue
        if mentions(m, company):
            by_thread.setdefault(m.thread_id, m)
    found = list(by_thread.values())
    return LinkResult(thread_id=found[0].thread_id if len(found) == 1 else None,
                      candidates=found)
