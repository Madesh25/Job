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
Since Module 03, `/fetch` screens the new jobs right after the sweep (see
`docs/screening.md`).

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
3. **Collect** from each source: Gmail alerts, Adzuna, ATS feeds (Greenhouse, Lever,
   SmartRecruiters, Workday, amazon.jobs).
4. **Normalise and scope.** Title filters, country and city, seniority, years required.
5. **Dedupe and write.** Update the rows already in Notion. New jobs are ranked by a free
   match score and only the best `sweep.daily_new_limit` (30) per day are created; see
   "Best matches only" below. Before the cut, the full description of the jobs near the top
   is read from the job page ("Full descriptions" below).
6. **Ghost risk** for every row touched.
7. **Summary**, for example:
   `Sweep done: 10 new, 4 updated, 2 reposts, 3 skipped (out of scope), 1 high ghost risk. Sources: gmail 6, adzuna 7, ats 6. Not supported: 1 companies.`
   Extra lines follow for skipped sources and notes, and the High ghost risk jobs come last.

Counts: **new** rows created; **updated** existing rows seen again, either with a posting ID
they already had or on another board (a first ID from a new source); **reposts** existing rows
that got a new posting ID from a source they already had; **skipped** postings dropped as out of scope; **Sources** raw postings per
source; **Not supported** Target Companies whose job board could not be found (a custom
careers site that links to no known ATS).

## Sources

| Source | Needs | Notes |
|---|---|---|
| Gmail alerts | `GMAIL_ALERTS_TOKEN_JSON` (madeshwaranm02, `gmail.readonly`) | Query `sweep.gmail.query`. Board from the sender domain. Links are read from the email and never requested, tracking links included. Descriptions are never set (see "Email alerts setup"). |
| Adzuna | `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | `pl` and `nl` only (Adzuna has no Ireland), at most `sweep.adzuna.max_calls_per_run` calls, split evenly between the countries (3 each by default) so every country is searched. The feed gives a snippet; the full text is read from the job page for jobs that may be saved. Predicted salaries are ignored. |
| Jooble | `JOOBLE_API_KEY` (free, https://jooble.org/api/about) | Job search over many job boards, Ireland included. One call per country and term in `sweep.jooble` (3 x 4 = 12 calls). Board is the site Jooble found the job on when it is a known board (IrishJobs.ie, Indeed and so on, from `sweep.gmail.sender_boards`), else `Other`. The feed gives a snippet; the full text is read from the job page. Skipped without the key. |
| ATS feeds | nothing | Every active Target Company on Greenhouse, Lever, SmartRecruiters, Workday or amazon.jobs. The board comes from `Careers URL`, from the careers page it links to, or from `sweep.ats_boards`. Board is `Company site`. |

A source whose secret is missing is skipped with a line in the summary; the run continues.

### ATS details

- Greenhouse: `boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`. Posted date is
  `first_published` only; `updated_at` is never used.
- Lever: `api.lever.co/v0/postings/{token}?mode=json` (`api.eu.lever.co` for `jobs.eu.lever.co`
  URLs). Posted date from `createdAt`. An explicit `salaryRange` is stored.
- SmartRecruiters: `api.smartrecruiters.com/v1/companies/{token}/postings`, then one detail call
  per posting that passed the filters, at most `sweep.ats.max_detail_calls` per run.
- Workday (`<tenant>.<wdN>.myworkdayjobs.com/<site>`): the JSON search the career site itself
  uses (`POST /wday/cxs/<tenant>/<site>/jobs`), once per `sweep.ats.search_terms` term, 20
  results each (`workday_pages_per_term` pages). Then one detail call per kept posting, at
  most `sweep.ats.workday_detail_calls` per run: full description, start date and location.
  A posting listed as "3 Locations" is kept only when its detail places it in an active
  country. Posting ID `workday-<tenant>-<last path part>`. Any `myworkdayjobs.com` subdomain is
  allowed through `safety.allowed_host_suffixes`.
- amazon.jobs: `www.amazon.jobs/en/search.json` once per search term, Ireland, Poland and the
  Netherlands only, 100 newest results. Every result has the full description and
  qualifications. Searched once per sweep even when Amazon and AWS are both Target Companies.
- Every feed is filtered by title and location before any detail call. Postings filtered out
  here are reported in one note line and not counted as skipped.
- **Finding the board.** When `Careers URL` is the company's own site:
  1. the careers page is read (the same safe page reader as below) and the ATS it redirects
     to or links to most is used: Greenhouse (including embeds), Lever, SmartRecruiters,
     Workday or amazon.jobs;
  2. otherwise (also when the site refuses robots with HTTP 403) the company name is tried
     on the public Greenhouse, Lever and SmartRecruiters APIs: "Acme Cloud (IE)" is tried as
     `acmecloud` and `acme-cloud`. A board counts only when it lists at least one job
     (`sweep.ats.guess_by_name: false` switches this off).
  The result, also "nothing found" and why, is kept in Bot State `ats.detected` and checked
  again after `sweep.ats.detect_every_days` (7). At most `sweep.ats.max_detect_per_run` (25)
  companies are checked per sweep, so a long Target Companies list is covered over a few
  runs. Target Companies itself is never changed.
- **Sites still missing.** The `/fetch` summary lists the companies whose site blocked us
  (`Intel (IE) (HTTP 403)`) and those with no job board found; `/sources` lists all of them.
  The fix is one field: put the company's job board link in `Careers URL` (for example
  `https://intel.wd1.myworkdayjobs.com/External`, a `boards.greenhouse.io/...` or a
  `jobs.lever.co/...` link). Big companies usually have one: open their careers site in a
  browser, click any job, and copy the address of the page that lists the jobs. Workday,
  Greenhouse, Lever, SmartRecruiters and amazon.jobs links are then read directly. A company
  site we cannot read at all (its own custom system) is only covered through the job boards:
  Adzuna, Jooble and email alerts.
