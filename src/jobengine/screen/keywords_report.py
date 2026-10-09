"""LinkedIn keywords report (flow feature 8, 9 Oct): /keywords, and a monthly reminder in
the weekly digest.

Recruiters search LinkedIn by keywords. The report counts the tools your best jobs (Approved,
Applied, Screening, Interview, Offer; the newest MAX_JOBS) name in their descriptions, with
the free skill matcher, and shows the TOP most named ones:

- a tool you have that your profile text does not name: add it to your headline or skills;
- a tool you have that is on it: nothing to do;
- a tool you do not have: never on your profile; it is a gap (/gaps has the learning plan).

The bot never opens LinkedIn: your profile text is what you paste into Notion Config
`profile.linkedin_headline` and `profile.linkedin_skills` (and `profile.linkedin_about`).
No AI, nothing written.
"""

from __future__ import annotations

import re

BEST = ("Approved", "Applied", "Screening", "Interview", "Offer")
MAX_JOBS = 40
TOP = 15
PROFILE_KEYS = ("profile.linkedin_headline", "profile.linkedin_skills", "profile.linkedin_about")


def _norm(text: str) -> str:
    return " " + " ".join(re.sub(r"[^a-z0-9+#]+", " ", text.casefold()).split()) + " "


def on_profile(term: str, profile: str) -> bool:
    return bool(profile.strip()) and _norm(term) in _norm(profile)


def report(jobs: list[tuple[list[str], list[str]]], profile: str) -> str:
    """`jobs`: (tools you have, tools you lack) named by each job's description."""
    if not jobs:
        return ("No jobs to count yet: the report uses your Approved, Applied and Interview "
                "jobs with a description.")
    have: dict[str, int] = {}
    lack: dict[str, int] = {}
    for yours, missing in jobs:
        for term in dict.fromkeys(yours):
            have[term] = have.get(term, 0) + 1
        for term in dict.fromkeys(missing):
            lack[term] = lack.get(term, 0) + 1
    ranked = sorted([(n, t, True) for t, n in have.items()] +
                    [(n, t, False) for t, n in lack.items()],
                    key=lambda item: (-item[0], item[1]))[:TOP]
    lines = [f"LinkedIn keywords: the {len(ranked)} tools your {len(jobs)} best jobs name most "
             "(recruiters search by these words):"]
    add = []
    for i, (count, term, yours) in enumerate(ranked, 1):
        if not yours:
            note = "you do not have it: keep it off your profile (/gaps)"
        elif not profile.strip():
            note = "you have it"
        elif on_profile(term, profile):
            note = "on your profile"
        else:
            note = "you have it, NOT on your profile: add it"
            add.append(term)
        lines.append(f"{i}. {term}: {count} job{'s' if count != 1 else ''} ({note})")
    lines.append("")
    if not profile.strip():
        lines.append("Paste your LinkedIn headline and skills into Notion Config "
                     "profile.linkedin_headline and profile.linkedin_skills: the next report "
                     "then shows which of these are missing on your profile.")
    elif add:
        lines.append(f"Add to your LinkedIn headline or Skills: {', '.join(add)}. Then update "
                     "Config profile.linkedin_headline or profile.linkedin_skills too.")
    else:
        lines.append("Every tool you have from this list is on your profile.")
    return "\n".join(lines)
