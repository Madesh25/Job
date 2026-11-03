"""The two header lines that change per job (your approval, PR 11). Pure, no AI.

- Headline: the whole headline becomes the job's title when it is one of your approved
  titles (`resume.headline_titles`, Config key of the same name wins), so "Cloud Engineer"
  and never "Cloud Engineer | Cloud & Platform Engineering". The approved title is used,
  never the posting's wording: "Senior Site Reliability Engineer (m/f/d)" gives "Site
  Reliability Engineer". No approved title in the role: the master's first title only
  ("DevOps Engineer | Cloud & Platform Engineering" gives "DevOps Engineer", Phase 19 A-02).
- Summary (Phase 19 A-02): "4+ years ..., 3+ of them" becomes "4 years ..., 3 of them": the
  "+" claims more than your experience answer (apply/facts.py does the same in the letters).
- Relocation line (p.reloc): `resume.relocation_template` with {place} (the job's city and
  country), {city} and {country}, always on one line: a "\n" in the template becomes " | ",
  so "Chennai, India\nOpen to relocate to {country}" gives "Chennai, India | Open to
  relocate to Poland". Without a template the Config
  `resume.relocation_line` stays as it is.

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
SUMMARY_RE = re.compile(r'(<p class="summary">)(.*?)(</p>)', re.DOTALL)
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
    if country == "Other":  # a remote job open across the EU or Europe
        country = "Europe"
    if not city or city.casefold() == "remote":
        return country
    return f"{city}, {country}" if country and country.casefold() != city.casefold() else city


def relocation_line(s: Settings, config: ConfigStore, values: dict[str, Any]) -> str | None:
    """The relocation text for this job; "\n" separates lines."""
    template = config.get("resume.relocation_template") or s.resume.get("relocation_template")
    where = place(values)
    if not template or not where:
        return None
    country = (values.get("Country") or "").strip() or where
    if country == "Other":  # a remote job open across the EU or Europe
        country = "Europe"
    city = (values.get("City") or "").strip()
    if not city or city.casefold() == "remote":
        city = country
    text = str(template).replace("\\n", "\n")
    return text.replace("{place}", where).replace("{country}", country).replace("{city}", city)


def _swap_headline(html: str, title: str) -> str:
    """The whole headline becomes the title (no second part after "|")."""
    return HEADLINE_RE.sub(lambda m: m.group(1) + htmllib.escape(title) + m.group(3), html,
                           count=1)


def main_title(html: str) -> str | None:
    """The master headline's first title ("DevOps Engineer" of "DevOps Engineer | Cloud &
    Platform Engineering"), or None when the headline has one part only."""
    m = HEADLINE_RE.search(html)
    if not m:
        return None
    text = " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", m.group(2))).split())
    first = re.split(r"\s*[|·•]\s*", text)[0].strip()
    return first if first and first != text else None


def _no_plus(html: str) -> str:
    from jobengine.apply.facts import PLUS, PLUS_IN

    return SUMMARY_RE.sub(lambda m: m.group(1) + PLUS_IN.sub(r"\1", PLUS.sub(r"\1 ", m.group(2)))
                          + m.group(3), html, count=1)


def _swap_reloc(html: str, line: str) -> str:
    """One line in the header: the template's lines are joined with " | "."""
    inner = " | ".join(htmllib.escape(part.strip()) for part in line.split("\n")
                        if part.strip())
    return RELOC_RE.sub(lambda m: m.group(1) + inner + m.group(3), html, count=1)


def for_job(master: MasterResume, s: Settings, config: ConfigStore,
            values: dict[str, Any]) -> MasterResume:
    """The master with this job's headline and relocation line."""
    html, frozen = master.html, master.frozen_html
    title = headline_title(values.get("Role"), approved_titles(s, config)) or main_title(html)
    if title:
        html, frozen = _swap_headline(html, title), _swap_headline(frozen, title)
    html, frozen = _no_plus(html), _no_plus(frozen)
    line = relocation_line(s, config, values)
    if line:
        html, frozen = _swap_reloc(html, line), _swap_reloc(frozen, line)
    return replace(master, html=html, frozen_html=frozen)


def headline_text(master: MasterResume) -> str:
    m = HEADLINE_RE.search(master.html)
    return " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", m.group(2))).split()) if m else ""
