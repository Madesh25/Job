"""The two header lines that change per job (your approval, PR 11). Pure, no AI.

- Headline: the first part (before "|") becomes the job's title when it is one of your
  approved titles (`resume.headline_titles`, Config key of the same name wins). The approved
  title is used, never the posting's wording: "Senior Site Reliability Engineer (m/f/d)"
  gives "Site Reliability Engineer". No approved title in the role: the headline stays.
- Relocation line (p.reloc): `resume.relocation_template` with {place} = the job's city and
  country, for example "Chennai, India (relocating to Warsaw, Poland)". Without a template
  the Config `resume.relocation_line` stays as it is.

The skills and bullets are untouched, so the integrity gate is unchanged.
"""

from __future__ import annotations

import html as htmllib
import re
from dataclasses import replace
from typing import Any

from jobengine.config_store import ConfigStore
from jobengine.resume.master import RELOC_RE
from jobengine.resume.models import MasterResume
from jobengine.settings import Settings
from jobengine.sweep.normalize import canon, canon_title

HEADLINE_RE = re.compile(r'(<p class="headline">)(.*?)(</p>)', re.DOTALL)
LEFT_RE = re.compile(r"^(\s*)(.*?)((?:&nbsp;|\s)*)$", re.DOTALL)
# Short forms a posting uses for an approved title.
ALIASES = {"sre": "Site Reliability Engineer", "k8s engineer": "Kubernetes Engineer"}


def _list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    return [str(p).strip() for p in value or [] if str(p).strip()]


def approved_titles(s: Settings, config: ConfigStore) -> list[str]:
    return _list(config.get("resume.headline_titles") or s.resume.get("headline_titles"))


def headline_title(role: str | None, titles: list[str]) -> str | None:
    """The approved title the job's role names (the longest match), else None."""
    words = f" {canon_title(role)} "
    found = [t for t in titles if f" {canon(t)} " in words]
    found += [title for alias, title in ALIASES.items()
              if f" {alias} " in words and title in titles]
    return max(found, key=len) if found else None


def place(values: dict[str, Any]) -> str:
    city = (values.get("City") or "").strip()
    country = (values.get("Country") or "").strip()
    if not city or city.casefold() == "remote":
        return country
    return f"{city}, {country}" if country and country.casefold() != city.casefold() else city


def relocation_line(s: Settings, config: ConfigStore, values: dict[str, Any]) -> str | None:
    template = config.get("resume.relocation_template") or s.resume.get("relocation_template")
    where = place(values)
    if not template or not where:
        return None
    return str(template).replace("{place}", where)


def _swap_headline(html: str, title: str) -> str:
    def swap(m: re.Match[str]) -> str:
        inner = m.group(2)
        cut = inner.find("|")
        if cut < 0:
            return m.group(1) + htmllib.escape(title) + m.group(3)
        left = LEFT_RE.match(inner[:cut])
        assert left is not None
        return (m.group(1) + left.group(1) + htmllib.escape(title) + left.group(3)
                + inner[cut:] + m.group(3))

    return HEADLINE_RE.sub(swap, html, count=1)


def _swap_reloc(html: str, line: str) -> str:
    return RELOC_RE.sub(lambda m: m.group(1) + htmllib.escape(line) + m.group(3), html, count=1)


def for_job(master: MasterResume, s: Settings, config: ConfigStore,
            values: dict[str, Any]) -> MasterResume:
    """The master with this job's headline and relocation line."""
    html, frozen = master.html, master.frozen_html
    title = headline_title(values.get("Role"), approved_titles(s, config))
    if title:
        html, frozen = _swap_headline(html, title), _swap_headline(frozen, title)
    line = relocation_line(s, config, values)
    if line:
        html, frozen = _swap_reloc(html, line), _swap_reloc(frozen, line)
    return replace(master, html=html, frozen_html=frozen)


def headline_text(master: MasterResume) -> str:
    m = HEADLINE_RE.search(master.html)
    return " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", m.group(2))).split()) if m else ""
