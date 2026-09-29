"""Checks before a mail is sent (mail mode "send"). Pure.

Every mail is checked twice: the message the bot built, and the draft exactly as Gmail holds
it (read back after the draft was created). Any problem keeps that mail as a draft; the
problems are shown to you. What is checked:

- To: exactly one address, well formed, and the expected one (the contact in prod, your
  redirect address elsewhere). No Cc, no Bcc, no Reply-To.
- Subject: present, the expected text, no placeholder left, no long dash, not too long, and
  the environment label outside prod.
- Body: a plain text and an HTML part, no placeholder left, no long dash, the signature.
- Attachment: exactly the approved resume of this job (same file name and same bytes), a
  real PDF, within the size limit; or none when resumes are not attached.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses

from jobengine.mail.compose import LONG_DASHES
from jobengine.mail.fill import PLACEHOLDER_RE

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")
MAX_SUBJECT = 200
MIN_BODY_CHARS = 80
BRACES = re.compile(r"\{\{|\}\}|\{[A-Za-z_ ]+\}")


@dataclass(frozen=True)
class Expected:
    to: str  # after safety.route_recipients
    subject: str
    signature_text: str
    attachment: tuple[str, bytes] | None  # the approved resume of this job
    max_kb: int
    env_label: str = ""  # "[DEV]" outside prod: the subject must start with it


def parse(raw: bytes) -> EmailMessage:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert isinstance(message, EmailMessage)
    return message


def _text_parts(message: EmailMessage) -> dict[str, str]:
    parts: dict[str, str] = {}
    for part in message.walk():
        if part.get_content_maintype() == "text" and not part.get_filename():
            parts.setdefault(part.get_content_subtype(), part.get_content())
    return parts


def _attachments(message: EmailMessage) -> list[tuple[str, str, bytes]]:
    out = []
    for part in message.walk():
        if part.get_filename() or part.get_content_disposition() == "attachment":
            data = part.get_payload(decode=True) or b""
            out.append((part.get_filename() or "", part.get_content_type(), data))
    return out


def _has_placeholder(text: str) -> bool:
    return bool(PLACEHOLDER_RE.search(text) or BRACES.search(text))


def check(message: EmailMessage, expected: Expected) -> list[str]:
    """Problems with this mail; empty when it may be sent."""
    problems: list[str] = []
    # Recipients
    to = [addr for _, addr in getaddresses(message.get_all("To") or [])]
    if len(to) != 1:
        problems.append(f"To has {len(to)} addresses, expected 1")
    elif not EMAIL_RE.match(to[0]):
        problems.append(f"To {to[0]!r} is not a valid address")
    elif to[0].casefold() != expected.to.casefold():
        problems.append(f"To is {to[0]}, expected {expected.to}")
    for header in ("Cc", "Bcc", "Reply-To"):
        if message.get(header):
            problems.append(f"{header} is set ({message.get(header)}), expected none")
    # Subject
    subject = str(message.get("Subject") or "").strip()
    if not subject:
        problems.append("the subject is empty")
    else:
        if subject != expected.subject.strip():
            problems.append(f"the subject is {subject!r}, expected {expected.subject!r}")
        if _has_placeholder(subject):
            problems.append("the subject has a placeholder left")
        if any(dash in subject for dash in LONG_DASHES):
            problems.append("the subject has a long dash")
        if len(subject) > MAX_SUBJECT:
            problems.append(f"the subject is {len(subject)} characters long")
        if expected.env_label and not subject.startswith(expected.env_label):
            problems.append(f"the subject does not start with {expected.env_label}")
    # Body
    parts = _text_parts(message)
    for kind in ("plain", "html"):
        text = parts.get(kind)
        if text is None:
            problems.append(f"the {kind} text part is missing")
            continue
        if _has_placeholder(text):
            problems.append(f"the {kind} body has a placeholder left")
        if any(dash in text for dash in LONG_DASHES):
            problems.append(f"the {kind} body has a long dash")
    plain = parts.get("plain", "")
    if plain and len(plain.strip()) < MIN_BODY_CHARS:
        problems.append(f"the body is only {len(plain.strip())} characters")
    first_line = expected.signature_text.strip().splitlines()[0] if \
        expected.signature_text.strip() else ""
    if plain and first_line and first_line not in plain:
        problems.append("the signature is missing")
    # Attachment
    found = _attachments(message)
    if expected.attachment is None:
        if found:
            problems.append(f"{len(found)} attachment(s), expected none")
        return problems
    name, data = expected.attachment
    if len(found) != 1:
        problems.append(f"{len(found)} attachments, expected 1 (the resume)")
        return problems
    got_name, got_type, got_data = found[0]
    if got_name != name:
        problems.append(f"the attachment is {got_name!r}, expected this job's resume {name!r}")
    if got_type != "application/pdf" or not got_data.startswith(b"%PDF-"):
        problems.append("the attachment is not a PDF")
    if got_data != data:
        problems.append("the attachment is not the approved resume of this job")
    size_kb = len(got_data) / 1024
    if not got_data:
        problems.append("the attachment is empty")
    elif size_kb > expected.max_kb:
        problems.append(f"the attachment is {size_kb:.0f} KB, over {expected.max_kb} KB")
    return problems


def check_raw(raw: bytes, expected: Expected) -> list[str]:
    try:
        message = parse(raw)
    except Exception as exc:  # a draft Gmail cannot give back readably is never sent
        return [f"Gmail's copy could not be read ({exc})"]
    return check(message, expected)
