# Strategy gate and IND register refresh (Module 08)

`/fetch` is blocked when the strategy is older than Config `strategy_refresh_days` (30).
`/update` runs web research on current ATS, application, outreach and interview practice for
DevOps roles in Poland, the Netherlands and Ireland, logs each tip to Strategy and asks you to
Adopt or Reject it in Telegram. When every tip of the run is decided, the gate is stamped and
`/fetch` opens again. The same run refreshes the IND sponsor status of the Dutch target
companies. `/rules` shows the current rules.

## Rules

- **Research is never applied.** The bot writes Strategy rows and asks you. It never edits the
  V16 page, the Resume Build Spec or the Cold Mail Templates (a runtime guard in the Notion
  client refuses any write to the pages under `notion.pages`, and a repo test checks the
  code), and the only Config value it writes is `last_strategy_update`.
- **Known myths are rejected automatically** and still logged as `Rejected` with the reason:
  the patterns in `config/base.yaml` `strategy.reject_patterns` (keyword stuffing, white or
  hidden text, ATS score checkers, copying the job description, fake experience, mass
  identical applications, mail tracking) and any tip the model marks as conflicting with V16.
- **Sources required.** A tip counts only with a source URL that the web search actually
  returned; tips without one are dropped.
- **Duplicates** of existing Strategy rows (rapidfuzz ratio 85 or more) are dropped.
- **The gate opens only after the review is complete.** A month with no new tips needs one
  `Confirm review` tap.
- Target Companies: only `IND sponsor` and `Last checked` are written, in prod only
  (`safety.target_companies_writable_fields`).

## Research

One LLM call, stage `strategy` (Config `model.strategy`; haiku outside prod), with Anthropic's
server-side web search tool (`web_search_20250305`, `max_uses` = Config
`strategy.max_searches`, 8). It is `complete_json_with_search` in `llm.py`, the only call that
enables web search. The prompt carries the V16 non-negotiables (read from the V16 page) and
the adopted Strategy tips, and asks for at most Config `strategy.max_tips` (8) tips.

## Strategy rows

`Tip / rule`, `Category`, `Status` (`Testing` = waiting for your review, or `Rejected` for
automatic rejections), `Source` (one URL per line), `Date added`, and `Notes`
(`countries: ...; why: ...; run <id>`, plus `auto-rejected: <reason>`). Prod writes Strategy;
local and dev write Strategy (DEV).

## Telegram

```
Tip 2/3 (Application, Netherlands)
Check the current Highly Skilled Migrant salary threshold before applying in the Netherlands.
Why: The threshold changes every January.
Source: https://recruiting.example.org/netherlands-hsm-salary-2026
Ref ST-50000008
[Adopt] [Reject]
```

Reply to a card with text to store it in the tip's Notes (`your note: ...`). `/update` while
tips are undecided re-sends those cards; `/update new` researches again. After the last
decision: `Strategy updated. /fetch is open until <date>.` and the adopted tips with the
reminder to fold them into V16 by hand.

## Stamping the gate

- Prod: Config `last_strategy_update` = today (Value and Updated).
- Local and dev (Config is read only there): `bot_state["last_strategy_update"]`. Outside
  prod the gate uses that date when it is newer than Config; in prod it reads only Config.

## IND register refresh

At the end of every `/update` (and `python -m jobengine.strategy ind`): the register page
(Config `ind_register.url`) is downloaded and parsed, and every Target Companies row with
Region `Netherlands` is matched (as in screening): `Verified` or `Not listed`. Prod writes
`IND sponsor` (when it changed) and `Last checked`; elsewhere the report lists what would
change. A failed download changes nothing. A change from `Verified` to `Not listed` is
highlighted, because it demotes that company's jobs in screening.

## Monthly reminder

`run_strategy_reminder` (sent daily by Module 09) says `Strategy review due in N days. Run
/update.` when 3 or fewer days are left, and nothing on other days.

## Notion prerequisites

- Strategy (DEV) in the DEV Sandbox with the Strategy schema (done; its ID is in
  `config/local.yaml` and `config/dev.yaml`).
- Config `strategy.max_tips` = 8 and `strategy.max_searches` = 8 (done); `model.strategy`,
  `strategy_refresh_days`, `last_strategy_update` and `ind_register.url` exist.

## Run it

```bash
python -m jobengine.strategy update --fake --today 2026-10-01   # fixture research
python -m jobengine.strategy ind --fake
python -m jobengine.strategy remind --fake --today 2026-10-20
```
