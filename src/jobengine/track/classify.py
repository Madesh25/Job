"""Reply classification (spec section 4): deterministic pre-checks, then the LLM (stage
`score`). The LLM is advisory: low confidence goes to Telegram."""

from __future__ import annotations

import re

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
