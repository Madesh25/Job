"""IND register refresh (spec section 6): the sponsor status of the Dutch target companies.

Runs at the end of every /update and via `python -m jobengine.strategy ind`. Only
`IND sponsor` and `Last checked` are written, in prod only; elsewhere the report lists what
would change. A failed download or parse changes nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from jobengine import http
from jobengine.config_store import ConfigStore
from jobengine.ind_register import load_register
from jobengine.reference import Company
from jobengine.settings import Settings

log = logging.getLogger("jobengine.strategy")

VERIFIED, NOT_LISTED = "Verified", "Not listed"
REGION = "Netherlands"


@dataclass(frozen=True)
class IndChange:
    company: str
    old: str | None
    new: str

    @property
    def demoted(self) -> bool:
        return self.old == VERIFIED and self.new == NOT_LISTED


@dataclass
class IndReport:
    checked: int = 0
    changes: list[IndChange] = field(default_factory=list)
    error: str | None = None
    written: bool = False

    def text(self) -> str:
        if self.error:
            return f"IND register check failed: {self.error}. Nothing was changed."
        changed = "; ".join(f"{c.company}: {c.old or 'empty'} -> {c.new}" for c in self.changes)
        line = (f"IND register: {self.checked} NL companies checked, {len(self.changes)} changed"
                + (f" ({changed})" if changed else "") + ".")
        if self.changes and not self.written:
            line += " Not written outside prod."
        lines = [line]
        for c in self.changes:
            if c.demoted:
                lines.append(f"Warning: {c.company} is no longer on the IND register. Its jobs "
                             "now rank lower in screening.")
        return "\n".join(lines)


@dataclass
class IndDeps:
    s: Settings
    config: Callable[[], ConfigStore]
    companies: Callable[[], list[Company]]
    get_text: Callable[[str], str]
    # (page_id, props) -> None. Prod only; checks safety.target_companies_writable_fields.
    writer: Callable[[str, dict[str, Any]], None] | None = None
    today: Callable[[], date] = date.today
    write: bool = True


def refresh(deps: IndDeps) -> IndReport:
    report = IndReport()
    url = deps.config().get("ind_register.url")
    try:
        if not url:
            raise ValueError("Config ind_register.url is missing")
        register = load_register(url, deps.get_text)
    except (http.HttpError, ValueError) as exc:
        report.error = str(exc)
        return report
    today = deps.today()
    writes: list[tuple[str, dict[str, Any]]] = []
    for company in deps.companies():
        if company.region != REGION:
            continue
        report.checked += 1
        new = VERIFIED if register.match(company.name) else NOT_LISTED
        props: dict[str, Any] = {"Last checked": today}
        if new != company.ind_sponsor:
            report.changes.append(IndChange(company.name, company.ind_sponsor, new))
            props["IND sponsor"] = new
        writes.append((company.row_id, props))
    if deps.writer is not None and deps.write and deps.s.app_env == "prod":
        for page_id, props in writes:
            deps.writer(page_id, props)
        report.written = True
    else:
        log.info("DRY RUN: would update %d Target Companies rows", len(writes))
    return report


def fake_companies() -> list[Company]:
    import json

    from jobengine.settings import ROOT_DIR

    rows = json.loads((ROOT_DIR / "fixtures" / "strategy" / "target_companies.json")
                      .read_text(encoding="utf-8"))
    return [Company(row_id=r["id"], name=r["Company"], region=r.get("Region"),
                    tier=r.get("Tier"), ind_sponsor=r.get("IND sponsor")) for r in rows]


def fake_deps(s: Settings, writer: Callable[[str, dict[str, Any]], None] | None = None,
              write: bool = True) -> IndDeps:
    from jobengine.settings import ROOT_DIR

    def get_text(url: str) -> str:
        return (ROOT_DIR / "fixtures" / "ind_register.html").read_text(encoding="utf-8")

    return IndDeps(s=s, config=ConfigStore.fake, companies=fake_companies, get_text=get_text,
                   writer=writer, write=write)


def real_deps(s: Settings, write: bool = True) -> IndDeps:
    from jobengine.notion_repo import NotionClient, target_companies_writer
    from jobengine.reference import Reference

    if not s.notion_token:
        raise ValueError("IND refresh cannot run: NOTION_TOKEN missing")
    client = NotionClient(s.notion_token, s)
    return IndDeps(
        s=s, config=lambda: ConfigStore.load(client, s),
        companies=lambda: Reference.load(client, s).companies,
        get_text=lambda url: http.get_text(url, s=s),
        writer=target_companies_writer(s, client), write=write,
    )
