# Screening (Module 03)

Screening reads every Job Opportunities row whose `Screen verdict` is `Unscreened`, asks the
LLM what the job description actually says, and decides: `Skip` (with a reason), `Apply high`,
`Apply normal`, `Apply low` or `Needs review`. You then go through the results in Telegram
with `/pending`, one job at a time.

## Running it

```bash
# Fixture rows, fake LLM answers and a fake IND register. No network, nothing real written.
python -m jobengine.screen --fake --today 2026-10-01

# Real run: screens every Unscreened row in Job Opportunities for this env
python -m jobengine.screen

# Show the verdicts without writing anything (safe on real data)
python -m jobengine.screen --no-write

# Screen one row again: page ID, URL or posting ID (for example linkedin:4012345678)
python -m jobengine.screen --row https://jobs.example.com/123
```

## Daily limit

At most `screening.daily_limit` jobs (30, in `config/base.yaml`) go to the LLM per day,
counting every run that day together (CLI, `/fetch`, `/screen`, the daily job). The newest
postings go first; the rest stay `Unscreened` and are picked up by the next day's runs.

- The count is in Bot State, key `screen.day` (`{"date": ..., "count": ...}`).
- Rows without a description never reach the LLM and do not count.
- `--no-write` runs are capped too but do not use up the day.
- `--row` / `/screen <url>` / `/jd` always run (your explicit ask) and count toward the day.
- `--limit N` caps one CLI run at N instead of what is left of today.

When the limit is reached the summary says so:
`daily limit 30 reached (30 screened today): 99 Unscreened rows wait for the next run.`

On your laptop use `APP_ENV=local` (the default): only `local` reads `.env`, and it still
writes to the DEV Sandbox. On Windows cmd: `set APP_ENV=local`. A real run needs
`NOTION_TOKEN` and `ANTHROPIC_API_KEY` in `.env`, and the `job-engine-dev` integration
connected to every reference database (a 404 "Could not find database" means one is not).
`DRY_RUN` does not block LLM calls. Outside prod the model is always `claude-haiku-4-5`.

Fake output:

```
Screening done: 14 screened (5 high, 1 normal, 2 low, 1 needs review, 5 skipped), 1 waiting for JD.
- Apply high: Tulip Data B.V., Cloud Engineer (Utrecht) | ... | visa: On IND register, Sponsorship stated
- Apply high: Vistula Cloud, DevOps Engineer (Kraków) | 3 strong, 1 transferable, 0 gaps
...
- Skip (Tech mismatch): Odra Systems S.A., Platform Engineer (Poznań) | ...
```

## Free checks before the AI

Before a job goes to the LLM it is checked for free, with the same rules the sweep uses
(docs/sweep.md, "Only jobs you can take"):

- a Lead, Principal, Staff, Head of, VP or Vice President title;
- the years, the higher of `Years required` and the years in the description text, over
  `screening.max_years_required` (4); with no years stated, a Senior title or a description
  that calls the role senior;
- a language other than English required, in the description or the title, or a posting
  written in Polish or Dutch, even when it also asks for English (`Polish required` / `Dutch
  required`; other languages are `Other`, with the language in the note);
- a job in Poland with a B2B contract only (`B2B only`);
- no visa sponsorship or relocation stated, or the right to work required (`No
  sponsorship`, note `No visa sponsorship`). The Skip reason options are not changed from
  code: the `No sponsorship` option is added to the column by hand (done in DEV; prod in
  go-live step B3).

Such a job is skipped at once (`Skip`, reason `Seniority`, `Experience >5 yrs` above 5
years, or the language or contract reason) with the note `(no AI used)`; it does not count
toward the daily 30. When the description asks for more years than the `Years required`
cell says, the cell is raised (`2 years` becomes `5 years`). The same limit applies after
the LLM reads the years.

## Several jobs at once

After the first job goes through the LLM, screening sends `screening.workers` (4) jobs at the
same time, never more than what is left of the daily limit. The first job goes alone, so a
refused API key still stops the run after one call. Results and the daily count are the same
as one at a time; only the wait is shorter. `screening.workers: 1` screens one at a time.

## Keeping the AI cheap

- The free checks above skip many jobs before any AI call; the reply says how many
  (`N skipped without the AI`).
