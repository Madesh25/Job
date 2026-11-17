"""Render to PDF, measure, and fit (Build Spec sections 6 and 8).

The fit fallback never touches fonts, margins or spacing and never drops a bullet: on overflow
it reverts bullet edits (least important first) and then removes low-relevance skill items; on
underfill it restores removed skill rows and items.
"""

from __future__ import annotations

import io
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from jobengine.resume.gate import identify_row, item_key
from jobengine.resume.master import LONG_DASHES, render_html
from jobengine.resume.models import MasterResume, Measure, Plan, PlanRow, Profile
from jobengine.resume.sections import SectionPlan

log = logging.getLogger("jobengine.resume.render")

FILL_MIN = 88.0
FILL_MAX = 96.0
FONT = "Lato"
NO_WEASYPRINT = (
    "WeasyPrint cannot run here (it needs Pango). Render in WSL (Ubuntu) or Docker with "
    "fonts-lato, see docs/resume-builder.md."
)
NO_LATO = "The Lato font is not installed (fonts-lato). Refusing to render in a fallback font."
DEFAULT_FILENAME = "Madeshwaran_Devops_<Company>.pdf"
MAX_ROLE_CHARS = 40

Measurer = Callable[[str], Measure]


class RenderError(Exception):
    """The PDF cannot be rendered or does not pass the checks."""


# ---------------------------------------------------------------- render and measure


def render_pdf(html: str) -> bytes:
    try:
        import weasyprint
    except (OSError, ImportError) as exc:  # Pango or the package itself is missing
        raise RenderError(f"{NO_WEASYPRINT} ({type(exc).__name__})") from None
    return weasyprint.HTML(string=html).write_pdf()


def _font(name: str) -> str:
    return name.split("+", 1)[-1]  # drop the subset prefix, "ABCDEF+Lato-Bold" -> "Lato-Bold"


def measure_pdf(pdf: bytes) -> Measure:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        pages = len(doc.pages)
        last = doc.pages[-1]
        words = last.extract_words()
        bottom = max((w["bottom"] for w in words), default=0.0)
        fill = 100.0 * (pages - 1) + 100.0 * bottom / float(last.height)
        text = "\n".join(page.extract_text() or "" for page in doc.pages)
        links = [h.get("uri") for page in doc.pages for h in page.hyperlinks if h.get("uri")]
        fonts = {_font(c.get("fontname", "")) for page in doc.pages for c in page.chars}
    return Measure(pages=pages, fill=round(fill, 1), text=text, links=links, fonts=fonts)


def measure_html(html: str) -> Measure:
    return measure_pdf(render_pdf(html))


def check_measure(m: Measure, profile: Profile, fill_min: float, fill_max: float) -> list[str]:
    """Build Spec section 8: one page, fill in range, no long dashes, links from Config."""
    errors = []
    if m.fonts and any(FONT not in f for f in m.fonts):
        errors.append(NO_LATO)
    if m.pages != 1:
        errors.append(f"the resume has {m.pages} pages, it must have exactly 1")
    if not fill_min <= m.fill <= fill_max:
        errors.append(f"page fill is {m.fill}%, it must be between {fill_min:g}% and {fill_max:g}%")
    if any(ch in m.text for ch in LONG_DASHES):
        errors.append("the PDF text contains an em dash or en dash")
    if m.links != profile.links():
        errors.append(f"header links in the PDF do not match Config profile: {m.links}")
    return errors


# ---------------------------------------------------------------- fit


class FitError(RenderError):
    def __init__(self, message: str, fill: float):
        super().__init__(message)
        self.fill = fill


@dataclass
class FitResult:
    plan: Plan
    measure: Measure
    html: str
    steps: list[str] = field(default_factory=list)


def _overflow(m: Measure, fill_max: float) -> bool:
    return m.pages > 1 or m.fill > fill_max


def _revert_order(plan: Plan) -> list[str]:
    """Least important bullet edit first: edits missing from priority, then priority reversed."""
    ranked = [i for i in plan.priority.bullet_edits if plan.edit_for(i) is not None]
    unranked = [e.id for e in plan.bullet_edits if e.id not in ranked]
    return unranked + list(reversed(ranked))


