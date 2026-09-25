# Job Engine

Job Engine is a personal job search bot for Madeshwaran M, a DevOps engineer looking for roles in Poland, the Netherlands and Ireland. It finds and screens openings, builds tailored resumes, finds contacts, drafts outreach emails in Gmail and tracks everything in Notion, with Telegram as the control surface. This repository currently holds the foundation: environments, configuration and the safety layer that keeps test runs away from production data and real people.

## Run locally

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # fill in dev values only
python -m jobengine.main
```

`APP_ENV` defaults to `local` and `DRY_RUN` defaults to `true`.

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

After you approve a resume in Telegram the bot finds 4 contacts at the company (cache first,
then Apollo, Hunter, Snov). See [docs/contacts.md](docs/contacts.md).

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
report (`/today` in Telegram). See [docs/tracking.md](docs/tracking.md).

## Strategy review and IND refresh

```bash
python -m jobengine.strategy update --fake --today 2026-10-01
```

`/update` researches current practice for your review; `/fetch` opens again when every tip is
decided. See [docs/strategy.md](docs/strategy.md).

## Deploy (Cloud Run)

The same code runs on Cloud Run as `job-engine-dev` (from `develop`) and `job-engine-prod`
(from `main`), with a Telegram webhook and Cloud Scheduler tasks:

```bash
python -m jobengine.web                                   # the container entry point
python -m jobengine.deploy.set_webhook --url <service url>
```

GitHub Actions deploys after CI with Workload Identity Federation. The console, secrets,
Notion and go-live checklists are in [docs/deploy.md](docs/deploy.md).

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
- [Contact finder](docs/contacts.md)
- [Gmail drafts](docs/gmail.md)
- [Tracking and digest](docs/tracking.md)
- [Strategy gate](docs/strategy.md)
- [Deploy and go-live](docs/deploy.md)
