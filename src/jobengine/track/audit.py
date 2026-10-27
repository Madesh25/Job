"""The weekly self-audit (27 Oct, your request): where the job search went wrong this week,
what works, and what keeps being done without results, as one Notion page a week.

It runs every Sunday from AT (India time), after the digest, or on the first day after when
the bot was off; it cannot be switched off. The page is written to the Weekly Audit table and
ends with a prompt to give Opus. You review it, set its Status and write the Actions you
decide; the next audit reports what changed after them. The morning check names an audit
that is missing or still not reviewed.

Everything here is counted from your own data, with no AI: Job Opportunities, Contacts, the
day ledger (what the bot did each day), the warnings and errors the bot logged
(track/issue_log.py), your spot check notes, the Auto-apply issues and the earlier audits.

Reply rates are read on a "settled" group: jobs applied 7 to 35 days ago, because replies
take a week or more; this week's applications are too fresh to judge.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from jobengine.track.stats import CONTACTED, Funnel, pct

DONE_KEY = "audit.done"  # bot_state: {"week": <iso Monday of the last audited week>}
WEEKDAY = 6  # Sunday
AT = time(20, 0)  # with the digest (Cloud Scheduler je-digest-prod, Sunday 20:00)
REVIEW_DAYS = 3  # an audit still "New" this many days later is named in the morning check
SETTLED = (7, 35)  # days ago: the applications whose replies can be judged
REPEAT_DAYS = 28
REPEAT_MIN = 8  # applications in a group before "no replies at all" counts
MIN_SAMPLE = 5  # applications in a group before its reply rate is compared
NOT_APPLIED_DAYS = 2
FAST_REJECT_DAYS = 3
TOP_ISSUES = 10
RATE_STEP = 0.02  # a change of 2 points in the reply rate counts as better or worse
NEW, REVIEWED, ACTIONS_SET = "New", "Reviewed", "Actions set"
LEVELS = ("high", "medium", "info")
ROLE_WORDS = (("sre", r"\bsre\b|site reliability"), ("platform", r"platform"),
              ("devops", r"devops|dev ops"), ("cloud", r"cloud"),
              ("infrastructure", r"infra"), ("kubernetes", r"kubernetes|k8s"))
SPLITS = ("Country", "Board", "Screen verdict", "Sponsorship", "Role family")
OPUS_PROMPT = (
    "You are reviewing one week of an automated job search for a DevOps engineer in India "
    "who applies to jobs in Poland, the Netherlands and Ireland (needs visa sponsorship; "
    "office or hybrid only). The bot finds jobs, screens them, builds a resume per job, finds "
    "contacts and writes cold and referral mails. Using only the numbers on this page (and "
    "earlier weeks if I paste them): 1) name the 3 biggest problems, most costly first, with "
    "the number that shows each; 2) for each, the most likely cause and the smallest change "
    "to test next week, with the number that would show it worked; 3) what to stop doing "
    "because it keeps failing; 4) what is working and should get more effort. Say when the "
    "data is too small to judge. Do not invent numbers.")


@dataclass
class Finding:
    level: str  # "high", "medium", "info"
    text: str

    def line(self) -> str:
        mark = {"high": "🔴", "medium": "🟠"}.get(self.level, "🔵")
        return f"{mark} {self.text}"


@dataclass
class AuditInput:
    today: date
    jobs: list[dict[str, Any]]
    contacts: list[dict[str, Any]]
    ledger: Mapping[str, Mapping[str, Any]]  # iso day -> the day ledger entry
    issues: Mapping[str, int]  # issue_log patterns of this week
    previous: list[dict[str, Any]] = field(default_factory=list)  # earlier audits, newest first
    spot_notes: list[dict[str, str]] = field(default_factory=list)
    form_issues: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Audit:
    monday: date
    sunday: date
    title: str
    props: dict[str, Any]
    sections: list[tuple[str, list[str]]]
    summary: str

    def text(self) -> str:
        out = [self.title]
        for head, lines in self.sections:
            out += ["", head, *lines]
        return "\n".join(out)


# ---------------------------------------------------------------- schedule


def week_bounds(today: date) -> tuple[date, date]:
    """Monday and Sunday of the audited week: the week that ends on the last Sunday on or
    before `today`."""
    sunday = today - timedelta(days=(today.weekday() - WEEKDAY) % 7)
    return sunday - timedelta(days=6), sunday


def due(state: Any, now: datetime) -> bool:
    """From Sunday AT; on a later day when that week has no audit yet. The very first audit
    waits for a Sunday evening (no audit was missed before there was one)."""
    monday, _sunday = audited_week(now)
    done = ((state.get(DONE_KEY) if state is not None else None) or {}).get("week")
    if done is None:
        return now.weekday() == WEEKDAY and now.time() >= AT
    return done < monday.isoformat()


def audited_week(now: datetime) -> tuple[date, date]:
    monday, sunday = week_bounds(now.date())
    if now.date() == sunday and now.time() < AT:
        return monday - timedelta(days=7), sunday - timedelta(days=7)
    return monday, sunday


def mark_done(state: Any, monday: date) -> None:
    if state is not None:
        state.set(DONE_KEY, {"week": monday.isoformat()})


def morning_lines(state: Any, now: datetime,
                  rows: Iterable[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """(blockers, needs you soon) for the morning check: a missing audit is a blocker (it is
    compulsory); an audit still New after REVIEW_DAYS needs you."""
    blockers, soon = [], []
    monday, _sunday = audited_week(now)
    done = ((state.get(DONE_KEY) if state is not None else None) or {}).get("week")
    if done is not None and now.date() > monday + timedelta(days=7) \
            and done < monday.isoformat():
        blockers.append(f"The weekly audit for the week of {monday:%d %b} is missing. Fix: "
                        "send /audit.")
    for row in rows:
        start = _day(row.get("Week start"))
        if start is None or (row.get("Status") or NEW) != NEW:
            continue
        if (now.date() - (start + timedelta(days=6))).days >= REVIEW_DAYS:
            soon.append(f"Weekly audit '{row.get('Week')}' is not reviewed yet: give it to "
                        "Opus, then set Status and Actions in Notion (Weekly Audit).")
    return blockers, soon[:2]


# ---------------------------------------------------------------- helpers


def _day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _between(value: Any, start: date, end: date) -> bool:
    day = _day(value)
    return day is not None and start <= day <= end


def role_family(role: str | None) -> str:
    text = (role or "").lower()
    for name, rx in ROLE_WORDS:
        if re.search(rx, text):
            return name
    return "other"


def _key(values: dict[str, Any], split: str) -> str:
    if split == "Role family":
        return role_family(values.get("Role"))
    return str(values.get(split) or "Unknown")


def _ledger_sum(ledger: Mapping[str, Mapping[str, Any]], start: date, end: date
                ) -> dict[str, float]:
    total: Counter[str] = Counter()
    for day, entry in ledger.items():
        if not _between(day, start, end):
            continue
        for name, value in entry.items():
            if isinstance(value, (int, float)):
                total[name] += value
        for run in entry.get("sweeps") or []:  # each /fetch: {"new": jobs saved, ...}
            total["new"] += run.get("new") or 0
    return dict(total)


def _n(value: float) -> str:
    return f"{value:.2f}" if value != int(value) else str(int(value))


def _change(now: float, before: float) -> str:
    if before == now:
        return "same as last week"
    return f"{'+' if now > before else ''}{_n(now - before)} vs last week"


# ---------------------------------------------------------------- the parts


def settled(jobs: Iterable[dict[str, Any]], today: date) -> Funnel:
    start, end = today - timedelta(days=SETTLED[1]), today - timedelta(days=SETTLED[0])
    funnel = Funnel()
    for values in jobs:
        if _between(values.get("Applied date"), start, end):
            funnel.add(values.get("Status"))
    return funnel


def scorecard(data: AuditInput, monday: date, sunday: date) -> tuple[list[str], dict[str, Any]]:
    week = _ledger_sum(data.ledger, monday, sunday)
    applied = sum(1 for v in data.jobs if _between(v.get("Applied date"), monday, sunday))
    applied_before = sum(1 for v in data.jobs if _between(
        v.get("Applied date"), monday - timedelta(days=7), sunday - timedelta(days=7)))
    group = settled(data.jobs, sunday)
    interviews = sum(1 for v in data.jobs if v.get("Status") in ("Interview", "Offer")
                     and _between(v.get("Applied date"), sunday - timedelta(days=60), sunday))
    days_run = sum(1 for d in data.ledger if _between(d, monday, sunday))
    screened = sum(v for k, v in week.items() if k.startswith("screened."))
    good = week.get("screened.Apply high", 0) + week.get("screened.Apply normal", 0)
    lines = [
        f"- Applications this week: {applied} ({_change(applied, applied_before)})",
        f"- Reply rate of the settled group (applied {SETTLED[0]} to {SETTLED[1]} days ago): "
        f"{group.replied}/{group.applied} = {pct(group.rate)}; interviews {group.interview}, "
        f"rejected {group.rejected}, ghosted {group.ghosted}",
        f"- Jobs in interview or offer now (applied in the last 60 days): {interviews}",
        f"- What the bot did (day ledger, {days_run} of 7 days recorded): "
        f"{_n(week.get('new', 0))} new jobs found, {_n(screened)} screened "
        f"({_n(good)} Apply high or normal), {_n(week.get('resumes', 0))} resumes, "
        f"{_n(week.get('contacts', 0))} contacts, {_n(week.get('drafts', 0))} drafts, "
        f"{_n(week.get('mails_sent', 0))} mails sent, {_n(week.get('replies', 0))} replies, "
        f"AI about ${week.get('cost', 0):.2f}",
    ]
    props = {"Applied": applied, "Replies": int(week.get("replies", 0)),
             "Interviews": interviews, "Reply rate": round(group.rate, 4),
             "Mails sent": int(week.get("mails_sent", 0)), "AI cost": round(week.get("cost", 0), 2)}
    return lines, props


def leaks(data: AuditInput, monday: date, sunday: date) -> list[Finding]:
    """Where jobs fall out of the funnel."""
    out: list[Finding] = []
    stuck = [v for v in data.jobs if v.get("Status") in ("Approved", "Resume built")
             and (_day(v.get("Last activity date")) or _day(v.get("First seen")) or sunday)
             <= sunday - timedelta(days=NOT_APPLIED_DAYS)]
    if stuck:
        out.append(Finding("high" if len(stuck) >= 5 else "medium",
                           f"{len(stuck)} approved jobs with a resume are not applied after "
                           f"{NOT_APPLIED_DAYS}+ days: the first applicants get the most "
                           "replies. Apply sooner, or approve fewer."))
    wasted = [v for v in data.jobs if v.get("Status") == "Expired" and v.get("Resume")
              and _between(v.get("Last activity date") or v.get("Swept date"), monday, sunday)]
    if wasted:
        out.append(Finding("medium", f"{len(wasted)} jobs expired after their resume was "
                                     "built: the work was lost. Apply within 2 days."))
    applied = [v for v in data.jobs if _between(v.get("Applied date"), monday, sunday)]
    bare = [v for v in applied if not v.get("Contacts")]
    if applied and len(bare) / len(applied) > 0.5:
        out.append(Finding("medium", f"{len(bare)} of {len(applied)} applications this week "
                                     "have no contact: applications alone get few replies. "
                                     "Use /fetchcontacts and the referral asks."))
    unsent = [c for c in data.contacts if c.get("Gmail draft ID")
              and c.get("Status") not in CONTACTED
              and (_day(c.get("Date found")) or sunday) <= sunday - timedelta(days=3)]
    if unsent:
        out.append(Finding("medium", f"{len(unsent)} Gmail drafts are older than 3 days and "
                                     "not sent: send them or switch to /mailmode send."))
    bounced = [c for c in data.contacts if c.get("Status") == "Bounced"
               and _between(c.get("Last contacted"), monday, sunday)]
    if bounced:
        sources = Counter(str(c.get("Source") or "Unknown") for c in bounced)
        out.append(Finding("high" if len(bounced) >= 3 else "medium",
                           f"{len(bounced)} mails bounced (wrong address); by source: "
                           + ", ".join(f"{k} {n}" for k, n in sources.most_common())
                           + ". A source with many bounces gives bad emails."))
    fast = [v for v in data.jobs if v.get("Status") == "Rejected"
            and _between(v.get("Last activity date"), monday, sunday)
            and _day(v.get("Applied date")) is not None
            and (_day(v.get("Last activity date")) - _day(v.get("Applied date"))).days
            <= FAST_REJECT_DAYS]
    rejected = [v for v in data.jobs if v.get("Status") == "Rejected"
                and _between(v.get("Last activity date"), monday, sunday)]
    if rejected:
        days = [(_day(v.get("Last activity date")) - _day(v.get("Applied date"))).days
                for v in rejected if _day(v.get("Applied date"))]
        median = f", median {statistics.median(days):g} days after applying" if days else ""
        level = "high" if len(fast) >= 3 else "info"
        text = f"{len(rejected)} rejections this week{median}"
        if fast:
            text += (f"; {len(fast)} within {FAST_REJECT_DAYS} days, which is usually an "
                     "automatic filter (years, visa, keywords, location), not a person")
        out.append(Finding(level, text + "."))
    return out


def repeats(data: AuditInput, sunday: date) -> list[Finding]:
    """The same thing done again and again without one reply in the last REPEAT_DAYS (this
    week's applications left out: too fresh)."""
    start, end = sunday - timedelta(days=REPEAT_DAYS), sunday - timedelta(days=SETTLED[0])
    out: list[Finding] = []
    for split in SPLITS:
        groups: dict[str, Funnel] = {}
        for values in data.jobs:
            if _between(values.get("Applied date"), start, end):
                groups.setdefault(_key(values, split), Funnel()).add(values.get("Status"))
        for key, f in sorted(groups.items(), key=lambda kv: -kv[1].applied):
            if key != "Unknown" and f.applied >= REPEAT_MIN and f.replied == 0 \
                    and f.interview == 0:
                out.append(Finding("high", f"{split} '{key}': {f.applied} applications in 4 "
                                           "weeks, 0 replies. Stop, or change one thing "
                                           "(role level, resume focus, contacts) and measure."))
    kinds: dict[str, list[int]] = {}
    for c in data.contacts:
        if c.get("Status") in CONTACTED and _between(c.get("Last contacted"), start, end):
            n = kinds.setdefault(str(c.get("Type") or "Unknown"), [0, 0])
            n[0] += 1
            n[1] += c.get("Status") == "Replied"
    for kind, (sent, replied) in kinds.items():
        if sent >= REPEAT_MIN and replied == 0:
            out.append(Finding("high", f"Mails to {kind}: {sent} in 4 weeks, 0 replies. "
                                       "Change the subject or first line, or mail another "
                                       "kind of person."))
    followed = [c for c in data.contacts if c.get("Status") == "Followed up"
                and _between(c.get("Last contacted"), start, end)]
    if len(followed) >= REPEAT_MIN:
        out.append(Finding("medium", f"{len(followed)} follow-ups in 4 weeks are still "
                                     "unanswered: follow-ups are not working as written."))
    swept: dict[str, list[int]] = {}
    for values in data.jobs:
        if _between(values.get("First seen"), sunday - timedelta(days=REPEAT_DAYS), sunday):
            n = swept.setdefault(str(values.get("Board") or "Unknown"), [0, 0])
            n[0] += 1
            n[1] += values.get("Screen verdict") in ("Apply high", "Apply normal")
    for board, (found, good) in sorted(swept.items(), key=lambda kv: -kv[1][0]):
        if board != "Unknown" and found >= 20 and good == 0:
            out.append(Finding("medium", f"Source '{board}': {found} jobs in 4 weeks, none "
                                         "Apply high or normal. Narrow its search or drop it."))
    return out


def what_works(data: AuditInput, sunday: date) -> list[str]:
    start, end = sunday - timedelta(days=SETTLED[1]), sunday - timedelta(days=SETTLED[0])
    lines = []
    for split in SPLITS:
        groups: dict[str, Funnel] = {}
        for values in data.jobs:
            if _between(values.get("Applied date"), start, end):
                groups.setdefault(_key(values, split), Funnel()).add(values.get("Status"))
        fair = sorted(((k, f) for k, f in groups.items()
                       if f.applied >= MIN_SAMPLE and k != "Unknown"),
                      key=lambda kv: (-kv[1].rate, -kv[1].applied))
        if fair:
            lines.append(f"- By {split.lower()}: " + "; ".join(
                f"{k} {f.replied}/{f.applied} ({pct(f.rate)})" for k, f in fair))
    referral = [c for c in data.contacts if c.get("Status") in CONTACTED
                and _between(c.get("Last contacted"), start, end)]
    if referral:
        by: dict[str, list[int]] = {}
        for c in referral:
            n = by.setdefault(str(c.get("Type") or "Unknown"), [0, 0])
            n[0] += 1
            n[1] += c.get("Status") == "Replied"
        lines.append("- Mails by kind of person: " + "; ".join(
            f"{k} {r}/{s} replied ({pct(r / s)})" for k, (s, r) in
            sorted(by.items(), key=lambda kv: -kv[1][1] / kv[1][0])))
    return lines or [f"- Not enough settled applications yet (needs {MIN_SAMPLE} in a group, "
                     f"applied {SETTLED[0]} to {SETTLED[1]} days ago)."]


def reliability(data: AuditInput, monday: date, sunday: date) -> tuple[list[Finding], int]:
    out: list[Finding] = []
    days_run = sum(1 for d in data.ledger if _between(d, monday, sunday))
    if days_run < 7:
        out.append(Finding("high" if days_run < 5 else "medium",
                           f"The bot recorded work on {days_run} of 7 days: it was off or "
                           "stopped on the other days (the heartbeat mail says when)."))
    errors = sum(n for k, n in data.issues.items() if k.startswith("ERROR|"))
    warnings = sum(n for k, n in data.issues.items() if k.startswith("WARNING|"))
    if data.issues:
        top = sorted(data.issues.items(), key=lambda kv: -kv[1])[:TOP_ISSUES]
        level = "high" if errors >= 10 else "medium" if errors else "info"
        out.append(Finding(level, f"{errors} errors and {warnings} warnings were logged. The "
                                  "most frequent:"))
        for key, n in top:
            lvl, logger, text = (key.split("|", 2) + ["", ""])[:3]
            out.append(Finding("info", f"   {n}x {lvl.lower()} in {logger}: {text}"))
    return out, errors


def quality(data: AuditInput, monday: date, sunday: date) -> list[Finding]:
    out = []
    notes = [n for n in data.spot_notes if _between(n.get("date"), monday, sunday)]
    if notes:
        out.append(Finding("high", f"You marked {len(notes)} resume(s) wrong in the spot "
                                   "check: " + "; ".join(f"{n.get('job')}: {n.get('note')}"
                                                         for n in notes[:5])))
    forms = [i for i in data.form_issues if _between(i.get("Date"), monday, sunday)]
    if forms:
        kinds = Counter(str(i.get("Issue type") or "Other") for i in forms)
        out.append(Finding("medium", f"{len(forms)} wrong answers in application forms (dry "
                                     "runs): " + ", ".join(f"{k} {n}" for k, n in
                                                          kinds.most_common())))
    return out


def last_actions(data: AuditInput, rate: float, applied: int) -> list[str]:
    if not data.previous:
        return ["- First audit: nothing to compare with yet."]
    prev = data.previous[0]
    actions = str(prev.get("Actions") or "").strip()
    before_rate = float(prev.get("Reply rate") or 0)
    lines = [f"- Last week ({prev.get('Week')}): status {prev.get('Status') or NEW}; "
             f"reply rate {pct(before_rate)} -> {pct(rate)}; applications "
             f"{_n(float(prev.get('Applied') or 0))} -> {applied}."]
    if actions:
        lines.append(f"- Actions you set: {actions}")
        lines.append("- Did they work? Compare the numbers above; replies take 1 to 3 weeks, "
                     "so judge an action after two audits.")
    else:
        lines.append("- No Actions were written for last week: review it with Opus and write "
                     "what you decide, so this check can follow it.")
    return lines


def verdict(rate: float, settled_n: int, previous: list[dict[str, Any]]) -> str:
    if settled_n < 10 or not previous:
        return "Too little data"
    before = float(previous[0].get("Reply rate") or 0)
    if rate >= before + RATE_STEP:
        return "Improving"
    if rate <= before - RATE_STEP:
        return "Worse"
    return "Flat"


# ---------------------------------------------------------------- the audit


def build(data: AuditInput, monday: date) -> Audit:
    sunday = monday + timedelta(days=6)
    title = f"{monday.isocalendar()[0]}-W{monday.isocalendar()[1]:02d}: {monday:%d %b} to " \
            f"{sunday:%d %b}"
    score, props = scorecard(data, monday, sunday)
    group = settled(data.jobs, sunday)
    problems = leaks(data, monday, sunday)
    stale = repeats(data, sunday)
    reliable, errors = reliability(data, monday, sunday)
    marked = quality(data, monday, sunday)
    # The most serious first; the reliability lines last, in their own order (the error
    # counts, then the most frequent ones).
    found = [*sorted([*problems, *marked], key=lambda f: LEVELS.index(f.level)), *reliable]
    result = verdict(group.rate, group.applied, data.previous)
    props.update({"Week": title, "Week start": monday, "Errors": errors, "Verdict": result,
                  "Status": NEW})
    high = sum(f.level == "high" for f in [*found, *stale])
    sections = [
        ("Scorecard", score),
        ("Where it went wrong this week",
         [f.line() for f in found] or ["- Nothing went wrong that the data shows."]),
        ("Done again and again without results (last 4 weeks)",
         [f.line() for f in stale] or ["- Nothing yet (needs "
                                       f"{REPEAT_MIN}+ tries in one group with 0 replies)."]),
        ("What works (settled applications)", what_works(data, sunday)),
        ("Last week's actions", last_actions(data, group.rate, props["Applied"])),
        ("For Opus", [OPUS_PROMPT, "When you decide, set Status to Actions set and write the "
                                   "Actions here; next week's audit follows them."]),
    ]
    summary = (f"🧭 Weekly audit {title}: {result}. {props['Applied']} applied, settled reply "
               f"rate {pct(group.rate)} ({group.replied}/{group.applied}), {high} serious "
               f"problem(s), {errors} errors logged. Read it in Notion (Weekly Audit), give it "
               "to Opus, then write your Actions there.")
    return Audit(monday, sunday, title, props, sections, summary)
