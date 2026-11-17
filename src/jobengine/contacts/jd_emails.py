"""Email addresses written in the job description (spec section 2, step 3). Pure.

A named person (a name on the same line or the line before) becomes a Recruiter/TA contact;
a generic mailbox (careers@, jobs@...) is kept with the mailbox as its name and Type Other.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from jobengine.contacts.classify import PERSONAL_DOMAINS, is_personal

EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}")
_UPPER = "A-Z\u00c0-\u00de\u0141\u015a\u0179\u017b"
_LOWER = "a-z\u00df-\u00ff\u0142\u015b\u017a\u017c\u0105\u0119\u0107\u0144'"
_WORD = f"[{_UPPER}][{_LOWER}]+(?:-[{_UPPER}][{_LOWER}]+)?"
NAME_RE = re.compile(rf"\b({_WORD}(?:\s+{_WORD}){{1,2}})")
NOT_NAME_WORDS = {
    "contact", "email", "e-mail", "mail", "write", "send", "please", "questions", "question",
    "recruiter", "recruitment", "apply", "our", "your", "reach", "the", "for", "and", "cv",
    "talent", "acquisition", "hr", "team", "partner", "via", "to", "at", "or", "us",
}
GENERIC_MAILBOXES = ("careers", "jobs", "hr", "recruitment", "talent", "praca", "rekrutacja")


@dataclass(frozen=True)
class JdEmail:
    email: str
    name: str | None  # the person's name, or the mailbox for a generic address
    generic: bool


def _name_in(text: str) -> str | None:
    for match in reversed(list(NAME_RE.finditer(text))):
        words = [w for w in match.group(1).split() if w.casefold() not in NOT_NAME_WORDS]
        if len(words) >= 2:
            return " ".join(words[-3:])
    return None


def extract(
    text: str,
    personal: Iterable[str] = PERSONAL_DOMAINS,
    generic_mailboxes: Iterable[str] = GENERIC_MAILBOXES,
) -> list[JdEmail]:
    generic = {g.casefold() for g in generic_mailboxes}
    lines = (text or "").splitlines()
    out: list[JdEmail] = []
    seen: set[str] = set()
    for i, line in enumerate(lines):
        for match in EMAIL_IN_TEXT.finditer(line):
            email = match.group(0).strip(".")
            key = email.lower()
            if key in seen or is_personal(email, personal):
                continue
            seen.add(key)
            local = key.split("@", 1)[0]
            if local in generic:
                out.append(JdEmail(email=email, name=email, generic=True))
                continue
            name = _name_in(line[:match.start()]) or (_name_in(lines[i - 1]) if i else None)
            if name:
                out.append(JdEmail(email=email, name=name, generic=False))
    return out
