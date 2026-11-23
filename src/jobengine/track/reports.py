"""Report a problem from Telegram (S2, 23 Nov, your ask): reply to any bot message with
"wrong: what is wrong" (or send "/bug what is wrong") and the bot saves it as one row of the
Error Log with that message attached, so no logs or screenshots need pasting. Claude Code reads
the rows with Status New and fixes them, as for the bot's own problems.

The row: Problem "You: <your words>", Level "Reported by you", Likely cause "Your report",
Status New, Count 1, and in Example your words, then the bot message you replied to with its
time (emails, keys, tokens and link queries blanked). A report never pings: you sent it.
Without a reply the row says no message was attached. Saving never fails anything else: a
problem is told in the answer.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from jobengine.track.run_log import redact, where

log = logging.getLogger("jobengine.track")

LEVEL = "Reported by you"
CAUSE = "Your report"
STAGE = "Reported"
TITLE_CHARS = 150
EXAMPLE_CHARS = 1900  # Notion keeps 2000 characters in one text value
WORDS = re.compile(r"^\s*(?:wrong|bug)\s*[:\-]\s*(.*)$", re.I | re.S)
COMMAND = re.compile(r"^\s*/bug(?:@\w+)?(?:\s+(.*))?$", re.I | re.S)

HOW = ("To report a problem: reply to the bot message that is wrong with "
       "\"wrong: what is wrong\", or send \"/bug what is wrong\". It is saved in the Error Log "
       "(Status New) with that message, for Claude Code.")


def words(text: str) -> str | None:
    """Your words when `text` is a report ("wrong: ...", "bug: ...", "/bug ..."), else None.
    An empty report gives "" (the answer then says how to write one)."""
    for pattern in (COMMAND, WORDS):
        match = pattern.match(text or "")
        if match:
            return (match.group(1) or "").strip()
    return None


def _when(replied: Mapping[str, Any], zone: Any) -> str:
    stamp = replied.get("edit_date") or replied.get("date")
    if not stamp:
        return ""
    try:
        return datetime.fromtimestamp(int(stamp), zone).strftime("%d %b %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def row(said: str, replied: Mapping[str, Any] | None, now: datetime) -> dict[str, Any]:
    """The Error Log row of one report."""
    replied = replied or {}
    original = str(replied.get("text") or replied.get("caption") or "")
    said = redact(said)
    if original:
        when = _when(replied, now.tzinfo)
        head = f"The bot's message{f' ({when})' if when else ''}:"
        example = f"Your words: {said}\n\n{head}\n{redact(original)}"
    else:
        example = f"Your words: {said}\n\n(No bot message attached: it was not a reply.)"
    key_id = replied.get("message_id") or now.strftime("%H%M%S%f")
    return {
        "Problem": f"You: {said}"[:TITLE_CHARS], "Day": now.date(), "First seen": now,
        "Last seen": now, "Count": 1, "Stage": STAGE, "Level": LEVEL, "Likely cause": CAUSE,
        "Where": where(), "Status": "New", "Example": example[:EXAMPLE_CHARS],
        "Key": f"{now.date().isoformat()}|report-{key_id}",
    }


def save(table: Any, now: Callable[[], datetime], said: str,
         replied: Mapping[str, Any] | None) -> str:
    """Save one report and say so (the text of the answer)."""
    if not said:
        return HOW
    if table is None:
        return ("Not saved: the Error Log cannot be written here (DRY RUN for Notion). "
                "Write it in a Phase 23 Log row instead.")
    try:
        moment = now()
    except Exception:  # a clock that needs Notion must not lose the report
        moment = datetime.now().astimezone()
    try:
        table.create(row(said, replied, moment))
    except Exception as exc:
        log.warning("report not saved: %s", redact(str(exc))[:200])
        return "Not saved: Notion did not answer. Send it again in a minute."
    attached = "with the message you replied to" if replied else "(no message attached)"
    return (f"\U0001F4DD Saved in the Error Log, Status New, {attached}: \"{said[:80]}\". "
            "Claude Code reads it with the other rows.")
