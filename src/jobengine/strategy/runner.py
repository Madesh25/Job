"""/update (spec section 2): web research, validated tips logged to Strategy for your review,
and the gate stamped only when every tip of the run is decided.

Research is never applied: the bot writes Strategy rows and asks you. The only Config value
it writes is last_strategy_update (prod); outside prod the stamp goes to bot_state.
"""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from jobengine.bot_state import BotState
from jobengine.config_store import ConfigStore
from jobengine.llm import LLMError, SearchLLM
from jobengine.notion_repo import StrategyRepo
from jobengine.settings import Settings
from jobengine.strategy import validate
from jobengine.strategy.models import Tip
from jobengine.strategy.rules_view import adopted_tips, non_negotiables
from jobengine.sweep.gate import STAMP_KEY

log = logging.getLogger("jobengine.strategy")

STAGE = "strategy"
RUN_KEY = "strategy.run"
MAX_TOKENS = 4000
REF_RE = re.compile(r"Ref ST-([0-9a-f]{8})")
STARTED = "Researching current practice. This takes a minute."
FOLD_REMINDER = "Adopted tips are not in V16 yet. Ask Claude to fold them in."

SYSTEM_PROMPT = """You research current job search practice for one candidate: a DevOps
engineer with about 3 years of experience, an Indian national who needs a work permit,
applying to DevOps, Platform, SRE and Cloud roles in Poland, the Netherlands and Ireland.

Search the web for recent, concrete advice about applicant tracking systems, applications,
cold outreach and interviews for these roles and countries. Prefer primary sources (company
hiring pages, government permit pages, recruiters' published guides) and recent articles.

Return one JSON object only:
{"tips": [{"tip": <one sentence>, "category": "ATS" | "Application" | "Interview" |
 "Outreach" | "Resume" | "Other", "countries": ["Poland" | "Netherlands" | "Ireland" | "All"],
 "why": <one sentence>, "sources": [<URLs from your search results>],
 "conflicts_with_v16": <true when the tip breaks one of the current rules below>,
 "published": <"YYYY-MM" or "unknown">}]}

Rules:
- At most MAX_TIPS tips. Every tip needs at least one source URL that your searches returned.
- Skip tips that are already in the adopted list below.
- Never suggest keyword stuffing, hidden or white text, ATS score checkers, copying the job
  description, invented experience, mass identical applications or mail tracking.
- Never use a long dash; use commas or a plain hyphen.
"""


class StrategyError(Exception):
    """The review cannot be finished. The message is shown to you."""


@dataclass
class Card:
    text: str
    buttons: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class UpdateResult:
    messages: list[str] = field(default_factory=list)
    cards: list[Card] = field(default_factory=list)


@dataclass
class StrategyDeps:
    s: Settings
    config: Callable[[], ConfigStore]
    repo: StrategyRepo | None  # Strategy (prod) or Strategy (DEV); None: DRY RUN
    existing: Callable[[], list[tuple[str, dict[str, Any]]]]  # every row to dedupe against
    state: BotState
    llm: Callable[[ConfigStore], SearchLLM | None]
    v16_blocks: Callable[[], list[dict[str, Any]]]
    # Prod: writes Config last_strategy_update (safety.check_config_write inside).
    stamp_config: Callable[[ConfigStore, date], None] | None = None
    today: Callable[[], date] = date.today
    write: bool = True
    ind: Callable[[], str] | None = None  # the IND register refresh, run by every /update


def hex_id(page_id: str) -> str:
    return page_id.replace("-", "").lower()


def _setting(deps: StrategyDeps, config: ConfigStore, key: str, default: int) -> int:
    fallback = int(deps.s.strategy.get(key, default))
    return config.get_int(f"strategy.{key}", fallback) or fallback


def user_prompt(deps: StrategyDeps, config: ConfigStore, today: date, max_tips: int) -> str:
    rules = non_negotiables(deps.v16_blocks())
    adopted = adopted_tips(deps.existing(), limit=50)
    return "\n".join([
        f"Today is {today.isoformat()}. Return at most {max_tips} tips.",
        "Current rules (V16 non-negotiables):",
        *[f"- {r}" for r in rules],
        "Already adopted tips (skip these):",
        *([f"- {t}" for t in adopted] or ["- none"]),
    ])


# ---------------------------------------------------------------- cards


def ref(page_id: str) -> str:
    return f"Ref ST-{hex_id(page_id)[:8]}"


