"""Build the draft MIME (spec section 5). Pure.

The body is the filled template as HTML paragraphs plus the Config signature, with a
text/plain alternative made from the same text, in a multipart/mixed message with the resume
PDF. No From, Bcc, tracking or custom headers: Gmail sets From for the authorised account.
"""

from __future__ import annotations

import base64
import html as htmllib
import re
from dataclasses import dataclass
from email.message import EmailMessage

from bs4 import BeautifulSoup

from jobengine.config_store import ConfigStore
from jobengine.mail.fill import PLACEHOLDER_RE

LONG_DASHES = (chr(0x2014), chr(0x2013))
SIGNATURE_KEY = "mail.signature"
LINK_KEYS = ("profile.linkedin_url", "profile.website_url")
SIGNATURE_TAGS = {"a", "br"}
PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")


class ComposeError(Exception):
    """The draft cannot be built within the rules. That draft is aborted."""


@dataclass(frozen=True)
class Signature:
    html: str
    text: str


@dataclass
class Mail:
    to: str
    subject: str
    body: str  # the filled template text
    signature: Signature
    attachment: tuple[str, bytes] | None
    html: str
    text: str
    message: EmailMessage

    def raw(self) -> str:
        return base64.urlsafe_b64encode(self.message.as_bytes()).decode("ascii")

    def preview(self) -> str:
        """Plain text for Telegram and --no-write: subject, body, signature, attachment name."""
        name = self.attachment[0] if self.attachment else "none"
        return (f"To: {self.to}\nSubject: {self.subject}\n\n{self.text}\n\n"
                f"Attachment: {name}")


# ---------------------------------------------------------------- body


def body_html(text: str) -> str:
    """Blank lines split paragraphs; single newlines become <br>. Text is escaped."""
    paragraphs = [p.strip("\n") for p in PARAGRAPH_SPLIT.split(text.strip()) if p.strip()]
    return "\n".join("<p>" + "<br>".join(htmllib.escape(line) for line in p.split("\n"))
                     + "</p>" for p in paragraphs)


# ---------------------------------------------------------------- signature


def _profile_value(config: ConfigStore, key: str) -> str:
    value = config.get(key)
    if not value:
        raise ComposeError(f"the signature needs Config {key}, which is missing")
    return value


def signature(config: ConfigStore, raw: str | None = None) -> Signature:
    """Config mail.signature with {profile.*} filled from Config. Every href must be exactly
    profile.linkedin_url or profile.website_url; the phone is never a link."""
    raw = raw if raw is not None else config.get(SIGNATURE_KEY)
    if not raw:
        raise ComposeError(f"Config {SIGNATURE_KEY} is missing")

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if not key.startswith("profile."):
            raise ComposeError(f"the signature has an unknown placeholder {match.group(0)}")
        return htmllib.escape(_profile_value(config, key))

    filled = PLACEHOLDER_RE.sub(replace, raw.replace("\r\n", "\n").strip())
    allowed = {config.get(k) for k in LINK_KEYS} - {None}
    soup = BeautifulSoup(filled, "html.parser")
    for tag in soup.find_all(True):
        if tag.name not in SIGNATURE_TAGS:
            raise ComposeError(f"the signature has a <{tag.name}> tag; only links are allowed")
        if tag.name == "a" and tag.get("href") not in allowed:
            raise ComposeError(f"the signature links to {tag.get('href')!r}, which is not "
                               "profile.linkedin_url or profile.website_url")
    phone = config.get("profile.phone")
    if "tel:" in filled.casefold() or (phone and any(phone in a.get_text()
                                                     for a in soup.find_all("a"))):
        raise ComposeError("the phone number must be plain text, never a link")
    html_lines = filled.split("\n")
    return Signature(html="<p>" + "<br>".join(html_lines) + "</p>",
                     text="\n".join(_plain_line(line) for line in html_lines))


def _plain_line(line: str) -> str:
    """One signature line as plain text: links become their URL."""
    soup = BeautifulSoup(line, "html.parser")
    for a in soup.find_all("a"):
        href, label = a.get("href") or "", a.get_text()
        a.replace_with(href if label.casefold() in href.casefold() else f"{label} {href}")
    return soup.get_text()


# ---------------------------------------------------------------- message


def check_dashes(message: EmailMessage) -> None:
    """No em or en dash anywhere in the final message text (subject, body, signature)."""
    texts = [str(message.get("Subject", ""))]
    for part in message.walk():
        if part.get_content_maintype() == "text":
            texts.append(part.get_content())
    if any(dash in text for text in texts for dash in LONG_DASHES):
        raise ComposeError("the mail contains an em dash or en dash")


def check_attachment(data: bytes, max_kb: int) -> None:
    size_kb = len(data) / 1024
    if size_kb > max_kb:
        raise ComposeError(f"the resume PDF is {size_kb:.0f} KB, over the {max_kb} KB limit "
                           "(Config mail.max_attachment_kb)")


def compose(to: str, subject: str, body: str, sig: Signature,
            attachment: tuple[str, bytes] | None, max_kb: int = 250) -> Mail:
    if attachment is not None:
        check_attachment(attachment[1], max_kb)
    html = (f'<div dir="ltr">\n{body_html(body)}\n{sig.html}\n</div>')
    text = f"{body.strip()}\n\n{sig.text}"
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    message.set_content(text, charset="utf-8")
    message.add_alternative(html, subtype="html", charset="utf-8")
    if attachment is not None:
        message.add_attachment(attachment[1], maintype="application", subtype="pdf",
                               filename=attachment[0])
    else:
        message.make_mixed()
    check_dashes(message)
    return Mail(to=to, subject=subject, body=body, signature=sig, attachment=attachment,
                html=html, text=text, message=message)
