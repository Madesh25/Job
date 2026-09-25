"""Optional resume sections from the Resume Sections table (Build Spec section 4). Pure.

Header, Summary, Skills, Experience and Education are always present in the fixed order.
Certifications, Languages and the GDPR line appear only when their row is Enabled and its
Countries contain All or the job's country. Projects are not supported yet.
"""

from __future__ import annotations

import html as htmllib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jobengine.config_store import ConfigStore
from jobengine.settings import ROOT_DIR

if TYPE_CHECKING:
    from jobengine.notion_repo import NotionClient
    from jobengine.settings import Settings

FIXTURES = ROOT_DIR / "fixtures" / "resume"
PLACEHOLDER_DATE = "<Month Year>"
CERT_TITLE = "<h2>Certifications</h2>"
NO_CERT_DATE = "Certifications is enabled but has no date."
PROJECTS_REFUSED = (
    "Projects section is enabled but project selection is not built yet. "
    "Untick it in Resume Sections."
)
SHRINK_KEY = "resume.shrink_job_when_certs"
SHORT_BULLET_KEY = "resume.ncr_short_bullet"
GDPR_STYLE = "font-size: 8pt; font-style: italic; text-align: center; margin-top: 6px;"


class SectionError(Exception):
    """The Resume Sections settings cannot be rendered. The message is shown to you."""


@dataclass(frozen=True)
class SectionRow:
    section: str
    enabled: bool
    order: float | None
    countries: tuple[str, ...]
    content: str

    def applies(self, country: str | None) -> bool:
        return self.enabled and ("All" in self.countries or (country or "") in self.countries)

    @property
    def kind(self) -> str:
        name = self.section.strip().casefold()
        return "gdpr" if name.startswith("gdpr") else name


@dataclass(frozen=True)
class SectionPlan:
    cert_html: str | None = None
    shrink_company: str | None = None
    short_bullet: str | None = None
    languages: str | None = None
    gdpr: str | None = None

    def apply(self, html: str) -> str:
        if self.cert_html:
            html = html.replace("<h2>Education</h2>", f"{self.cert_html}\n\n<h2>Education</h2>", 1)
        tail = ""
        if self.languages:
            tail += f"\n<h2>Languages</h2>\n<p>{_esc(self.languages)}</p>\n"
        if self.gdpr:
            tail += f'\n<p style="{GDPR_STYLE}">{_esc(self.gdpr)}</p>\n'
        if tail:
            html = html.replace("\n</body>", f"{tail}\n</body>", 1)
        return html


def _esc(text: str) -> str:
    return htmllib.escape(text, quote=False)


def cert_lines(content: str) -> list[str]:
    """One <p> per non-empty line; the part before the first comma is bold."""
    out = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        name, comma, rest = line.partition(",")
        out.append(f"<p><b>{_esc(name.strip())}</b>{comma}{_esc(rest)}</p>")
    return out


def resolve(
    rows: list[SectionRow], country: str | None, config: ConfigStore,
) -> SectionPlan:
    """The optional sections for a job in `country`. Raises SectionError with a clear message."""
    applicable = {row.kind: row for row in rows if row.applies(country)}
    if "projects" in applicable:
        raise SectionError(PROJECTS_REFUSED)

    cert_html = shrink = short = None
    cert = applicable.get("certifications")
    if cert:
        content = cert.content.strip()
        if not content or PLACEHOLDER_DATE in content:
            raise SectionError(NO_CERT_DATE)
        for key in (SHRINK_KEY, SHORT_BULLET_KEY):
            if not config.get(key):
                raise SectionError(f"Certifications is enabled but Config {key} is missing.")
        cert_html = "\n".join([CERT_TITLE, *cert_lines(content)])
        shrink, short = config.get(SHRINK_KEY), config.get(SHORT_BULLET_KEY)

    languages = applicable.get("languages")
    gdpr = applicable.get("gdpr") if country == "Poland" else None
    return SectionPlan(
        cert_html=cert_html,
        shrink_company=shrink,
        short_bullet=short,
        languages=" ".join(languages.content.split()) if languages else None,
        gdpr=" ".join(gdpr.content.split()) if gdpr else None,
    )


# ---------------------------------------------------------------- loading


def _row(values: dict[str, Any]) -> SectionRow:
    return SectionRow(
        section=values.get("Section") or "",
        enabled=bool(values.get("Enabled")),
        order=values.get("Order"),
        countries=tuple(values.get("Countries") or ()),
        content=values.get("Content") or "",
    )


def load_rows(client: NotionClient, s: Settings) -> list[SectionRow]:
    from jobengine.notion_repo import page_values

    return [_row(page_values(page)) for page in client.query(s.notion_read["resume_sections"])
            if page_values(page).get("Section")]


def fake_rows(path: Path = FIXTURES / "resume_sections.json") -> list[SectionRow]:
    return [_row(values) for values in json.loads(path.read_text(encoding="utf-8"))]