def tip_card(i: int, n: int, item: dict[str, Any]) -> Card:
    countries = ", ".join(item.get("countries") or ["All"])
    text = "\n".join([
        f"Tip {i}/{n} ({item.get('category')}, {countries})",
        item.get("tip") or "",
        f"Why: {item.get('why') or 'not given'}",
        f"Source: {item.get('source') or 'none'}",
        ref(item["id"]),
    ])
    pid = hex_id(item["id"])
    return Card(text, [("Adopt", f"sa:{pid}"), ("Reject", f"sr:{pid}")])


def confirm_card(run_id: str) -> Card:
    return Card("No new tips this month. Tap to confirm the monthly review.",
                [("Confirm review", f"sc:{run_id}")])


def pending_cards(run: dict[str, Any]) -> list[Card]:
    tips = run.get("tips") or []
    decided = run.get("decided") or {}
    return [tip_card(i, len(tips), item) for i, item in enumerate(tips, 1)
            if item["id"] not in decided]


# ---------------------------------------------------------------- the run


def _notes(tip: Tip, run_id: str, reason: str | None = None) -> str:
    notes = f"countries: {', '.join(tip.countries)}; why: {tip.why}; run {run_id}"
    return f"{notes}; auto-rejected: {reason}" if reason else notes


def _row(tip: Tip, status: str, today: date, notes: str) -> dict[str, Any]:
    return {"Tip / rule": tip.tip, "Category": tip.category, "Status": status,
            "Source": "\n".join(tip.sources), "Date added": today, "Notes": notes}


def run_update(deps: StrategyDeps, force: bool = False) -> UpdateResult:
    """Research, validate, log and ask. With undecided tips from an earlier run, re-send those
    cards instead (unless `force`, i.e. /update new)."""
    run = deps.state.get(RUN_KEY) or {}
    if run and not run.get("complete") and not force:
        cards = pending_cards(run) or ([confirm_card(run["run_id"])]
                                       if not run.get("tips") else [])
        if cards:
            return UpdateResult([f"You still have {len(cards)} to review from the run of "
                                 f"{run.get('started')}. Send /update new to research again."],
                                cards)
    config = deps.config()
    today = deps.today()
    llm = deps.llm(config)
    if llm is None:
        return UpdateResult(["/update cannot research: ANTHROPIC_API_KEY is not set."])
    max_tips = _setting(deps, config, "max_tips", 8)
    max_searches = _setting(deps, config, "max_searches", 8)
    try:
        raw, urls = llm.complete_json_with_search(
            STAGE, SYSTEM_PROMPT.replace("MAX_TIPS", str(max_tips)),
            user_prompt(deps, config, today, max_tips), max_tokens=MAX_TOKENS,
            max_uses=max_searches, key="update")
    except LLMError as exc:
        return UpdateResult([f"/update research failed: {exc}. Nothing was written; the gate "
                             "is unchanged."])
    patterns = deps.s.strategy.get("reject_patterns") or []
    existing = [v.get("Tip / rule") or "" for _, v in deps.existing()]
    checked = validate.check(validate.parse_tips(raw, max_tips), urls, patterns, existing)
    run_id = f"{today.strftime('%Y%m%d')}-{secrets.token_hex(2)}"

    def create(props: dict[str, Any], n: int) -> str:
        if deps.repo is None or not deps.write:
            log.warning("DRY RUN: would write to strategy: %s", props["Tip / rule"])
            return f"00000000-0000-4000-8000-{n:012x}"
        return deps.repo.create(props)

    for n, (tip, reason) in enumerate(checked.rejected, 1):
        create(_row(tip, "Rejected", today, _notes(tip, run_id, reason)), 100 + n)
    items = []
    for n, tip in enumerate(checked.kept, 1):
        page_id = create(_row(tip, "Testing", today, _notes(tip, run_id)), n)
        items.append({"id": page_id, "tip": tip.tip, "category": tip.category,
                      "countries": tip.countries, "why": tip.why, "source": tip.sources[0]})
    new_run = {"run_id": run_id, "started": today.isoformat(),
               "tip_page_ids": [i["id"] for i in items], "tips": items, "decided": {}}
    if deps.write:
        deps.state.set(RUN_KEY, new_run)
    summary = (f"Research done: {len(items)} tips to review, {len(checked.rejected)} rejected "
               f"automatically, {checked.no_source} without a search source dropped, "
               f"{checked.duplicates} duplicates dropped.")
    lines = [summary]
    lines += [f"Auto-rejected: {tip.tip} ({reason})" for tip, reason in checked.rejected]
    cards = pending_cards(new_run) if items else [confirm_card(run_id)]
    messages = ["\n".join(lines)]
    if deps.ind is not None:
        try:
            messages.append(deps.ind())
        except Exception as exc:  # the review goes on even when the IND check breaks
            log.exception("IND refresh failed")
            messages.append(f"IND register check failed: {exc}. Nothing was changed.")
    return UpdateResult(messages, cards)


