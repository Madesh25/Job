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

## Run tests

```bash
ruff check .
pytest
```

## Docs

- [Branching model](docs/branching.md)
- [Environments and safety rules](docs/environments.md)