- For a company whose board is still not found, add an override in `config/base.yaml`:

  ```yaml
  sweep:
    ats_boards:
      Shamrock Systems: {ats: greenhouse, token: shamrocksystems}
      Baltic Bank: {ats: workday, token: balticbank, host: balticbank.wd3.myworkdayjobs.com, site: careers}
  ```

## Seeing jobs day by day in Notion

Job Opportunities (DEV) has a view **By day**: grouped by `First seen` (the day the sweep
found the job), newest first, with Company, Role, Screen verdict, Status, Board, place, dates,
seniority, salary and URL. Each group folds open and closed. Notion's API can only create the
grouping as relative dates (Today, Yesterday, Last 7 days); for one group per date open the
view, click **Group**, then set **Date by** to **Day**. Do the same in the prod database when
it goes live. `Posted date` can be used instead of `First seen`, but it is empty for jobs whose
source does not state it (email alerts).

## Full descriptions

Screening and resumes need the whole job description, not a two-line snippet. For the new
jobs near the top of today's list (`daily_new_limit` minus what was already added today, plus
`sweep.fulltext.lookahead` more), the sweep reads the job page when the source gave a snippet,
nothing, or less than `screening.full_min_chars`:

1. the schema.org `JobPosting` in the page's JSON-LD, which most job boards and ATS pages have
2. otherwise the largest block that looks like a job description (`main`, `article`, or an
   element whose id or class names a description), with menus, headers, footers, forms and
   scripts removed
3. otherwise the whole visible page text, when it is clearly more than a snippet

Then the jobs are ranked again (a full description often names skills the snippet left out)
and the best are saved. The page body starts with
`Description source: adzuna (full page, careers.example.com)`, naming the host the text came
from. A page that cannot be read, or gives less text than the snippet, keeps the snippet
(`Description source: adzuna (snippet only)`), and screening then asks for `/jd` as before.
The summary has one line, for example `Full descriptions: read 24 of 27 job pages`.

The page reader (`http.get_page`) is separate from the API client:

- plain GET with a JobEngine user agent: no login, no cookies (cleared on every hop), no forms
- https only, public host names only: IP addresses, `localhost` and internal names
  (`.internal`, `.local` and similar) are refused
- redirects are followed by hand, at most 5, and every hop is checked again
- LinkedIn is never read, not even when a redirect points there
- only HTML or text answers, at most 3 MB
- only for jobs that may be saved today, at most `sweep.fulltext.max_pages` (45) per sweep
- email alert links are not read (`sweep.fulltext.sources` is `[adzuna, ats]`): they are
  tracking links. Rows already in Notion are not re-read.