# ---------------------------------------------------------------- monthly reminder


def run_strategy_reminder(deps: StrategyDeps, today: date | None = None) -> str | None:
    """Sent daily by Module 09; says something only when 3 or fewer days are left."""
    from jobengine.sweep.gate import last_update

    today = today or deps.today()
    config = deps.config()
    updated = last_update(config, deps.state, deps.s)
    days = config.get_int("strategy_refresh_days") or 30
    if updated is None:
        return "Strategy review due now: Config last_strategy_update is missing. Run /update."
    left = days - (today - updated).days
    if left > 3:
        return None
    if left > 0:
        return f"Strategy review due in {left} day{'s' if left != 1 else ''}. Run /update."
    if left == 0:
        return "Strategy review due today. Run /update."
    return f"Strategy review is {-left} days overdue: /fetch is blocked. Run /update."


# ---------------------------------------------------------------- decisions


def stamp(deps: StrategyDeps, config: ConfigStore) -> date:
    """Open the gate: Config in prod, bot_state elsewhere. Returns the date /fetch closes."""
    today = deps.today()
    if deps.s.app_env == "prod":
        if deps.stamp_config is None:
            raise StrategyError("Config last_strategy_update cannot be written here.")
        if deps.write:
            deps.stamp_config(config, today)
    elif deps.write:
        deps.state.set(STAMP_KEY, {"date": today.isoformat()})
    days = config.get_int("strategy_refresh_days") or 30
    return today + timedelta(days=days)


def _finish(deps: StrategyDeps, run: dict[str, Any]) -> list[str]:
    config = deps.config()
    until = stamp(deps, config)
    run["complete"] = True
    if deps.write:
        deps.state.set(RUN_KEY, run)
    lines = [f"Strategy updated. /fetch is open until {until.isoformat()}."]
    adopted = [i["tip"] for i in run.get("tips") or []
               if (run.get("decided") or {}).get(i["id"]) == "Adopted"]
    if adopted:
        lines += ["Adopted this month:", *[f"- {t}" for t in adopted], FOLD_REMINDER]
    return ["\n".join(lines)]


def decide(deps: StrategyDeps, page_hex: str, adopt: bool) -> list[str]:
    """sa:<hex> / sr:<hex>: Adopt or Reject one tip of the current run."""
    run = deps.state.get(RUN_KEY) or {}
    item = next((i for i in run.get("tips") or [] if hex_id(i["id"]) == page_hex.lower()), None)
    if item is None:
        return ["That tip is not in the current review. Send /update to see what is open."]
    decided = run.setdefault("decided", {})
    if item["id"] in decided:
        return [f"Already decided: {decided[item['id']]}."]
    status = "Adopted" if adopt else "Rejected"
    if deps.repo is not None and deps.write:
        deps.repo.update(item["id"], {"Status": status})
    decided[item["id"]] = status
    if deps.write:
        deps.state.set(RUN_KEY, run)
    left = len(run["tips"]) - len(decided)
    if left:
        return [f"{status}: {item['tip']}\n{left} left to review."]
    return [f"{status}: {item['tip']}", *_finish(deps, run)]


def confirm_review(deps: StrategyDeps, run_id: str) -> list[str]:
    """sc:<run id>: the zero-tip month still counts once you confirm it."""
    run = deps.state.get(RUN_KEY) or {}
    if run.get("run_id") != run_id or run.get("tips"):
        return ["That review is no longer open. Send /update."]
    if run.get("complete"):
        return ["Already confirmed."]
    return _finish(deps, run)


def add_note(deps: StrategyDeps, replied_to: str, text: str) -> str | None:
    """A reply to a tip card (Ref ST-xxxxxxxx) is stored in the row's Notes."""
    match = REF_RE.search(replied_to or "")
    if not match:
        return None
    run = deps.state.get(RUN_KEY) or {}
    item = next((i for i in run.get("tips") or [] if hex_id(i["id"]).startswith(match.group(1))),
                None)
    if item is None or deps.repo is None:
        return "That tip is no longer in the current review."
    row = deps.repo.get(item["id"]) or {}
    notes = (row.get("Notes") or "").strip()
    line = f"your note: {' '.join(text.split())}"
    if deps.write:
        deps.repo.update(item["id"], {"Notes": f"{notes}\n{line}" if notes else line})
    return "Note saved on the tip."