- The AI reads the description without legal boilerplate (`src/jobengine/jdtrim.py`):
  privacy and data-processing notices (GDPR, the Polish RODO clause) and equal-opportunity
  statements, when they are a paragraph of 120 characters or more, and repeated lines. A
  short line such as "Knowledge of GDPR" is a requirement and stays. Quotes are still checked
  against the full description, and the Notion page keeps the full text.
- The reply ends with `AI used: N calls, X tokens in, Y out, about $Z` for that run, priced
  from `PRICES_PER_MTOK` in `src/jobengine/llm.py` (cache reads at a tenth of the input
  price). Web searches (`/update`) are billed on top and are not in that figure.
- Resumes use Haiku too (Config `model.tailor` = `claude-haiku-4-5`; local and dev always
  use Haiku).

## Half-price batch

`/screen batch` (CLI: `python -m jobengine.screen --batch`) runs the free checks now (skips
are written at once) and sends the other Unscreened jobs, up to what is left of today's
limit, to Anthropic's Message Batches API in one batch. Batch calls cost half the normal
price. Anthropic answers most batches within an hour, at most within 24 hours. The batch ID
and its rows are kept in Bot State (`screen.batch`), and the daily count goes up when the
batch is sent.

`/screen collect` (CLI: `--collect`) checks the batch. While it is still working, the reply
says how many answers are ready. Once it has ended, each answer goes through the same quote
check, gates, tier and Notion writes as a normal screening, and the reply ends with the half
price `AI used` line. A row whose answer failed stays Unscreened for the next `/screen`.

Only one batch waits at a time. Plain `/screen` leaves the rows of a waiting batch alone and
says how many wait, so no job is paid for twice. A good habit: `/screen batch` in the
evening, `/screen collect` in the morning.

## When the API key is refused

If Anthropic answers HTTP 401 or 403 (a wrong or revoked `ANTHROPIC_API_KEY`, or an account
without credit), screening stops after the first job with `Screening stopped: Anthropic
refused ANTHROPIC_API_KEY (HTTP 401) ...` instead of trying every job. Nothing is written and
the daily count does not change. The key is read only when the bot starts: put the new key in
`.env`, stop the bot and start it again (on Cloud Run: add a secret version and redeploy),
then run `/screen`.

## The flow

1. Load Config, the reference data (Skills Inventory, Term Map, Target Companies, all read
   only) and the rows with `Screen verdict` = `Unscreened`.
