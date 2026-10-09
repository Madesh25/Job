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
# 7 Oct: one run turned all 28 Verified companies (ASML, Adyen, ING...) into Not listed, a
# failed read of the register page, not 28 real changes. When this many Verified companies
# (and more than a third of them; 9 Oct: 14 of 28 got through a "more than half" rule) would
# drop at once, nothing is changed and it is reported.
MASS_DEMOTION = 3
MAX_MISS_LINES = 15


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
    names: int = 0  # organisations read from the register page
    sample: list[str] = field(default_factory=list)  # a few names, to see what was read
    # Verified companies that no longer match: (company, nearest register names), so a
    # wrong miss shows at once (9 Oct: "ING" vs "ING Bank N.V.").
    misses: list[tuple[str, list[str]]] = field(default_factory=list)

    def text(self) -> str:
        if self.error:
            return "\n".join([f"IND register check failed: {self.error}. Nothing was changed."
                              + self._sample(), *self._miss_lines()])
        changed = "; ".join(f"{c.company}: {c.old or 'empty'} -> {c.new}" for c in self.changes)
        line = (f"IND register: {self.checked} NL companies checked, {len(self.changes)} changed"
                + (f" ({changed})" if changed else "") + ".")
        if self.changes and not self.written:
            line += " Not written outside prod."
        line += f" ({self.names} names read from the register.)"
        lines = [line + self._sample()]
        rank = "now rank lower" if self.written else "will rank lower once this is written (prod)"
        for c in self.changes:
            if c.demoted:
                lines.append(f"Warning: {c.company} is no longer on the IND register. Its jobs "
                             f"{rank} in screening.")
        return "\n".join([*lines, *self._miss_lines()])

    def _miss_lines(self) -> list[str]:
        if not self.misses:
            return []
        lines = ["Not found on the register, nearest names (if one is the same company, add "
                 "a Config ind_register.aliases line \"Company = Register name\"):"]
        for company, near in self.misses[:MAX_MISS_LINES]:
            lines.append(f"- {company}: {'; '.join(near) or 'none'}")
        return lines

    def _sample(self) -> str:
        # Phase 6: 13,020 "names" matched nothing; they were KvK numbers. Show what was read.
        return f"\nNames read, for example: {'; '.join(self.sample)}" if self.sample else ""


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
    register.add_aliases(deps.config().get("ind_register.aliases"))
    report.names = len(register.names)
    step = max(1, len(register.names) // 3)
    report.sample = register.names[::step][:3]
    log.info("IND register: %d names read, for example %s", report.names, report.sample)
    today = deps.today()
    writes: list[tuple[str, dict[str, Any]]] = []
    companies = deps.companies()
    for company in companies:
        if company.region != REGION:
            continue
        report.checked += 1
        new = VERIFIED if register.match(company.name) else NOT_LISTED
        props: dict[str, Any] = {"Last checked": today}
        if new != company.ind_sponsor:
            change = IndChange(company.name, company.ind_sponsor, new)
            report.changes.append(change)
            if change.demoted:
                report.misses.append((company.name, register.closest(company.name)))
            props["IND sponsor"] = new
        writes.append((company.row_id, props))
    verified = sum(1 for c in companies if c.region == REGION
                   and c.ind_sponsor == VERIFIED)
    dropped = sum(1 for c in report.changes if c.demoted)
    if dropped >= MASS_DEMOTION and dropped * 3 > verified:
        report.error = (f"{dropped} of {verified} Verified companies would become Not listed "
                        f"at once ({report.names} names read from {url}); that looks like a "
                        "changed or partly loaded register page, not real changes")
        report.changes = []
        log.warning("IND refresh refused: %s", report.error)
        return report
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
