"""What to do next (9 Oct, your idea): every answer ends with the next command to send.

`hint` reads the command you sent and the bot's answer and returns one "👉 Next: ..." line
(Telegram makes each /command in it tappable), or None when the answer already shows its own
next step (buttons, "Next:") or nothing follows. `checklist` is /next: the whole day in order
with what is done and what is waiting, so you always know where you are.

The day in order: /fetch, /screen, /jd (paste descriptions), /pending (Approve), Approve
resume and apply, /fetchcontacts (contacts and drafts), /today (replies), /followups.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

NEXT = "\n\n\U0001F449 Next: "


def _count(pattern: str, text: str) -> int:
    match = re.search(pattern, text)
    return int(match.group(1)) if match else 0


def hint(command: str | None, args: str, text: str) -> str | None:
    """The next-step line for this command's answer, without the "Next:" prefix."""
    if not command or "Next:" in text or text.startswith(("Send /", "No ")):
        return None  # a usage line or "nothing found": the answer says what to send
    word = args.strip().lower()
    if command == "screen" and word == "batch":
        if "half-price batch" in text and "Sent" in text:
            return "/screen collect in about an hour (it says when the answers are in)."
        return None
    if command == "screen":
        steps = []
        if _count(r"(\d+) ready to review", text) or "wait in /pending" in text:
            steps.append("/pending to review the screened jobs (Approve or Skip)")
        if _count(r"(\d+) jobs? needs? the JD", text) or _count(r"(\d+) waiting for JD", text):
            steps.append("/jd to paste the descriptions of the jobs the bot cannot read")
        return "; ".join(steps) + "." if steps else None
    if command == "done":
        return "/pending to review it, or /jd for the next job waiting for a description."
    if command == "jd" and not word:
        return None  # the list itself says how to paste
    if command == "update" and "Strategy updated" in text:
        return "/rules to see the rules and your tips, then /fetch for today's jobs."
    if command == "rules":
        return "/fetch for today's jobs."
    if command in ("alertcheck", "health", "sources"):
        return "/fetch to save today's jobs."
    if command == "fetchreport":
        return "/screen to screen the new jobs, or /pending if they are screened."
    if command == "applypack":
        return "apply on the company's page, then tap I applied under the resume."
    if command == "fetchcontacts" and "Done:" in text:
        return "check the drafts in Gmail; tomorrow /today shows sent mails and replies."
    if command == "drafts":
        return "open Gmail drafts (or /mailqueue in send mode); tomorrow /today."
    if command == "today":
        return "/followups for the mails to follow up, /stats for the week."
    if command == "followups":
        return "/drafts <job> writes the follow-up drafts; /today tomorrow."
    if command == "gaps":
        return "tap Hands-on or Production for a skill you have; /pending to continue."
    if command == "autopilot":
        return "check the resumes and drafts above; /pending for the jobs it left for you."
    return None


@dataclass(frozen=True)
class DayState:
    fetched_today: bool
    unscreened: int  # Unscreened rows that have a description
    waiting_jd: int
    pending: int
    applied_today: int
    contacts_done: bool  # /fetchcontacts ran today


def checklist(state: DayState) -> str:
    """/next: today's steps with ✅ done, 👉 the one to do now, ⏳ later."""
    steps = [
        ("/fetch", "find today's new jobs", state.fetched_today),
        ("/screen", f"screen the new jobs ({state.unscreened} waiting)", not state.unscreened),
        ("/jd", f"paste descriptions the bot cannot read ({state.waiting_jd} waiting)",
         not state.waiting_jd),
        ("/pending", f"review and Approve ({state.pending} ready)", not state.pending),
        ("Approve resume", "apply on the company page, tap I applied", state.applied_today > 0),
        ("/fetchcontacts", "contacts and drafts for today's applied jobs", state.contacts_done),
        ("/today", "sent mails, replies and follow-ups (next morning)", False),
    ]
    lines = ["Your day, in order:"]
    now = None
    for command, what, done in steps:
        if done:
            mark = "✅"
        elif now is None:
            mark, now = "\U0001F449", command
        else:
            mark = "⏳"
        lines.append(f"{mark} {command}: {what}")
    if now:
        lines.append(f"\nDo now: {now}")
    return "\n".join(lines)