2. Read the description from the page body: the newest block that starts with
   `Description source:`, up to the next one or a `Screening (` section.
   - `full`: at least 600 characters (`screening.full_min_chars`) and not `(snippet only)`
   - `snippet`: marked `(snippet only)` or shorter
   - `none`: no description. The row stays `Unscreened` and counts as "waiting for JD".
     LinkedIn is never fetched, so LinkedIn rows wait here until you paste the JD with `/jd`,
     unless the sweep found the same job on another site (see "LinkedIn descriptions from other
     sites" in [sweep.md](sweep.md)).
3. One LLM call (stage `score`) extracts the facts, then the quote check, the gates, the
   tier and the visa checks run, and the results are written.

## The quote rule (nothing is invented)

Every value the LLM returns must carry a verbatim quote from the description. The quote is
compared whitespace- and case-insensitively. A value whose quote is not in the text is
dropped and treated as "not stated", and the run logs which fields were dropped. Salary,
sponsorship and dates are only taken when the text states them. `specific_details` (for
Module 06 mails) must have at least 60% of their content words in the description, at most
8 words each, at most 3. The LLM only ever gets the job title, company, country and
description: never contacts, emails or phone numbers.

## Gates (first hit wins, verdict `Skip`)

| # | Gate | Fires when | Skip reason |
|---|---|---|---|
| 1 | Seniority / years | `lead`, `principal`, `head of`, `staff`, `vp` in the title; more years required than the limit; lead or principal in the text only when no years are stated ("3+ years" and "lead technical projects" passes) | `Seniority` / `Experience >5 yrs` |
| 2 | Language | Polish or Dutch is mandatory ("a plus" or "preferred" never skips) | `Polish required` / `Dutch required` |
| 3 | B2B only | Poland and B2B is the only contract stated | `B2B only` |
| 4 | Tech mismatch | a mandatory tool has no term backed by a Production or Hands-on skill or an Active Term Map row (`Learning` skills and `(none)` rows do not back). Terms match without extra words (`MS Azure` is Azure, `Linux OS` is Linux, `Apache Kafka` is Kafka), each part of a slash term counts (`Unix/Linux` is backed by Linux) and by other names (`Amazon Web Services` is AWS, `K8s` is Kubernetes). General phrases with no tool name (`build tooling`, `artifact repositories`, `CI/CD pipelines`) never fail it | `Tech mismatch`, unbacked terms in `Gaps` |
| 5 | Already applied | another row with the same Dedupe key, or the same company and role, is Applied or later | `Already applied` |
| 6 | Expired | Expires is before today | `Expired` |

Exactly 5 years passes and only ranks lower.

## Tier

Every requirement gets a strength: `Strong` (Production skill or Active Term Map row),
`Transferable` (Hands-on skill) or `Gap`. `gap_count` counts the gaps in mandatory non-tool
requirements and nice-to-haves.

- Employer size (Config `employer.size_rules`): `large` for Target Companies Tier 1, 2, 3, 4
  or 6, or `On IND register`; `weak` for a company outside Target Companies without stated
  sponsorship, `Agency posting (IE)` or `Not on IND register`; otherwise `normal`.
- Permanent contract: Poland `UoP` or both; Netherlands and Ireland `permanent` stated, or not
  stated as `contract`.
- Base: 0 gaps, large employer and permanent contract: `Apply high`. At most 1 gap:
  `Apply normal`. 2 gaps: `Apply low`. 3 or more: `Needs review`.
- Then: stated sponsorship moves up one tier and adds the Visa flag `Sponsorship stated`; a
  weak employer is capped at `Apply low`; a job that cannot sponsor (sponsorship stated no,
  `Not on IND register`, `Agency posting (IE)`) becomes at most `Apply low` and goes to the
  bottom of `/pending`. A `Needs review` row is never promoted by this rule.
- A snippet that survives the gates is `Needs review` ("snippet only, paste the full JD
  with /jd").

`/pending` order: tier, then rows that cannot sponsor last, then points: years 2 to 4 +3,
unknown +1, exactly 5 -2; sponsorship stated +3; Target Companies tier 1 to 4 or 6 +2; ghost
risk High -5, Medium -2; posted in the last 7 days +1.

## Country rules

- Netherlands: Target Companies `IND sponsor` `Verified` gives `On IND register`, `Not listed`
  gives `Not on IND register`. Other companies are looked up in the IND public register
  (Config `ind_register.url`), downloaded once per run through `http.py` (`ind.nl` is on the
  allowlist). Names are compared without `B.V.`, `N.V.`, `Holding`, `Nederland` and
  `Netherlands`, with rapidfuzz token-set ratio 90 or more. A second check (token-sort ratio
  75 or more) stops a short name matching a longer one only because its words are a subset
  ("Tulip" is not "Tulip Data"). If the page cannot be downloaded or parsed there is no flag
  and the summary says "IND register unavailable".
- Agency posts (all countries since 30 Sep): the text says it is posted for a client, the
  company starts with a known agency name (`screen/visa.py` KNOWN_AGENCIES, plus Config
  `agency_names` and `ireland.agency_names`, comma-separated), or its name says recruitment,
  staffing, personnel, headhunting, executive search, talent solutions or human capital.
  Ireland: `Agency posting (IE)` (an agency cannot hold a Critical Skills permit for you, so
  the job is capped as below). Other countries: `Agency posting`, the job is kept as it is.
  `/fetch` already marks a new job whose company name is an agency's, and screening marks
  it even when a skip rule fires (1 Oct: Verks Recruitment and VERITA HR were skipped
  unmarked). Either way the outreach planner and `/fetchcontacts` send no cold mails and spend no
  credits on it (apply through the agency); `/contacts <job>` still works if you want.
- Salary: `Salary below visa minimum` only when the text states a salary with a clear period
  (per year or per month) and Config has `visa.salary_threshold.<country>`. Ireland also gets
  `IE lower band - degree risk` when Config has `visa.ie_lower_band_max`. Thresholds are never
  hardcoded; without the Config keys nothing is flagged. Hourly or daily pay, or text without
  a period, is ambiguous and ignored.

## What gets written

`Screen verdict`, `Skip reason` (Skip only), `Status` = `Screened` (only from `New`),
`Language required`, `Contract type` (Poland; others `Unknown`), `Sponsorship`, `Work mode`
and `Expires` (only if stated), `Salary` and `Years required` (only if empty, as `3+ years`, or raised when the description
asks for more),
`Visa flags`
(merged, never removed), `Tech stack`, `Gaps`, and a body section:

```
Screening (V16, 2026-10-01)
Strong | Kubernetes in production | Kubernetes
Transferable | Prometheus | Prometheus
Gap | CS degree | none
Specific details: moving our workloads to EKS ; running GitOps with Argo CD
Verdict: Apply high
```

A re-screen (`/screen <ref>` or `--row`) appends a new section and never deletes blocks. It
sets `Status` to `Screened` only if it is `New` or `Screened`, so a row never moves backwards.

## Telegram

- **Apply high alert:** after `/screen` (and `/screen collect`), each new Apply high job
  comes as its own card, starting with `Apply high: apply today while the posting is fresh.`,
  with **Approve** and **Skip**, so you can apply the same day. At most 5 cards at once; the
  rest are in `/pending`. Jobs you already approved or skipped get no card. `/autopilot`
  approves them itself, so it sends no alert.
- Typing `/` shows the command menu. The bot registers it with Telegram (`setMyCommands`)
  each time it starts, and `python -m jobengine.deploy.set_webhook` does the same for
  Cloud Run.
- Every command and button shows "typing..." until the reply is ready (sent again every 4
  seconds, since Telegram clears it after about 5).
- `/screen` without a job keeps one progress message updated ("Screening: 10 of 24 jobs
  checked"), like `/fetch`.
- A button tap is answered at once with a short note ("Skipping...", "Approving, building
  your resume...") and "typing...", then the reply follows. Skip reuses the review list it
  already read, so it needs 2 Notion requests instead of 4.
- `/fetch` runs the sweep, then screening, and ends with `N ready to review: /pending`.
- `/pending` shows one card at a time, best first, with `Approve`, `Skip` and `Next`.
- One country at a time (30 Sep): when the jobs waiting are in more than one country,
  `/pending` first shows a button per country with its count (Poland, Netherlands, Ireland,
  Remote EU for remote jobs open across Europe) and All. After a tap only that country's
  jobs come, and the next job after Skip, I applied or Not applying stays in it, so the VPN
  is switched once per country. Each card has `Change country`; when a country is done the
  buttons of the others come. `/pending poland` (pl), `netherlands` (nl), `ireland` (ie),
  `remote` or `all` goes straight to one. The choice is kept in Bot State `pending.country`.
  Below the screening match counts the card shows, from the description and your reference
  data (free, no AI, `sweep/rank.py`):
  - `Skill match: 75% (3 of 4 tools it names)`: the tools the job names that you have.
  - `Missing keywords: java, snowflake`: tools it names that you do not have (at most 6).
  - `Use their word: EKS (your Kubernetes on AWS)`: the job's word for a skill you have under
    another name (Active Term Map rows); say it their way in the resume and the mail.
  Approve sets `Status` = `Approved` and builds the resume (see `docs/resume-builder.md`),
  Skip sets `Declined`. A card already handled (a double tap, or changed in Notion) answers
  "Already handled". Buttons only work for your chat ID.
- `/jd <url>` starts a paste. Send the description in as many messages as you like, then
  `/done`. It is saved in the page body as `Description source: pasted (full)` and screened at
  once. If the job is not in Notion yet, start with `Company: ...` and `Role: ...` lines
  (optionally `Country:` and `City:`). The paste is kept in Bot State (key `jd_capture`), so a
  bot restart does not lose it; after 20 minutes without `/done` it is discarded.
- `/jd` alone lists the LinkedIn jobs waiting for a description.
- `/screen` screens what is not screened yet; `/screen <url or id>` screens one job again.

In the fake bot (`python -m jobengine.telegram_bot --fake`), `tap <data>` presses a button,
for example `tap ap:pl-clean`. The fake bot uses the sweep and screening fixtures in one
in-memory Job Opportunities; rows without an LLM fixture are screened as "nothing stated".

## Checking it on your laptop

1. `python -m jobengine.screen --fake --today 2026-10-01` prints the summary above.
2. With `APP_ENV=local` and `NOTION_TOKEN` and `ANTHROPIC_API_KEY` in `.env`:
   `python -m jobengine.screen --no-write` shows what real screening would decide.
3. Then `python -m jobengine.screen` writes the results to Job Opportunities (DEV).
4. In Telegram: `/pending`, tap the buttons, and try `/jd` with a LinkedIn job.
