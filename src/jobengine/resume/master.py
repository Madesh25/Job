"""The Golden Master: fetched from Notion at runtime, parsed into editable zones, and rebuilt
from a plan.

The master is edited as text, not re-serialised through an HTML parser, so everything outside
the two editable zones stays byte-identical. Zone 1 is the Technical Skills region between the
`EDITABLE ZONE 1` comments; zone 2 is the text inside each `<li>` of Professional Experience.
"""

from __future__ import annotations

import hashlib
import html as htmllib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bs4 import BeautifulSoup

from jobengine.config_store import ConfigStore
from jobengine.resume.models import Bullet, MasterResume, Plan, PlanRow, Profile, SkillRow
from jobengine.settings import ROOT_DIR

if TYPE_CHECKING:
    from jobengine.notion_repo import NotionClient
    from jobengine.resume.sections import SectionPlan

FIXTURES = ROOT_DIR / "fixtures" / "resume"
NOT_FOUND = "Golden Master not found in Notion. Nothing built."
MASTER_START = "<!DOCTYPE html>"
CERT_START = "<h2>Certifications</h2>"
SKILLS_PLACEHOLDER = "{{SKILLS_ZONE}}"
LONG_DASHES = (chr(0x2014), chr(0x2013))  # em dash, en dash

ZONE1_RE = re.compile(
    r"(<!-- EDITABLE ZONE 1 START.*?-->)(.*?)(<!-- EDITABLE ZONE 1 END -->)", re.DOTALL
)
EXPERIENCE_RE = re.compile(r"(<h2>Professional Experience</h2>)(.*?)(?=<h2>)", re.DOTALL)
JOB_RE = re.compile(r'<div class="job">.*?</ul>', re.DOTALL)
# Tempered so the "<li>" mentioned in the ZONE 2 comment never starts a match.
LI_RE = re.compile(r"<li>((?:(?!<li>|-->).)*?)</li>", re.DOTALL)
COMPANY_RE = re.compile(r'<span class="co">(.*?)</span>', re.DOTALL)
CONTACT_RE = re.compile(r'(<p class="contact">)(\s*)(.*?)(\s*)(</p>)', re.DOTALL)
RELOC_RE = re.compile(r'(<p class="reloc">)(.*?)(</p>)', re.DOTALL)
SUBHEAD_RE = re.compile(r'<p class="subhead">.*?</p>', re.DOTALL)
BULLET_PLACEHOLDER_RE = re.compile(r"\{\{BULLET:(j\d+b\d+)\}\}")


class MasterError(Exception):
    """The Golden Master is missing or does not have the expected structure."""


def bullet_placeholder(bullet_id: str) -> str:
    return "{{BULLET:" + bullet_id + "}}"


def _text(fragment: str) -> str:
    return " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _esc(text: str) -> str:
    return htmllib.escape(text, quote=False)


# ---------------------------------------------------------------- fetching


def code_blocks(blocks: list[dict[str, Any]]) -> list[str]:
    """Text of every code block. Notion splits long code into 2000-character pieces."""
    out = []
    for block in blocks:
        if block.get("type") != "code":
            continue
        pieces = (block.get("code") or {}).get("rich_text") or []
        out.append("".join(p.get("plain_text") or (p.get("text") or {}).get("content", "")
                           for p in pieces))
    return out


def find_master(blocks: list[dict[str, Any]]) -> tuple[str, str | None]:
    """(Golden Master HTML, Certifications block) from the Build Spec page blocks."""
    codes = code_blocks(blocks)
    master = next((c for c in codes if c.lstrip().startswith(MASTER_START)), None)
    if not master:
        raise MasterError(NOT_FOUND)
    cert = next((c for c in codes if c.lstrip().startswith(CERT_START)), None)
    return master, cert


