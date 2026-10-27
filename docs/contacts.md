# Contact finder (Module 05)

When you approve a resume, the bot looks for at least 4 people at that company in the job's
country (2 peer engineers, 1 hiring manager, 1 recruiter or TA), saves them in Contacts, links
them to the job, and lists them in Telegram. Since Module 10 the paid part (domain question and
provider waterfall) runs only for jobs within the outreach budget
([outreach.md](outreach.md)); apply-only jobs use the cache and the job description only. Module 06 then writes Gmail drafts
([gmail.md](gmail.md)).

## Rules

- **No guessed emails.** Emails come only from a provider answer, the job description text or
  the Contacts cache. No `first.last@domain` patterns, no SMTP probing, no LinkedIn or people
  search pages. A repo test fails if code in `contacts/` builds an email from a name.
- **Own accounts, one key per provider** (`APOLLO_API_KEY`, `HUNTER_API_KEY`,
  `SNOV_CLIENT_ID` + `SNOV_CLIENT_SECRET`, and the free plans `PROSPEO_API_KEY`,
  `TOMBA_API_KEY` + `TOMBA_API_SECRET`). One account per provider: several free accounts
  break their terms.
- **Paid calls only in prod with DRY_RUN=false** (`safety.paid_api_allowed`). In local and dev
  the providers answer from invented fixtures (`fixtures/contacts/`). In prod with DRY_RUN on,
  providers are skipped entirely, so test people never reach the production Contacts.
