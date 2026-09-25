# Environments

`APP_ENV` selects the environment: `local`, `dev` or `prod`. It defaults to `local`. Any other value stops the app at startup.

| | local (laptop) | dev (Cloud Run, later) | prod (Cloud Run, later) |
|---|---|---|---|
| Notion writes go to | DEV Sandbox databases | DEV Sandbox databases | Production databases |
| Notion integration token | `job-engine-dev` | `job-engine-dev` | `job-engine-prod` |
| Gmail sender account | madeshwaranm02@gmail.com | madeshwaranm02@gmail.com | madeshwaran.manikam@gmail.com |
| Outgoing mail recipients | all redirected to madeshwaranm02@gmail.com | all redirected to madeshwaranm02@gmail.com | real recipients |
| Telegram bot | DEV bot, messages prefixed `[LOCAL]` | DEV bot, messages prefixed `[DEV]` | prod bot, no prefix |
| Contact finder APIs (Apollo, Hunter, Snov) | never called, fixtures used | never called, fixtures used | real calls |
| Claude model | forced to `claude-haiku-4-5` | forced to `claude-haiku-4-5` | models from Notion Config |
| Scheduler | manual runs only | manual runs only | Cloud Scheduler |

Local and dev read the production reference databases (Target Companies, Config, Strategy, Skills Inventory, Term Map, Evidence Library, Project Registry, Resume Sections) but never write to them. They write only to the DEV Sandbox copies of Job Opportunities, Contacts and Resume Log.

## DRY_RUN

`DRY_RUN` is a second switch that sits on top of `APP_ENV`.

- `DRY_RUN=true` is the default in every environment. Nothing is written to Gmail and no paid API is called. Such actions are only logged as `DRY RUN: would ...`. Notion sandbox writes and Telegram messages still happen.
- `DRY_RUN=false` lets Gmail drafts be created. In local and dev they are still redirected to madeshwaranm02@gmail.com. Only prod with `DRY_RUN=false` reaches real people.
- `DRY_RUN` is false only when the variable is exactly the string `false`. `False`, `0`, `no`, an empty value or a missing variable all mean true.

## Startup safety check

`python -m jobengine.main` runs `safety.check_startup` and exits with code 1 if:

- local or dev has a Notion write target that is a production data source ID
- local or dev still has a `<placeholder>` write target
- local or dev uses any Gmail sender other than madeshwaranm02@gmail.com
- prod uses any Gmail sender other than madeshwaran.manikam@gmail.com
- prod has a forced model

## Secrets per environment

Secrets are read from environment variables only. For local they may also live in a git-ignored `.env` file (see `.env.example`).

| Secret | local (.env) | dev (Secret Manager) | prod (Secret Manager) |
|---|---|---|---|
| NOTION_TOKEN | dev integration | `notion-token-dev` | `notion-token-prod` |
| TELEGRAM_BOT_TOKEN | dev bot | `telegram-token-dev` | `telegram-token-prod` |
| TELEGRAM_CHAT_ID | same chat | same chat | same chat |
| ANTHROPIC_API_KEY | dev key | `anthropic-key-dev` | `anthropic-key-prod` |
| GMAIL_ALERTS_TOKEN_JSON | m02 token | `gmail-m02-token` | `gmail-m02-token` |
| GMAIL_SENDER_TOKEN_JSON | m02 token | `gmail-m02-token` | `gmail-main-token` |
| Apollo, Hunter, Snov keys | not needed | not needed | prod only |

The dev Cloud Run service account may access only the dev secrets and `gmail-m02-token`. The `gmail-main-token` secret exists only for prod.
