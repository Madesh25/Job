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
- **Cache first.** Contacts at the same company (canonical name), found within Config
  `contacts.cache_months` (6), and not `Bounced` or `Do not contact`, are used before any
  paid call. A company whose cache already fills the mix costs nothing.
- **Minimum data:** name, title, work email, company, country, source, date found, type. No
  phone numbers, no personal addresses (gmail.com, outlook.com, yahoo.com, hotmail.com,
  icloud.com, proton.me), no LinkedIn URLs, no photos.

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
5. GitHub (free, when Config `contacts.github` is `on`; see above) for the Peer engineer
   slots, then the waterfall for the slots still open: Apollo (search, then one reveal per open slot), Hunter (one
   domain search), Snov (prospects, then an email search per chosen person), then the free
   plans: Prospeo (one people search, then one bulk reveal of verified emails for the open
   slots only) and Tomba (one domain search). It stops as soon
   as the mix is full. A provider is skipped when its key is missing, its counter is used up,
   or paid calls are off.
6. Candidates are classified by title (`contacts.title_patterns` in `config/base.yaml`; HR,
   Human Resources, People Operations and Talent titles count as Recruiter/TA, so they get the
   cold mail and never the referral ask),
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
  `2 of 4 found. Missing: 1 hiring, 1 recruiter.`, or
  `No contacts found. Apply through the portal only.`
  When contacts were found, a **Write Gmail drafts** button writes the drafts; they are not
  written until you tap it. In mail mode send the button is **Check and send mails** (see
  [gmail.md](gmail.md)). `/drafts <job>` does the same.
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
