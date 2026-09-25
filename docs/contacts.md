# Contact finder (Module 05)

When you approve a resume, the bot looks for at least 4 people at that company in the job's
country (2 peer engineers, 1 hiring manager, 1 recruiter or TA), saves them in Contacts, links
them to the job, and lists them in Telegram. It writes no mail (Module 06).

## Rules

- **No guessed emails.** Emails come only from a provider answer, the job description text or
  the Contacts cache. No `first.last@domain` patterns, no SMTP probing, no LinkedIn or people
  search pages. A repo test fails if code in `contacts/` builds an email from a name.
- **Own accounts, one key per provider** (`APOLLO_API_KEY`, `HUNTER_API_KEY`,
  `SNOV_CLIENT_ID` + `SNOV_CLIENT_SECRET`).
- **Paid calls only in prod with DRY_RUN=false** (`safety.paid_api_allowed`). In local and dev
  the providers answer from invented fixtures (`fixtures/contacts/`). In prod with DRY_RUN on,
  providers are skipped entirely, so test people never reach the production Contacts.
- **Cache first.** Contacts at the same company (canonical name), found within Config
  `contacts.cache_months` (6), and not `Bounced` or `Do not contact`, are used before any
  paid call. A company whose cache already fills the mix costs nothing.
- **Minimum data:** name, title, work email, company, country, source, date found, type. No
  phone numbers, no personal addresses (gmail.com, outlook.com, yahoo.com, hotmail.com,
  icloud.com, proton.me), no LinkedIn URLs, no photos.

## The flow

1. Mix from Config `contacts.mix` (`peer=2, hiring=1, recruiter=1`) and `contacts.per_job`;
   the target is the larger of the two. Unreadable: 2/1/1 and a note.
2. Cache: in-country first, then the most recent.
3. Job description: a named person's email becomes a Recruiter/TA contact (Source Job
   posting); a generic mailbox (`careers@`, `jobs@`, `hr@`, ...) is saved as Type Other.
4. Domain: Target Companies `Domain`, then Config `contacts.domains`
   (`Company Name = domain.tld` lines), then a domain you gave in Telegram. Job board and ATS
   hosts (greenhouse.io, lever.co, teamtailor.com, ...) are never used. Unknown: the bot asks
   and the lookup waits for your reply.
5. Waterfall for the open slots: Apollo (search, then one reveal per open slot), Hunter (one
   domain search), Snov (prospects, then an email search per chosen person). It stops as soon
   as the mix is full. A provider is skipped when its key is missing, its counter is used up,
   or paid calls are off.
6. Candidates are classified by title (`contacts.title_patterns` in `config/base.yaml`),
   personal and off-domain emails are dropped, in-country people come first (others only when
   no one in the country fits, noted `outside <country>`), and emails are deduped against the
   whole Contacts database: a known email reuses its row.

## What gets written

- **Contacts**, for each new person: Name, Title, Email, Company, Country, Type, Source,
  Status (Verified when the provider says so, else Unverified), Date found, Related jobs, Notes
  (`outside <country>` or `country unverified`). Cached contacts only get the job added to
  Related jobs; nothing else on them changes.
- **Job Opportunities**: Contacts relation, Contact source (`Job posting` when a contact came
  from the JD, `Not found` when nobody was found), Contact person (up to 5 names).
- **Config** (prod only): after each paid call the provider's `credits.<provider>` row
  (`<used> / <limit> per month`) and its Updated date. A counter updated in an earlier month
  starts again at 0. Only `credits.apollo`, `credits.hunter` and `credits.snov` can ever be
  written (`safety.config_writable_keys`); any other key raises `SafetyError`. Outside prod the
  counters are simulated in memory.

## Telegram

- After **Approve resume**: "Finding contacts for ..." and then the list, for example
  `4 of 4 found. Credits: Apollo 2/75, Hunter 0/25, Snov 0/50`, or
  `2 of 4 found. Missing: 1 hiring, 1 recruiter.`, or
  `No contacts found. Apply through the portal only.`
- Domain question: `What is the email domain for <Company>? Reply to this message with the
  domain, e.g. example.com. Ref JOB-xxxxxxxx`. Reply to that message with the domain; it is kept
  in Bot State (`domain:<company>`) and the lookup continues. Copy it into the Target Companies
  `Domain` column when you have a moment (the code never writes Target Companies).
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
