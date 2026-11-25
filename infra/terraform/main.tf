# APIs and the Docker repository (docs/deploy.md B1.2 and B1.3).

locals {
  apis = [
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudscheduler.googleapis.com",
    "secretmanager.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "sts.googleapis.com", # GitHub's token is exchanged here (Workload Identity Federation)
  ]
}

resource "google_project_service" "apis" {
  for_each = toset(local.apis)

  service            = each.value
  disable_on_destroy = false # never switch an API off when this code is removed
}

resource "google_artifact_registry_repository" "job_engine" {
  location      = var.region
  repository_id = "job-engine"
  format        = "DOCKER"
  description   = "Job Engine images, pushed by .github/workflows/deploy.yml"

  depends_on = [google_project_service.apis]
}