Set `sweep.fulltext.enabled: false` to switch it off.

## Email alerts setup

The code is ready; only the Gmail side is missing. When you want it:

1. In the madeshwaranm02 Gmail, create job alerts that send email: IrishJobs.ie, Jobs.ie and
   JobsIreland for Ireland (Adzuna has no Ireland), plus LinkedIn, JustJoin IT, NoFluffJobs,
   Pracuj.pl or IamExpat if you like. The query in `sweep.gmail.query` already matches all of
   these senders; `sweep.gmail.sender_boards` sets the Board.
2. Create the alerts token with `gmail.readonly` (see `docs/gmail.md`) and put the one-line
   JSON into `.env` as `GMAIL_ALERTS_TOKEN_JSON` (Secret Manager in dev and prod).
3. Check the parser against the real emails without writing anything:
   `python -m jobengine.sweep --parse-report`. Each line shows board, title, company and
   location. A board whose cards come out as `(unknown)` can get a pattern in
   `sweep.gmail.job_url_patterns`.

Email alerts give title, company and place only. Those jobs are ranked on that and screening
asks for the description with `/jd`. To read their pages too, add `gmail` to
`sweep.fulltext.sources` (LinkedIn links are still never read).

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
  https only, host on `safety.allowed_hosts` (or a subdomain of `safety.allowed_host_suffixes`,
  only `myworkdayjobs.com`), and any linkedin host refused even if it is added to the
  allowlist. Query strings (which carry the Adzuna key) are never logged.
- Job and careers pages go through `http.get_page` and `safety.assert_page_fetch_allowed`
  (see "Full descriptions"): public https hosts, never linkedin, every redirect checked.
- The only other network code is `telegram_bot.py` and the Google client in `gmail_reader.py`,
  which uses only `users.messages.list` and `users.messages.get`.
- Notion: `Notion-Version: 2025-09-03`, data source endpoints, about 3 requests per second,
  HTTP 429 retried with `Retry-After`. The sweep never changes a Notion schema.
- The `httpx` and `httpcore` loggers are kept at WARNING, because they would otherwise log full
  request URLs, Adzuna key included.
- Tests and CI never touch the network (`tests/conftest.py` makes any socket connection fail).

## Fixtures

`fixtures/sweep/` holds three synthetic alert emails, Adzuna PL and NL responses, Greenhouse,
Lever, SmartRecruiters, Workday and amazon.jobs responses, job and careers pages in
`fixtures/sweep/pages/`, five Target Companies, Config, and three seeded Job
Opportunities rows (a repost, a posting seen again, and an old row that becomes High ghost
risk). All companies are invented and all email addresses are at `example.com`.

## Best matches only (daily limit)

A sweep can find hundreds of postings, but only the best 30 new ones per day are saved
(`sweep.daily_new_limit` in `config/base.yaml`, counted across all sweeps that day in Bot
State key `sweep.day`). Screening (the paid LLM step) then only ever sees those.

The ranking is free (no LLM, `src/jobengine/sweep/rank.py`) and uses your Skills Inventory,
Term Map and Target Companies:

| Signal | Points |
|---|---|
| each skill you own named in the title or description (Production +4, Hands-on or Active Term Map +3, at most 8) | +3 / +4 |
| each known gap named (Term Map `(none)` row) | -2 |
| Target Companies tier 1 to 4 or 6 / other listed company | +6 / +3 |
| seniority Mid / Unknown / Senior | +3 / +1 / -2 |
| years required 2 to 4 / more than 5 | +2 / -8 |
| Polish or Dutch stated as required | -6 |
| posted in the last 3 days / 7 days / older than 21 days | +2 / +1 / -2 |
| full description (not a snippet) | +1 |

Ties go to the newest posting. Jobs already in Notion are always updated (seen again,
reposts), whatever the limit. Jobs below the cut are not saved; if they are still posted,
a later sweep can pick them up. The summary says how many were left out, for example
`Kept the best 30 of 142 new jobs (daily limit 30, 0 already added today). 112 weaker
matches were not saved.`
