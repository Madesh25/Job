"""Learning plan under /gaps (flow feature 14, 9 Oct).

Which certificate closes the most of your gaps: every certificate in config/base.yaml
`learning.certificates` lists the skills it teaches (as job descriptions name them); it is
scored by the number of jobs with at least one gap it covers. The top ones are shown with
the gaps they cover; gaps no certificate covers are listed apart (learn them with a small
project, then add them to Skills Inventory). No AI, nothing written.
"""

from __future__ import annotations

import re

MAX_PLAN = 3
MAX_UNCOVERED = 6


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9+#]+", " ", text.casefold()).split())


def covers(skill: str, gap: str) -> bool:
    """True when a certificate's skill names the gap: "Azure" covers "Azure DevOps"."""
    s, g = _norm(skill), _norm(gap)
    if not s or not g:
        return False
    return s == g or (len(s) >= 3 and re.search(rf"(?<!\S){re.escape(s)}(?!\S)", g) is not None)


def plan(jobs: list[list[str]], certificates: dict[str, list[str]]) -> list[str]:
    """The lines under /gaps: `jobs` is each job's gaps."""
    if not jobs or not certificates:
        return []
    scored = []
    for name, skills in certificates.items():
        closed, named = 0, {}
        for gaps in jobs:
            hit = [g for g in dict.fromkeys(gaps) if any(covers(s, g) for s in skills or [])]
            if hit:
                closed += 1
                for g in hit:
                    label, count = named.get(g.casefold(), (g, 0))
                    named[g.casefold()] = (label, count + 1)
        if closed:
            top = sorted(named.values(), key=lambda kv: (-kv[1], kv[0].casefold()))
            scored.append((closed, name, [g for g, _ in top[:4]]))
    scored.sort(key=lambda item: (-item[0], item[1]))
    lines = ["", "Learning plan (the certificate that covers gaps in the most jobs first):"]
    if not scored:
        lines.append("- no certificate in learning.certificates covers your gaps.")
    for n, (closed, name, gaps) in enumerate(scored[:MAX_PLAN], 1):
        jobs_word = f"{closed} job{'s' if closed != 1 else ''}"
        lines.append(f"{n}. {name}: {jobs_word} ({', '.join(gaps)})")
    every = [s for skills in certificates.values() for s in skills or []]
    uncovered: dict[str, tuple[str, int]] = {}
    for gaps in jobs:
        for g in dict.fromkeys(gaps):
            if not any(covers(s, g) for s in every):
                name, count = uncovered.get(g.casefold(), (g, 0))
                uncovered[g.casefold()] = (name, count + 1)
    if uncovered:
        top = sorted(uncovered.values(), key=lambda kv: (-kv[1], kv[0].casefold()))
        shown = ", ".join(f"{g} ({c})" for g, c in top[:MAX_UNCOVERED])
        lines.append(f"No certificate covers: {shown}. Learn them with a small project.")
    lines.append("Passed one already? It is not counted as a skill until you add its skills "
                 "to Skills Inventory.")
    return lines
