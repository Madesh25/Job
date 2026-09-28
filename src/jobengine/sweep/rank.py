"""Free match score for new postings (no LLM): which new jobs are worth saving today.

The sweep keeps only the best `sweep.daily_new_limit` new jobs per day; screening (the LLM
step) then reads just those. Points, highest first:

- each owned term (Production or Hands-on skill, Active Term Map row) named in the title or
  description: +3, Production skills +1 more, at most 8 terms
- each known gap (Term Map "(none)" row) named: -2
- Target Companies: tier 1 to 4 or 6 +6, other listed companies +3
- seniority from the title: Mid +3, Unknown +1, Senior -2
- years required: 2 to 4 +2, more than 5 -8
- Polish or Dutch stated as required: -6
- posted in the last 3 days +2, last 7 days +1, older than 21 days -2
- a full description (not a snippet): +1

`match()` lists the tools a job names that you have and do not have; the sweep drops a job
whose share of your tools is below `sweep.min_skill_match` (sweep/runner.py).
"""

from __future__ import annotations

import re
from datetime import date

from jobengine.reference import OWNED_LEVELS, Reference, canon_term
from jobengine.sweep.models import Job
from jobengine.sweep.normalize import canon

MAX_TERMS = 8
TOP_TIERS = (1, 2, 3, 4, 6)
SENIORITY_POINTS = {"Mid": 3, "Unknown": 1, "Junior": 0, "Senior": -2, "Lead": -8}
LANGUAGE_REQUIRED = re.compile(
    r"\b(?:fluent|native|required|mandatory|must|excellent|proficient)\W+(?:\w+\W+){0,3}?"
    r"(?:polish|dutch)\b|\b(?:polish|dutch)\W+(?:\w+\W+){0,3}?"
    r"(?:required|mandatory|is a must|needed)\b"
)


# Tools and languages that jobs name, in canonical form. Those you do not own (Skills
# Inventory, Active Term Map) count against the skill match; owned ones count for it.
COMMON_TECH = frozenset(t.strip() for t in """
    java|spring|spring boot|hibernate|maven|gradle|scala|kotlin|c#|dotnet|net core|c++|
    ruby|rails|php|laravel|node js|nodejs|javascript|typescript|react|angular|vue|django|
    flask|fastapi|kafka|rabbitmq|openshift|vmware|vsphere|nutanix|hyper v|citrix|sccm|
    intune|active directory|windows server|macos|jamf|sap|salesforce|oracle|mainframe|
    cobol|tableau|power bi|splunk|dynatrace|datadog|new relic|puppet|chef|saltstack|bamboo|
    teamcity|jenkins|azure devops|bicep|arm templates|powershell|azure cli|cloudformation|
    pulumi|nomad|consul|istio|openstack|ceph|elasticsearch|mongodb|cassandra|redis|
    postgresql|mysql|sql server|snowflake|databricks|spark|hadoop|airflow|golang|rust|perl|
    selinux|servicenow|aws|azure|gcp|terraform|ansible|kubernetes|docker|helm|argo cd|
    prometheus|grafana|gitlab ci|github actions|python|bash|linux
""".split("|")) - {""}


def _longest(terms: list[str]) -> list[str]:
    """Named terms without those inside a longer one: "spring boot" counts once, not also as
    "spring"."""
    return sorted(t for t in terms if not any(t != o and f" {t} " in f" {o} " for o in terms))


class Ranker:
    """Scores new postings against the reference data. Build once per sweep."""

    def __init__(self, ref: Reference):
        self.ref = ref
        production = ref.skill_terms(("Production",))
        owned = ref.skill_terms(OWNED_LEVELS)
        for row in ref.term_map:
            if row.backs:
                owned.update(canon_term(t) for t in row.jd_terms)
                owned.add(canon_term(row.truthful_equivalent))
        gaps = {canon_term(t) for row in ref.term_map if row.is_gap for t in row.jd_terms}
        self.owned = {t for t in owned if t}
        self.production = {t for t in production if t}
        self.gaps = {t for t in gaps if t and t not in self.owned}
        self.not_owned = (self.gaps | {canon_term(t) for t in COMMON_TECH}) - self.owned

    def match(self, job: Job) -> tuple[list[str], list[str]]:
        """(your tools the job names, tools it names that you do not have)."""
        text = f" {canon(job.role)} {canon(job.description or '')} "
        have = _longest([t for t in self.owned if f" {t} " in text])
        missing = _longest([t for t in self.not_owned if f" {t} " in text])
        return have, missing

    def score(self, job: Job, today: date) -> int:
        text = f" {canon(job.role)} {canon(job.description or '')} "
        points = 0
        named = [t for t in self.owned if f" {t} " in text][:MAX_TERMS]
        points += sum(4 if t in self.production else 3 for t in named)
        points -= 2 * sum(1 for t in self.gaps if f" {t} " in text)

        company = self.ref.company(job.company)
        if company:
            points += 6 if company.tier_number in TOP_TIERS else 3

        points += SENIORITY_POINTS.get(job.seniority, 0)
        years = job.years_required
        if years is not None:
            points += 2 if 2 <= years <= 4 else -8 if years > 5 else 0
        if LANGUAGE_REQUIRED.search(text):
            points -= 6
        if job.posted_date:
            age = (today - job.posted_date).days
            points += 2 if age <= 3 else 1 if age <= 7 else -2 if age > 21 else 0
        if job.description and not job.description_is_snippet:
            points += 1
        return points
