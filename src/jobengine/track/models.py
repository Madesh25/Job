"""Data carried through the daily tracking run."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# Reply classes (section 4).
ACK, POSITIVE, NEUTRAL = "ack", "positive", "neutral"
SCREENING, INTERVIEW, OFFER = "screening", "interview", "offer"
REJECTION, OPT_OUT, AUTO_REPLY, OTHER = "rejection", "opt_out", "auto_reply", "other"
# LOG-18 (25 Nov, your rule): the person you mailed says they cannot help or are not the right
# person. They are not mailed again for this company; its next job gets a new person.
CANT_HELP = "cant_help"
CLASSES = (ACK, POSITIVE, NEUTRAL, SCREENING, INTERVIEW, OFFER, REJECTION, OPT_OUT, AUTO_REPLY,
           CANT_HELP, OTHER)
# Not a reply class: the event "your follow-up was sent".
FOLLOWUP_SENT = "followup_sent"


@dataclass(frozen=True)
class Message:
    """One Gmail message, reduced to what tracking needs."""

    id: str
    thread_id: str
    when: datetime
    sender: str  # the address only, lowercase
    subject: str = ""
    snippet: str = ""
    text: str = ""
    labels: tuple[str, ...] = ()
    headers: dict[str, str] = field(default_factory=dict)  # lowercase names

    @property
    def sender_domain(self) -> str:
        return self.sender.rpartition("@")[2]

    @property
    def message_id(self) -> str:
        return self.headers.get("message-id", "")

    @property
    def sent(self) -> bool:
        return "SENT" in self.labels


@dataclass(frozen=True)
class Classification:
    kind: str
    confidence: float
    quote: str = ""
    source: str = "llm"  # llm | header | phrase
