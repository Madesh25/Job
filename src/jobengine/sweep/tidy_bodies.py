"""Old job descriptions tidied during /fetch (2 Nov, your idea), DEV only.

Pages written before textblocks.py (PR #223) show HTML tags as text, words cut in two and
descriptions in one line. Each /fetch reads up to `max_pages` pages not checked yet, newest
first, and rewrites each description that tidy() would write differently, in the same
place on the page: the new text right after its "Description source:" line, then the old
blocks removed. Screening notes, links and the other lines stay as they are. A page is
checked once (Bot State `sweep.tidy_bodies`), so the pass ends when all old pages are done.
A better tidy() (VERSION) checks every page again: version 2 mends sentences cut over
many lines (Accenture, 2 Nov).
It runs only with `sweep.tidy_bodies.enabled` (local and dev): at go-live every page is
written tidy from the start, so prod never needs it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jobengine import textblocks
from jobengine.notion_repo import JobsRepo
from jobengine.sweep.crossmatch import DESCRIPTION_HEADER
from jobengine.sweep.dedupe import DESCRIPTION_BLOCK_CHARS, DESCRIPTION_MAX_BLOCKS

log = logging.getLogger("jobengine.sweep")

KEY = "sweep.tidy_bodies"  # bot_state: {"version": n, "checked": [page ids without dashes]}
VERSION = 3  # pages checked by an older tidy() are checked again (3: lone bullets, 2 Nov)
DEFAULT_MAX_PAGES = 60
PROGRESS_EVERY = 20  # a progress line every this many pages
# Lines that end a description: the next section of the page.
SECTION_STARTS = (DESCRIPTION_HEADER, "Screening (", "Earlier link", "Also posted at:",
                  "Adzuna link:", "Company site check")


@dataclass
class Result:
    checked: int = 0
    rewritten: int = 0
    left: int = 0  # pages still to check at the next /fetch


def settings(s: Any) -> dict[str, Any]:
    return dict(s.sweep.get("tidy_bodies") or {})


def enabled(s: Any) -> bool:
    return bool(settings(s).get("enabled", False))


def original(parts: list[str]) -> str:
    """The description as it was written: old pages cut it every 2000 characters (join
    without a separator), new pages at line ends (join with a line break)."""
    if len(parts) > 1 and all(len(p) == DESCRIPTION_BLOCK_CHARS for p in parts[:-1]):
        return "".join(parts)
    return "\n".join(parts)


def sections(blocks: list[tuple[str, str]]) -> list[tuple[str, list[str], list[str]]]:
    """(header block id, description block ids, their texts) for each description."""
    out = []
    i = 0
    while i < len(blocks):
        block_id, text = blocks[i]
        if not text.startswith(DESCRIPTION_HEADER):
            i += 1
            continue
        ids, texts = [], []
        j = i + 1
        while j < len(blocks) and not blocks[j][1].startswith(SECTION_STARTS):
            ids.append(blocks[j][0])
            texts.append(blocks[j][1])
            j += 1
        if ids:
            out.append((block_id, ids, texts))
        i = j
    return out


def tidy_page(repo: JobsRepo, page_id: str) -> bool:
    """Rewrite the page's descriptions that are not tidy. True when one was rewritten."""
    changed = False
    for header_id, ids, texts in sections(repo.body_blocks(page_id)):
        new = textblocks.chunks(textblocks.tidy(original(texts)), DESCRIPTION_BLOCK_CHARS,
                                DESCRIPTION_MAX_BLOCKS - 1)
        if new and new != texts:
            repo.replace_blocks(page_id, header_id, ids, new)
            changed = True
    return changed


def _short(page_id: str) -> str:
    return page_id.replace("-", "").lower()


def run(s: Any, repo: JobsRepo, state: Any,
        progress: Callable[[str], None] | None = None) -> Result:
    result = Result()
    max_pages = int(settings(s).get("max_pages", DEFAULT_MAX_PAGES))
    saved = (state.get(KEY) if state is not None else None) or {}
    same = saved.get("version", 1) == VERSION
    checked = set(saved.get("checked") or []) if same else set()
    rows = repo.query_rows(None)
    rows.sort(key=lambda r: str(r[1].get("Swept date") or ""), reverse=True)
    todo = [pid for pid, _ in rows if _short(pid) not in checked]
    for page_id in todo[:max_pages]:
        if progress is not None and result.checked % PROGRESS_EVERY == 0:
            progress(f"Tidying old descriptions: {result.checked + 1} of "
                     f"{min(len(todo), max_pages)}")
        try:
            if tidy_page(repo, page_id):
                result.rewritten += 1
        except Exception:  # one page never stops the others
            log.exception("description of %s not tidied", page_id)
            continue
        checked.add(_short(page_id))
        result.checked += 1
    result.left = len(todo) - result.checked
    if state is not None:
        state.set(KEY, {"version": VERSION, "checked": sorted(checked)})
    log.info("old descriptions: %d pages checked, %d rewritten, %d still to check",
             result.checked, result.rewritten, result.left)
    return result
