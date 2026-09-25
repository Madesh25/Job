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
