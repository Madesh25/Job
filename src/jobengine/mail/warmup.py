"""Mail warm-up (PR 17): the daily send cap grows week by week, so Gmail and the receiving
servers see a new sender's volume rise slowly instead of jumping to the full count.

- Week 1 of sending: 10 a day, week 2: 15, week 3: 20, week 4: 25, then 30
  (`mail.warmup.steps` in the yaml).
- Week 1 starts on the day the bot first sends a mail (Bot State `mail.warmup_start`), or on
  Notion Config `mail.warmup_start` (YYYY-MM-DD) when you set it, for example because you
  already mailed by hand from this address.
- Notion Config `mail.daily_send_cap` stays the ceiling: the cap is the smaller of the two.
  Raise it to 30 to let the warm-up reach 30.
- Notion Config `mail.warmup` = `off` turns the warm-up off (the cap is then only
  `mail.daily_send_cap`).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from jobengine.bot_state import BotState

START_KEY = "mail.warmup_start"  # bot_state {"date"} and Notion Config (YYYY-MM-DD)
SWITCH_KEY = "mail.warmup"  # Notion Config: "off"
DEFAULT_STEPS = (10, 15, 20, 25, 30)


def steps(mail_yaml: dict[str, Any]) -> tuple[int, ...]:
    raw = (mail_yaml.get("warmup") or {}).get("steps") or DEFAULT_STEPS
    try:
        values = tuple(max(0, int(v)) for v in raw)
    except (TypeError, ValueError):
        return DEFAULT_STEPS
    return values or DEFAULT_STEPS


def enabled(config: Any) -> bool:
    return (config.get(SWITCH_KEY) or "").strip().lower() not in ("off", "false", "no")


def start(state: BotState | None, config: Any) -> date | None:
    configured = config.get_date_prefix(START_KEY)
    if configured is not None:
        return configured
    value = (state.get(START_KEY) or {}) if state is not None else {}
    try:
        return date.fromisoformat(value["date"]) if value.get("date") else None
    except ValueError:
        return None


def week(state: BotState | None, config: Any, today: date) -> int:
    """1 in the first week of sending (and before the first send), 2 in the second, ..."""
    first = start(state, config)
    if first is None or today < first:
        return 1
    return (today - first).days // 7 + 1


def cap(mail_yaml: dict[str, Any], state: BotState | None, config: Any,
        today: date) -> int | None:
    """Today's warm-up cap, or None when the warm-up is off."""
    if not enabled(config):
        return None
    values = steps(mail_yaml)
    return values[min(week(state, config, today), len(values)) - 1]


def mark_first_send(state: BotState | None, today: date) -> None:
    if state is not None and not (state.get(START_KEY) or {}).get("date"):
        state.set(START_KEY, {"date": today.isoformat()})


def line(mail_yaml: dict[str, Any], state: BotState | None, config: Any, today: date,
         ceiling: int) -> str:
    """For /mailmode: where the warm-up is."""
    today_cap = cap(mail_yaml, state, config, today)
    if today_cap is None:
        return f"Warm-up: off (Config mail.warmup); at most {ceiling} mails a day."
    values = steps(mail_yaml)
    n = week(state, config, today)
    first = start(state, config)
    since = f"since {first.isoformat()}" if first else "starts with the first mail sent"
    plan = ", ".join(str(v) for v in values)
    return (f"Warm-up: week {n} ({since}), {min(today_cap, ceiling)} mails a day today "
            f"(plan {plan} a day, week by week; Config mail.daily_send_cap {ceiling} is the "
            "ceiling).")
