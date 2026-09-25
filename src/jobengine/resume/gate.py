"""The integrity gate (Build Spec section 1, V16 section 6). Pure, no LLM.

`check` returns error strings; an empty list means the plan may be rendered. Only two things
may change: the Technical Skills tables and a few words inside existing Experience bullets.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from jobengine.reference import Reference
from jobengine.resume.master import LONG_DASHES, frozen_diff, render_html
from jobengine.resume.models import MasterResume, Plan, PlanRow, SkillRow
from jobengine.resume.sections import SectionPlan
from jobengine.sweep.normalize import canon

MIN_MAIN_ROWS = 5
MIN_ALSO_ROWS = 1
MAX_WORDS_PER_BULLET = 8
MAX_WORDS_TOTAL = 25
MAX_TERM_WORDS = 4
CONNECTOR_WORDS = frozenset(
    "and with using for on in the a an via to of across including through into by its "
    "their".split()
)
NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
STRIP_CHARS = ".,;:()[]\"'!?"


def _key(text: str) -> str:
    return " ".join(text.split()).casefold()


def _clean(word: str) -> str:
    return word.strip(STRIP_CHARS)


# ---------------------------------------------------------------- word differences


@dataclass(frozen=True)
class Chunk:
    """A contiguous insert or replace inside one bullet."""

    words: tuple[str, ...]


@dataclass
class BulletDiff:
    bullet_id: str
    inserted: int
    chunks: list[Chunk] = field(default_factory=list)


def word_diff(bullet_id: str, original: str, new: str) -> BulletDiff:
    """Inserted words (replacements count as inserts of the replacement)."""
    a, b = original.split(), new.split()
    # Match on words without punctuation, so "AWS," and "AWS" are the same word.
    keys_a = [_clean(w).casefold() or w for w in a]
    keys_b = [_clean(w).casefold() or w for w in b]
    diff = BulletDiff(bullet_id=bullet_id, inserted=0)
    matcher = difflib.SequenceMatcher(None, keys_a, keys_b, autojunk=False)
    for op, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if op in ("insert", "replace"):
            diff.inserted += j2 - j1
            diff.chunks.append(Chunk(words=tuple(b[j1:j2])))
    return diff


def _in_text(phrase: str, text: str) -> bool:
    key = canon(phrase)
    return bool(key) and f" {key} " in f" {canon(text)} "


def chunk_errors(
    bullet_id: str, chunk: Chunk, original: str, reference: Reference, connectors: frozenset[str],
) -> list[str]:
    """Every inserted chunk needs a backed term (or one already in the bullet); every other
    word must be a connector."""
    words = [w for w in (_clean(w) for w in chunk.words) if w]
    has_term = False
    i = 0
    while i < len(words):
        for n in range(min(MAX_TERM_WORDS, len(words) - i), 0, -1):
            phrase = " ".join(words[i:i + n])
            if reference.lookup(phrase) or _in_text(phrase, original):
                has_term = True
                i += n
                break
        else:
            if words[i].casefold() not in connectors:
                return [f"{bullet_id}: inserted word {words[i]!r} is not a backed term or a "
                        "connector word"]
            i += 1
    if not has_term:
        return [f"{bullet_id}: inserted {' '.join(chunk.words)!r} has no backed term"]
    return []


# ---------------------------------------------------------------- skills


def _identify(row: PlanRow, originals: list[SkillRow]) -> SkillRow | None:
    """The master row a plan row comes from: it keeps at least half of its original items."""
    items = {_key(i) for i in row.items}
    best: tuple[int, SkillRow] | None = None
    for original in originals:
        kept = sum(1 for i in original.items if _key(i) in items)
        if original.items and 2 * kept >= len(original.items) and kept > 0:
            if best is None or kept > best[0]:
                best = (kept, original)
    return best[1] if best else None


def _label_ok(label: str, original: SkillRow, reference: Reference) -> bool:
    if original.label.casefold() in label.casefold():
        return True
    term = reference.term_row(label)
    return bool(term and term.backs and canon(term.truthful_equivalent) == canon(original.label))


def skill_errors(master: MasterResume, plan: Plan, reference: Reference) -> list[str]:
    errors: list[str] = []
    if len(plan.skills_main) < MIN_MAIN_ROWS:
        errors.append(f"skills: main table has {len(plan.skills_main)} rows, at least "
                      f"{MIN_MAIN_ROWS} are required")
    if len(plan.skills_also) < MIN_ALSO_ROWS:
        errors.append("skills: the 'Also worked with' table needs at least 1 row")

    originals = master.skills_main + master.skills_also
    original_labels = {r.label.casefold() for r in originals}
    master_items = {_key(i) for i in master.all_items()}
    # An item may appear as often as in the master (some masters list CloudWatch twice), and
    # a new item once.
    allowed = Counter(_key(i) for i in master.all_items())
    counts = Counter(_key(i) for r in plan.skills_main + plan.skills_also for i in r.items)
    for key, count in counts.items():
        if count > max(allowed.get(key, 0), 1):
            item = next(i for r in plan.skills_main + plan.skills_also for i in r.items
                        if _key(i) == key)
            errors.append(f"skills: {item!r} appears twice")
    for row in plan.skills_main + plan.skills_also:
        if not row.items:
            errors.append(f"skills: row {row.label!r} has no items")
        if row.label.casefold() not in original_labels:
            original = _identify(row, originals)
            if original is None:
                errors.append(f"skills: label {row.label!r} is new and keeps too few items of "
                              "any original row")
            elif not _label_ok(row.label, original, reference):
                errors.append(f"skills: label {row.label!r} is not an allowed rename of "
                              f"{original.label!r}")
        for item in row.items:
            key = _key(item)
            if key in master_items:
                continue
            if reference.is_known_gap(item):
                errors.append(f"skills: {item!r} is a known gap in Term Map ((none))")
            elif reference.skill_level(item) == "Learning" and not reference.lookup(item):
                errors.append(f"skills: {item!r} is only a Learning skill")
            elif not reference.lookup(item):
                errors.append(f"skills: {item!r} is not backed by Skills Inventory "
                              "(Production or Hands-on) or an Active Term Map row")
    return errors


# ---------------------------------------------------------------- bullets


def bullet_errors(
    master: MasterResume,
    plan: Plan,
    reference: Reference,
    connectors: Iterable[str] = CONNECTOR_WORDS,
    max_per_bullet: int = MAX_WORDS_PER_BULLET,
    max_total: int = MAX_WORDS_TOTAL,
) -> list[str]:
    errors: list[str] = []
    connector_set = frozenset(w.casefold() for w in connectors)
    total = 0
    seen: set[str] = set()
    for edit in plan.bullet_edits:
        bullet = master.bullet(edit.id)
        if bullet is None:
            errors.append(f"{edit.id}: no such bullet")
            continue
        if edit.id in seen:
            errors.append(f"{edit.id}: edited twice")
        seen.add(edit.id)
        diff = word_diff(edit.id, bullet.text, edit.text)
        total += diff.inserted
        if diff.inserted > max_per_bullet:
            errors.append(f"{edit.id}: +{diff.inserted} words, at most {max_per_bullet} per bullet")
        for chunk in diff.chunks:
            errors.extend(chunk_errors(edit.id, chunk, bullet.text, reference, connector_set))
        if Counter(NUMBER_RE.findall(bullet.text)) != Counter(NUMBER_RE.findall(edit.text)):
            errors.append(f"{edit.id}: a number changed (metrics must stay as in the master)")
    if total > max_total:
        errors.append(f"experience: +{total} words in total, at most {max_total}")
    return errors


# ---------------------------------------------------------------- whole document


def check(
    master: MasterResume,
    plan: Plan,
    reference: Reference,
    *,
    sections: SectionPlan | None = None,
    connectors: Iterable[str] = CONNECTOR_WORDS,
    max_per_bullet: int = MAX_WORDS_PER_BULLET,
    max_total: int = MAX_WORDS_TOTAL,
) -> list[str]:
    """All integrity errors for a plan; empty means pass."""
    errors = skill_errors(master, plan, reference)
    errors += bullet_errors(master, plan, reference, connectors, max_per_bullet, max_total)
    texts = [r.label for r in plan.skills_main + plan.skills_also]
    texts += [i for r in plan.skills_main + plan.skills_also for i in r.items]
    texts += [e.text for e in plan.bullet_edits]
    if any(ch in t for t in texts for ch in LONG_DASHES):
        errors.append("an em dash or en dash is not allowed")
    if errors:
        return errors
    html = render_html(master, plan, sections)
    expected = render_html(master, Plan.unchanged(master), sections)
    errors += frozen_diff(expected, html)
    if any(ch in html for ch in LONG_DASHES):
        errors.append("the rendered HTML contains an em dash or en dash")
    return errors


# ---------------------------------------------------------------- change summary


@dataclass
class Changes:
    words: int = 0
    bullets: int = 0
    skills: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)

    @property
    def diff_score(self) -> str:
        return f"+{self.words} words in {self.bullets} bullets, {len(self.skills)} skill changes"


def summarise(master: MasterResume, plan: Plan) -> Changes:
    """What changed against the master, for Resume Log and the Telegram caption."""
    out = Changes()
    originals = master.skills_main + master.skills_also
    plan_rows = plan.skills_main + plan.skills_also
    plan_items = {_key(i): i for r in plan_rows for i in r.items}
    master_items = {_key(i): i for i in master.all_items()}
    for key, item in master_items.items():
        if key not in plan_items:
            out.skills.append(f"-{item}")
    for key, item in plan_items.items():
        if key not in master_items:
            out.skills.append(f"+{item}")
    for row in plan_rows:
        if row.label.casefold() not in {o.label.casefold() for o in originals}:
            original = _identify(row, originals)
            if original:
                out.skills.append(f"{original.label} -> {row.label}")
    main_master = {_key(i) for r in master.skills_main for i in r.items}
    main_plan = {_key(i) for r in plan.skills_main for i in r.items}
    for key, item in plan_items.items():
        if key in master_items and (key in main_master) != (key in main_plan):
            where = "main table" if key in main_plan else "Also worked with"
            out.skills.append(f"{item} to {where}")
    out.lines = [f"skills: {change}" for change in out.skills]
    for edit in plan.bullet_edits:
        bullet = master.bullet(edit.id)
        if bullet is None:
            continue
        diff = word_diff(edit.id, bullet.text, edit.text)
        if diff.inserted or edit.text != bullet.text:
            out.bullets += 1
            out.words += diff.inserted
            added = " ".join(" ".join(c.words) for c in diff.chunks)
            out.lines.append(f"{edit.id}: +{diff.inserted} words \"{added}\"")
    return out
