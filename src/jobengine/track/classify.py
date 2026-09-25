"""Reply classification (spec section 4): deterministic pre-checks, then the LLM (stage
`score`). The LLM is advisory: low confidence goes to Telegram."""

from __future__ import annotations

import logging
import re
from typing import Any

from jobengine.llm import LLMClient, LLMError
from jobengine.track.models import AUTO_REPLY, CLASSES, OPT_OUT, OTHER, Classification, Message

log = logging.getLogger("jobengine.track")

MAX_CHARS = 3000
QUOTE_START = re.compile(
    r"^\s*(On .{0,300}wrote:\s*$|>|-----\s*Original Message\s*-----|"
    r"From:\s.+|W dniu .{0,200}pisze:|Op .{0,200}schreef.*:)",
    re.IGNORECASE,
)


def strip_quoted(text: str, limit: int = MAX_CHARS) -> str:
    """The new part of a reply: everything before the first quote marker, at most `limit`
    characters."""
    lines = []
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        if QUOTE_START.match(line):
            break
        lines.append(line)
    return "\n".join(lines).strip()[:limit]


# ---------------------------------------------------------------- classification

STAGE = "score"
MAX_TOKENS = 300
AUTO_SUBJECTS = ("automatic reply", "out of office", "autosvar", "auto:", "autoreply",
                 "odpowiedz automatyczna", "automatische antwoord")
DEFAULT_OPT_OUT = ("do not contact", "remove me", "unsubscribe", "not interested, please stop",
                   "nie kontaktuj")

SYSTEM_PROMPT = """You classify one email reply received during a job search. Return one JSON
object only: {"class": <one of ack, positive, neutral, screening, interview, offer, rejection,
opt_out, auto_reply, other>, "confidence": <0.0 to 1.0>, "quote": <an exact short phrase
copied from the text that shows the class>}.

ack: an automatic or template confirmation that an application was received.
positive: interested, wants to help, will forward the CV, asks a follow-up question.
neutral: polite reply with no clear next step.
screening: asks for a call or a recruiter screen, or sends a questionnaire.
interview: invites to an interview or a technical round, or proposes interview times.
offer: makes a job offer.
rejection: says no, the role is filled, or the process will not continue.
opt_out: asks not to be contacted again.
auto_reply: out of office or another automatic reply.
other: none of the above.
The quote must be copied exactly from the text. When unsure, use a low confidence.
"""


def _norm(text: str) -> str:
    return " ".join((text or "").casefold().split())


def is_auto_reply(message: Message) -> bool:
    h = message.headers
    auto = h.get("auto-submitted", "").strip().casefold()
    if auto and auto != "no":
        return True
    if "x-autoreply" in h or "x-autorespond" in h:
        return True
    subject = message.subject.strip().casefold()
    return subject.startswith(AUTO_SUBJECTS)


def opt_out_phrase(text: str, phrases: tuple[str, ...] | list[str]) -> str | None:
    lowered = _norm(text)
    return next((p for p in phrases if _norm(p) and _norm(p) in lowered), None)


def user_prompt(message: Message, text: str) -> str:
    return (f"Subject: {message.subject}\nSender domain: {message.sender_domain}\n"
            f"Text:\n<<<\n{text}\n>>>")


def classify(message: Message, llm: LLMClient | None, *, phrases: tuple[str, ...] | list[str]
             = DEFAULT_OPT_OUT, key: str | None = None) -> Classification:
    """Pre-checks first (auto-reply headers, opt-out phrases), then the LLM. Only the
    subject, the sender domain and the new reply text are sent."""
    if is_auto_reply(message):
        return Classification(AUTO_REPLY, 1.0, source="header")
    text = strip_quoted(message.text or message.snippet)
    phrase = opt_out_phrase(text, phrases)
    if phrase:
        return Classification(OPT_OUT, 1.0, quote=phrase, source="phrase")
    if llm is None:
        return Classification(OTHER, 0.0, source="none")
    try:
        raw = llm.complete_json(STAGE, SYSTEM_PROMPT, user_prompt(message, text),
                                max_tokens=MAX_TOKENS, key=key or f"reply_{message.id}")
    except LLMError as exc:
        log.warning("reply classification failed for %s: %s", message.id, exc)
        return Classification(OTHER, 0.0, source="error")
    return parse(raw, text)


def parse(raw: dict[str, Any], text: str) -> Classification:
    kind = str(raw.get("class") or "").strip().lower()
    if kind not in CLASSES:
        kind = OTHER
    try:
        confidence = min(1.0, max(0.0, float(raw.get("confidence") or 0)))
    except (TypeError, ValueError):
        confidence = 0.0
    quote = str(raw.get("quote") or "").strip()
    if not quote or _norm(quote) not in _norm(text):
        confidence = 0.0  # the quote rule from Module 03
    return Classification(kind, confidence, quote=quote)
