"""Job alert emails (spec section 3.1).

Links in the emails are only read, never requested: tracking and redirect links are stored
exactly as they are. LinkedIn jobs are known only from these emails.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from email.utils import parseaddr
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from jobengine.gmail_reader import GmailMessage
from jobengine.settings import Settings
from jobengine.sweep.models import RawPosting, SourceResult
from jobengine.sweep.normalize import canon

SOURCE = "gmail"
MAX_CARD_PARENTS = 4
MAX_LINE_CHARS = 120


def board_for_sender(sender: str, sender_boards: Mapping[str, str]) -> str:
    """Board name from the sender's domain; the longest matching configured domain wins."""
    address = parseaddr(sender)[1].lower()
    domain = address.rsplit("@", 1)[-1] if "@" in address else ""
    matches = [d for d in sender_boards if domain == d or domain.endswith("." + d)]
    return sender_boards[max(matches, key=len)] if matches else "Other"


def _lines(node: Tag) -> list[str]:
    return [line.strip() for line in node.get_text("\n").split("\n") if line.strip()]


def card_fields(anchor: Tag, title: str) -> tuple[str, str]:
    """(company, location) from the card around an anchor, or ("(unknown)", "")."""
    node = anchor
    for _ in range(MAX_CARD_PARENTS):
        node = node.parent
        if not isinstance(node, Tag):
            break
        lines = _lines(node)
        if 2 <= len(lines) <= 6 and all(len(line) < MAX_LINE_CHARS for line in lines):
            if title in lines:
                rest = lines[lines.index(title) + 1:]
                company = rest[0] if rest else "(unknown)"
                location = rest[1] if len(rest) > 1 else ""
                return company, location
            break
    return "(unknown)", ""


def _looks_like_title(text: str, ignore: list[str]) -> bool:
    if not 3 <= len(text) <= MAX_LINE_CHARS or "@" in text or not re.search(r"[A-Za-z]", text):
        return False
    value = canon(text)
    return not any(canon(term) in value for term in ignore)


def fallback_posting_id(board: str, title: str, company: str) -> str:
    return hashlib.sha1(f"{board}|{title}|{company}".encode()).hexdigest()[:16]


def parse_alert(message: GmailMessage, gmail_cfg: Mapping[str, Any]) -> list[RawPosting]:
    """Postings found in one alert email."""
    board = board_for_sender(message.sender, gmail_cfg.get("sender_boards") or {})
    patterns = gmail_cfg.get("job_url_patterns") or {}
    pattern = re.compile(patterns[board]) if board in patterns else None
    ignore = list(gmail_cfg.get("ignore_link_texts") or [])
    soup = BeautifulSoup(message.html, "html.parser")

    # First pass: group anchors by the job they point to, keeping the one with text.
    found: dict[str, tuple[Tag, str, str | None]] = {}
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        text = anchor.get_text(" ", strip=True)
        if pattern is not None:
            match = pattern.search(href)
            if not match:
                continue
            posting_id = match.group(match.lastindex) if match.lastindex else None
            key = f"id:{posting_id}" if posting_id else f"url:{href}"
        else:
            if not href.lower().startswith(("http://", "https://")):
                continue
            if not _looks_like_title(text, ignore):
                continue
            posting_id = None
            key = f"url:{href}"
        if key not in found or (text and not found[key][0].get_text(strip=True)):
            found[key] = (anchor, href, posting_id)

    postings = []
    for anchor, href, posting_id in found.values():
        title = anchor.get_text(" ", strip=True)
        if not title:
            continue
        company, location = card_fields(anchor, title)
        postings.append(
            RawPosting(
                source=SOURCE,
                board=board,
                title=title,
                company=company,
                location_text=location,
                url=href,
                posting_id=posting_id or fallback_posting_id(board, title, company),
                description=None,
            )
        )
    return postings


def collect(messages: list[GmailMessage], gmail_cfg: Mapping[str, Any]) -> list[RawPosting]:
    """Parse all messages; the same job link found twice in one run is kept once."""
    seen: set[tuple[str, str]] = set()
    postings = []
    for message in messages:
        for posting in parse_alert(message, gmail_cfg):
            key = (posting.board, posting.posting_id)
            if key in seen:
                continue
            seen.add(key)
            postings.append(posting)
    return postings