# ---------------------------------------------------------------- deps


def fake_deps(s: Settings, write: bool = True, state: BotState | None = None,
              key: str = "update") -> StrategyDeps:
    """Seeded Strategy rows, a fake V16 page, FakeLLM research (fixtures/llm/strategy/<key>)
    and, in prod settings, a recorder instead of the Config write."""
    import json

    from jobengine.bot_state import FakeBotState
    from jobengine.llm import FakeLLM
    from jobengine.notion_repo import FakeStrategyRepo
    from jobengine.safety import check_config_write
    from jobengine.settings import ROOT_DIR

    base = ROOT_DIR / "fixtures" / "strategy"
    repo = FakeStrategyRepo.from_fixture(base / "strategy_seed.json")
    llm = FakeLLM()
    stamped: list[tuple[str, str, date]] = []

    def stamp_config(config: ConfigStore, day: date) -> None:
        check_config_write(STAMP_KEY)
        stamped.append((STAMP_KEY, day.isoformat(), day))

    class _FixedKey:
        """FakeLLM with the fixture key chosen by the caller."""

        calls = llm.calls

        def complete_json_with_search(self, stage, system, user, **kwargs):
            kwargs["key"] = key
            return llm.complete_json_with_search(stage, system, user, **kwargs)

        def complete_json(self, *args, **kwargs):
            return llm.complete_json(*args, **kwargs)

    from jobengine.strategy import ind_refresh

    ind_writes: list[tuple[str, dict[str, Any]]] = []

    def ind_writer(page_id: str, props: dict[str, Any]) -> None:
        from jobengine.safety import check_target_companies_write

        check_target_companies_write(list(props))
        ind_writes.append((page_id, props))

    ind_deps = ind_refresh.fake_deps(s, writer=ind_writer, write=write)
    deps = StrategyDeps(
        s=s, config=ConfigStore.fake, repo=repo, existing=repo.all_rows,
        state=state or FakeBotState(), llm=lambda config: _FixedKey(),
        v16_blocks=lambda: json.loads((base / "v16_page.json").read_text(encoding="utf-8")),
        stamp_config=stamp_config, write=write,
        ind=lambda: ind_refresh.refresh(ind_deps).text(),
    )
    deps.ind_deps = ind_deps  # type: ignore[attr-defined]  # read by tests and the CLI
    deps.ind_writes = ind_writes  # type: ignore[attr-defined]
    deps.stamped = stamped  # type: ignore[attr-defined]  # read by tests
    return deps


def real_deps(s: Settings, state: BotState, write: bool = True) -> StrategyDeps:
    from jobengine.llm import AnthropicLLM
    from jobengine.notion_repo import (
        NotionClient,
        config_writer_for,
        page_values,
        strategy_repo_for,
    )
    from jobengine.resume.master import fetch_blocks
    from jobengine.safety import llm_allowed

    if not s.notion_token:
        raise StrategyError("/update cannot run: NOTION_TOKEN missing")
    client = NotionClient(s.notion_token, s)
    repo = strategy_repo_for(s, client)
    read_id = s.notion_read.get("strategy", "")

    def existing() -> list[tuple[str, dict[str, Any]]]:
        rows = [(p["id"], page_values(p)) for p in client.query(read_id)] if read_id else []
        if repo is not None and repo.data_source_id != read_id:
            rows += repo.all_rows()
        return rows

    def stamp_config(config: ConfigStore, day: date) -> None:
        writer = config_writer_for(s, client, config)
        if writer is None:
            raise StrategyError("Config is not writable in this environment.")
        writer(STAMP_KEY, day.isoformat(), day)

    from jobengine.strategy import ind_refresh

    ind_deps = ind_refresh.real_deps(s, write=write)
    deps = StrategyDeps(
        s=s, config=lambda: ConfigStore.load(client, s), repo=repo, existing=existing,
        state=state, llm=lambda config: AnthropicLLM(s, config) if llm_allowed(s) else None,
        v16_blocks=lambda: fetch_blocks(client, s.notion_pages.get("v16_spec", "")),
        stamp_config=stamp_config if s.app_env == "prod" else None, write=write,
        ind=lambda: ind_refresh.refresh(ind_deps).text(),
    )
    deps.ind_deps = ind_deps  # type: ignore[attr-defined]
    return deps
