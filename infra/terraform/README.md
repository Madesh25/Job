# Terraform: the Google Cloud setup in code

The Google Cloud setup of `docs/deploy.md` (B1, B2, B4) as Terraform, for one project with
the dev and prod services: APIs, the Docker repository, the four service accounts and their
roles, the GitHub login for the deploy workflow (Workload Identity Federation, this
repository only), the Secret Manager containers and who reads them, the two Cloud Run
services, and the Cloud Scheduler jobs.

Nothing here holds a secret value, and `terraform plan` changes nothing. You run it from
your PC with your own Google login; the bot and GitHub never run it.

## Not in Terraform (stay by hand)

- **Billing** (the budget alert stays as it is, B1.7).
- **The state bucket** (step 1 below): Terraform cannot create the bucket that holds its own
  state.
- **Secret values**: add each with `gcloud secrets versions add NAME --data-file=-`, so no
  key is ever in this code or in the state file.
- **What runs in Cloud Run**: the image, the secrets mounted and the environment are set by
  `.github/workflows/deploy.yml` on every deploy; Terraform creates each service once and then
  leaves its revisions alone.
- **Notion production changes** (B3) and **the Telegram webhook**
  (`python -m jobengine.deploy.set_webhook`).
- **The GitHub repository variables** (B1.6): copy them from `terraform output`.

## Run it

1. **The state bucket, once** (versions kept, so a mistake can be undone):
   ```bash
   gcloud storage buckets create gs://job-search-engine-509510-tfstate \
     --project job-search-engine-509510 --location europe-west1 --uniform-bucket-level-access
   gcloud storage buckets update gs://job-search-engine-509510-tfstate --versioning
   ```
2. **Your login** for Terraform: `gcloud auth application-default login`.
3. **Adopt what already exists.** Most of B1 and B2 was done by hand. Copy
   `imports.tf.example` to `imports.tf` and delete the blocks of anything that does not exist
   yet (`gcloud run services list`, `gcloud iam service-accounts list`,
   `gcloud secrets list`, `gcloud scheduler jobs list --location europe-west1`).
4. **Settings**: copy `terraform.tfvars.example` to `terraform.tfvars` and list your optional
   secrets that have a value.
5. **Look first, then apply:**
   ```bash
   cd infra/terraform
   terraform init
   terraform plan      # read it: "import" for what exists, "create" for what is new
   terraform apply     # only when the plan shows what you expect
   ```
   Then delete `imports.tf`.
6. **Go-live**: set `scheduler_enabled = true` in `terraform.tfvars` and apply again; the jobs
   that `docs/deploy.md` keeps paused (`je-daily-dev`, `je-digest-prod`) stay paused until you
   change their `paused` in `scheduler.tf`.

## Files

| File | What |
|---|---|
| `versions.tf`, `backend.tf` | Terraform and the Google provider; the state in the bucket |
| `variables.tf` | project, region, repository, optional secrets, scheduler switch |
| `main.tf` | APIs, Artifact Registry `job-engine` |
| `iam.tf` | service accounts, the deployer's roles, Workload Identity Federation |
| `secrets.tf` | secret containers and read access (dev never reads a prod secret) |
| `run.tf` | Cloud Run `job-engine-dev` and `job-engine-prod`, public invoker |
| `scheduler.tf` | the 12 Cloud Scheduler jobs of B4 |
| `outputs.tf` | the GitHub repository variables |
| `imports.tf.example` | adopting what exists |
