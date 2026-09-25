"""Texts for /followups, /status, /sources and /health (spec section 6.2)."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

from jobengine.config_store import ConfigStore
from jobengine.settings import Settings
from jobengine.sweep.runner import LAST_SUMMARY as SWEEP_SUMMARY
from jobengine.track.followup import NOTE as FOLLOWUP_NOTE
from jobengine.track.runner import MARKER, TrackDeps

SOURCE_SECRETS = {"gmail": ("GMAIL_ALERTS_TOKEN_JSON",), "adzuna": ("ADZUNA_APP_ID",
                                                                     "ADZUNA_APP_KEY"),
                  "ats": ()}


def _day(value: Any) -> date | None:
    return value.date() if isinstance(value, datetime) else value if isinstance(value, date) \
        else None


def followup_days(deps: TrackDeps, config: ConfigStore) -> int:
    default = int(deps.s.tracking.get("followup_days", 7))
    return config.get_int("followup.days", default) or default


def unsent_drafts(contacts: list[dict[str, Any]]) -> tuple[int, int]:
    drafts = sum(1 for c in contacts if c.get("Status") == "Drafted")
    followups = sum(1 for c in contacts if c.get("Follow-up draft ID")
                    and c.get("Status") in ("Contacted", "Followed up"))
    return drafts, followups


def followups_due(contacts: list[dict[str, Any]], today: date, days: int,
                  ahead: int) -> list[tuple[date, dict[str, Any]]]:
    """Contacted people without a follow-up yet whose follow-up is due by today + ahead."""
    out = []
    for c in contacts:
        last = _day(c.get("Last contacted"))
        if c.get("Status") != "Contacted" or last is None or c.get("Follow-up draft ID"):
            continue
        if FOLLOWUP_NOTE in (c.get("Notes") or ""):
            continue
        due = last + timedelta(days=days)
        if due <= today + timedelta(days=ahead):
            out.append((due, c))
    return sorted(out, key=lambda x: x[0])


def followups_text(deps: TrackDeps, today: date) -> str:
    contacts = [v for _, v in deps.contacts.all_rows()] if deps.contacts else []
    config = deps.config()
    drafted = [c for c in contacts if c.get("Follow-up draft ID")
               and c.get("Status") in ("Contacted", "Followed up")]
    lines = ["Follow-up drafts waiting in Gmail (send them yourself):"]
    lines += [f"- {c.get('Name')} ({c.get('Company')})" for c in drafted] or ["- none"]
    lines.append("Follow-ups due now or in the next 3 days (drafted by the daily check):")
    due = followups_due(contacts, today, followup_days(deps, config), 3)
    lines += [f"- {c.get('Name')} ({c.get('Company')}): due {when.isoformat()}"
              for when, c in due] or ["- none"]
    return "\n".join(lines)


def _counts(values: list[dict[str, Any]]) -> str:
    counter = Counter(v.get("Status") or "empty" for v in values)
    return ", ".join(f"{k} {n}" for k, n in sorted(counter.items(), key=lambda kv: -kv[1])) \
        or "none"


def status_lines(deps: TrackDeps) -> str:
    jobs = [v for _, v in deps.jobs.query_rows()] if deps.jobs else []
    contacts = [v for _, v in deps.contacts.all_rows()] if deps.contacts else []
    drafts, followups = unsent_drafts(contacts)
    last = (deps.state.get(MARKER) or {}).get("at") or "never"
    return "\n".join([
        f"Jobs by Status: {_counts(jobs)}",
        f"Contacts by Status: {_counts(contacts)}",
        f"Drafts waiting: {drafts} cold mails, {followups} follow-ups",
        f"Last daily check: {last}",
    ])


def _secret_set(s: Settings, var: str) -> bool:
    from jobengine.settings import SECRET_VARS

    return bool(getattr(s, SECRET_VARS[var]))


def sources_text(deps: TrackDeps) -> str:
    s = deps.s
    last = deps.state.get(SWEEP_SUMMARY) or {}
    counts = last.get("sources") or {}
    lines = [f"Sweep sources (last sweep: {last.get('at') or 'never'}):"]
    for name, secrets in SOURCE_SECRETS.items():
        enabled = (s.sweep.get(name) or {}).get("enabled", True)
        if secrets:
            secret = "secret set" if all(_secret_set(s, v) for v in secrets) else \
                f"missing {', '.join(v for v in secrets if not _secret_set(s, v))}"
        else:
            secret = "no secret needed"
        found = counts.get(name, "-")
        lines.append(f"{name}: {'enabled' if enabled else 'disabled'}, {secret}, last sweep "
                     f"{found} jobs")
    if last:
        lines.append(f"Last sweep: {last.get('new', 0)} new, {last.get('updated', 0)} updated")
    return "\n".join(lines)


def strategy_line(config: ConfigStore, today: date) -> str:
    updated = config.get_date_prefix("last_strategy_update")
    days = config.get_int("strategy_refresh_days")
    if updated is None or days is None:
        return "Strategy gate: /fetch is blocked (Config last_strategy_update missing)."
    left = days - (today - updated).days
    if left < 0:
        return "Strategy gate: /fetch is blocked. Run /update."
    return f"Strategy gate: /fetch stays open for {left} more days (then run /update)."


def health_text(deps: TrackDeps) -> str:
    s = deps.s
    try:
        deps.config()
        notion = "Notion: OK"
    except Exception as exc:  # report, never raise
        notion = f"Notion: FAILED ({exc})"
    lines = [notion]
    for name in ("alerts", "sender"):
        reason = deps.token_check(name)
        lines.append(f"Gmail {name} token: {'OK' if reason is None else f'FAILED ({reason})'}")
    lines.append(f"Anthropic key: {'set' if s.anthropic_api_key else 'missing'}")
    lines.append(f"Last daily check: {(deps.state.get(MARKER) or {}).get('at') or 'never'}")
    lines.append(f"Last sweep: {(deps.state.get(SWEEP_SUMMARY) or {}).get('at') or 'never'}")
    lines.append(f"Environment: {s.app_env}, DRY_RUN {'on' if s.dry_run else 'off'}")
    return "\n".join(lines)
