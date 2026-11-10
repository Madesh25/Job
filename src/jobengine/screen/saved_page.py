"""A job page you saved in your browser and sent to the bot (10 Nov, your request).

LinkedIn, Pracuj.pl and other sites refuse the bot, so their jobs wait for /jd. Copying the
text works but is slow; now you can save the page instead (Ctrl+S in the browser while the
job is open) and send the file to the bot in Telegram. The bot never opens the site: it only
reads the file you saved, which holds what you saw in your own browser.

Accepted files:
- "Webpage, Single File" (.mhtml or .mht, Chrome and Edge): best, one file;
- "Webpage, Complete" or "HTML only" (.html or .htm): send the .html file only.

The job is the one of the open /jd (after /jd <link> or in /linkedin), else the one whose
link the saved page names ("saved from url", its canonical link or the MHTML address).
The description is the job's own part of the page when the site is known (LinkedIn's "About
the job"), else the description the page gives as JobPosting data, else its longest text
block (the same reader as the job pages of /fetch).

Pure parts here; the desk (screen/desk.py) finds the job, saves and screens it.
"""

from __future__ import annotations

import email
import email.policy
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from jobengine import textblocks
from jobengine.sweep import fulltext

FILE_TYPES = (".html", ".htm", ".mhtml", ".mht")
MAX_BYTES = 20 * 1024 * 1024  # Telegram's limit for files a bot downloads
# The job description part of known sites, first match wins (LinkedIn's job view when you
# are signed in, then its public page).
DESCRIPTION_PARTS = (
    "div.jobs-description__content",
    "div.jobs-description-content__text",
    "article.jobs-description__container",
    "div#job-details",
    "div.jobs-box__html-content",
    "div.show-more-less-html__markup",
    "section.description",
)
MIN_PART = 300  # a known part shorter than this is not the description (a teaser)
SAVED_FROM = re.compile(r"<!--\s*saved from url=\(\d+\)(\S+?)\s*-->", re.I)


@dataclass
class SavedPage:
    html: str
    url: str = ""  # the job's address when the file names it


def is_saved_page(name: str) -> bool:
    return name.lower().endswith(FILE_TYPES)


def _decode(data: bytes, charset: str | None = None) -> str:
    for encoding in (charset, "utf-8", "cp1252"):
        if not encoding:
            continue
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _from_mhtml(data: bytes) -> SavedPage:
    message = email.message_from_bytes(data, policy=email.policy.default)
    url = str(message.get("Snapshot-Content-Location") or "")
    for part in message.walk():
        if part.get_content_type() != "text/html":
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        html = _decode(payload, part.get_content_charset())
        return SavedPage(html, url or str(part.get("Content-Location") or ""))
    return SavedPage("", url)


def read(name: str, data: bytes) -> SavedPage:
    """The page inside the file and the job's address when it names one."""
    if name.lower().endswith((".mhtml", ".mht")) or data[:400].lstrip().startswith(
            (b"From:", b"MIME-Version", b"Snapshot-Content-Location")):
        page = _from_mhtml(data)
    else:
        page = SavedPage(_decode(data))
    if not page.url:
        page.url = page_url(page.html)
    return page


def page_url(html: str) -> str:
    """The address a saved HTML page names: "saved from url", canonical, og:url."""
    match = SAVED_FROM.search(html[:3000])
    if match:
        return match.group(1)
    soup = BeautifulSoup(html, "html.parser")
    link = soup.find("link", rel="canonical")
    if link is not None and str(link.get("href") or "").startswith("http"):
        return str(link.get("href"))
    meta = soup.find("meta", property="og:url")
    if meta is not None and str(meta.get("content") or "").startswith("http"):
        return str(meta.get("content"))
    return ""


def description(html: str) -> str:
    """The job description in a saved page ("" when none is found)."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "noscript", "svg"]):
        if tag.name == "script" and "ld+json" in str(tag.get("type") or ""):
            continue  # JobPosting data, read below
        tag.decompose()
    for selector in DESCRIPTION_PARTS:
        part = soup.select_one(selector)
        if part is None:
            continue
        text = textblocks.tidy(textblocks.text_of(part))
        if len(text) >= MIN_PART:
            return text
    return textblocks.tidy(fulltext.extract(str(soup)) or "")
