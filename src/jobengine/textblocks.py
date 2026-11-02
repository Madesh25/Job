"""Job descriptions as tidy paragraphs for the Notion page body (2 Nov, your check).

Pages showed three problems: HTML tags as text ("<br />" all through the dotLinkers and
Jobgether descriptions: their pages send the HTML escaped, so one reading leaves the tags),
words cut in two ("De" / "velop": the text was cut every 2000 characters), and whole
descriptions in one line (PwC: a job feed without line breaks). tidy() turns leftover HTML
into lines, with "• " for list items, and gives a one-line description breaks before its
section headings ("Requirements:", "Nice to have:"); chunks() cuts at line ends, and inside
a too long line at a word.

Words and sentences cut over many lines (2 Nov, Accenture and other Workday pages: "I" /
"mplement", "At Accenture the Netherlands" / ", we're seeking" / "DevOps"): the job text was
read with a line break after every HTML piece, also inside a sentence (<span>, <b>, <a>).
text_of() reads HTML the way a browser shows it: a new line only for <br>, paragraphs, list
items, headings and table rows. Descriptions saved before that are mended by tidy(): a text
with such cut pieces gets them joined back into their sentences.
"""

from __future__ import annotations

import html
import re

from bs4 import BeautifulSoup, CData, NavigableString, Tag

BLOCK_CHARS = 2000  # Notion's limit for one text item
MAX_BLOCKS = 90  # a page body gets at most 100 blocks in one request
# Tags left as text after one reading of an escaped description.
_TAGS = re.compile(r"</?(?:br|p|li|ul|ol|div|span|strong|b|em|i|u|h[1-6]|tr|td)\b[^>]*>", re.I)
# Section headings a one-line description gets a line break before.
_HEADINGS = re.compile(
    r"(?<=[.!?:;)\]])\s+(?=(?:About (?:the role|the job|the team|the project|us|you|our client)"
    r"|Your (?:future role|role|tasks|responsibilities|profile|team)|Responsibilities|Requirements"
    r"|Qualifications|Must have|Nice to have|Apply if you|What you(?:'ll| will)? (?:do|bring|get)"
    r"|What we offer|We offer|Benefits|Who you are|Tech stack|Technologies|Skills"
    r"|Recruitment process|By joining us|Job Description)\b)", re.I)
LONG_LINE = 1200  # a description in one line longer than this gets the heading breaks
# Tags that start a new line; every other tag (<span>, <b>, <a>, ...) is part of its line.
_LINE_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "dd", "div", "dl", "dt", "fieldset",
    "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header",
    "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table", "tbody", "td", "tfoot",
    "th", "thead", "tr", "ul"})
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "head", "title", "svg"})
_HAS_LINE_TAG = re.compile(r"<(?:br|p|div|li|ul|ol|h[1-6]|tr|table|section|article)\b", re.I)
_END = object()
# A text cut into pieces: a line that is only punctuation, a line that starts with it, and a
# lone letter before the rest of its word. This many make a text "cut" and mended.
_CUT_START = re.compile(r"^(?:[,.;:!?)%\u2014\u2013]|\+(?:\s|$))")
_CUT_SIGNS = 3
# Words a line never ends with when the sentence goes on in the next line.
_OPEN_ENDS = frozenset({
    "a", "an", "the", "of", "to", "for", "in", "on", "at", "by", "with", "from", "and", "or",
    "as", "into", "than", "like", "under", "across", "within", "via", "including", "our",
    "your", "their", "its", "is", "are", "be", "we", "you", "&"})
# "I" or "A" before one of these words is a word of its own, not a cut letter.
_SHORT_WORDS = frozenset({
    "a", "am", "an", "and", "are", "as", "at", "be", "can", "for", "good", "great", "have",
    "in", "is", "must", "new", "of", "on", "or", "plus", "strong", "team", "the", "to",
    "will", "with", "would"})


def tidy(text: str | None) -> str:
    """Plain lines without HTML; one line per paragraph or list item ("• ")."""
    value = (text or "").strip()
    if not value:
        return ""
    if _TAGS.search(value) or "&lt;" in value:
        value = text_of(html.unescape(value) if "&lt;" in value else value)
    lines = [re.sub(r"\s+", " ", line).strip() for line in value.splitlines()]
    out: list[str] = []
    for line in lines:
        if line and (not out or out[-1] != line):
            out.append(line)
    out = _bullets(out)
    if _cut_signs(out) >= _CUT_SIGNS:
        out = mend(out)
    if len(out) == 1 and len(out[0]) > LONG_LINE:
        out = [part.strip() for part in _HEADINGS.split(out[0]) if part.strip()]
    return "\n".join(out)


