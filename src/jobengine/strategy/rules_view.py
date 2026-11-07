"""/rules text (spec section 5) and the V16 non-negotiables reader. Read only."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from jobengine.config_store import ConfigStore
from jobengine.contacts.classify import parse_mix
from jobengine.sweep.gate import last_update, strategy_gate

V16_URL = "https://app.notion.com/p/3e36edc2b0d481348577f84b2c39d066"
HEADINGS = ("heading_1", "heading_2", "heading_3")
BULLETS = ("bulleted_list_item", "numbered_list_item")
MAX_RULES = 8
MAX_ADOPTED = 10
DEFAULT_COUNTRIES = ("Poland", "Netherlands", "Ireland")


def _text(block: dict[str, Any]) -> str:
    inner = block.get(block.get("type") or "") or {}
    return "".join(p.get("plain_text") or (p.get("text") or {}).get("content", "")
                   for p in inner.get("rich_text") or []).strip()


def non_negotiables(blocks: list[dict[str, Any]], limit: int = MAX_RULES) -> list[str]:
    """The first `limit` bullets under the V16 "Non-negotiables" heading."""
    rules: list[str] = []
    inside = False
    for block in blocks:
        kind = block.get("type")
        if kind in HEADINGS:
            if inside:
                break
            inside = "non-negotiable" in _text(block).casefold()
        elif inside and kind in BULLETS and _text(block):
            rules.append(_text(block))
    return rules[:limit]


def adopted_tips(rows: list[tuple[str, dict[str, Any]]], limit: int = MAX_ADOPTED) -> list[str]:
    adopted = [v for _, v in rows if v.get("Status") == "Adopted" and v.get("Tip / rule")]
    adopted.sort(key=lambda v: v.get("Date added") or date.min, reverse=True)
    return [v["Tip / rule"] for v in adopted[:limit]]


def gate_line(config: ConfigStore, today: date, state: Any, s: Any) -> str:
    blocked = strategy_gate(config, today, state, s)
    if blocked:
        return blocked
    updated = last_update(config, state, s)
    days = config.get_int("strategy_refresh_days") or 30
    assert updated is not None  # the gate is open, so the date exists
    return (f"/fetch is open until {(updated + timedelta(days=days)).isoformat()} "
            f"(strategy last updated {updated.isoformat()}).")


def target_line(s: Any) -> str:
    """Where you look (your decision of 23 Oct): the countries of the Apply pack."""
    countries = list(((getattr(s, "apply_pack", None) or {}).get("countries") or {}).keys())
    names = countries or list(DEFAULT_COUNTRIES)
    where = (", ".join(names[:-1]) + " and " + names[-1]) if len(names) > 1 else names[0]
    return f"Targets: {where} only, office or hybrid only (remote jobs are skipped)."


def screening_lines(config: ConfigStore, s: Any) -> list[str]:
    """The screening thresholds (Phase 22 Q-08: /rules named none of them)."""
    screening = getattr(s, "screening", None) or {}
    sweep = getattr(s, "sweep", None) or {}
    years = int(screening.get("max_years_required", 4))
    match = float(sweep.get("min_skill_match", 0.6))
    terms = int(sweep.get("min_skill_terms", 4))
    age = int(sweep.get("max_posted_age_days", 2))
    limit = int(screening.get("daily_limit", 50))
    approvals = config.get_int("screening.autopilot_approvals",
                               int(screening.get("autopilot_approvals", 10)))
    return [
        f"- years asked: at most {years} (a range counts by its lower number)",
        f"- skill match: at least {round(match * 100)}% of the tools a job names are yours "
        f"(jobs naming fewer than {terms} tools are kept)",
        f"- posted in the last {age + 1} days (jobs without a date are kept)",
        f"- screening: at most {limit} jobs a day by the AI",
        f"- autopilot approvals: at most {approvals} a day",
    ]


def rules_text(config: ConfigStore, blocks: list[dict[str, Any]],
               rows: list[tuple[str, dict[str, Any]]], today: date, state: Any, s: Any,
               resume: dict[str, Any] | None = None) -> str:
    resume = resume or {}
    mix = parse_mix(config.get("contacts.mix"), config.get("contacts.per_job"))
    fill_min = config.get("resume.fill_min") or resume.get("fill_min", 88)
    fill_max = config.get("resume.fill_max") or resume.get("fill_max", 96)
    lines = [f"V16 rules: {V16_URL}", target_line(s), "Non-negotiables:"]
    lines += [f"- {rule}" for rule in non_negotiables(blocks)] or ["- (none found on the page)"]
    lines += [
        "Thresholds:",
        f"- follow-up after {config.get('followup.days') or 7} days, ghosted after "
        f"{config.get('ghosted.days') or 14} more",
        f"- strategy refresh every {config.get('strategy_refresh_days') or 30} days",
        f"- resume fill {fill_min} to {fill_max}%",
        f"- contacts per job: {mix.peer} peer, {mix.hiring} hiring, {mix.recruiter} recruiter",
        *screening_lines(config, s),
        "Adopted strategy tips:",
    ]
    lines += [f"- {tip}" for tip in adopted_tips(rows)] or ["- none yet"]
    lines.append(gate_line(config, today, state, s))
    return "\n".join(lines)
