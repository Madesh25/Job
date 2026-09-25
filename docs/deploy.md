# Deploy (Module 09)

One container runs on Cloud Run as two services: `job-engine-dev` (deployed from `develop`)
and `job-engine-prod` (deployed from `main`). It serves the Telegram webhook, the scheduled
tasks and a health check. GitHub Actions deploys it after CI passes, without service account
keys (Workload Identity Federation).

Part A (the code) is in this repository. **Part B below is console and Notion work done by
hand**; nothing in Part B is done from code. Work through it top to bottom.

## What runs where

| Path | Who may call it | Does |
|---|---|---|
| `GET /healthz` | anyone | `ok` and nothing else |
| `POST /telegram/webhook` | Telegram (secret header `X-Telegram-Bot-Api-Secret-Token`) | dedupes the update, answers 200 at once, handles it in the background worker (same handler as long polling, so the chat ID lock still applies) |
| `POST /tasks/daily` | Cloud Scheduler (OIDC token) | daily check (Module 07), then the strategy reminder (Module 08) |
| `POST /tasks/digest` | Cloud Scheduler | weekly digest |
| `POST /tasks/sweep` | Cloud Scheduler | sweep and screening, only when Config `schedule.auto_fetch` is `true` (the strategy gate still applies) |

The service allows unauthenticated calls (Telegram must reach it); every path is protected in
code. A task failure returns 500 (Scheduler records it) and sends you a Telegram message.

Local runs keep long polling: `python -m jobengine.telegram_bot` (and `--fake`). With
`BOT_MODE=webhook` the polling bot refuses to start.

## Settings used below

```bash
PROJECT=job-search-engine-509510
REGION=europe-west1          # see step B1.1
PROJECT_NUMBER="$(gcloud projects describe $PROJECT --format='value(projectNumber)')"
```

## B1. Google Cloud setup

- [ ] **1. Region.** Default `europe-west1` (a Tier 1 Cloud Run region). Check the current
      Cloud Run pricing and free tier for the region before choosing; use the same region for
      both services, Artifact Registry and Cloud Scheduler.
- [ ] **2. APIs.**
      ```bash
      gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
        cloudbuild.googleapis.com cloudscheduler.googleapis.com secretmanager.googleapis.com \
        iamcredentials.googleapis.com --project $PROJECT
      ```
- [ ] **3. Artifact Registry** Docker repo `job-engine`:
      ```bash
      gcloud artifacts repositories create job-engine --repository-format=docker \
        --location=$REGION --project $PROJECT
      ```
- [ ] **4. Service accounts.**
      ```bash
      gcloud iam service-accounts create job-engine-dev-runner --project $PROJECT
      gcloud iam service-accounts create job-engine-scheduler --project $PROJECT
      gcloud iam service-accounts create job-engine-deployer --project $PROJECT
      # job-pipeline-runner (prod runtime) already exists.
      DEPLOYER=job-engine-deployer@$PROJECT.iam.gserviceaccount.com
      for role in roles/run.admin roles/artifactregistry.writer roles/cloudbuild.builds.editor; do
        gcloud projects add-iam-policy-binding $PROJECT \
          --member serviceAccount:$DEPLOYER --role $role
      done
      for sa in job-engine-dev-runner job-pipeline-runner; do
        gcloud iam service-accounts add-iam-policy-binding \
          $sa@$PROJECT.iam.gserviceaccount.com \
          --member serviceAccount:$DEPLOYER --role roles/iam.serviceAccountUser
      done
      ```
      The scheduler account needs no `roles/run.invoker` (the service is public); its OIDC
      token identity is checked in code. Whoever creates the Scheduler jobs needs
      `roles/iam.serviceAccountUser` on `job-engine-scheduler`.