- **Real lookups on your PC: `CONTACTS_LIVE=true`** in `.env` (16 Oct, after every real
  company got "No contacts found" in DEV). Only with `APP_ENV=local`, only the contact
  finders (Apollo, Hunter, Snov, Prospeo, Tomba); `DRY_RUN` stays on, so no mail is sent and
  nothing else becomes real. The calls use your real free credits; the DEV Config `credits.*`
  rows are not written, so check the provider dashboards. People found go to the DEV
  Contacts table. Set it back to `false` (or remove it) when the test is done. `/health`,
  `/credits` and each lookup say which mode is on ("Contact lookups: real ..." or "test
  data ...").
- **Cache first.** Contacts at the same company (canonical name), found within Config
  `contacts.cache_months` (6), and not `Bounced` or `Do not contact`, are used before any
  paid call. A company whose cache already fills the mix costs nothing.
- **Minimum data:** name, title, work email, company, country, source, date found, type. No
  phone numbers, no personal addresses (gmail.com, outlook.com, yahoo.com, hotmail.com,
  icloud.com, proton.me), no LinkedIn URLs, no photos.

## Location rule (16 Oct, your choice: strict)

Config `contacts.location` in `config/base.yaml` (default `strict`):

- **strict:** only people a provider places in the job's country (Poland, Netherlands,
  Ireland) are used; people outside it or with an unknown location are left out, even when a
  slot stays empty. The answer says how many were left out ("location rule strict: left out 2
  outside Netherlands and 3 with unknown location"). Saved contacts whose location was never
  checked (Notes "country unverified") are not reused. People named in the job posting count.
- **country_first:** the rule before 16 Oct: in the country first, then unknown, then outside.
- Remote and Other jobs have no country to check.
- **More local people** (16 Oct: 1 of 4 at HCLTech, Netherlands): while slots stay open,
  Prospeo searches senior engineering/IT people, then the HR department (recruiter), then
  anyone at the company, all in the job's country (one credit each when it finds people).
  Snov searches the whole domain when the titles find nobody. Hunter's API ignores the
  country filter on the free plan, so up to 3 Hunter people whose title fits an open slot are
  looked up in Prospeo by email (one credit each when Prospeo knows them); the ones it places
  in the job's country are used, noted "location from Prospeo".
- **Hunter** gives no location per person, so its search now filters by country (a POST with
  `location`); its people count as placed only when Hunter's answer confirms the filter.
- **Prospeo** searches with `person_location_search` = the country; if Prospeo refuses that
  filter, it searches once without it and the people are checked by their location.
- Telegram shows where each person is: `[Prospeo, verified, Ireland]` or `location
  unknown`. Notion stores Country "Other" for an unknown location (before 16 Oct it stored the
  job's country, as if it had been checked).

## Wider search when slots stay open (16 Oct)

The first real lookup (State Street, Dublin) found 1 of 4: Hunter returned 10 people, but
bank titles such as "Vice President" fit no slot. Now, after everyone whose title fits:

- **Senior tech people fill the open slots** (`classify.wide_kind`): head, director, VP or
  manager in tech fills Hiring; senior, lead, principal, staff or architect fills Peer; an HR
  department person fills Recruiter. A tech word in the title or a tech department (Hunter's
  `department`) is always needed, so a sales director never counts. They show the note
  "senior, wider search" and come after people whose title fits.
- **Prospeo searches once more** when Peer or Hiring is still open: seniority Senior,
  Manager, Head, Director or Vice President in the Engineering & Technical and Information
  Technology departments (1 credit when it finds people). Prospeo's `NO_RESULTS` (HTTP 400)
  is a normal answer and costs nothing; other refusals show their code (`INVALID_FILTERS`
  and the filter).
- **Hunter** addresses marked `invalid` (they bounce) are never used. When slots stay open
  after its first page, Hunter searches the HR department (for the recruiter) and the next
  page (for engineers), one credit each, only while credits are left.
- **Snov** really waits between its result checks (2, 4, 6, 8, 10 seconds); before 16 Oct it
  asked five times at once and gave up. It now says what it saw ("N people at <domain>, none
  fit the open slots" or "no people found"). The email search uses Snov's own
  `search_emails_start` and `links.result` addresses; a prospect without one is named with
  the fields Snov sent, and no call is made (16 Oct: an empty id gave HTTP 404).
- **Prospeo** asks for emails one person per call (`/enrich-person`), 1.5 seconds apart: its
  free plan refused the bulk call with HTTP 429 even after 22 seconds of waiting (16 Oct).
  `NO_MATCH` (no verified email) is quiet and costs nothing. An HTTP 429 without
  Retry-After is retried after 2, 5 and 15 seconds.
- **Hunter** sends its country filter as a POST with every filter in the JSON body; when its
  answer does not confirm the filter, the note lists what it answered with. Its free plan
  stops at 10 people per search ("no next page").
- **Snov** people get their country from `country`, or from a place field (`locality`,
  `location`); without one, the note lists the fields Snov sent.
- People found by the wider search are marked "wider search" in Telegram.
- **Snov** gets at most 10 titles, as query parameters (11 gave HTTP 422).
- API keys are cleaned of spaces, quotes and Windows line ends when read from `.env`.
- Apollo's free plan answers `API_INACCESSIBLE`: no API people search on that plan, whatever
  the key (16 Oct). The bot says so; remove `APOLLO_API_KEY` from `.env` to skip Apollo.
- Apollo answers HTTP 403 to the people search both for a key that is not a **master key**
  and for a plan without API search. The bot now shows Apollo's own words plus the hint: in
  Apollo, Settings, Integrations, API, Create new key, tick "Set as master key" (Apollo's
  docs say free access also needs an account on a work email address).

## Second accounts (Apollo, Hunter, Snov, Prospeo, Tomba)

Each of the five can have a second (free) account. Put its keys in `.env` (Secret Manager in
prod, mounted when the secret exists):

| Provider | Second account keys | Config counter |
|---|---|---|
| Apollo | `APOLLO_API_KEY_2` | `credits.apollo_2` |
| Hunter | `HUNTER_API_KEY_2` | `credits.hunter_2` |
| Snov | `SNOV_CLIENT_ID_2`, `SNOV_CLIENT_SECRET_2` | `credits.snov_2` |
| Prospeo | `PROSPEO_API_KEY_2` | `credits.prospeo_2` |
| Tomba | `TOMBA_API_KEY_2`, `TOMBA_API_SECRET_2` | `credits.tomba_2` |

- The first account is always used first. The second is used only when the first has no
  credits left this month, or when its search fails (a refused key or an exhausted plan).
  Never both for one job: they see the same people.
- Only a second account (no first key, 23 Oct: Tomba): the second is used on its own and
  needs its Config row `credits.<provider>_2` (`0 / <limit> per month`).
- Add a Config row `credits.<provider>_2` (`0 / <limit> per month`) to keep the second
  account's count. Without it the second account gets the first one's monthly limit, counted
  for one run only.
- Contacts found with a second account say so in Notes ("found with Hunter-2"). `/credits`
  shows the second accounts that have a key or a Config row.
- **Test a second key (23 Oct):** when look-ups are real (`CONTACTS_LIVE=true` on your PC),
  `/credits` asks every account with a key, second ones too, what it has left ("Tomba-2: 25
  of 25 searches left"). These calls spend no credit. "could not ask (401 ...)" means a wrong
  key or secret. Tomba's account answer was not seen yet: if it gives no numbers, the line
  says "the key works" instead.
- Apollo and Snov: a second free account does not help. Apollo's free plan has no API
  people search and Snov's API needs a paid plan; both answer the same for any free account.
- No IP switching or other tricks: each account is a normal free account used within its
  own limits.

## Free plans: Prospeo and Tomba

They run after Apollo, Hunter and Snov, only for slots still open, and roughly double the
emails you can find each month:

| Provider | Free plan | One job costs | Keys (`.env`) | Config counter |
|---|---|---|---|---|
| Prospeo | 75 credits a month | 1 search + 1 per verified email (at most 5) | `PROSPEO_API_KEY` | `credits.prospeo` = `0 / 75 per month` |
| Tomba | 25 searches a month | 1 domain search | `TOMBA_API_KEY`, `TOMBA_API_SECRET` | `credits.tomba` = `0 / 25 per month` |

- Endpoints come from the providers' own code: Prospeo's MCP server (`POST /search-person`,
  `POST /bulk-enrich-person`, header `X-KEY`) and Tomba's SDKs (`GET /v1/domain-search`,
  headers `X-Tomba-Key` and `X-Tomba-Secret`).
- **Set up:** sign up, put the keys in `.env`, and add the two Config rows above (Type
  `Credit counter`). A provider without its Config row is skipped quietly and left out of the
  credits line; with the row it counts down like the others (`/credits`, `/outreach`).
- **Prod:** create the secrets `prospeo-api-key`, `tomba-api-key` and `tomba-api-secret`
  first, then add them to `SECRETS` in `.github/workflows/deploy.yml` (a missing secret stops
  the deploy, so they are not listed there yet).
- **Lusha (step 1: the probe).** Lusha's official MCP server (npm `@lusha-org/mcp`) shows how
  to call it (`api_key` header, `POST /prospecting/contact/search`, then
  `/prospecting/contact/enrich` with `revealEmails`) but passes the answer on as it is, so it
  does not show where the people and the revealed emails are. Emails are never guessed, so
  the lookup waits for one real answer: in prod with `DRY_RUN=false` and `LUSHA_API_KEY` set,
  run `python -m jobengine.contacts --probe lusha --domain "<Company Name>"`. It makes one
  search (no reveal), and prints only the field names and value types (no names, emails or
  values). Send that list to Claude to build the Lusha lookup (step 2).
- **GetProspect** is not in: no official source for its API could be read, and a request or
  answer shape is never guessed. Its docs page (or an official SDK) is needed first.

## Engineers from GitHub (free)

Many engineering teams keep a public GitHub organisation, and some engineers publish their
work email on their profile. With Notion Config `contacts.github` = `on`, the lookup reads it
before any paid provider, for the Peer engineer slots (`src/jobengine/contacts/providers/github.py`):

1. **The organisation:** Config `contacts.github_orgs` (`Company Name = org-login` lines),
   else a GitHub search for organisations named like the company. One is taken only when its
   own website or email is on the company's email domain; the answer (or "none") is kept in
   Bot State `github_org:<company>` so the search runs once per company.
2. **Its public members**, then their profiles, at most `contacts.github.max_profiles` (20)
   a job.
3. **Kept:** only people whose own profile shows an email on the company domain, exactly as
   published (never guessed; gmail.com and other personal addresses are dropped), and not
   outside the job's country (no location: `country unverified`). The bio is the Title; a
   bio that reads HR or recruiter makes a Recruiter/TA contact (cold mail), a manager bio
   Hiring, anything else Peer engineer (they are in the company's engineering organisation).
   Source `GitHub`, Status Unverified, Notes `public GitHub profile, org <org>`.

GitHub allows 60 calls an hour without a token. Set `GITHUB_TOKEN` (a fine-grained token with
no permissions: it only reads public data) for 5,000 an hour. Like the providers, real calls
happen only in prod with `DRY_RUN=false`; elsewhere invented fixtures answer.

## Which people (22 Oct, your rule)

- **Country first:** the strict location rule above stays.
- **Any role, up to 4:** `contacts.roles` = `any` (default, `config/base.yaml`): engineers,
  engineering or IT managers and HR in any mix, the best first; the slots of the mix are no
  longer fixed (4 engineers is fine). Each person keeps a type, which picks the mail template
  (engineer: referral ask; manager or HR: cold mail). `mix` brings back the fixed numbers of
  Config `contacts.mix`.
- **No people outside IT** (22 Oct): a title naming legal, finance, sales, marketing,
  procurement, supply chain, logistics, manufacturing, project or process engineering,
  mechanical, electrical, electronics, hardware, construction or facilities never fills
  a peer or hiring slot, from any source and also in the wider search ("Legal Team
  Leader" is not a manager for you). HR people still fill the recruiter slot.
- **Saved contacts follow the same rules** (22 Oct): a contact saved before a rule came
  in (a junior before #135, someone outside IT before #137) is not reused from the cache,
  and `/drafts` skips them with "title outside your contact rules (junior or not IT)",
  even when they are still linked to the job. Their Contacts rows stay as they are.
- **No junior people:** titles with junior, jr, intern, trainee, graduate, apprentice,
  student, werkstudent, stażysta, praktykant or stagiair are never taken, from any source.
- Fewer than 4 is fine: nothing is forced.

## Provider limits (22 Oct)

- **Snov HTTP 402:** Snov's API works only on a paid plan (it says "ran out of credits"
  although its site shows credits). The answer says once `snov: its API needs a paid plan
  (HTTP 402), skipped until the 1st`; later look-ups that month show `snov: skipped this
  month` and make no Snov call (Bot State `contacts.off:snov`). On the 1st it is asked again.
- **Prospeo HTTP 429:** after the usual waits (2, 5 and 15 seconds) a refusal gives one line,
  `prospeo: busy (rate limit ...), stopped for this look-up`, and no more Prospeo calls in
  that look-up: not its other emails, not the location check (`prospeo: busy, could not
  check where N people without a location are`), not the AI search. The next look-up asks
  Prospeo again. Try again a few minutes later.

## Real credits left (22 Oct)

When look-ups are real, /credits ends with "Real credits left", asked from Hunter
(`/v2/account`), Snov (`/v1/get-balance`), Prospeo (`/account-information`) and Tomba
(`/v1/me`, since 23 Oct) at that moment, for each account with a key (second accounts too);
these calls are free. On 22 Oct the bot's own count said Snov 3 of 50 while Snov
answered "out of credits": the providers' numbers are the truth. On the PC the Config
counters stay simulated (never written); in prod they are still written after each look-up.

## Company domain found by the bot (22 Oct, your request)

When a company has no Domain (Target Companies, Config `contacts.domains`, or one you gave),
the bot finds it itself (`src/jobengine/contacts/domain_search.py`), the first sure answer
wins:

1. The job's own pages (job link, careers page, emails in the description): sure when exactly
   one domain there carries the company's name.
2. An AI web search (Config `model.people`): sure only when a search result really lives on
   that domain and the domain or the result's title names the company, and it is no job
   board, recruiting software, LinkedIn or personal mail domain.

A sure domain is remembered (Bot State) and, in prod, written to Target Companies `Domain`
when that cell is empty (the only Target Companies column contacts may write). The answer
says "domain X found ..., saved for next time". Only when nothing is sure does the bot ask
you, as before. Emails are still never guessed: the finders give them.

## AI web search for contacts (20 Oct)

The last step, when the cache, the job description, GitHub and the providers left slots
open. Config `contacts.ai_search` = `on` turns it on (off when the row is missing).

1. The AI (Config `model.people`; plan: `gemini-3.5-flash-lite` with Google Search, Haiku
   when not set; DeepSeek is never used here) searches the company's **public pages** for
   people in the job's country who fit the open slots: team and about pages, engineering
   blogs, conference and meetup speaker pages, press releases, GitHub. At most 6 names, at
   most 5 searches. The searches it ran are written to the log.
   **22 Oct, your decision:** LinkedIn profile results too, by their **title in the search
   results only** ("Anna Lis - Platform Engineer - Acme | LinkedIn"); LinkedIn is never
   opened (`contacts.ai_linkedin_titles: true` in `config/base.yaml`, false switches it off).
   Works with Claude's web search; Gemini gives only the site name as the title, so on
   Gemini LinkedIn people cannot be checked and are dropped.
2. Each name is checked, so the AI cannot invent people:
   - its page must be one the search really returned (the sites that block the bot are never
     used);
   - the bot reads that page once (a plain public page read, like a job page) and the full
     name and the company must both be on it (Polish letters match plain ones); a LinkedIn
     result is checked on its title instead, never opened;
   - the person counts as in the country only when the page names the country or one of its
     cities, or the LinkedIn address is the country's own (`nl.`, `pl.`, `ie.linkedin.com`);
     the others are looked up in Prospeo by email (up to 3, "location from Prospeo") and the
     strict location rule leaves out who is not placed;
   - junior titles are dropped before any email look-up.
3. The email is **never guessed**: Hunter's email finder (name + company domain, 1 credit
   when it finds one), else Prospeo's enrich by name (1 credit, verified emails only). An
   address Hunter calls invalid is dropped, never replaced. At most 4 look-ups per job.
4. The contact is saved like any other: Source `Hunter` or `Prospeo`, Notes `found by AI web
   search on <site>`. The answer says how many names were found, checked and given an email,
   and why the others were dropped (counts only).

Only with real look-ups (prod with DRY_RUN false, or CONTACTS_LIVE=true on the PC): with test
data an invented address would sit next to a real name, so the step is skipped with the note
"AI search: off while contact look-ups use test data". Cost: one AI call with web search per
job (Gemini: free up to 5,000 a month) and the Hunter or Prospeo credits of the emails found.

## The flow

1. Mix from Config `contacts.mix` (`peer=2, hiring=1, recruiter=1`) and `contacts.per_job`;
   the target is the larger of the two. Unreadable: 2/1/1 and a note.
2. Cache: in-country first, then the most recent.
3. Job description: a named person's email becomes a Recruiter/TA contact (Source Job
   posting); a generic mailbox (`careers@`, `jobs@`, `hr@`, ...) is saved as Type Other.
4. Domain: Target Companies `Domain`, then Config `contacts.domains`
   (`Company Name = domain.tld` lines), then a domain you gave in Telegram or the bot found
   (22 Oct, "Company domain found by the bot" above). Job board and ATS hosts
   (greenhouse.io, lever.co, teamtailor.com, ...) are never used. Still unknown: the bot asks
   and the lookup waits for your reply.
5. The company's own website (free, 27 Oct; Config `contacts.site`, default on, `off` stops
   it): see "Addresses on the company website" below. It runs for apply-only jobs too, when
   the domain is already known.
6. GitHub (free, when Config `contacts.github` is `on`; see above) for the Peer engineer
   slots, then the waterfall for the slots still open: Apollo (search, then one reveal per open slot), Hunter (one
   domain search), Snov (prospects, then an email search per chosen person), then the free
   plans: Prospeo (one people search, then one bulk reveal of verified emails for the open
   slots only) and Tomba (one domain search). It stops as soon
   as the mix is full. A provider is skipped when its key is missing, its counter is used up,
   or paid calls are off.
7. Candidates are classified by title (`contacts.title_patterns` in `config/base.yaml`; HR,
   Human Resources, People Operations and Talent titles count as Recruiter/TA, so they get the
   cold mail and never the referral ask),
   personal and off-domain emails are dropped, in-country people come first (others only when
   no one in the country fits, noted `outside <country>`), and emails are deduped against the
   whole Contacts database: a known email reuses its row.

## Addresses on the company website (27 Oct, your request: free contacts)

Before any paid lookup the bot reads the company's home page (`https://<domain>/`), its
careers URL from Target Companies, and at most 4 of its own pages whose link names careers,
jobs or contact (English, Polish and Dutch words: careers, jobs, join, werken, vacatures,
kariera, praca, kontakt, over-ons ...). Only addresses **written on those pages** on the
company's domain are kept; nothing is guessed.

- A hiring mailbox (`careers@`, `jobs@`, `hr@`, `recruitment@`, `rekrutacja@`, `werkenbij@`,
  `people@` ...) is saved like a mailbox in a job posting: Type Other, Source Careers page.
- A named person's address only from a careers or jobs page, or when the line around it says
  recruiting, HR, talent or people: Type Recruiter/TA, Source Careers page.
- `info@`, `sales@`, `support@`, `biuro@` and the like, people on other domains, and addresses
  only inside page scripts are left out. Notes say which page the address was on.
- Each company's result is kept in Bot State (`site_emails:<company>`) for 30 days, so its
  next jobs read no page. A page that cannot be read is skipped; LinkedIn is never opened.

With 2 contacts per job (Config `contacts.per_job`) and the free steps (cache, job posting,
company website, GitHub, AI search on Gemini's free searches, your LinkedIn connections),
the paid credits are kept for the jobs the free steps cannot fill.

## What gets written

- **Contacts**, for each new person: Name, Title, Email, Company, Country, Type, Source,
  Status (Verified when the provider says so, else Unverified), Date found, Related jobs, Notes
  (`outside <country>` or `country unverified`). Cached contacts only get the job added to
  Related jobs; nothing else on them changes.
- **Job Opportunities**: Contacts relation, Contact source (`Job posting` when a contact came
  from the JD, `Not found` when nobody was found), Contact person (up to 5 names).
- **Config** (prod only): after each paid call the provider's `credits.<provider>` row
  (`<used> / <limit> per month`) and its Updated date. A counter updated in an earlier month
  starts again at 0. Only the `credits.*` rows of the providers and their second accounts
  can ever be written (`safety.config_writable_keys`); any other key raises `SafetyError`. Outside prod the
  counters are simulated in memory.

## Telegram

- **After your review, in one go: `/fetchcontacts`.** After **Approve resume** the bot shows
  **I applied** and **Not applying**; nothing is looked up while you review. When you are done
  with the day's jobs, `/fetchcontacts` lists every job you marked I applied today, with how
  many saved contacts each has, and waits for **Go**. Go then works through the jobs one by
  one: a job with saved contacts (its Contacts relation) goes straight to the drafts; any
  other job gets a lookup (the Contacts cache and the job posting first, then the paid
  providers; it counts against the week's outreach budget like `/contacts`) and then the
  Gmail drafts with the approved resume attached. A job whose email domain is unknown asks
  the domain question below; reply to it, then tap **Write Gmail drafts**. It ends with a
  summary: drafts written, jobs with no contacts, jobs waiting for a domain. Running it again
  is safe: saved contacts are reused and drafts already written are skipped. The Go button
  only works on the day it was sent.
- `/contacts <job>` looks up one job by hand (older messages may still have a **Find
  contacts** button, which does the same). Then "Finding contacts for ..." and the list, for
  example
  `4 of 4 found. Credits: Apollo 2/75, Hunter 0/25, Snov 0/50`, or
  `2 of 4 found. Missing: 2 more (engineers, managers or HR).` (with Config
  `contacts.roles` mix: `Missing: 1 hiring, 1 recruiter.`), or
  `No contacts found. Apply through the portal only.`
  When contacts were found, a **Write Gmail drafts** button writes the drafts; they are not
  written until you tap it. In mail mode send the button is **Check and send mails** (see
  [gmail.md](gmail.md)). `/drafts <job>` does the same.
- Domain question: `What is the email domain for <Company>? Reply to this message with the
  domain, e.g. example.com. Ref JOB-xxxxxxxx`. Reply to that message with the domain; it is kept
  in Bot State (`domain:<company>`) and the lookup continues. Copy it into the Target Companies
  `Domain` column when you have a moment (the code never writes Target Companies).
  Sending the domain on its own (not as a reply) also answers the open question. A domain that
  is refused gets an answer saying so, and the question stays open. Job-board (ATS) domains
  are refused, except the ATS company's own one: `workday.com` for Workday (Phase 9, P9-24).
- `/contacts <job URL or page id>`: runs the lookup again, filling only the missing slots.
- `/credits`: each provider's counter, when it was updated, and whether its key is set.

## Running it

```bash
python -m jobengine.contacts --fake --job fixture-clean-pl            # fixtures, no network
python -m jobengine.contacts --job <page_id> --no-write               # real Notion, writes nothing
APP_ENV=prod DRY_RUN=false python -m jobengine.contacts --probe hunter --domain example.com
```

`--probe` makes exactly one search call (no reveal), prints the number of results and the
fields present, writes nothing and changes no counter. Use it once per provider to check that
your free plan has API access before relying on it. It is refused outside prod.

## Provider APIs

The documentation sites were not reachable from the build environment, so the clients follow
the endpoints as documented in 2026 (Apollo `mixed_people/api_search` and `people/match`,
Hunter `v2/domain-search`, Snov OAuth plus the v2 `domain-search/prospects` start/result pairs)
and read the answers tolerantly. Run the probe for each provider before the first real lookup;
if a field or endpoint differs, the probe output shows it.

## Your LinkedIn connections (flow feature 1, step 1, 23 Oct)

Notion table **🤝 LinkedIn Connections** (one table for every env, your decision of 23 Oct;
`notion.read.linkedin_connections`). Its first seven columns have the same names as
LinkedIn's `Connections.csv` (First Name, Last Name, URL, Email Address, Company, Position,
Connected On), so the first import can be done in Notion (Merge with CSV, after deleting the
"Notes:" lines at the top of the file). Kind is a Notion formula from Position (HR, Manager,
Engineer, Other). Referral, Last asked and Notes are yours; the bot never changes them.

The bot never opens LinkedIn. Once a month:

1. LinkedIn > Me > Settings & Privacy > Data privacy > Get a copy of your data > Connections
   (or the full archive) > Request archive.
2. When LinkedIn's email comes, download the zip and send it to the bot as a file (the zip or
   only `Connections.csv`; up to 20 MB, Telegram's limit for bots). Other files are not read.
3. The bot matches people by profile URL (by name and company when there is none):
   - new people are added (First seen = today);
   - a new company, position or email is written (an empty email never wipes a saved one);
   - people missing from the export are ticked **Removed**; a Removed person who is back is
     unticked;
   - the answer lists what changed and the next due date.
4. A file with less than half of the people in the table is refused and nothing changes (a
   cut or wrong file would tick most people Removed).

`/connections` shows the last import and the steps. From 30 days after the last import the
bot reminds you once a week (long polling: with the scheduled checks; Cloud Run: the daily
task). The date is kept in Bot State (`connections.imported`). In `--fake`, type
`file <path to the zip>` to send the export.

### Referrals (feature 1, step 2, 23 Oct)

Under every contacts answer (Find contacts, /contacts) the bot lists your connections at
the job's company (`connections.same_company`: "Capgemini Polska" and "Capgemini (IE)" are
Capgemini), read with a Notion filter on the company's first word, not the whole table:

- the job's country first, then location unknown, then other countries; engineers, then
  managers, HR and the rest; Removed and junior people are left out;
- each line says the person's kind, country, whether there is an email and when they were
  last asked (within 30 days);
- with nobody in the job's country, a note: a referral from another office usually still
  counts.

**Layer 1 (free):** a person the contact finder found who is also your connection (same
name) gives the connection its Country (Location source Contacts) and work email; only empty
fields are filled, and a personal address (gmail.com ...) is never stored as a work email.

**Referral asks** (button `rf:`): a LinkedIn message per connection, from the peer note
(`linkedin.note_peer`), with their profile link; you send it yourself.

**Mail them** (button `rm:`): connections with an email (work email first, else the export's
email, personal allowed: your OK of 23 Oct), a tech, manager or HR position, not asked in 30
days, are added as Peer engineer contacts of the job (Source Other, Notes "LinkedIn
connection (<kind>); referral ask") and marked Referral Asked with today's Last asked. Write
Gmail drafts then uses the referral ask template word for word.

### Location and work email (feature 1, step 3, 23 Oct)

The export has no location and LinkedIn is never opened. `contacts/connection_lookup.py`:

- **Prospeo** `/enrich-person` by the LinkedIn profile link: Country (Location source
  Prospeo) and a VERIFIED work email; 1 credit when it finds the person, none for NO_MATCH.
- **Hunter** `/email-finder` by name and the company's Target Companies Domain: a work
  email (1 credit when found; an address Hunter calls invalid is not kept). No location.

Only empty fields are written, and a personal address is never a work email. Paid calls
follow `safety.paid_api_allowed` (prod with DRY_RUN off, or CONTACTS_LIVE on your PC).

1. **On demand:** the "Location and email" button (`rl:`) under a job's connections asks
   Prospeo for at most 3 people there without a country.
2. **Leftover credits (your idea):** once a day the bot asks Prospeo and Hunter how many
   credits are left and when new ones come (Prospeo: days left; Hunter: its reset date, else
   `contacts.hunter_renewal_day`, 23). On the last day before a renewal it spends what is
   left, at most 60 people per provider, on your engineers, managers and HR at Target
   Companies first (Prospeo: then anyone without a country; Hunter: only Target Companies
   with a Domain). Once per renewal (Bot State `connections.leftover`); the answer says how
   many were looked up and filled.

## Recruitment agencies (flow feature 7, /agencies)

`src/jobengine/contacts/agencies.py`. Established recruiters with IT teams in Poland, the
Netherlands and Ireland are listed in `config/base.yaml` `agencies.list` (each website checked
on 10 Oct; Reperio, Computer Futures and YoungCapital were left out: no official site found,
or not a DevOps focus).

- `/agencies` lists them by country with their website and when you last contacted them.
- `/agencies <name>` gives the intro mail, word for word from `agencies.intro` (Notion Config
  `agency.intro` wins), filled with your Apply pack answers (experience, notice, relocation)
  and the country's permit line. The bot sends nothing: register on the agency's website and
  paste it, or mail it with your resume.
- An email address is never guessed: put one from the agency's website in Notion Config
  `agency_email.<key>` (the name in lower case, spaces as `_`, for example
  `agency_email.hays_poland`); the intro then says where to send it.
- `/agencies sent <name>` records today's date. The weekly digest names the agencies not
  contacted in the last 30 days.
- Jobs that agencies post stay marked "Agency posting" (no cold mails), as before.
