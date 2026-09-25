"""Forward-only status rules (spec section 3). Pure.

A job moves only forward along JOB_ORDER, or into a terminal state from a non-terminal one.
A terminal job (Rejected, Ghosted, Withdrawn, Declined, Expired) is never changed here.
"""

from __future__ import annotations

from dataclasses import dataclass

from jobengine.track.models import (
    ACK,
    FOLLOWUP_SENT,
    INTERVIEW,
    NEUTRAL,
    OFFER,
    OPT_OUT,
    POSITIVE,
    REJECTION,
    SCREENING,
)

JOB_ORDER = ("New", "Screened", "Approved", "Resume built", "Applied", "Followed up", "Replied",
             "Screening", "Interview", "Offer")
JOB_TERMINAL = ("Rejected", "Ghosted", "Withdrawn", "Declined", "Expired")
# Never overwritten by the daily run (Offer is the last forward step, not terminal).
NEVER_CHANGED = (*JOB_TERMINAL, "Offer")
# Contacts: Ghosted can still turn into Replied (a late answer); Bounced and Do not contact
# are final.
CONTACT_ORDER = ("Unverified", "Verified", "Drafted", "Contacted", "Followed up", "Ghosted",
                 "Replied")
CONTACT_FINAL = ("Bounced", "Do not contact")
APPLIED_OR_LATER = JOB_ORDER[JOB_ORDER.index("Applied"):]
REPLIED_OR_BETTER = JOB_ORDER[JOB_ORDER.index("Replied"):]

# Reply class -> (job target, contact target). None: no change.
EFFECTS: dict[str, tuple[str | None, str | None]] = {
    POSITIVE: ("Replied", "Replied"),
    NEUTRAL: ("Replied", "Replied"),
    SCREENING: ("Screening", "Replied"),
    INTERVIEW: ("Interview", "Replied"),
    OFFER: ("Offer", "Replied"),
    REJECTION: ("Rejected", "Replied"),
    OPT_OUT: (None, "Do not contact"),
    ACK: (None, None),
    FOLLOWUP_SENT: ("Followed up", "Followed up"),
}
# Job targets that only apply from these statuses.
ONLY_FROM = {"Replied": ("Applied", "Followed up"), "Followed up": ("Applied",)}


def advance_job(current: str | None, target: str | None) -> str | None:
    """`target` when the move is allowed, else None."""
    if not target or current == target or current in NEVER_CHANGED:
        return None
    if target in ONLY_FROM and current not in ONLY_FROM[target]:
        return None
    if target in JOB_TERMINAL:
        return target
    if target not in JOB_ORDER:
        return None
    rank = JOB_ORDER.index(current) if current in JOB_ORDER else -1
    return target if JOB_ORDER.index(target) > rank else None


def advance_contact(current: str | None, target: str | None) -> str | None:
    if not target or current == target or current in CONTACT_FINAL:
        return None
    if target in CONTACT_FINAL:
        return target
    if target == "Ghosted" and current != "Followed up":
        return None
    rank = CONTACT_ORDER.index(current) if current in CONTACT_ORDER else -1
    return target if target in CONTACT_ORDER and CONTACT_ORDER.index(target) > rank else None


@dataclass(frozen=True)
class Effect:
    job_status: str | None  # the new job Status, or None
    contact_status: str | None
    touch: bool  # set Last activity date (every applied event)


def effect(kind: str, job_status: str | None, contact_status: str | None = None,
           cold_mail: bool = True) -> Effect:
    """What an event does. `cold_mail` False: a job-level thread, no contact involved."""
    job_target, contact_target = EFFECTS.get(kind, (None, None))
    new_job = advance_job(job_status, job_target)
    new_contact = advance_contact(contact_status, contact_target) if cold_mail else None
    applied = kind in EFFECTS and (new_job is not None or new_contact is not None or kind == ACK)
    return Effect(job_status=new_job, contact_status=new_contact, touch=applied)
