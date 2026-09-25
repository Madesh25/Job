# Job sweep (Module 02)

The sweep finds new job postings and writes them into **Job Opportunities**. It does no
screening, scoring, resume or mail work. Screening is Module 03.

## Running it

```bash
# Fixtures for every source and an in-memory Notion. No network, nothing written anywhere.
python -m jobengine.sweep --fake --today 2026-10-01

# Real run (reads Notion Config and Target Companies, writes Job Opportunities for this env)
python -m jobengine.sweep

# Only one source
python -m jobengine.sweep --source adzuna

# Check the Gmail alert parser against real emails without writing anything
python -m jobengine.sweep --parse-report
```

In Telegram, `/fetch` runs the same sweep. It replies at once, keeps one message updated with
the current step in plain words ("Searching Adzuna...", "Saving to your Notion: 125 of 300
jobs checked"), then sends a short summary: new jobs per country (in Config
`countries.active` order), jobs seen again, jobs skipped, possible ghost jobs, and any source
that was not checked. The terminal CLI keeps the detailed summary shown below. With
`python -m jobengine.telegram_bot --fake`, `/fetch` uses the fixtures and the in-memory repo.

Progress lines (time, source counts, pages, rows processed) go to stderr while it runs; the
summary is printed at the end.

`--fake` runs default to `--today 2026-10-01` so the fixture dates and the strategy gate stay
meaningful. Exit codes: 0 done, 1 could not run (for example `NOTION_TOKEN` missing), 2 blocked
by the strategy gate.

## What a sweep does

1. **Strategy gate.** Config `last_strategy_update` (leading `YYYY-MM-DD`) and
   `strategy_refresh_days`. When the update is older than the refresh days the sweep stops with
   `/fetch is blocked: strategy last updated <date>, older than <n> days. Run /update first.`
2. **Load.** Target Companies (read only) and the Job Opportunities index (dedupe keys and
   posting IDs).
3. **Collect** from each source: Gmail alerts, Adzuna, ATS feeds.
4. **Normalise and scope.** Title filters, country and city, seniority, years required.
5. **Dedupe and write.** Create new rows, update existing ones.
6. **Ghost risk** for every row touched.
7. **Summary**, for example:
   `Sweep done: 8 new, 4 updated, 2 reposts, 3 skipped (out of scope), 1 high ghost risk. Sources: gmail 6, adzuna 7, ats 4. Not supported: 2 companies.`
   Extra lines follow for skipped sources and notes, and the High ghost risk jobs come last.

Counts: **new** rows created; **updated** existing rows seen again, either with a posting ID
they already had or on another board (a first ID from a new source); **reposts** existing rows
that got a new posting ID from a source they already had; **skipped** postings dropped as out of scope; **Sources** raw postings per
source; **Not supported** Target Companies on Workday, Custom or unknown boards.

## Sources

