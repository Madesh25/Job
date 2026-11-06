"""/timetable (5 Nov, the autopilot timetable): when everything runs, in your time and
theirs, from the settings in force (yaml and Notion Config), plus your part.

Times are in the report time zone (India by default); the mail windows are shown in your
time for today, so a European clock change (last Sunday of March and October) shows itself.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from jobengine.mail import timing
from jobengine.screen import check, schedule
from jobengine.track import daily_report, health_report, nudge, timed

if TYPE_CHECKING:
    from jobengine.screen.desk import Desk


def _window_here(w: timing.Window, country: str, local: datetime) -> str:
    """The recipient's window today, in your time ("12:30 to 15:30")."""
    tz = timing.zone(country)
    day = local.astimezone(tz).date()
    start = datetime.combine(day, w.start, tzinfo=tz).astimezone(local.tzinfo)
    end = datetime.combine(day, w.end, tzinfo=tz).astimezone(local.tzinfo)
    return f"{start:%H:%M} to {end:%H:%M}"


def text(desk: Desk) -> str:
    config = desk.deps.config()
    local = daily_report.local(desk.tracking_now(), config)
    p = schedule.plan(desk)
    w = timing.window(desk.s.mail, config)
    from jobengine.screen import autopilot

    lines = [f"🗓 Timetable ({local.tzname() or 'your time'}; today {local:%a %d %b})", "",
             "Every day:"]
    health = schedule._time(config.get(health_report.TIME_KEY), health_report.DEFAULT_TIME)
    lines.append(f"{health:%H:%M} Morning check (tokens, keys, Notion, AI credit)")
    if p.on:
        for at in p.starts:
            lines.append(f"{at:%H:%M} Autopilot ({p.days_text(p.days_for(at))}): fetch, "
                         f"screen, approve the best, resumes, contacts, drafts")
        lines.append(f"   Approvals: {autopilot.day_approvals(desk)} a day; a morning run "
                     f"uses at most {autopilot.share(desk)}% of the day.")
    else:
        lines.append(f"Autopilot: off (it would run {p.when_text()}); Config "
                     f"{schedule.SWITCH_KEY} = true turns it on.")
    for at in timed.times(config) if timed.enabled(config) else ():
        lines.append(f"{at:%H:%M} Daily check (replies, applications, rejections, follow-ups)")
    if check.status_text(desk).startswith("Afternoon check: on"):
        lines.append(check.status_text(desk))
    if w.enabled:
        pl = _window_here(w, "Poland", local)
        ie = _window_here(w, "Ireland", local)
        lines.append(f"{pl} Mails to Poland and the Netherlands "
                     f"({w.start:%H:%M} to {w.end:%H:%M} their time, {w.days_text_short()})")
        lines.append(f"{ie} Mails to Ireland. Follow-ups Tue to Thu only; public holidays "
                     "skipped; a mail waits until you applied.")
        ahead = timing.holidays_ahead(w, local.date())
        if ahead:  # TT-01 (6 Nov): name the days, as /mailqueue does
            lines.append("   No mails on these public holidays (next 14 days): "
                         + "; ".join(ahead) + ".")
        elif w.holidays:
            lines.append("   No public holiday in Poland, the Netherlands or Ireland in the "
                         "next 14 days.")
    else:
        lines.append("Mails: sent at once (Config mail.send_window = off).")
    lines.append("07:00 to 23:45 Reply pings every 15 minutes (with Send in their morning)")
    lines.append(f"{daily_report.report_time(config):%H:%M} Daily report (Telegram and email)")
    if (config.get(nudge.SWITCH_KEY) or "true").strip().lower() != "false":
        at = schedule._time(config.get(nudge.TIME_KEY), nudge.DEFAULT_TIME)
        lines.append(f"{at:%H:%M} Apply nudge: you apply to tonight's jobs")
    lines += ["", "Every week:",
              "Sat 11:00 New companies from the visa sponsor lists (/newcompanies)",
              "Sun 09:00 Link check (closed jobs Expired), 10:00 job board check",
              "Sun 20:00 Weekly digest, spot check (2 resumes) and the Weekly Audit page",
              "", "Every month:",
              "Friday evening before /fetch closes: /update research, for your Saturday review",
              "", "Your part: read the morning summary (2 min); apply at 22:00 and tap I applied;"
              " answer replies; Saturday /newcompanies (and /update once a month); Sunday "
              "spot check and the audit with Opus; Gmail tokens every 7 days."]
    return "\n".join(lines)