def text_of(value: str | Tag | None) -> str:
    """The text of HTML as a browser shows it: one line per paragraph, list item ("• "),
    heading or table row, and words inside a sentence (<span>, <b>, <a>) kept on their line.
    A string with no line tags at all keeps its own line breaks (plain text)."""
    if value is None:
        return ""
    if isinstance(value, str):
        keep = not _HAS_LINE_TAG.search(value)
        root: Tag = BeautifulSoup(value, "html.parser")
    else:
        keep, root = False, value
    parts: list[str] = []
    stack: list[object] = list(reversed(root.contents))
    while stack:  # no recursion: a deep page never reaches Python's limit
        item = stack.pop()
        if item is _END:
            parts.append("\n")
        elif isinstance(item, Tag):
            name = (item.name or "").lower()
            if name == "br":
                parts.append("\n")
            elif name not in _SKIP_TAGS:
                if name in _LINE_TAGS:
                    parts.append("\n")
                    stack.append(_END)
                if name == "li":
                    parts.append("• ")
                stack.extend(reversed(item.contents))
        elif type(item) in (NavigableString, CData):
            text = str(item)
            parts.append(text if keep else re.sub(r"\s+", " ", text))
    lines = (re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in "".join(parts).splitlines())
    return "\n".join(_bullets([line for line in lines if line]))


def _bullets(lines: list[str]) -> list[str]:
    """A bullet on a line of its own joined with its text: "•" then "Design ..." becomes
    "• Design ..." (2 Nov: Roche's list items hold a paragraph)."""
    out: list[str] = []
    for line in lines:
        if out and out[-1] in ("•", "-", "*", "\u2013") and not line.startswith(("•", "- ", "* ")):
            out[-1] = f"• {line}"
        else:
            out.append(line)
    return out


def _cut_signs(lines: list[str]) -> int:
    signs = 0
    for i, line in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if _CUT_START.match(line) or (
                len(line) == 1 and line.isalpha() and nxt[:1].islower()):
            signs += 1
    return signs


def _words_go_on(line: str) -> bool:
    """True when `line` stops in the middle of a sentence."""
    if line.endswith((",", "\u2014", "\u2013", "/", "&")):
        return True
    last = line.rsplit(" ", 1)[-1].lower()
    return last in _OPEN_ENDS


def _joined(prev: str, line: str, after: list[str]) -> str | None:
    """`prev` and `line` as one line when `line` goes on the sentence of `prev`."""
    if line.startswith("• "):
        return None
    if _CUT_START.match(line):
        return prev + line
    first = line[:1]
    if len(prev) == 1 and prev.isalpha() and first.islower():
        word = line.split(" ", 1)[0].lower()
        return f"{prev} {line}" if prev in "IA" and word in _SHORT_WORDS else prev + line
    if prev.endswith((".", "!", "?", ":", ";")):
        return None
    if first.islower() or _words_go_on(prev):
        return prev + line if prev.endswith(("\u2014", "\u2013")) else f"{prev} {line}"
    # "we're seeking" / "DevOps" / "Engineers" / "who bring ...": single capitalised words
    # that lead to the lower case rest of the sentence.
    if prev[-1:].islower() and _short_piece(line):
        for nxt in after[:3]:
            if nxt[:1].islower():
                return f"{prev} {line}"
            if not _short_piece(nxt):
                break
    return None


def _short_piece(line: str) -> bool:
    """One capitalised word, as "DevOps" or "Engineers" (a lone letter is a cut word)."""
    return " " not in line and len(line) > 1 and line[:1].isupper() and line[-1:].isalnum()


def mend(lines: list[str]) -> list[str]:
    """Lines of a text cut into pieces, with the pieces joined back into their sentences."""
    out: list[str] = []
    for i, line in enumerate(lines):
        joined = _joined(out[-1], line, lines[i + 1:]) if out else None
        if joined is None:
            out.append(line)
        else:
            out[-1] = joined
    return out


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
