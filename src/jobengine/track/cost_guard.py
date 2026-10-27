"""AI cost guard (Autopilot I6, 26 Oct): a monthly cap on the paid AI.

Notion Config `ai.monthly_cap` is the most the AI may cost in a month, in US dollars (the
same estimate as the "AI in <month>" line, track/costs.py). Empty or 0: no cap.

- At 80% of the cap the bot warns you once that month.
- At 100% it pauses the paid AI until the 1st of next month: every AI call says so instead
  of running (screening, resumes, mails, contact search, /update). Reading your replies goes
  on: it costs about a cent a day and a missed interview reply costs more.
- The pause message has a button "Allow AI for the rest of <month>"; raising the cap in
  Config works too.

Claude's own cap (27 Oct, your decision): DeepSeek does the work and Claude is only the safety
net (it answers when DeepSeek or Gemini fails). Notion Config `ai.claude_cap` is the most
Claude may cost in a month (you set 5). At 80% the bot warns once; at the cap Claude stops
until the 1st: DeepSeek keeps working, and a call DeepSeek fails then fails (the job waits)
instead of going to Claude. Reading your replies still goes to Claude when DeepSeek fails.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import date
from typing import Any

from jobengine.track.costs import KEY as COST_KEY
from jobengine.track.costs import provider_spent

CAP_KEY = "ai.monthly_cap"
ALERT_KEY = "ai.cap_alert"  # bot_state: {"month": "2026-10", "level": 80 or 100}
ALLOW_KEY = "ai.cap_allow"  # bot_state: {"month": "2026-10"}: you allowed AI past the cap
WARN_SHARE = 0.8
FREE_STAGES = ("reply",)  # never paused: reading replies
CLAUDE_CAP_KEY = "ai.claude_cap"
CLAUDE_ALERT_KEY = "ai.claude_alert"  # bot_state: {"month": "2026-10", "level": 80 or 100}


def cap(config: Any, key: str = CAP_KEY) -> float | None:
    raw = str(config.get(key) or "").strip().lstrip("$").replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _month(day: date) -> str:
    return day.strftime("%Y-%m")


def spent(state: Any, today: date) -> float:
    value = (state.get(COST_KEY) if state is not None else None) or {}
    return float(value.get("total") or 0.0) if value.get("month") == _month(today) else 0.0


def allowed_past_cap(state: Any, today: date) -> bool:
    value = (state.get(ALLOW_KEY) if state is not None else None) or {}
    return value.get("month") == _month(today)


def allow(state: Any, today: date) -> None:
    state.set(ALLOW_KEY, {"month": _month(today)})


def paused(config: Any, state: Any, today: date) -> bool:
    limit = cap(config)
    return (limit is not None and spent(state, today) >= limit
            and not allowed_past_cap(state, today))


def claude_paused(config: Any, state: Any, today: date) -> bool:
    limit = cap(config, CLAUDE_CAP_KEY)
    return limit is not None and provider_spent(state, today, "claude") >= limit


def blocked(config: Any, state: Any, today: date, stage: str,
            provider: str = "any") -> str | None:
    """Why this AI call may not run now; None when it may. `provider` "claude": the call is
    about to go to Claude (also as the fallback of a failed DeepSeek or Gemini call)."""
    if stage in FREE_STAGES:
        return None
    if paused(config, state, today):
        return (f"AI paused: this month's AI cost (about ${spent(state, today):.2f}) reached "
                f"Config {CAP_KEY} (${cap(config):.2f}). It starts again on the 1st, or tap "
                "\"Allow AI\" on the cap message")
    if provider == "claude" and claude_paused(config, state, today):
        return (f"Claude paused: this month's Claude cost (about "
                f"${provider_spent(state, today, 'claude'):.2f}) reached Config "
                f"{CLAUDE_CAP_KEY} (${cap(config, CLAUDE_CAP_KEY):.2f}). DeepSeek keeps "
                "working; what DeepSeek fails waits until the 1st, or raise the cap")
    return None


def guard(state: Any, today: Callable[[], date]) -> Callable[..., str | None]:
    """What the bot passes to jobengine.llm.set_cost_guard."""
    return lambda stage, config, provider="any": blocked(config, state, today(), stage,
                                                          provider)


def claude_alert(config: Any, state: Any, today: date) -> str | None:
    """Claude's cap at 80% and at 100%, once a month each; None when nothing is new."""
    limit = cap(config, CLAUDE_CAP_KEY)
    if limit is None:
        return None
    used = provider_spent(state, today, "claude")
    now = 100 if used >= limit else 80 if used >= WARN_SHARE * limit else 0
    sent = state.get(CLAUDE_ALERT_KEY) or {}
    if not now or (sent.get("month") == _month(today) and int(sent.get("level") or 0) >= now):
        return None
    state.set(CLAUDE_ALERT_KEY, {"month": _month(today), "level": now})
    month = today.strftime("%B")
    if now == 80:
        return (f"🛟 Claude (the safety net) cost about ${used:.2f} of its ${limit:.2f} for "
                f"{month}: DeepSeek is failing more than usual. The AI watch messages say "
                f"what failed. Config {CLAUDE_CAP_KEY} is the limit.")
    return (f"⛔ Claude paused for the rest of {month}: about ${used:.2f} used of "
            f"${limit:.2f} (Config {CLAUDE_CAP_KEY}). DeepSeek keeps working; when DeepSeek "
            "fails, that job now waits instead of going to Claude. Raise the cap to restart "
            "Claude.")


