"""Data carried through a contact lookup."""

from __future__ import annotations

from dataclasses import dataclass, field

PEER, HIRING, RECRUITER, OTHER = "Peer engineer", "Hiring", "Recruiter/TA", "Other"
SLOT_TYPES = {"peer": PEER, "hiring": HIRING, "recruiter": RECRUITER}
TYPE_ORDER = (PEER, HIRING, RECRUITER)
NOTION_COUNTRIES = ("Poland", "Netherlands", "Ireland")


@dataclass(frozen=True)
class Candidate:
    """A person from a provider or the job description, before filtering."""

    name: str
    first_name: str
    title: str
    email: str
    country: str | None
    provider_verified: bool
    source: str  # Apollo | Hunter | Snov | Job posting
    country_unverified: bool = False
    person_id: str | None = None  # provider id, for a later reveal


@dataclass
class ProviderResult:
    candidates: list[Candidate] = field(default_factory=list)
    credits_used: int = 0
    skipped_reason: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Mix:
    peer: int = 2
    hiring: int = 1
    recruiter: int = 1
    total: int = 4
    note: str | None = None

    def slots(self) -> dict[str, int]:
        return {PEER: self.peer, HIRING: self.hiring, RECRUITER: self.recruiter}


@dataclass
class Chosen:
    """A contact chosen for the job: a cached row, or a new one to write."""

    name: str
    title: str
    email: str
    type: str
    source: str
    country: str
    verified: bool = False
    notes: str = ""
    page_id: str | None = None  # set for cached rows (and after writing)
    cached: bool = False

    def label(self) -> str:
        if self.cached:
            tag = "cache"
        else:
            tag = f"{self.source}, verified" if self.verified else self.source
        title = f" ({self.title})" if self.title else ""
        return f"{self.type}: {self.name}{title} [{tag}]"


@dataclass
class ContactsResult:
    job_id: str
    company: str
    role: str
    country: str
    target: int
    contacts: list[Chosen] = field(default_factory=list)
    missing: dict[str, int] = field(default_factory=dict)
    credits_line: str = ""
    notes: list[str] = field(default_factory=list)
    waiting_for_domain: bool = False
    status: str = "done"  # done | waiting_domain | refused | failed
    message: str = ""
