"""find_contacts (spec section 2): cache, job description, domain, then the paid waterfall
(Apollo, Hunter, Snov) for the slots still open. Writes Contacts and the job's relation."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from jobengine.bot_state import BotState, FakeBotState
from jobengine.config_store import ConfigStore
from jobengine.contacts import cache as contact_cache
from jobengine.contacts import jd_emails
from jobengine.contacts.classify import PERSONAL_DOMAINS, fill_slots, parse_mix
from jobengine.contacts.credits import CreditBook
from jobengine.contacts.models import (
    NOTION_COUNTRIES,
    OTHER,
    RECRUITER,
    TYPE_ORDER,
    Candidate,
    Chosen,
    ContactsResult,
)
from jobengine.contacts.providers import apollo, hunter, snov
from jobengine.contacts.providers.base import FixtureHttp, ProviderDeps, Request, real_request
from jobengine.notion_repo import (
    ContactsRepo,
    FakeContactsRepo,
    FakeJobsRepo,
    JobsRepo,
    NotionClient,
    config_writer_for,
    contacts_repo_for,
    jobs_repo_for,
)
from jobengine.reference import Reference
from jobengine.safety import paid_api_allowed
from jobengine.screen.runner import description
from jobengine.settings import ROOT_DIR, Settings
from jobengine.sweep.normalize import canon_company

log = logging.getLogger("jobengine.contacts")

FIXTURES = ROOT_DIR / "fixtures" / "contacts"
WATERFALL = (("apollo", apollo), ("hunter", hunter), ("snov", snov))
DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")
ATS_HOSTS = ("greenhouse.io", "lever.co", "smartrecruiters.com", "myworkdayjobs.com",
             "workday.com", "teamtailor.com", "recruitee.com", "personio.de")
FIXTURE_KEYS = {"apollo_api_key": "fixture-apollo-key", "hunter_api_key": "fixture-hunter-key",
                "snov_client_id": "fixture-snov-id", "snov_client_secret": "fixture-snov-secret"}
NONE_FOUND = "No contacts found. Apply through the portal only."
MAX_CONTACT_PERSON = 5
SLOT_WORDS = {"Peer engineer": "peer", "Hiring": "hiring", "Recruiter/TA": "recruiter"}


@dataclass
class ContactDeps:
    s: Settings
    config: Callable[[], ConfigStore]
    reference: Callable[[], Reference]
    jobs: JobsRepo | None
    contacts: ContactsRepo | None
    state: BotState
    # How providers make HTTP calls: real (prod, DRY_RUN false) or fixtures.
    request: Callable[[str], Request]
    config_writer: Callable[[ConfigStore], Any] = lambda config: None
    today: Callable[[], date] = date.today
    write: bool = True
    calls: list[str] = field(default_factory=list)  # providers searched, in order


def fake_deps(
    s: Settings, jobs: JobsRepo | None = None, state: BotState | None = None, write: bool = True,
) -> ContactDeps:
    fixture = FixtureHttp(s)
    return ContactDeps(
        s=s, config=ConfigStore.fake, reference=Reference.fake,
        jobs=jobs or FakeJobsRepo.from_fixture(FIXTURES / "job_opportunities_seed.json"),
        contacts=FakeContactsRepo.from_fixture(FIXTURES / "contacts_seed.json"),
        state=state or FakeBotState(), request=lambda provider: fixture, write=write,
    )


def real_deps(s: Settings, state: BotState, write: bool = True) -> ContactDeps:
    if not s.notion_token:
        raise ValueError("contact finder cannot run: NOTION_TOKEN missing")
    client = NotionClient(s.notion_token, s)
    fixture = FixtureHttp(s)
    return ContactDeps(
        s=s, config=lambda: ConfigStore.load(client, s),
        reference=lambda: Reference.load(client, s), jobs=jobs_repo_for(s, client),
        contacts=contacts_repo_for(s, client), state=state,
        request=lambda provider: real_request(s) if paid_api_allowed(provider, s) else fixture,
        config_writer=lambda config: config_writer_for(s, client, config), write=write,
    )


# ---------------------------------------------------------------- domain


def is_ats_host(domain: str) -> bool:
    d = domain.lower()
    return any(d == h or d.endswith("." + h) for h in ATS_HOSTS)


def valid_domain(value: str) -> str | None:
    d = value.strip().lower().removeprefix("https://").removeprefix("http://")
    d = d.removeprefix("www.").split("/")[0].lstrip("@")
    if not DOMAIN_RE.match(d) or is_ats_host(d) or d in PERSONAL_DOMAINS:
        return None
    return d


def state_key(company: str) -> str:
    return f"domain:{canon_company(company)}"


def find_domain(company: str, reference: Reference, config: ConfigStore,
                state: BotState) -> str | None:
    """Target Companies Domain, then Config contacts.domains, then a domain you gave in
    Telegram. Never an ATS host."""
    target = reference.company(company)
    candidates = [target.domain if target else None]
    for line in (config.get("contacts.domains") or "").splitlines():
        name, eq, value = line.partition("=")
        if eq and canon_company(name) == canon_company(company):
            candidates.append(value)
    answer = state.get(state_key(company)) or {}
    candidates.append(answer.get("domain"))
    for value in candidates:
        if value and valid_domain(value):
            return valid_domain(value)
    return None


def job_ref(job_id: str) -> str:
    return "JOB-" + re.sub(r"[^0-9a-z]", "", job_id.lower())[:8]


def domain_question(company: str, job_id: str) -> str:
    return (f"What is the email domain for {company}? Reply to this message with the domain, "
            f"e.g. example.com. Ref {job_ref(job_id)}")


# ---------------------------------------------------------------- the lookup


def _country(value: str | None) -> str:
    return value if value in NOTION_COUNTRIES else "Other"


def _summary(result: ContactsResult, generic: list[Chosen], credits: str) -> str:
    lines = [f"Contacts for {result.company}, {result.role} ({result.country})"]
    for kind in TYPE_ORDER:
        lines += [c.label() for c in result.contacts if c.type == kind]
    lines += [f"Mailbox: {c.email} [Job posting]" for c in generic]
    found = len(result.contacts)
    if found == 0 and not generic:
        lines.append(NONE_FOUND)
        lines.append(credits)
    elif result.missing:
        missing = ", ".join(f"{n} {SLOT_WORDS[k]}" for k, n in result.missing.items())
        lines.append(f"{found} of {result.target} found. Missing: {missing}. {credits}")
    else:
        lines.append(f"{found} of {result.target} found. {credits}")
    lines.extend(result.notes)
    return "\n".join(lines)


def find_contacts(deps: ContactDeps, job_id: str) -> ContactsResult:
    today = deps.today()
    values = deps.jobs.get_values(job_id) if deps.jobs else None
    if not values:
        return ContactsResult(job_id=job_id, company="", role="", country="", target=0,
                              status="failed", message=f"No Job Opportunities row {job_id}.")
    company, role = values.get("Company") or "", values.get("Role") or ""
    country = values.get("Country") or "Other"
    config = deps.config()
    mix = parse_mix(config.get("contacts.mix"), config.get("contacts.per_job"))
    result = ContactsResult(job_id=job_id, company=company, role=role, country=country,
                            target=mix.total)
    if mix.note:
        result.notes.append(mix.note)
    months = config.get_int("contacts.cache_months") or int(
        deps.s.contacts.get("cache_months", 6))
    patterns = deps.s.contacts.get("title_patterns")
    personal = deps.s.contacts.get("personal_domains") or PERSONAL_DOMAINS

    # 1. Cache.
    all_rows = deps.contacts.all_rows() if deps.contacts else []
    by_email = {(v.get("Email") or "").strip().lower(): (pid, v) for pid, v in all_rows
                if v.get("Email")}
    blocked = {e for e, (_, v) in by_email.items() if v.get("Status") in contact_cache.BLOCKED}
    rows = contact_cache.company_rows(all_rows, company)
    chosen = contact_cache.pick(rows, mix, country, today, months, job_id)
    seen = {c.email.lower() for c in chosen} | blocked

    def open_slots() -> dict[str, int]:
        slots = mix.slots()
        for c in chosen:
            if c.type in slots:
                slots[c.type] -= 1
        return {k: v for k, v in slots.items() if v > 0}

    # 2. Job description.
    body = deps.jobs.read_body(job_id) if deps.jobs else []
    jd, _ = description(body, full_min=0)
    generic: list[Chosen] = []
    for found in jd_emails.extract(jd, personal, deps.s.contacts.get("generic_mailboxes")
                                   or jd_emails.GENERIC_MAILBOXES):
        key = found.email.lower()
        if key in seen:
            continue
        existing = by_email.get(key)
        if found.generic:
            seen.add(key)
            generic.append(Chosen(name=found.email, title="", email=found.email, type=OTHER,
                                  source="Job posting", country=_country(country),
                                  page_id=existing[0] if existing else None,
                                  cached=bool(existing)))
        elif open_slots().get(RECRUITER, 0) > 0:
            seen.add(key)
            chosen.append(Chosen(name=found.name or found.email, title="", email=found.email,
                                 type=RECRUITER, source="Job posting", country=_country(country),
                                 page_id=existing[0] if existing else None,
                                 cached=bool(existing)))

    # 3. Domain and the paid waterfall, only for slots still open.
    book = CreditBook(config, today, deps.config_writer(config))
    if open_slots():
        domain = find_domain(company, deps.reference(), config, deps.state)
        if domain is None:
            result.status = "waiting_domain"
            result.waiting_for_domain = True
            result.message = domain_question(company, job_id)
            deps.state.set(f"domain_ask:{job_ref(job_id)}", {"job_id": job_id,
                                                             "company": company})
            return result
        for name, provider in WATERFALL:
            slots = open_slots()
            if not slots:
                break
            if deps.s.app_env == "prod" and not paid_api_allowed(name, deps.s):
                result.notes.append(f"{name}: paid calls are off (DRY_RUN), skipped")
                continue
            left = book.available(name)
            if left <= 0:
                result.notes.append(f"{name}: no credits left this month, skipped")
                continue
            s = deps.s if paid_api_allowed(name, deps.s) else deps.s.model_copy(
                update=FIXTURE_KEYS)
            pdeps = ProviderDeps(s=s, request=deps.request(name),
                                 search_titles=deps.s.contacts.get("search_titles") or {},
                                 credits_left=left, patterns=patterns)
            deps.calls.append(name)
            found_result = provider.search(company, domain, country, slots, pdeps)
            book.spend(name, found_result.credits_used)
            if found_result.skipped_reason:
                result.notes.append(found_result.skipped_reason)
            result.notes.extend(found_result.notes)
            for candidate, kind, note in fill_slots(found_result.candidates, slots, country,
                                                    domain, seen, patterns, personal):
                chosen.append(_chosen(candidate, kind, note, by_email, country))

    result.contacts = chosen
    result.missing = open_slots()
    _write(deps, job_id, values, chosen, generic, today)
    result.message = _summary(result, generic, book.line())
    return result


def _chosen(candidate: Candidate, kind: str, note: str,
            by_email: dict[str, tuple[str, dict[str, Any]]], country: str) -> Chosen:
    existing = by_email.get(candidate.email.lower())
    if existing:  # same email already in Contacts: reuse that row, never a second one
        return contact_cache.CachedRow(existing[0], existing[1]).chosen()
    return Chosen(name=candidate.name, title=candidate.title, email=candidate.email, type=kind,
                  source=candidate.source,
                  country=_country(candidate.country if note != "country unverified" else country),
                  verified=candidate.provider_verified, notes=note)


def _write(deps: ContactDeps, job_id: str, values: dict[str, Any], chosen: list[Chosen],
           generic: list[Chosen], today: date) -> None:
    if not deps.write or deps.contacts is None or deps.jobs is None:
        return
    for contact in [*chosen, *generic]:
        if contact.page_id:
            deps.contacts.add_job(contact.page_id, job_id)
            continue
        contact.page_id = deps.contacts.create({
            "Name": contact.name, "Title": contact.title, "Email": contact.email,
            "Company": values.get("Company") or "", "Country": contact.country,
            "Type": contact.type, "Source": contact.source,
            "Status": "Verified" if contact.verified else "Unverified",
            "Date found": today, "Related jobs": [job_id], "Notes": contact.notes,
        })
    linked = list(dict.fromkeys([*(values.get("Contacts") or []),
                                 *(c.page_id for c in [*chosen, *generic] if c.page_id)]))
    update: dict[str, Any] = {"Contacts": linked}
    if any(c.source == "Job posting" for c in [*chosen, *generic]):
        update["Contact source"] = "Job posting"
    elif not chosen and not generic:
        update["Contact source"] = "Not found"
    names = [c.name for c in chosen][:MAX_CONTACT_PERSON]
    if names:
        update["Contact person"] = ", ".join(names)
    deps.jobs.update(job_id, update)


def on_contacts_ready(job_id: str, contacts: list[Chosen]) -> None:
    """Contacts are ready for a job. The bot's desk then writes the Gmail drafts (Module 06,
    jobengine.mail.drafter); this only logs."""
    log.info("contacts ready for %s: %d", job_id, len(contacts))


# ---------------------------------------------------------------- domain replies


def answer_domain(deps: ContactDeps, replied_to: str, text: str) -> ContactsResult | str | None:
    """A reply to the domain question. None when the replied-to message is not one;
    a string when the domain is not valid; otherwise the resumed lookup."""
    match = re.search(r"Ref (JOB-[0-9a-z]{1,8})", replied_to)
    if not match:
        return None
    asked = deps.state.get(f"domain_ask:{match.group(1)}")
    if not asked:
        return "That domain question is no longer open. Run /contacts <job> again."
    domain = valid_domain(text)
    if domain is None:
        return ("That does not look like a company email domain (and job boards or personal "
                "mail domains are not allowed). Reply with something like example.com.")
    deps.state.set(state_key(asked["company"]), {"domain": domain})
    deps.state.delete(f"domain_ask:{match.group(1)}")
    return find_contacts(deps, asked["job_id"])

