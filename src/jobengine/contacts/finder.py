"""find_contacts (spec section 2): cache, job description, domain, then the paid waterfall
(Apollo, Hunter, Snov, then the free plans of Prospeo and Tomba) for the slots still open.
Writes Contacts and the job's relation."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from jobengine import http
from jobengine.bot_state import BotState, FakeBotState
from jobengine.config_store import ConfigStore
from jobengine.contacts import cache as contact_cache
from jobengine.contacts import jd_emails
from jobengine.contacts.classify import (
    PERSONAL_DOMAINS,
    classify_title,
    country_ok,
    fill_slots,
    keep,
    parse_mix,
)
from jobengine.contacts.credits import OPTIONAL, CreditBook, account_label
from jobengine.contacts.models import (
    HIRING,
    NOTION_COUNTRIES,
    OTHER,
    PEER,
    RECRUITER,
    TYPE_ORDER,
    Candidate,
    Chosen,
    ContactsResult,
)
from jobengine.contacts.providers import apollo, github, hunter, prospeo, snov, tomba
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
WATERFALL = (("apollo", apollo), ("hunter", hunter), ("snov", snov), ("prospeo", prospeo),
             ("tomba", tomba))
DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")
ATS_HOSTS = ("greenhouse.io", "lever.co", "smartrecruiters.com", "myworkdayjobs.com",
             "workday.com", "teamtailor.com", "recruitee.com", "personio.de")
FIXTURE_KEYS = {"apollo_api_key": "fixture-apollo-key", "hunter_api_key": "fixture-hunter-key",
                "snov_client_id": "fixture-snov-id", "snov_client_secret": "fixture-snov-secret",
                "prospeo_api_key": "fixture-prospeo-key", "tomba_api_key": "fixture-tomba-key",
                "tomba_api_secret": "fixture-tomba-secret"}
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


def domain_question(company: str, job_id: str, options: list[str] | None = None) -> str:
    if options:
        return (f"What is the email domain for {company}? Tap the right one below (found on "
                "the job or careers page), or send the domain, e.g. example.com. "
                f"Ref {job_ref(job_id)}")
    return (f"What is the email domain for {company}? Send the domain, e.g. example.com. "
            f"Ref {job_ref(job_id)}")


# Phase 6 (8 Oct): the domain question offers the domains found on the job's own pages as
# buttons. Only domains seen there are offered (never one made up from the company name),
# and none is used until you tap it or send it.
OPEN_ASK = "domain_ask:open"  # the latest open question, for a domain sent as plain text
MAX_OPTIONS = 3
URL_HOST = re.compile(r"https?://([a-z0-9.-]+\.[a-z]{2,})", re.I)
EMAIL_HOST = re.compile(r"[\w.+-]+@([a-z0-9.-]+\.[a-z]{2,})", re.I)
NOT_COMPANY = ("adzuna.", "jooble.", "europa.eu", "justjoin.it", "nofluffjobs.com",
               "pracuj.pl", "theprotocol.it", "bulldogjob.pl", "irishjobs.ie", "jobs.ie",
               "jobsireland.ie", "iamexpat.nl", "indeed.", "nationalevacaturebank.nl",
               "linkedin.", "lnkd.in", "glassdoor.", "google.", "goo.gl", "bit.ly", "t.co",
               "facebook.", "instagram.", "twitter.", "x.com", "youtube.", "youtu.be",
               "tiktok.", "apple.com", "microsoft.com", "w3.org", "schema.org", "gstatic.",
               "cloudflare.com", "ashbyhq.com", "workable.com", "jobvite.com", "icims.com",
               "bamboohr.com", "successfactors.", "oraclecloud.com", "taleo.net",
               "eightfold.ai", "phenompeople.com", "breezy.hr", "jobylon.com", "homerun.co",
               "join.com", "softgarden.", "onlyfy.", "erecruiter.pl", "traffit.com")
# Second-level suffixes where the company's domain has three parts (example.com.pl).
TWO_PART = ("com.pl", "co.uk", "org.uk", "com.au", "co.nz", "com.br", "co.za", "com.tr")


def _not_company(domain: str) -> bool:
    """A job board, ATS, social or web service domain ("indeed." is any indeed.<tld>)."""
    for name in NOT_COMPANY:
        if name.endswith("."):
            if domain.startswith(name) or f".{name}" in f".{domain}":
                return True
        elif domain == name or domain.endswith("." + name):
            return True
    return False


def base_domain(host: str) -> str:
    labels = host.lower().strip(".").removeprefix("www.").split(".")
    keep = 3 if ".".join(labels[-2:]) in TWO_PART else 2
    return ".".join(labels[-keep:])


def domain_options(company: str, urls: list[str | None], jd: str) -> list[str]:
    """Company email domains seen on the job's pages: the job link and careers page (when on
    the company's own site), email addresses in the description, and links in it that carry
    a word of the company name (a DevOps job links kubernetes.io too). Name matches first;
    at most MAX_OPTIONS."""
    words = [w for w in re.findall(r"[a-z0-9]+", canon_company(company).lower()) if len(w) > 2]

    def named(domain: str) -> bool:
        return any(w in domain for w in words)

    seen = [(m.group(1), True) for u in urls if u for m in [URL_HOST.match(u)] if m]
    seen += [(h, True) for h in EMAIL_HOST.findall(jd or "")]
    seen += [(h, False) for h in URL_HOST.findall(jd or "")]
    found: list[str] = []
    for host, trusted in seen:
        domain = valid_domain(base_domain(host))
        if not domain or domain in found or _not_company(domain):
            continue
        if trusted or named(domain):
            found.append(domain)
    found.sort(key=lambda d: not named(d))  # stable: page order kept otherwise
    return found[:MAX_OPTIONS]


def open_question(state: BotState) -> dict[str, Any] | None:
    """The latest domain question still waiting, or None."""
    asked = state.get(OPEN_ASK) or {}
    ref = asked.get("ref")
    return {**asked, "ref": ref} if ref and state.get(f"domain_ask:{ref}") else None


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


APPLY_ONLY_NOTE = ("Apply only: no paid lookup for this job (outreach budget). Tap Find "
                   "contacts anyway to spend credits on it.")


def find_contacts(deps: ContactDeps, job_id: str, paid: bool = True) -> ContactsResult:
    """`paid` False (an apply-only job, Module 10): only the free sources, the Contacts cache
    and emails in the job description; no domain question and no provider calls."""
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
    if open_slots() and not paid:
        result.notes.append(APPLY_ONLY_NOTE)
    elif open_slots():
        domain = find_domain(company, deps.reference(), config, deps.state)
        if domain is None:
            result.status = "waiting_domain"
            result.waiting_for_domain = True
            target = deps.reference().company(company)
            options = domain_options(company, [values.get("URL"),
                                               target.careers_url if target else None], jd)
            result.domain_options = options
            result.message = domain_question(company, job_id, options)
            deps.state.set(f"domain_ask:{job_ref(job_id)}", {"job_id": job_id,
                                                             "company": company})
            deps.state.set(OPEN_ASK, {"ref": job_ref(job_id), "company": company})
            return result
        if (config.get(GITHUB_SWITCH) or "").strip().lower() == "on" and \
                open_slots().get(PEER, 0) > 0:
            _github_step(deps, config, company, domain, country, result, chosen, seen,
                         by_email, open_slots, patterns, personal)
        for name, provider in WATERFALL:
            if not open_slots():
                break
            if deps.s.app_env == "prod" and not paid_api_allowed(name, deps.s):
                result.notes.append(f"{name}: paid calls are off (DRY_RUN), skipped")
                continue
            if book.counters.get(name) is None and name in OPTIONAL:
                continue  # a free plan you have not set up (no Config credits.<name>)
            _provider_step(deps, book, name, provider, company, domain, country, result,
                           chosen, seen, by_email, open_slots, patterns, personal)

    result.contacts = chosen
    result.missing = open_slots()
    _write(deps, job_id, values, chosen, generic, today)
    result.message = _summary(result, generic, book.line())
    return result


# Second accounts (6 Oct): Settings field -> its account-2 field. An account is used when all
# its fields are set; the second only when the first has no credits left or fails.
SECOND_FIELDS = {
    "apollo": {"apollo_api_key": "apollo_api_key_2"},
    "hunter": {"hunter_api_key": "hunter_api_key_2"},
    "snov": {"snov_client_id": "snov_client_id_2", "snov_client_secret": "snov_client_secret_2"},
    "prospeo": {"prospeo_api_key": "prospeo_api_key_2"},
}


def accounts(name: str, s: Settings) -> list[tuple[str, Settings]]:
    """(account, settings with that account's keys): the first, then the second if set."""
    found = [(name, s)]
    fields = SECOND_FIELDS.get(name)
    if fields and all(getattr(s, second) for second in fields.values()):
        found.append((f"{name}_2", s.model_copy(
            update={first: getattr(s, second) for first, second in fields.items()})))
    return found


def _provider_step(deps: ContactDeps, book: CreditBook, name: str, provider: Any,
                   company: str, domain: str, country: str, result: ContactsResult,
                   chosen: list[Chosen], seen: set[str],
                   by_email: dict[str, tuple[str, dict[str, Any]]],
                   open_slots: Callable[[], dict[str, int]], patterns: Any,
                   personal: Any) -> None:
    """One provider: its first account, and its second when the first has no credits left
    or its search fails (a refused key, an exhausted plan). Never both when the first
    answered: the second account sees the same people."""
    options = accounts(name, deps.s)
    for index, (account, s) in enumerate(options):
        label = account_label(account)
        has_next = index + 1 < len(options)
        if index:
            book.ensure(account, name)
        left = book.available(account)
        if left <= 0:
            result.notes.append(f"{account}: no credits left this month"
                                + (", trying the second account" if has_next else ", skipped"))
            continue
        if not paid_api_allowed(name, deps.s):
            s = s.model_copy(update=FIXTURE_KEYS)
        pdeps = ProviderDeps(s=s, request=deps.request(name),
                             search_titles=deps.s.contacts.get("search_titles") or {},
                             credits_left=left, patterns=patterns)
        deps.calls.append(account)
        found = provider.search(company, domain, country, open_slots(), pdeps)
        book.spend(account, found.credits_used)
        result.notes.extend(found.notes)
        if found.skipped_reason:
            failed_over = has_next and not found.candidates
            result.notes.append(found.skipped_reason
                                + (" (trying the second account)" if failed_over else ""))
            if failed_over:
                continue
        for candidate, kind, note in fill_slots(found.candidates, open_slots(), country,
                                                domain, seen, patterns, personal):
            picked = _chosen(candidate, kind, note, by_email, country)
            if index and not picked.cached:
                picked.notes = "; ".join(p for p in (picked.notes, f"found with {label}") if p)
            chosen.append(picked)
        return


GITHUB_SWITCH = "contacts.github"  # Notion Config: "on" reads the company's GitHub org first
GITHUB_ORGS = "contacts.github_orgs"  # Notion Config: "Company Name = org-login" lines


def github_org_known(deps: ContactDeps, config: ConfigStore, company: str) -> str | None:
    """A configured or remembered org login; "" when none was found before; None: unknown."""
    for line in (config.get(GITHUB_ORGS) or "").splitlines():
        name, eq, value = line.partition("=")
        if eq and canon_company(name) == canon_company(company) and value.strip():
            return value.strip()
    remembered = deps.state.get(f"github_org:{canon_company(company)}")
    return None if remembered is None else str(remembered.get("org") or "")


def _github_step(deps: ContactDeps, config: ConfigStore, company: str, domain: str,
                 country: str, result: ContactsResult, chosen: list[Chosen], seen: set[str],
                 by_email: dict[str, tuple[str, dict[str, Any]]],
                 open_slots: Callable[[], dict[str, int]], patterns: Any, personal: Any) -> None:
    """Engineers from the company's public GitHub organisation (free), before any paid
    lookup. Only people who published an email on the company domain; nobody outside the
    job's country (the paid providers may still find in-country people)."""
    pdeps = ProviderDeps(s=deps.s, request=deps.request("github"), search_titles={},
                         patterns=patterns)
    known = github_org_known(deps, config, company)
    deps.calls.append("github")
    try:
        org, _ = github.find_org(company, domain, pdeps, lambda name: known)
    except http.HttpError as exc:
        result.notes.append(f"github: {exc}")
        return
    if known is None:
        deps.state.set(f"github_org:{canon_company(company)}", {"org": org or ""})
    if not org:
        result.notes.append(f"github: no public organisation on {domain} for {company}")
        return
    most = int((deps.s.contacts.get("github") or {}).get("max_profiles",
                                                         github.DEFAULT_PROFILES))
    found = github.search(company, domain, org, pdeps, most)
    if found.skipped_reason:
        result.notes.append(found.skipped_reason)
    for candidate in found.candidates:
        key = candidate.email.strip().lower()
        if key in seen or not keep(candidate, domain, personal):
            continue
        where = country_ok(candidate, country)
        if where == "outside":
            continue
        kind = classify_title(candidate.title, patterns)
        if kind not in (RECRUITER, HIRING):
            kind = PEER  # a member of the company's engineering organisation
        if open_slots().get(kind, 0) <= 0:
            continue
        seen.add(key)
        note = "country unverified" if where == "unverified" else ""
        picked = _chosen(candidate, kind, note, by_email, country)
        if not picked.page_id:
            picked.notes = "; ".join(p for p in (note, f"public GitHub profile, org {org}") if p)
        chosen.append(picked)


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
    if (deps.state.get(OPEN_ASK) or {}).get("ref") == match.group(1):
        deps.state.delete(OPEN_ASK)
    return find_contacts(deps, asked["job_id"])

