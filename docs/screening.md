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

On Windows cmd set the environment first: `set APP_ENV=dev`, run the command, then
`set APP_ENV=` to clear it. A real run needs `NOTION_TOKEN` and `ANTHROPIC_API_KEY`.
`DRY_RUN` does not block LLM calls. Outside prod the model is always `claude-haiku-4-5`.

Fake output:

```
Screening done: 14 screened (5 high, 1 normal, 2 low, 1 needs review, 5 skipped), 1 waiting for JD.
- Apply high: Tulip Data B.V., Cloud Engineer (Utrecht) | ... | visa: On IND register, Sponsorship stated
- Apply high: Vistula Cloud, DevOps Engineer (Kraków) | 3 strong, 1 transferable, 0 gaps
...
- Skip (Tech mismatch): Odra Systems S.A., Platform Engineer (Poznań) | ...
```

## The flow

1. Load Config, the reference data (Skills Inventory, Term Map, Target Companies, all read
   only) and the rows with `Screen verdict` = `Unscreened`.
2. Read the description from the page body: the newest block that starts with
   `Description source:`, up to the next one or a `Screening (` section.
   - `full`: at least 600 characters (`screening.full_min_chars`) and not `(snippet only)`
   - `snippet`: marked `(snippet only)` or shorter
   - `none`: no description. The row stays `Unscreened` and counts as "waiting for JD".
     LinkedIn is never fetched, so LinkedIn rows wait here until you paste the JD with `/jd`.
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
| 1 | Seniority / years | lead or principal in the text, `lead`, `principal`, `head of` or `staff` in the title, or more than 5 years required | `Seniority` / `Experience >5 yrs` |
| 2 | Language | Polish or Dutch is mandatory ("a plus" or "preferred" never skips) | `Polish required` / `Dutch required` |
| 3 | B2B only | Poland and B2B is the only contract stated | `B2B only` |
| 4 | Tech mismatch | a mandatory tool has no term backed by a Production or Hands-on skill or an Active Term Map row (`Learning` skills and `(none)` rows do not back) | `Tech mismatch`, unbacked terms in `Gaps` |
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
- Ireland: `Agency posting (IE)` when the text says it is posted for a client, or the company
  is listed in Config `ireland.agency_names`.
- Salary: `Salary below visa minimum` only when the text states a salary with a clear period
  (per year or per month) and Config has `visa.salary_threshold.<country>`. Ireland also gets
  `IE lower band - degree risk` when Config has `visa.ie_lower_band_max`. Thresholds are never
  hardcoded; without the Config keys nothing is flagged. Hourly or daily pay, or text without
  a period, is ambiguous and ignored.

## What gets written

`Screen verdict`, `Skip reason` (Skip only), `Status` = `Screened` (only from `New`),
`Language required`, `Contract type` (Poland; others `Unknown`), `Sponsorship`, `Work mode`
and `Expires` (only if stated), `Salary` and `Years required` (only if empty), `Visa flags`
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

- `/fetch` runs the sweep, then screening, and ends with `N ready to review: /pending`.
- `/pending` shows one card at a time, best first, with `Approve`, `Skip` and `Next`.
  Approve sets `Status` = `Approved` (resume building arrives in Module 04), Skip sets
  `Declined`. A card already handled (a double tap, or changed in Notion) answers
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
2. With `APP_ENV=dev`, `NOTION_TOKEN` and `ANTHROPIC_API_KEY` set:
   `python -m jobengine.screen --no-write` shows what real screening would decide.
3. Then `python -m jobengine.screen` writes the results to Job Opportunities (DEV).
4. In Telegram: `/pending`, tap the buttons, and try `/jd` with a LinkedIn job.