| Source | Needs | Notes |
|---|---|---|
| Gmail alerts | `GMAIL_ALERTS_TOKEN_JSON` (madeshwaranm02, `gmail.readonly`) | Query `sweep.gmail.query`. Board from the sender domain. Links are read from the email and never requested, tracking links included. Descriptions are never set. |
| Adzuna | `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | `pl` and `nl` only, at most `sweep.adzuna.max_calls_per_run` calls, split evenly between the countries (3 each by default) so every country is searched. The description is a snippet. Predicted salaries are ignored. |
| ATS feeds | nothing | Active Target Companies on Greenhouse, Lever or SmartRecruiters, detected from `Careers URL` or `sweep.ats_boards`. Board is `Company site`. |

A source whose secret is missing is skipped with a line in the summary; the run continues.

### ATS details

- Greenhouse: `boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`. Posted date is
  `first_published` only; `updated_at` is never used.
- Lever: `api.lever.co/v0/postings/{token}?mode=json` (`api.eu.lever.co` for `jobs.eu.lever.co`
  URLs). Posted date from `createdAt`. An explicit `salaryRange` is stored.
- SmartRecruiters: `api.smartrecruiters.com/v1/companies/{token}/postings`, then one detail call
  per posting that passed the filters, at most `sweep.ats.max_detail_calls` per run.
- Every feed is filtered by title and location before any detail call. Postings filtered out
  here are reported in one note line and not counted as skipped.
- For a company whose careers page is on its own domain, add an override in
  `config/base.yaml`:

  ```yaml
  sweep:
    ats_boards:
      Shamrock Systems: {ats: greenhouse, token: shamrocksystems}
  ```

## Scope and normalising

- Titles must contain a `sweep.title_include` term and no `sweep.title_exclude` term, matched as
  whole words on the canonical title (so "Internal" does not match "intern"). Lead, Principal,
  Staff, Manager and similar are dropped.
- Seniority: Junior (`junior`, `jr`), Mid (`mid`, `regular`, `medior`), Senior (`senior`, `sr`),
  else Unknown.
- Years required: the smallest explicit number in the description (`3+ years`, `at least 4
  years`, `minimum 3 years`, `3-5 years`, `3 lata`, `min. 4 lat`). Empty otherwise.
- Country and city come from `sweep.locations`. Adzuna `location.area` is tried first. A
  country with no city but "remote" in the location gives city `Remote`. A country missing
  from Config `countries.active`, or a location that cannot be placed, is out of scope.
- Canonical form: lowercase, accents stripped (plus `ł` to `l`), only `a-z 0-9 + #` and single
  spaces. Company legal suffixes (`Sp. z o.o.`, `S.A.`, `B.V.`, `N.V.`, `Ltd`, `GmbH` and so on)
  and title gender markers (`(m/f/d)`, `(k/m)`, `m/w/d`, `(f/m/x)`, `(remote)`, `(hybrid)`)
  are removed. City aliases map to one name (`warsaw` to `warszawa`, `the hague` and
  `s gravenhage` to `den haag`, `cracow` to `krakow`).
- Salary and posted date are only stored when the source states them. Sponsorship is never set.

## Dedupe and writing

- Dedupe key: `company|title|city` (or country when there is no city), all canonical.
- `Posting IDs` holds `source:id` values, comma separated, last 50 kept.
- New key: a row with Company, Role, City, Country, Board, URL, Posted date, Salary, Seniority,
  Years required, Dedupe key, Posting IDs, First seen, Swept date, Times seen 1, Status New,
  Screen verdict Unscreened and Ghost job risk. The description goes into the page body
  (first line `Description source: <source>`, plus ` (snippet only)` for snippets).
- Existing key: Swept date is set to today. A new posting ID is always appended. Times seen
  goes up by one only for a repost: a new ID from a source the row already has. The same job
  seen on another board (the first ID from a new source) and a known posting ID do not change
  it. Empty URL, Posted date, Salary, Years required
  and page body are filled; filled values are never overwritten. Ghost risk is recomputed.
  **Status and Screen verdict are never changed.**
- The data source ID always comes from `safety.notion_write_target("job_opportunities", s)`.
  Local and dev write only to `Job Opportunities (DEV)`. When there is no write target the
  sweep logs `DRY RUN: would write to job_opportunities` and writes nothing.

## Ghost-job risk

Days = today minus First seen.

| Risk | Rule |
|---|---|
| High | Times seen 3 or more and days 60 or more |
| Medium | Times seen 2 or more and days 30 or more, or Posted date more than 45 days ago |
| Low | Posted date known and neither rule above applies |
| Unknown | otherwise |

High is never skipped. It is only reported, last in the summary.

## Safety

- All HTTP goes through `jobengine/http.py`, which calls `safety.assert_fetch_allowed` first:
  https only, host on `safety.allowed_hosts`, and any linkedin host refused even if it is added
  to the allowlist. Query strings (which carry the Adzuna key) are never logged.
- The only other network code is `telegram_bot.py` and the Google client in `gmail_reader.py`,
  which uses only `users.messages.list` and `users.messages.get`.
- Notion: `Notion-Version: 2025-09-03`, data source endpoints, about 3 requests per second,
  HTTP 429 retried with `Retry-After`. The sweep never changes a Notion schema.
- The `httpx` and `httpcore` loggers are kept at WARNING, because they would otherwise log full
  request URLs, Adzuna key included.
- Tests and CI never touch the network (`tests/conftest.py` makes any socket connection fail).

## Fixtures

`fixtures/sweep/` holds three synthetic alert emails, Adzuna PL and NL responses, Greenhouse,
Lever and SmartRecruiters responses, five Target Companies, Config, and three seeded Job
Opportunities rows (a repost, a posting seen again, and an old row that becomes High ghost
risk). All companies are invented and all email addresses are at `example.com`.
