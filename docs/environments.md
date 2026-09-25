# Environments

`APP_ENV` selects the environment: `local`, `dev` or `prod`. It defaults to `local`. Any other value stops the app at startup.

| | local (laptop) | dev (Cloud Run `job-engine-dev`) | prod (Cloud Run `job-engine-prod`) |
|---|---|---|---|
| Notion writes go to | DEV Sandbox databases | DEV Sandbox databases | Production databases |
| Notion integration token | `job-engine-dev` | `job-engine-dev` | `job-engine-prod` |
| Gmail sender account | madeshwaranm02@gmail.com | madeshwaranm02@gmail.com | madeshwaran.manikam@gmail.com |
| Outgoing mail recipients | all redirected to madeshwaranm02@gmail.com | all redirected to madeshwaranm02@gmail.com | real recipients |
| Telegram bot | DEV bot, messages prefixed `[LOCAL]` | DEV bot, messages prefixed `[DEV]` | prod bot, no prefix |
| Contact finder APIs (Apollo, Hunter, Snov) | never called, fixtures used | never called, fixtures used | real calls |
| Claude model | forced to `claude-haiku-4-5` | forced to `claude-haiku-4-5` | models from Notion Config |
| Telegram mode | long polling (`BOT_MODE=polling`) | webhook (`BOT_MODE=webhook`) | webhook (`BOT_MODE=webhook`) |
| Scheduler | manual runs only | `je-daily-dev` (paused) | Cloud Scheduler (daily, digest, sweep) |

Local and dev read the production reference databases (Target Companies, Config, Strategy, Skills Inventory, Term Map, Evidence Library, Project Registry, Resume Sections) but never write to them. They write only to the DEV Sandbox copies of Job Opportunities, Contacts and Resume Log.

## DRY_RUN

`DRY_RUN` is a second switch that sits on top of `APP_ENV`.

- `DRY_RUN=true` is the default in every environment. Nothing is written to Gmail and no paid API is called. Such actions are only logged as `DRY RUN: would ...`. Notion sandbox writes and Telegram messages still happen.
- `DRY_RUN=false` lets Gmail drafts be created. In local and dev they are still redirected to madeshwaranm02@gmail.com. Only prod with `DRY_RUN=false` reaches real people.
- `DRY_RUN` does **not** block LLM calls. They cost cents and nothing can be tested without them,
  so they run whenever `ANTHROPIC_API_KEY` is set (`safety.llm_allowed`). DRY_RUN blocks Gmail
  writes, Drive writes and paid contact APIs, not the LLM. Outside prod the model is still
  forced to `claude-haiku-4-5`.
- `DRY_RUN` is false only when the variable is exactly the string `false`. `False`, `0`, `no`, an empty value or a missing variable all mean true.

## Startup safety check

`python -m jobengine.main` runs `safety.check_startup` and exits with code 1 if:

- local or dev has a Notion write target that is a production data source ID
- local or dev still has a `<placeholder>` write target
- local or dev uses any Gmail sender other than madeshwaranm02@gmail.com
- prod uses any Gmail sender other than madeshwaran.manikam@gmail.com
- prod has a forced model

## Secrets per environment

Secrets are read from environment variables only. For local they may also live in a git-ignored
`.env` file (see `.env.example`). On Cloud Run they come from Secret Manager (the deploy
workflow maps them with `--set-secrets`); nothing is baked into the image or the workflow.

| Env var | local (.env) | dev secret | prod secret | Notes |
|---|---|---|---|---|
| `NOTION_TOKEN` | dev integration | `notion-token-dev` | `notion-token-prod` | dev shared with the DEV Sandbox and the reference DBs; prod shared with the Job Engine page |
| `TELEGRAM_BOT_TOKEN` | DEV bot | `telegram-token-dev` | `telegram-token-prod` | the existing `telegram-bot-token` becomes the prod one; a DEV bot comes from @BotFather |
| `TELEGRAM_CHAT_ID` | your chat | `telegram-chat-id` | `telegram-chat-id` | same chat |
| `TELEGRAM_WEBHOOK_SECRET` | not needed | `telegram-webhook-secret-dev` | `telegram-webhook-secret-prod` | random, 32+ letters and digits |
| `ANTHROPIC_API_KEY` | dev key | `anthropic-key-dev` | `anthropic-key-prod` | may be the same key at first |
| `GMAIL_ALERTS_TOKEN_JSON` | m02 token | `gmail-m02-token` | `gmail-m02-token` | authorised-user JSON from `jobengine.gmail_auth` |
| `GMAIL_SENDER_TOKEN_JSON` | m02 token | `gmail-m02-token` | `gmail-main-token` | the main account token exists only for prod |
| `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | your keys | `adzuna-app-id`, `adzuna-app-key` | same | exist |
| `APOLLO_API_KEY`, `HUNTER_API_KEY` | not needed | not mounted | `apollo-api-key`, `hunter-api-key` | exist |
| `SNOV_CLIENT_ID`, `SNOV_CLIENT_SECRET` | not needed | not mounted | `snov-client-id`, `snov-client-secret` | exist |

Plain environment variables (not secrets), set by the deploy workflow: `APP_ENV`, `BOT_MODE`,
`SERVICE_URL`, `SCHEDULER_SA_EMAIL`. `DRY_RUN` is never set by code or the workflow; it is
changed by hand at go-live (see [deploy.md](deploy.md)).

The dev runtime service account may read only the dev secrets, the shared `telegram-chat-id`,
`adzuna-*` and `gmail-m02-token`. The contact provider secrets and `gmail-main-token` are never
mounted in dev.