- [ ] **5. Workload Identity Federation** (GitHub, limited to `Madesh25/Job`):
      ```bash
      gcloud iam workload-identity-pools create github --location=global --project $PROJECT
      gcloud iam workload-identity-pools providers create-oidc github \
        --location=global --workload-identity-pool=github --project $PROJECT \
        --issuer-uri=https://token.actions.githubusercontent.com \
        --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
        --attribute-condition="assertion.repository=='Madesh25/Job'"
      gcloud iam service-accounts add-iam-policy-binding $DEPLOYER --project $PROJECT \
        --role roles/iam.workloadIdentityUser \
        --member "principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/attribute.repository/Madesh25/Job"
      ```
- [ ] **6. GitHub repository variables** (Settings > Secrets and variables > Actions >
      Variables; they are not secrets):
      `GCP_WIF_PROVIDER` = `projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/github/providers/github`,
      `GCP_DEPLOY_SA` = `job-engine-deployer@job-search-engine-509510.iam.gserviceaccount.com`,
      `GCP_PROJECT_ID` = `job-search-engine-509510`, `GCP_PROJECT_NUMBER` = the number above,
      `GCP_REGION` = the region. Optional: add a required reviewer to the `prod` environment.
- [ ] **7. Budget alert** already exists; keep it.
- [ ] **8. Cost note.** CPU is always allocated while an instance is alive
      (`--no-cpu-throttling`, needed for the background worker); it scales to zero when idle
      (`--min-instances 0`). A few interactions a day stay in or near the free tier. Check the
      billing report after the first week.

## B2. Secrets (Secret Manager)

Create the missing ones (`printf '%s' "$VALUE" | gcloud secrets create NAME --data-file=- --project $PROJECT`,
or `gcloud secrets versions add NAME --data-file=-` for a new value). Names are exactly these:

| Env var | dev secret | prod secret |
|---|---|---|
| `NOTION_TOKEN` | `notion-token-dev` | `notion-token-prod` |
| `TELEGRAM_BOT_TOKEN` | `telegram-token-dev` | `telegram-token-prod` |
| `TELEGRAM_CHAT_ID` | `telegram-chat-id` | `telegram-chat-id` |
| `TELEGRAM_WEBHOOK_SECRET` | `telegram-webhook-secret-dev` | `telegram-webhook-secret-prod` |
| `ANTHROPIC_API_KEY` | `anthropic-key-dev` | `anthropic-key-prod` |
| `GMAIL_ALERTS_TOKEN_JSON` | `gmail-m02-token` | `gmail-m02-token` |
| `GMAIL_SENDER_TOKEN_JSON` | `gmail-m02-token` | `gmail-main-token` |
| `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | `adzuna-app-id`, `adzuna-app-key` | same |
| `APOLLO_API_KEY`, `HUNTER_API_KEY` | not mounted | `apollo-api-key`, `hunter-api-key` |
| `SNOV_CLIENT_ID`, `SNOV_CLIENT_SECRET` | not mounted | `snov-client-id`, `snov-client-secret` |

- [ ] Create a DEV bot with @BotFather; store its token as `telegram-token-dev`. The existing
      `telegram-bot-token` value becomes `telegram-token-prod`.
- [ ] Webhook secrets: 32+ random letters and digits each, for example
      `python -c "import secrets,string;print(''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(48)))"`.
- [ ] `gmail-m02-token` and `gmail-main-token` hold the full authorised-user JSON from
      `python -m jobengine.gmail_auth` (Module 06, with gmail.modify).
- [ ] **Access, dev:** `job-engine-dev-runner` gets `roles/secretmanager.secretAccessor` on
      the dev secrets, `telegram-chat-id`, `adzuna-app-id`, `adzuna-app-key` and
      `gmail-m02-token` only:
      ```bash
      for s in notion-token-dev telegram-token-dev telegram-chat-id telegram-webhook-secret-dev \
               anthropic-key-dev gmail-m02-token adzuna-app-id adzuna-app-key; do
        gcloud secrets add-iam-policy-binding $s --project $PROJECT \
          --member serviceAccount:job-engine-dev-runner@$PROJECT.iam.gserviceaccount.com \
          --role roles/secretmanager.secretAccessor
      done
      ```
- [ ] **Access, prod:** `job-pipeline-runner` gets the same role on the prod secrets
      (`notion-token-prod`, `telegram-token-prod`, `telegram-chat-id`,
      `telegram-webhook-secret-prod`, `anthropic-key-prod`, `gmail-m02-token`,
      `gmail-main-token`, `adzuna-*`, `apollo-api-key`, `hunter-api-key`, `snov-client-*`).
      The dev account must not be able to read any prod secret.
- [ ] After go-live, delete the old refresh-token-only secrets.

## B3. Notion production changes (reviewer, with your OK)

These exist on the DEV copies since Modules 02 to 08 and are now applied to production:

- [ ] Job Opportunities: `Posting IDs` (text); Status options `Approved`, `Declined`.
- [ ] Resume Log: `Approved` (checkbox), `Revision` (number).
- [ ] Contacts: `Follow-up draft ID` (text); Status option `Ghosted`.
- [ ] Create `Bot State` (Key title, Value text, Updated date) under the Job Engine page; put
      its data source ID into `config/prod.yaml` under `notion.write.bot_state` (a one-line PR).
      Until then prod keeps bot state (update dedupe, /jd pastes, strategy runs) in memory only.
- [ ] Share the Job Engine page with the prod Notion integration.
- [ ] Config: add `schedule.auto_fetch` = `false`.

## B4. Cloud Scheduler (time zone Asia/Kolkata)

All jobs: HTTP POST, OIDC as `job-engine-scheduler`, audience = the service URL, attempt
deadline 900 s, 1 retry.

```bash
SCHED=job-engine-scheduler@$PROJECT.iam.gserviceaccount.com
DEV_URL="https://job-engine-dev-$PROJECT_NUMBER.$REGION.run.app"
PROD_URL="https://job-engine-prod-$PROJECT_NUMBER.$REGION.run.app"
job() {  # name schedule url path
  gcloud scheduler jobs create http "$1" --project $PROJECT --location $REGION \
    --schedule "$2" --time-zone "Asia/Kolkata" --uri "$3$4" --http-method POST \
    --oidc-service-account-email "$SCHED" --oidc-token-audience "$3" \
    --attempt-deadline 900s --max-retry-attempts 1
}
job je-daily-dev   "30 7 * * *" "$DEV_URL"  /tasks/daily  && gcloud scheduler jobs pause je-daily-dev --project $PROJECT --location $REGION
job je-daily-prod  "30 7 * * *" "$PROD_URL" /tasks/daily
job je-digest-prod "0 20 * * 0" "$PROD_URL" /tasks/digest && gcloud scheduler jobs pause je-digest-prod --project $PROJECT --location $REGION
job je-sweep-prod  "0 8 * * *"  "$PROD_URL" /tasks/sweep
```

- [ ] `je-daily-dev`: paused (run it by hand with `gcloud scheduler jobs run`).
- [ ] `je-daily-prod`: `30 7 * * *`, enabled at go-live step 3.
- [ ] `je-digest-prod`: `0 20 * * 0` (Sunday 20:00), paused until go-live step 4.
- [ ] `je-sweep-prod`: `0 8 * * *`; it does nothing unless Config `schedule.auto_fetch` is `true`.

## How deploys treat DRY_RUN

The workflow never sets `DRY_RUN`, so a new service runs with the safe default (true). It
applies `APP_ENV`, `BOT_MODE`, `SERVICE_URL` and `SCHEDULER_SA_EMAIL` with
`--update-env-vars`, which **keeps every other variable already on the service**. So once you
turn DRY_RUN off by hand (go-live steps 2 and 4), later deploys keep it off; turn it back on
the same way. Do not use `--set-env-vars` or edit the variables in the console "replace all"
way, or DRY_RUN would be lost (and default back to true).

## Go-live in 4 steps

Each step has a pass condition. Do not skip ahead.

**Step 1: dev on Cloud Run, DRY_RUN true.**
- [ ] Merge this PR into `develop` (auto-deploys `job-engine-dev` after CI).
- [ ] Point the DEV bot at it (with the dev token and secret in your shell):
      `TELEGRAM_BOT_TOKEN=... TELEGRAM_WEBHOOK_SECRET=... python -m jobengine.deploy.set_webhook --url $DEV_URL`
- [ ] In Telegram (DEV bot): `/health`, `/fetch`, `/pending`, approve one job, approve the
      resume, check contacts come from fixtures and drafts say DRY RUN, `/today`, `/update`.
- [ ] Pass: every reply starts with `[DEV]`; Notion writes only in the DEV Sandbox; no Gmail
      drafts; no provider calls in the logs.

**Step 2: dev, DRY_RUN false.**
- [ ] `gcloud run services update job-engine-dev --region $REGION --update-env-vars DRY_RUN=false`
- [ ] Repeat one job end to end.
- [ ] Pass: drafts in madeshwaranm02's Gmail with `[DEV] to <address> |` subjects and the PDF
      attached; the PDF is in `Job Engine Resumes (DEV)` on m02's Drive; sending one draft to
      yourself and running `/today` marks it Contacted; replying to it marks it Replied.

**Step 3: prod, DRY_RUN true.**
- [ ] B3 (Notion production changes) done.
- [ ] Open a PR `develop` into `main` and merge it (auto-deploys `job-engine-prod`).
- [ ] `set_webhook --url $PROD_URL` with the prod token and secret.
- [ ] Unpause nothing yet except `je-daily-prod`.
- [ ] Pass: the prod bot replies without a prefix; sweep and screening write to production
      Job Opportunities; resume previews arrive; no Gmail drafts, no Drive uploads, no
      Apollo/Hunter/Snov calls.
- [ ] Run the contact provider probes once each (`python -m jobengine.contacts --probe
      apollo|hunter|snov --domain <domain>` with prod settings and `DRY_RUN=false` in your
      shell, Module 05) and record the results in the Config notes.

**Step 4: prod, DRY_RUN false.**
- [ ] `gcloud run services update job-engine-prod --region $REGION --update-env-vars DRY_RUN=false`
- [ ] Do one real job (for example the Coforge role) end to end, reading every draft before
      sending.
- [ ] Pass: drafts in madeshwaran.manikam's Gmail with no prefix, the right signature and the
      PDF; credit counters in Config move; the next day's report shows the sent mails.
- [ ] Then resume `je-digest-prod`:
      `gcloud scheduler jobs resume je-digest-prod --project $PROJECT --location $REGION`

## Rollback (any step)

- Previous revision: `gcloud run revisions list --service <service> --region $REGION`, then
  `gcloud run services update-traffic <service> --region $REGION --to-revisions <previous revision>=100`
- Stop real actions: `gcloud run services update <service> --region $REGION --update-env-vars DRY_RUN=true`
- Silence the bot: `python -m jobengine.deploy.set_webhook --delete` (then long polling works
  again locally).
- Pause a job: `gcloud scheduler jobs pause <job> --project $PROJECT --location $REGION`

## Troubleshooting

- The revision fails to start: the startup check refused the configuration (a prod ID in dev,
  a missing `TELEGRAM_*` or `SERVICE_URL` variable, or a missing secret). The Cloud Run logs say
  which; nothing runs half-configured.
- Telegram shows "Wrong response from the webhook: 401": the webhook secret in Secret Manager
  and the one used with `set_webhook` differ. Run `set_webhook` again.
- A Scheduler job fails with 401/403: its OIDC audience must be the service URL exactly, and
  it must run as `job-engine-scheduler`.
