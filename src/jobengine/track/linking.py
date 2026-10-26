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


# ---------------------------------------------------------------- confirmation mails (A)
# Autopilot A (26 Oct): a "Thank you for applying" mail from a career site marks the job
# Applied, also when you applied outside the bot and never tapped "I applied".

ACK_PHRASES = (
    "thank you for applying", "thanks for applying", "thank you for your application",
    "thanks for your application", "thank you for your interest in", "we have received your "
    "application", "we've received your application", "your application has been received",
    "your application was received", "application received", "dziękujemy za aplikację",
    "dziękujemy za przesłanie aplikacji", "dziękujemy za złożenie aplikacji",
    "bedankt voor je sollicitatie", "bedankt voor uw sollicitatie")
# "Thank you for your interest" also opens many rejections: never an acknowledgement then.
NOT_ACK = ("unfortunately", "not to move forward", "not moving forward", "other candidates",
           "will not be progressing", "not been successful", "niestety", "helaas")


def phrase_query(window: str, phrases: tuple[str, ...]) -> str:
    return f"{window} ({' OR '.join(f'\"{p}\"' for p in phrases)})"


def says(message: Message, phrases: tuple[str, ...]) -> bool:
    text = f"{message.subject} {message.snippet} {message.text}".casefold()
    return any(p in text for p in phrases)


def is_ack(message: Message) -> bool:
    return says(message, ACK_PHRASES) and not says(message, NOT_ACK)


def matching_jobs(message: Message, jobs: dict[str, dict[str, object]]) -> list[str]:
    """The jobs whose company the mail names; with several at one company, the ones whose
    role it names too."""
    found = [pid for pid, job in jobs.items() if mentions(message, str(job.get("Company") or ""))]
    if len(found) <= 1:
        return found
    text = f" {canon(f'{message.subject} {message.snippet} {message.text}')} "
    named = [pid for pid in found
             if (role := canon(str(jobs[pid].get("Role") or ""))) and f" {role} " in text]
    return named or found
