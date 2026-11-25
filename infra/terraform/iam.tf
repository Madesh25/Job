# Service accounts, their roles and the GitHub login for the deploy workflow
# (docs/deploy.md B1.4 and B1.5).

locals {
  deployer_roles = [
    "roles/run.admin",
    "roles/artifactregistry.writer",
    "roles/cloudbuild.builds.editor",
    # The deploy checks which optional secrets exist: names only, never their values.
    "roles/secretmanager.viewer",
  ]
}

resource "google_service_account" "dev_runner" {
  account_id   = "job-engine-dev-runner"
  display_name = "Job Engine dev runtime"
}

resource "google_service_account" "prod_runner" {
  account_id   = var.prod_runtime_account_id
  display_name = "Job Engine prod runtime"
}

resource "google_service_account" "scheduler" {
  account_id   = "job-engine-scheduler"
  display_name = "Job Engine Cloud Scheduler (OIDC caller)"
}

resource "google_service_account" "deployer" {
  account_id   = "job-engine-deployer"
  display_name = "Job Engine deploy (GitHub Actions)"
}

resource "google_project_iam_member" "deployer" {
  for_each = toset(local.deployer_roles)

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

# The deployer runs each service as its runtime account.
resource "google_service_account_iam_member" "deployer_acts_as_runner" {
  for_each = {
    dev  = google_service_account.dev_runner.name
    prod = google_service_account.prod_runner.name
  }

  service_account_id = each.value
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}

# GitHub Actions of this repository only, without a service account key.
resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github"
  display_name              = "GitHub"

  depends_on = [google_project_service.apis]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub Actions"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
  }
  attribute_condition = "assertion.repository == '${var.github_repository}'"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "github_deploys" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repository}"
}
