"""Tip validation (spec section 2 steps 4 and 5). Pure.

- Sources: only URLs that appeared in the web search results count; a tip without one is
  dropped.
- Known myths (config strategy.reject_patterns) and tips that conflict with V16 are kept as
  Rejected, with the reason.
- Duplicates of existing Strategy rows (or of an earlier tip in the batch) are dropped.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

from rapidfuzz import fuzz

from jobengine.strategy.models import Checked, Tip

DUPLICATE_RATIO = 85
CONFLICT_REASON = "conflicts with V16"


def norm_url(url: str) -> str:
    """Scheme, host (without www.) and path, lowercase, without a trailing slash, query or
    fragment, so the same page matches however it was written."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    host = parts.hostname.lower().removeprefix("www.")
    return f"{host}{parts.path.rstrip('/')}".lower()


def verified_sources(tip: Tip, search_urls: Iterable[str]) -> list[str]:
    """The tip's sources that were in the search results, as the search returned them."""
    known = {norm_url(u): u for u in search_urls if norm_url(u)}
    return [known[norm_url(u)] for u in tip.sources if norm_url(u) in known]


def matches_pattern(text: str, pattern: str) -> bool:
    """Case-insensitive substring, or a regular expression when it is one."""
    if pattern.casefold() in text.casefold():
        return True
    try:
        return re.search(pattern, text, re.IGNORECASE) is not None
    except re.error:
        return False


def reject_reason(tip: Tip, patterns: Iterable[str]) -> str | None:
    text = f"{tip.tip} {tip.why}"
    hit = next((p for p in patterns if p and matches_pattern(text, p)), None)
    if hit:
        return hit
    return CONFLICT_REASON if tip.conflicts_with_v16 else None


def _key(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())


def is_duplicate(text: str, existing: Iterable[str], ratio: int = DUPLICATE_RATIO) -> bool:
    key = _key(text)
    return any(fuzz.ratio(key, _key(other)) >= ratio for other in existing if other)


def parse_tips(raw: dict[str, Any], max_tips: int) -> list[Tip]:
    items = raw.get("tips") if isinstance(raw, dict) else None
    tips = [t for t in (Tip.from_raw(item) for item in items or []) if t is not None]
    return tips[:max_tips]


def check(tips: list[Tip], search_urls: Iterable[str], patterns: Iterable[str],
          existing: Iterable[str]) -> Checked:
    """Sources first (no source: dropped), then duplicates (dropped), then myths and V16
    conflicts (kept as Rejected)."""
    urls = list(search_urls)
    patterns = list(patterns)
    seen = [t for t in existing if t]
    out = Checked()
    for tip in tips:
        sources = verified_sources(tip, urls)
        if not sources:
            out.no_source += 1
            continue
        tip.sources = sources
        if is_duplicate(tip.tip, seen):
            out.duplicates += 1
            continue
        seen.append(tip.tip)
        reason = reject_reason(tip, patterns)
        if reason:
            out.rejected.append((tip, reason))
        else:
            out.kept.append(tip)
    return out
