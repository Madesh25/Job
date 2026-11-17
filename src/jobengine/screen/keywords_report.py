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

Practices are listed apart (Phase 9 P9-13, 26 Oct): "incident management", "automation" or
"CI/CD pipelines" are ways of working, not tools; recruiters search them too, so they come
after the tools as words for your About text.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

BEST = ("Approved", "Applied", "Screening", "Interview", "Offer")
MAX_JOBS = 40
TOP = 15
PROFILE_KEYS = ("profile.linkedin_headline", "profile.linkedin_skills", "profile.linkedin_about")
TOP_PRACTICES = 8
# A word that makes a term a way of working, not a tool you install or use.
PRACTICE_WORDS = re.compile(
    r"\b(management|analysis|automation|automating|response|incident|incidents|on-?call|"
    r"monitoring|observability|troubleshooting|pipelines?|practices?|delivery|deployments?|"
    r"design|architecture|agile|scrum|kanban|culture|collaboration|communication|mentoring|"
    r"as code|reliability|security|networking|scalability|performance|testing|governance|"
    r"compliance|optimi[sz]ation|cost|disaster recovery|backup|high availability|devops|"
    r"gitops|sre|root cause|postmortems?|continuous|scanning)\b", re.I)
# Names that hold such a word but are tools: kept with the tools.
TOOL_NAMES = re.compile(r"\b(aws|azure|gcp|google|amazon|gitlab|github|jenkins|argo|grafana|"
                        r"prometheus|datadog|splunk|dynatrace|new relic|elastic|opsgenie|"
                        r"pagerduty|codepipeline|codedeploy|cloudwatch|sentry|sonarqube|"
                        r"snyk|vault|kyverno|trivy)\b", re.I)


def is_practice(term: str) -> bool:
    """"incident management", "CI/CD pipelines", "Infrastructure as Code": a way of working.
    "Azure DevOps Pipelines", "GCP Monitoring", "GitLab CI": tools."""
    return bool(PRACTICE_WORDS.search(term)) and not TOOL_NAMES.search(term)


def _norm(text: str) -> str:
    return " " + " ".join(re.sub(r"[^a-z0-9+#]+", " ", text.casefold()).split()) + " "


def on_profile(term: str, profile: str) -> bool:
    return bool(profile.strip()) and _norm(term) in _norm(profile)


def aliases(term_map: Iterable[Any]) -> dict[str, str]:
    """Names in one Active Term Map row count as one entry (23 Oct, P9-13: "rca" and "root
    cause analysis" were listed apart): lower-case name -> the row's names joined by " / "."""
    out: dict[str, str] = {}
    for row in term_map:
        terms = [t.strip() for t in getattr(row, "jd_terms", ()) if t.strip()]
        if len(terms) > 1 and getattr(row, "backs", False):
            label = " / ".join(terms)
            for term in terms:
                out.setdefault(term.lower(), label)
    return out


def report(jobs: list[tuple[list[str], list[str]]], profile: str,
           same: Mapping[str, str] | None = None) -> str:
    """`jobs`: (tools you have, tools you lack) named by each job's description. `same`
    (aliases()) merges the names of one Term Map row into one entry."""
    same = same or {}
    members: dict[str, list[str]] = {}
    for term, label in same.items():
        members.setdefault(label, []).append(term)

    def merged(terms: list[str]) -> list[str]:
        return list(dict.fromkeys(same.get(t.lower(), t) for t in terms))

    jobs = [(merged(yours), merged(missing)) for yours, missing in jobs]
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
    every = sorted([(n, t, True) for t, n in have.items()] +
                   [(n, t, False) for t, n in lack.items()],
                   key=lambda item: (-item[0], item[1]))
    ranked = [item for item in every if not is_practice(item[1])][:TOP]
    practices = [item for item in every if is_practice(item[1])][:TOP_PRACTICES]
    lines = [f"LinkedIn keywords: the {len(ranked)} tools your {len(jobs)} best jobs name most "
             "(recruiters search by these words):"]
    add = []
    for i, (count, term, yours) in enumerate(ranked, 1):
        if not yours:
            note = "you do not have it: keep it off your profile (/gaps)"
        elif not profile.strip():
            note = "you have it"
        elif any(on_profile(t, profile) for t in members.get(term, [term])):
            note = "on your profile"
        else:
            note = "you have it, NOT on your profile: add it"
            add.append(term)
        lines.append(f"{i}. {term}: {count} job{'s' if count != 1 else ''} ({note})")
    if practices:
        lines += ["", "Ways of working they also name (not tools: good words for your LinkedIn "
                      "About text):"]
        lines.append("; ".join(
            f"{term} {count} ({'you have it' if yours else 'not yours yet'})"
            for count, term, yours in practices))
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