def _remove_item(plan: Plan, item: str) -> bool:
    key = item_key(item)
    for table, minimum in ((plan.skills_main, 5), (plan.skills_also, 1)):
        for row in table:
            if key in {item_key(i) for i in row.items}:
                if len(row.items) == 1:
                    if len(table) <= minimum:
                        return False
                    table.remove(row)
                    return True
                row.items = [i for i in row.items if item_key(i) != key]
                return True
    return False


def _missing_rows(master: MasterResume, plan: Plan) -> list[tuple[str, int, PlanRow]]:
    """Master rows no plan row comes from, as (table, original index, row)."""
    present = [identify_row(r, master.skills_main + master.skills_also)
               for r in plan.skills_main + plan.skills_also]
    labels = {r.label.casefold() for r in plan.skills_main + plan.skills_also}
    out = []
    for table, rows in (("main", master.skills_main), ("also", master.skills_also)):
        for index, row in enumerate(rows):
            if row not in present and row.label.casefold() not in labels:
                out.append((table, index, PlanRow(label=row.label, items=list(row.items))))
    return out


def _missing_items(master: MasterResume, plan: Plan) -> list[tuple[str, str]]:
    """(master row label, item) for master items that are nowhere in the plan."""
    have = {item_key(i) for r in plan.skills_main + plan.skills_also for i in r.items}
    return [(row.label, item) for row in master.skills_main + master.skills_also
            for item in row.items if item_key(item) not in have]


def _restore_item(master: MasterResume, plan: Plan, label: str, item: str) -> bool:
    original = next(r for r in master.skills_main + master.skills_also if r.label == label)
    for row in plan.skills_main + plan.skills_also:
        if row.label.casefold() == label.casefold() or identify_row(row, [original]):
            row.items.append(item)
            return True
    return False


def fit(
    master: MasterResume,
    plan: Plan,
    sections: SectionPlan | None,
    measure: Measurer = measure_html,
    fill_min: float = FILL_MIN,
    fill_max: float = FILL_MAX,
) -> FitResult:
    """Render and adjust until the page fits. Raises FitError when it cannot."""
    plan = plan.model_copy(deep=True)
    steps: list[str] = []

    def run() -> tuple[str, Measure]:
        html = render_html(master, plan, sections)
        return html, measure(html)

    html, m = run()
    if _overflow(m, fill_max):
        for bullet_id in _revert_order(plan):
            if not _overflow(m, fill_max):
                break
            plan.bullet_edits = [e for e in plan.bullet_edits if e.id != bullet_id]
            steps.append(f"overflow: reverted {bullet_id}")
            html, m = run()
        for item in plan.priority.skill_items_low_relevance:
            if not _overflow(m, fill_max):
                break
            if _remove_item(plan, item):
                steps.append(f"overflow: removed {item}")
                html, m = run()
        if _overflow(m, fill_max):
            raise FitError(f"the resume does not fit on one page (fill {m.fill}%, "
                           f"{m.pages} pages) even after reverting edits", m.fill)

    if m.fill < fill_min:
        for table, index, row in _missing_rows(master, plan):
            target = plan.skills_main if table == "main" else plan.skills_also
            target.insert(min(index, len(target)), row)
            steps.append(f"underfill: restored row {row.label}")
            html, m = run()
            if _overflow(m, fill_max):
                target.remove(row)
                steps.pop()
                html, m = run()
            if m.fill >= fill_min:
                break
        for label, item in _missing_items(master, plan):
            if m.fill >= fill_min:
                break
            if _restore_item(master, plan, label, item):
                steps.append(f"underfill: restored {item}")
                html, m = run()
                if _overflow(m, fill_max):
                    _remove_item(plan, item)
                    steps.pop()
                    html, m = run()
        if m.fill < fill_min:
            raise FitError(f"the resume fills only {m.fill}% of the page (minimum "
                           f"{fill_min:g}%)", m.fill)
    for step in steps:
        log.info("fit: %s", step)
    return FitResult(plan=plan, measure=m, html=html, steps=steps)


# ---------------------------------------------------------------- filename


def clean_part(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", text or "")


def filename(template: str | None, company: str, role: str | None = None) -> str:
    """`Madeshwaran_Devops_<Company>.pdf`, or `..._<Company>_<Role>.pdf` when the company
    already has an approved resume for another job."""
    name = (template or DEFAULT_FILENAME).replace("<Company>", clean_part(company))
    if role:
        stem, dot, ext = name.rpartition(".")
        name = f"{stem}_{clean_part(role)[:MAX_ROLE_CHARS]}{dot}{ext}" if dot else name
    return name