def fetch_blocks(client: NotionClient, page_id: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    cursor = None
    while True:
        params: list[tuple[str, Any]] = [("page_size", 100)]
        if cursor:
            params.append(("start_cursor", cursor))
        data = client.request("GET", f"/blocks/{page_id}/children", params=params)
        blocks.extend(data.get("results") or [])
        if not data.get("has_more"):
            return blocks
        cursor = data.get("next_cursor")


def fake_blocks(path: Path = FIXTURES / "resume_build_spec_page.json") -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- profile


def contact_html(profile: Profile) -> str:
    sep = '<span class="sep">|</span>'
    links = [
        (f"mailto:{profile.email}", profile.email),
        (profile.tel, profile.phone),
        (profile.website_url, "Portfolio"),
        (profile.github, "GitHub"),
        (profile.linkedin_url, "LinkedIn"),
    ]
    return sep.join(f'<a href="{htmllib.escape(href)}">{_esc(text)}</a>' for href, text in links)


def fill_profile(raw: str, profile: Profile) -> str:
    """Header links (and the relocation line) from Config; the master holds a snapshot."""
    if not CONTACT_RE.search(raw):
        raise MasterError("Golden Master has no contact line (p.contact).")
    html = CONTACT_RE.sub(
        lambda m: m.group(1) + m.group(2) + contact_html(profile) + m.group(4) + m.group(5),
        raw, count=1,
    )
    if profile.relocation_line:
        html = RELOC_RE.sub(
            lambda m: m.group(1) + _esc(profile.relocation_line or "") + m.group(3),
            html, count=1,
        )
    return html


# ---------------------------------------------------------------- parsing


def _rows(table: Any) -> list[SkillRow]:
    rows = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue
        label = " ".join(cells[0].get_text(" ").split())
        items = tuple(i.strip() for i in " ".join(cells[1].get_text(" ").split()).split(", ")
                      if i.strip())
        rows.append(SkillRow(label=label, items=items))
    return rows


def parse_master(raw: str, profile: Profile, cert_block: str | None = None) -> MasterResume:
    version = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    html = fill_profile(raw, profile)

    zone = ZONE1_RE.search(html)
    if not zone:
        raise MasterError("Golden Master has no EDITABLE ZONE 1 markers.")
    soup = BeautifulSoup(zone.group(2), "html.parser")
    tables = soup.find_all("table", class_="skills")
    subhead = SUBHEAD_RE.search(zone.group(2))
    if len(tables) != 2 or not subhead:
        raise MasterError("Golden Master skills zone needs two skills tables and p.subhead.")
    skills_main, skills_also = _rows(tables[0]), _rows(tables[1])
    frozen = html[:zone.start(2)] + SKILLS_PLACEHOLDER + html[zone.end(2):]

    exp = EXPERIENCE_RE.search(frozen)
    if not exp:
        raise MasterError("Golden Master has no Professional Experience section.")
    bullets: list[Bullet] = []
    companies: list[str] = []

    def job_sub(job_match: re.Match[str]) -> str:
        job_index = len(companies)
        company = COMPANY_RE.search(job_match.group(0))
        companies.append(_text(company.group(1)) if company else "")
        counter = iter(range(1000))

        def li_sub(li: re.Match[str]) -> str:
            bullet_id = f"j{job_index}b{next(counter)}"
            bullets.append(Bullet(id=bullet_id, job=job_index, company=companies[-1],
                                  text=_text(li.group(1))))
            return f"<li>{bullet_placeholder(bullet_id)}</li>"

        return LI_RE.sub(li_sub, job_match.group(0))

    section = JOB_RE.sub(job_sub, exp.group(2))
    frozen = frozen[:exp.start(2)] + section + frozen[exp.end(2):]
    if not bullets:
        raise MasterError("Golden Master has no Experience bullets.")
    return MasterResume(
        html=html, frozen_html=frozen, skills_main=skills_main, skills_also=skills_also,
        subhead_html=subhead.group(0), bullets=bullets, companies=companies, version=version,
        cert_block=cert_block,
    )


# ---------------------------------------------------------------- rendering


def _table(rows: list[PlanRow]) -> str:
    lines = ['<table class="skills">']
    for row in rows:
        lines.append(f'  <tr><td class="k">{_esc(row.label)}</td>'
                     f'<td>{_esc(", ".join(row.items))}</td></tr>')
    lines.append("</table>")
    return "\n".join(lines)


def skills_zone(plan: Plan, subhead_html: str) -> str:
    return f"\n{_table(plan.skills_main)}\n{subhead_html}\n{_table(plan.skills_also)}\n"


def render_html(master: MasterResume, plan: Plan, sections: SectionPlan | None = None) -> str:
    """The resume for a plan. An unchanged plan gives back the master (after profile fill)."""
    html = master.frozen_html.replace(SKILLS_PLACEHOLDER, skills_zone(plan, master.subhead_html))
    shrink = (sections.shrink_company or "").strip().casefold() if sections else ""
    for bullet in master.bullets:
        placeholder = f"<li>{bullet_placeholder(bullet.id)}</li>"
        if shrink and bullet.company.strip().casefold() == shrink:
            if bullet.id.endswith("b0"):
                html = html.replace(placeholder, f"<li>{_esc(sections.short_bullet or '')}</li>")
            else:
                html = re.sub(r"\n[ \t]*" + re.escape(placeholder), "", html)
            continue
        text = plan.edit_for(bullet.id)
        text = text if text is not None else bullet.text
        html = html.replace(placeholder, f"<li>{_esc(text)}</li>")
    if sections:
        html = sections.apply(html)
    leftover = BULLET_PLACEHOLDER_RE.search(html)
    if leftover or SKILLS_PLACEHOLDER in html:
        raise MasterError("render left a placeholder in the resume")
    return html


# ---------------------------------------------------------------- comparison


def blank_zones(html: str) -> str:
    """The document with both editable zones emptied, for the frozen comparison."""
    html = ZONE1_RE.sub(lambda m: m.group(1) + m.group(3), html)
    exp = EXPERIENCE_RE.search(html)
    if exp:
        section = LI_RE.sub("<li></li>", exp.group(2))
        html = html[:exp.start(2)] + section + html[exp.end(2):]
    return html


def frozen_diff(expected: str, actual: str) -> list[str]:
    """Errors when anything outside the editable zones differs."""
    a, b = blank_zones(expected).splitlines(), blank_zones(actual).splitlines()
    if a == b:
        return []
    for i, (x, y) in enumerate(zip(a, b, strict=False)):
        if x != y:
            return [f"frozen part changed at line {i + 1}: {_text(y)[:80]!r} "
                    f"(master: {_text(x)[:80]!r})"]
    return [f"frozen part changed: {len(b)} lines instead of {len(a)}"]


def normalised_text(html: str) -> str:
    return " ".join(BeautifulSoup(html, "html.parser").get_text(" ").split())


PROFILE_KEYS = {
    "email": "profile.email",
    "phone": "profile.phone",
    "website_url": "profile.website_url",
    "github": "profile.github",
    "linkedin_url": "profile.linkedin_url",
}


def profile_from_config(config: ConfigStore) -> Profile:
    missing = [key for key in PROFILE_KEYS.values() if not config.get(key)]
    if missing:
        raise MasterError(f"Config is missing {', '.join(missing)}")
    values = {name: config.get(key) or "" for name, key in PROFILE_KEYS.items()}
    return Profile(**values, relocation_line=config.get("resume.relocation_line"))


def load_master(blocks: list[dict[str, Any]], config: ConfigStore) -> MasterResume:
    raw, cert = find_master(blocks)
    return parse_master(raw, profile_from_config(config), cert)