def label_term(label: str) -> str:
    """Gmail's search term for a label: "Job Alerts" -> label:job-alerts."""
    return "label:" + re.sub(r"[\s/&]+", "-", label.strip().lower())


def alert_query(gmail_cfg: Mapping[str, Any]) -> str:
    """The Gmail search: the alert label or a known sender, in the last `days` days."""
    if gmail_cfg.get("query"):
        return str(gmail_cfg["query"])
    days = int(gmail_cfg.get("days") or 2)
    parts = []
    if gmail_cfg.get("label"):
        parts.append(label_term(str(gmail_cfg["label"])))
    senders = list(gmail_cfg.get("sender_boards") or {})
    if senders:
        parts.append("from:(" + " OR ".join(senders) + ")")
    which = f" ({' OR '.join(parts)})" if parts else ""
    return f"newer_than:{days}d -in:spam -in:trash{which}"


def _loader(s: Settings, gmail_cfg: Mapping[str, Any]) -> Callable[[], list[GmailMessage]]:
    from jobengine import gmail_reader

    def load_messages() -> list[GmailMessage]:
        service = gmail_reader.build_service(s.gmail_alerts_token_json)
        return gmail_reader.fetch_messages(
            service, alert_query(gmail_cfg), int(gmail_cfg.get("max_messages", 100)))

    return load_messages


def fetch(
    s: Settings, load_messages: Callable[[], list[GmailMessage]] | None = None
) -> SourceResult:
    """Gmail source. load_messages is the fake; without it the real Gmail API is used."""
    gmail_cfg = s.sweep.get("gmail") or {}
    result = SourceResult(name=SOURCE)
    if load_messages is None:
        if not s.gmail_alerts_token_json:
            result.skipped_reason = "gmail skipped: GMAIL_ALERTS_TOKEN_JSON missing"
            return result
        load_messages = _loader(s, gmail_cfg)
    try:
        messages = load_messages()
    except Exception as exc:  # the Google client raises many error types
        result.skipped_reason = f"gmail failed: {type(exc).__name__}: {str(exc)[:200]}"
        return result
    result.emails = len(messages)
    result.postings = collect(messages, gmail_cfg)
    return result


def _hosts(html: str) -> list[str]:
    """Link hosts in an email, most frequent first (to see why no job link matched)."""
    counts: dict[str, int] = {}
    for anchor in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        host = (urlsplit(anchor["href"].strip()).hostname or "").lower()
        if host:
            counts[host] = counts.get(host, 0) + 1
    return sorted(counts, key=lambda h: -counts[h])


def check(s: Settings, load_messages: Callable[[], list[GmailMessage]] | None = None,
          limit: int = 20) -> str:
    """/alertcheck: each alert email the next /fetch would read, and the jobs found in it."""
    gmail_cfg = s.sweep.get("gmail") or {}
    if load_messages is None:
        if not s.gmail_alerts_token_json:
            return "GMAIL_ALERTS_TOKEN_JSON is not set: email alerts are not read."
        load_messages = _loader(s, gmail_cfg)
    try:
        messages = load_messages()
    except Exception as exc:  # the Google client raises many error types
        return f"Reading Gmail failed: {type(exc).__name__}: {str(exc)[:200]}"
    query = alert_query(gmail_cfg)
    if not messages:
        return (f"No alert email found. Gmail search used:\n{query}\nCheck that the alerts "
                "reach this mailbox and get the label (a Gmail filter).")
    lines = [f"{len(messages)} alert email(s) found (search: {query})."]
    total = 0
    for message in messages[:limit]:
        board = board_for_sender(message.sender, gmail_cfg.get("sender_boards") or {})
        jobs = parse_alert(message, gmail_cfg)
        total += len(jobs)
        address = parseaddr(message.sender)[1] or message.sender
        lines.append(f"- {board} | {address} | {message.subject[:70]}: {len(jobs)} job(s)")
        if not jobs:
            hosts = ", ".join(_hosts(message.html)[:5]) or "no links"
            lines.append(f"  no job link recognised; links go to: {hosts}")
    if len(messages) > limit:
        lines.append(f"... and {len(messages) - limit} more email(s).")
    lines.append(f"Jobs found in the emails shown: {total}.")
    return "\n".join(lines)