def level(config: Any, state: Any, today: date) -> int:
    """100 at or over the cap, 80 from 80% of it, else 0."""
    limit = cap(config)
    if limit is None:
        return 0
    used = spent(state, today)
    return 100 if used >= limit else 80 if used >= WARN_SHARE * limit else 0


def alert(config: Any, state: Any, today: date) -> tuple[str, bool] | None:
    """(text, with the Allow button) once per month per level; None when nothing is new."""
    now = level(config, state, today)
    if not now or (now == 100 and allowed_past_cap(state, today)):
        return None
    sent = state.get(ALERT_KEY) or {}
    if sent.get("month") == _month(today) and int(sent.get("level") or 0) >= now:
        return None
    state.set(ALERT_KEY, {"month": _month(today), "level": now})
    used, limit = spent(state, today), cap(config) or 0.0
    month = today.strftime("%B")
    if now == 80:
        return (f"💸 AI cost: about ${used:.2f} of your ${limit:.2f} cap for {month} "
                f"({used / limit:.0%}). At the cap the paid AI pauses until the 1st. Raise "
                f"Config {CAP_KEY} if you need more.", False)
    return (f"⛔ AI paused: about ${used:.2f} used, your cap for {month} is ${limit:.2f} "
            f"(Config {CAP_KEY}). Screening, resumes, mails, contact search and /update wait "
            "until the 1st; reading your replies goes on. Tap below to allow AI for the rest "
            f"of {month}, or raise the cap in Config.", True)


def claude_health_line(config: Any, state: Any, today: date) -> tuple[str, str] | None:
    """For the morning check: Claude's cap reached ("soon": DeepSeek still works) or at 80%."""
    limit = cap(config, CLAUDE_CAP_KEY)
    if limit is None:
        return None
    used = provider_spent(state, today, "claude")
    if used >= limit:
        return ("soon", f"Claude paused (Config {CLAUDE_CAP_KEY} ${limit:.2f} reached): "
                        "DeepSeek works alone; jobs it fails wait until the 1st.")
    if used >= WARN_SHARE * limit:
        return ("soon", f"Claude used about ${used:.2f} of ${limit:.2f} this month: DeepSeek "
                        "fails often.")
    return None


def health_line(config: Any, state: Any, today: date) -> tuple[str, str] | None:
    """For the morning check: ("blocker" or "soon", text); None when under 80%."""
    now = level(config, state, today)
    if now == 100 and not allowed_past_cap(state, today):
        return ("blocker", f"AI paused: the {today:%B} cap (Config {CAP_KEY}, "
                f"${cap(config):.2f}) is reached. Fix: raise the cap, or wait for the 1st.")
    if now:
        return ("soon", f"AI cost at {spent(state, today) / (cap(config) or 1):.0%} of the "
                f"{today:%B} cap (Config {CAP_KEY}).")
    return None


# ---------------------------------------------------------------- the AI watch (27 Oct)

WATCH_MIN = 3  # failed DeepSeek or Gemini calls before a message
WATCH_EVERY_HOURS = 3  # at most one message in this many hours
STAGE_WORDS = {"score": "screening", "resume": "resumes", "tailor": "resumes",
               "cover": "cover letters", "email": "mails", "reply": "reading replies",
               "prep": "interview prep", "people": "contact search", "strategy": "/update",
               "apply": "form answers"}


def watch_text(fails: list[tuple[Any, str, str, str]], since: Any, claude_used: float,
               limit: float | None, claude_off: bool) -> str:
    """The message when DeepSeek or Gemini keeps failing: how often, in which jobs, the last
    error, and what Claude did about it."""
    stages = Counter(STAGE_WORDS.get(stage, stage) for _when, stage, _model, _err in fails)
    models = sorted({model for _when, _stage, model, _err in fails})
    last = fails[-1][3] or "no error text"
    head = (f"🛟 AI watch: {', '.join(models)} failed {len(fails)} times since {since:%H:%M} "
            f"(" + ", ".join(f"{k} {n}" for k, n in stages.most_common()) + ").")
    if claude_off:
        what = ("Claude is paused (its monthly cap is reached), so those jobs wait and will be "
                "tried again on the next run.")
    else:
        what = "Claude answered them instead."
    money = (f" Claude this month: about ${claude_used:.2f}"
             + (f" of ${limit:.2f}." if limit is not None else "."))
    return (f"{head} {what}{money}\nLast error: {last}\nIf it goes on, check the DeepSeek key "
            "and credit (deepseek.com) and /health.")
