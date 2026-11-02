"""Job descriptions as tidy paragraphs for the Notion page body (2 Nov, your check).

Pages showed three problems: HTML tags as text ("<br />" all through the dotLinkers and
Jobgether descriptions: their pages send the HTML escaped, so one reading leaves the tags),
words cut in two ("De" / "velop": the text was cut every 2000 characters), and whole
descriptions in one line (PwC: a job feed without line breaks). tidy() turns leftover HTML
into lines, with "• " for list items, and gives a one-line description breaks before its
section headings ("Requirements:", "Nice to have:"); chunks() cuts at line ends, and inside
a too long line at a word.
"""

from __future__ import annotations

import html
import re

from bs4 import BeautifulSoup

BLOCK_CHARS = 2000  # Notion's limit for one text item
MAX_BLOCKS = 90  # a page body gets at most 100 blocks in one request
# Tags left as text after one reading of an escaped description.
_TAGS = re.compile(r"</?(?:br|p|li|ul|ol|div|span|strong|b|em|i|u|h[1-6]|tr|td)\b[^>]*>", re.I)
_BLOCK_TAGS = ("p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr")
# Section headings a one-line description gets a line break before.
_HEADINGS = re.compile(
    r"(?<=[.!?:;)\]])\s+(?=(?:About (?:the role|the job|the team|the project|us|you|our client)"
    r"|Your (?:future role|role|tasks|responsibilities|profile|team)|Responsibilities|Requirements"
    r"|Qualifications|Must have|Nice to have|Apply if you|What you(?:'ll| will)? (?:do|bring|get)"
    r"|What we offer|We offer|Benefits|Who you are|Tech stack|Technologies|Skills"
    r"|Recruitment process|By joining us|Job Description)\b)", re.I)
LONG_LINE = 1200  # a description in one line longer than this gets the heading breaks


def tidy(text: str | None) -> str:
    """Plain lines without HTML; one line per paragraph or list item ("• ")."""
    value = (text or "").strip()
    if not value:
        return ""
    if _TAGS.search(value) or "&lt;" in value:
        value = _from_html(html.unescape(value) if "&lt;" in value else value)
    lines = [re.sub(r"\s+", " ", line).strip() for line in value.splitlines()]
    out: list[str] = []
    for line in lines:
        if line and (not out or out[-1] != line):
            out.append(line)
    if len(out) == 1 and len(out[0]) > LONG_LINE:
        out = [part.strip() for part in _HEADINGS.split(out[0]) if part.strip()]
    return "\n".join(out)


def _from_html(value: str) -> str:
    soup = BeautifulSoup(value, "html.parser")
    for tag in soup.find_all("br"):
        tag.replace_with("\n")
    for tag in soup.find_all("li"):
        tag.insert(0, "• ")
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    return soup.get_text("")


def chunks(text: str, size: int = BLOCK_CHARS, limit: int = MAX_BLOCKS) -> list[str]:
    """Paragraph texts of at most `size` characters, cut at line ends (lines are kept
    together while they fit) and inside a longer line at a space, never inside a word."""
    out: list[str] = []
    current = ""
    for line in _pieces(text.strip(), size):
        if current and len(current) + 1 + len(line) > size:
            out.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        out.append(current)
    return out[:limit]


def _pieces(text: str, size: int) -> list[str]:
    """The lines of `text`; a line longer than `size` split at the last space before it."""
    pieces: list[str] = []
    for line in text.splitlines():
        while len(line) > size:
            cut = line.rfind(" ", 0, size)
            cut = cut if cut > size // 2 else size
            pieces.append(line[:cut].rstrip())
            line = line[cut:].lstrip()
        pieces.append(line)
    return pieces
