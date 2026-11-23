# Job Engine

Job Engine is a personal job search bot for Madeshwaran M, a DevOps engineer looking for roles in Poland, the Netherlands and Ireland (office or hybrid). Telegram is the control surface and Notion is the database. Every day it:

1. **finds jobs** from email alerts (LinkedIn, JustJoin IT, NoFluffJobs, IrishJobs and more), Adzuna, Jooble, NoFluffJobs, IamExpat and the career sites of the companies in Target Companies, and looks up each job's own link on the company's site;
2. **screens** them with the AI against your Skills Inventory: Apply high, normal or low, Needs review or Skip, with the gaps and the visa situation of each country;
3. **builds a one-page resume** per approved job, with the Apply pack (ready form answers) and the autofill code;
4. **fills application forms** (the autofill bookmark by hand, or the form bot in dry runs; auto-submit only when you switch it on);
5. **finds contacts** and writes the outreach mails as Gmail drafts (sent only in send mode, in the recipient's morning);
6. **tracks** replies, bounces, follow-ups and interviews, and reports every evening.

Every run leaves a row in Notion Run Log (its full report is the row's page) and every problem a row in Error Log. Safety first: `DRY_RUN` is on unless set to exactly `false`, and outside prod every mail goes only to your own address.

## Run locally

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # fill in dev values only
python -m jobengine.main
```

`APP_ENV` defaults to `local` and `DRY_RUN` defaults to `true`.

## Run on your PC (Docker, the usual way)

```powershell
git pull
docker build --build-arg FORM_BOT=true -t job-engine .
docker run --rm -it -p 127.0.0.1:8765:8765 --env-file .env -v "${PWD}\out:/app/out" job-engine python -m jobengine.telegram_bot 2>&1 | Tee-Object -FilePath out\bot-log.txt
```

This is the DEV bot (answers start with `[LOCAL]`, DEV Notion tables). Port 8765 is used only
while `/gmailtoken` renews a Gmail token. Rebuild after every `git pull`.

## Run the Telegram bot (local)

### With a dummy Telegram (no token, no network)

```bash
python -m jobengine.telegram_bot --fake
```

Each line you type is treated as a message from your chat and the bot's replies are printed:

```
you> /start
bot> [LOCAL] Job Engine bot is running (env=local).
```

Stop it with Ctrl+D or Ctrl+C.

### With the real DEV bot

Put the DEV bot token and your chat ID in `.env`:

```
TELEGRAM_BOT_TOKEN=<token from @BotFather>
TELEGRAM_CHAT_ID=<your chat id>
```

Then start the bot and leave it running:

```bash
python -m jobengine.telegram_bot
```

It sends `[LOCAL] Job Engine bot started.` to your chat and answers `/start`, `/status` and
`/help`. Messages from any other chat are ignored. Stop it with Ctrl+C.

While a long run goes (`/fetch`, `/screen`, `/autopilot`) the bot keeps reading your messages:
other commands wait their turn, and `/end` stops the run at its next step (what is already
saved stays). `/restart` stops the running job, then starts the bot again in the same process
(fresh code, Notion settings and state). Values in `.env` are read when the bot or container
starts, so after changing `.env` stop the container and `docker run` it again.

## Run the job sweep

```bash
python -m jobengine.sweep --fake --today 2026-10-01   # fixtures only, no network
python -m jobengine.sweep --parse-report              # check the Gmail parser, writes nothing
```

`/fetch` in the Telegram bot runs the same sweep. See [docs/sweep.md](docs/sweep.md).

## Run screening

```bash
python -m jobengine.screen --fake --today 2026-10-01   # fake LLM and fixtures, no network
python -m jobengine.screen --no-write                  # real verdicts, writes nothing
```

In Telegram, `/pending` shows the screened jobs one at a time and `/jd <url>` takes a pasted
job description. See [docs/screening.md](docs/screening.md).

## Autopilot

`/autopilot` in Telegram fetches, screens at half price, approves the best Apply high and
Apply normal jobs (at most 10 a day), saves their resumes, finds contacts and writes Gmail
drafts, then sends one summary with the PDFs. `/mailmode send` lets it send the drafts that
pass every check (To, Cc, subject, body, resume attachment), in the recipient's morning
(Monday to Friday, 08:00 to 11:00 their time, public holidays skipped; `/mailqueue` shows the waiting ones); the
default only writes drafts. See
[docs/autopilot.md](docs/autopilot.md). With Notion Config `schedule.autopilot` set to `true`
it also runs by itself at 07:00 and 19:30 India time, every day (`/autopilot when` shows the
next run).

## Apply pack

When a resume is approved the bot sends ready answers for the job's application form (visa,
salary floor, notice period, why this company) and saves them on the job's Notion page;
`/applypack <job>` sends them again. The resume's headline and location line follow the job.
See [docs/apply-pack.md](docs/apply-pack.md).

## Forms: autofill, dry runs and auto-apply

- `/autofill` gives the bookmark that fills a form from the Apply pack's code (in your own
  browser; it never presses Submit).
- `/dryrun <job>` fills the job's form in a browser on the bot and sends a screenshot and the
  answers; it never submits. `/answer question = answer` saves an answer for every form.
- `/autoapply` queues the forms of the jobs with a resume; it submits only when auto-submit is
  on (prod, your choice), at most 50 a day.

## Build a resume

```bash
python -m jobengine.resume --fake --job fixture-clean-pl   # fake Golden Master, no network
```

In Telegram, Approve in `/pending` builds a tailored one-page resume and sends a PDF preview.
Rendering needs WeasyPrint with Pango and the Lato font (use WSL on Windows). See
[docs/resume-builder.md](docs/resume-builder.md).

## Find contacts

```bash
python -m jobengine.contacts --fake --job fixture-clean-pl   # fixture people, no network
```

After your review, `/fetchcontacts` in Telegram finds the contacts of each job you applied
to today (Config `contacts.per_job`, 2 on your setup) (saved contacts first, then Apollo, Hunter, Snov) and writes the Gmail drafts with
the resume attached; `/contacts <job>` does one job. See [docs/contacts.md](docs/contacts.md). Paid lookups go only to
the best-ranked jobs the month's credits cover; the rest are apply-only (see
[docs/outreach.md](docs/outreach.md), `/outreach`).

## Write Gmail drafts

```bash
python -m jobengine.mail --fake --job fixture-clean-pl --no-write   # print the mails
```

After contacts are found the bot writes one draft per contact into Gmail Drafts (never
sends). See [docs/gmail.md](docs/gmail.md), including how to make a token with gmail.modify.

## Daily tracking and digest

```bash
python -m jobengine.track daily --fake --now 2026-10-15T08:00:00+05:30
```

Detects sent drafts, replies, bounces, drafts one follow-up after 7 days and sends a daily
report (`/today` in Telegram). Between daily checks, a reply from a contact or an employer
is pinged in Telegram within 15 minutes. See [docs/tracking.md](docs/tracking.md).

## Run Log and Error Log

Every scheduled step and every `/fetch`, `/screen` and `/autopilot` writes one row in Notion
Run Log (result, time, numbers, AI cost); the row's page holds the full report, so Telegram
gets only a short message. Every warning or site problem becomes one row a day in Error Log
(count, likely cause, example line); a new kind of problem is pinged once in Telegram.

## Strategy review and IND refresh

```bash
python -m jobengine.strategy update --fake --today 2026-10-01
```

`/update` researches current practice for your review; `/fetch` opens again when every tip is
decided. See [docs/strategy.md](docs/strategy.md).

## Branches, releases and go-live

| Branch | Holds | Deploys to (after green CI) |
|---|---|---|
| `develop` | every fix and feature, one pull request each | Cloud Run `job-engine-dev` (DEV Notion, test bot) |
| `main` | only what was proven on dev; changes arrive as a release pull request from `develop` | Cloud Run `job-engine-prod` (prod Notion, your real bot) |

The rule: **`main` is what runs in production.** A merge into `main` is a release, so it
happens only after `develop` was proven on dev. The order at go-live:

1. Merge the fixes into `develop`; CI deploys them to `job-engine-dev`.
2. Go-live steps 1 and 2 on dev (DRY_RUN true, then false for a moment); fix on `develop`.
3. When dev is stable, merge a release pull request `develop` into `main` and tag it
   (`v1.0.0`); CI deploys `job-engine-prod`. (The 17 Nov pull request #356 already put that
   day's `develop` into `main`; its deploys were skipped because Google Cloud is not set up
   yet, so nothing runs in prod. The go-live release is a new `develop` into `main` pull
   request.)
4. Go-live steps 3 and 4 on prod: DRY_RUN true first, then false.
5. Later: features keep landing on `develop` and dev; each release is a new `develop` into
   `main` pull request with a new tag. An urgent prod fix is a `hotfix/` branch from `main`,
   merged into `main` and then into `develop`. Rolling back = redeploying the previous tag.

The container runs `python -m jobengine.web` (Telegram webhook and Cloud Scheduler tasks);
`python -m jobengine.deploy.set_webhook --url <service url>` points Telegram at it. GitHub
Actions deploys with Workload Identity Federation. The console, secrets, Notion and go-live
checklists are in [docs/deploy.md](docs/deploy.md); the branch rules in
[docs/branching.md](docs/branching.md).

## Run tests

```bash
ruff check .
pytest
```

## Docs

- [Branching model](docs/branching.md)
- [Environments and safety rules](docs/environments.md)
- [Job sweep](docs/sweep.md)
- [Screening](docs/screening.md)
- [Resume builder](docs/resume-builder.md)
- [Apply pack](docs/apply-pack.md)
- [Autopilot](docs/autopilot.md)
- [Interviews](docs/interviews.md)
- [AI models and costs](docs/ai-models.md)
- [Contact finder](docs/contacts.md)
- [Outreach budget](docs/outreach.md)
- [Gmail drafts](docs/gmail.md)
- [Tracking and digest](docs/tracking.md)
- [Strategy gate](docs/strategy.md)
- [Deploy and go-live](docs/deploy.md)
