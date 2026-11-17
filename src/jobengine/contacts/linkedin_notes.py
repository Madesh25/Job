"""LinkedIn message drafts per contact (flow feature 12, 9 Oct).

The bot never opens LinkedIn and never sends anything there. Under a job's contacts the
"LinkedIn notes" button gives, for each contact, a connection note of at most NOTE_LIMIT
characters (LinkedIn's limit) to copy, and the name and company to search yourself:
engineers get a referral ask, recruiters and hiring managers a short interest note. The
wording comes from config/base.yaml `linkedin_notes` (a Notion Config key `linkedin.note_<type>`
wins), filled with the contact's first name, the role, the company, the job link (tracking
parts removed; a job board's own link becomes "on your careers page") and {applied} ("I
applied for" once the job is Applied, else "I am applying for"); it is cut at a word end to
fit. No AI is used.
"""

from __future__ import annotations

from typing import Any

from jobengine.contacts.models import HIRING, PEER, RECRUITER

NOTE_LIMIT = 300
TYPE_KEYS = {PEER: "peer", RECRUITER: "recruiter", HIRING: "hiring"}
# {applied}: "I applied for" once the job is Applied, else "I am applying for" (Phase 12,
# 28 Oct: the notes said "I applied" for jobs only Approved).
DEFAULTS = {
    "peer": ("Hi {first}, {applied} the {role} role at {company} ({link}). I work as a "
             "DevOps engineer on Kubernetes, Terraform and AWS. Would you be open to referring "
             "me or sharing how the team works? Thank you, {me}"),
    "recruiter": ("Hi {first}, {applied} the {role} role at {company} ({link}) and would "
                  "be glad to talk. I am a DevOps engineer ready to relocate. Thank you, {me}"),
    "hiring": ("Hi {first}, {applied} the {role} role on your team at {company} ({link}). "
               "I would welcome a short chat about the role. Thank you, {me}"),
}
APPLIED, APPLYING = "I applied for", "I am applying for"
NO_LINK = "on your careers page"
# Job boards whose links are not the company's page: the note says NO_LINK instead.
BOARD_HOSTS = ("adzuna.", "jooble.", "indeed.", "glassdoor.", "nofluffjobs.", "justjoin.",
               "pracuj.", "irishjobs.", "jobs.ie", "iamexpat.")


def note_link(url: str | None) -> str:
    """The job link for a note: tracking parts removed; "" for a job board's own page."""
    from urllib.parse import urlsplit

    from jobengine.mail.fill import job_link

    link = job_link(url or "")
    host = (urlsplit(link).hostname or "").lower()
    return "" if not link or any(b in host for b in BOARD_HOSTS) else link


def applied_words(status: str | None) -> str:
    return APPLIED if status in ("Applied", "Interview", "Offer") else APPLYING


def _fit(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= NOTE_LIMIT:
        return text
    cut = text[:NOTE_LIMIT]
    return cut[:cut.rfind(" ")].rstrip(",;") if " " in cut else cut


def note(template: str, *, name: str, role: str, company: str, link: str, me: str,
         status: str | None = None) -> str:
    first = (name or "there").split()[0]
    link = note_link(link)
    words = {"first": first, "role": role, "company": company, "me": me,
             "applied": applied_words(status)}
    text = template.format(link=link or NO_LINK, **words)
    if len(" ".join(text.split())) > NOTE_LIMIT and link:  # the link is the longest part
        text = template.format(link=NO_LINK, **words)
    return _fit(text)


def notes(contacts: list[dict[str, Any]], *, role: str, company: str, link: str, me: str,
          templates: dict[str, str], status: str | None = None) -> str:
    """The message: one block per contact with a type that has a template."""
    blocks = []
    for c in contacts:
        kind = TYPE_KEYS.get(c.get("Type") or "")
        name = c.get("Name") or ""
        if not kind or not name:
            continue
        text = note(templates.get(kind) or DEFAULTS[kind], name=name, role=role,
                    company=company, link=link, me=me, status=status)
        blocks.append(f"{name} ({c.get('Type')}), search \"{name} {company}\" on LinkedIn:\n"
                      f"{text}\n({len(text)} characters)")
    if not blocks:
        return (f"No engineer, recruiter or hiring manager among the contacts of {company}, "
                f"{role}: no LinkedIn notes.")
    head = (f"LinkedIn notes for {company}, {role} (copy one into \"Add a note\" when you "
            f"connect; you send it yourself, at most {NOTE_LIMIT} characters):")
    return "\n\n".join([head, *blocks])
