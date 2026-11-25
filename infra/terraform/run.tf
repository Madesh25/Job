# The two Cloud Run services (docs/deploy.md B1, .github/workflows/deploy.yml).
#
# Terraform creates each service once, with Google's sample image, and then leaves its
# revisions alone: the deploy workflow sets the image, the secrets and the environment on
# every deploy (gcloud run deploy), so `template` is ignored here. The settings below match
# that deploy, so the first revision already looks like the real one.

locals {
  services = {
    dev  = google_service_account.dev_runner.email
    prod = google_service_account.prod_runner.email
  }
}

resource "google_cloud_run_v2_service" "job_engine" {
  for_each = local.services

  name                = "job-engine-${each.key}"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = true

  template {
    service_account                  = each.value
    timeout                          = "900s"
    max_instance_request_concurrency = 4

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      image = "us-docker.pkg.dev/cloudrun/container/hello" # replaced by the first deploy

      resources {
        # 2Gi (17 Nov, measured): the auto-apply queue fills 3 forms at once.
        limits = {
          cpu    = "1"
          memory = "2Gi"
        }
        cpu_idle = false # --no-cpu-throttling: the background worker needs the CPU
      }
    }
  }

  lifecycle {
    ignore_changes = [template, client, client_version]
  }

  depends_on = [google_project_service.apis]
}

# Public, like --allow-unauthenticated: Telegram calls the webhook (checked by its secret
# header) and Cloud Scheduler's OIDC token is checked in the code.
resource "google_cloud_run_v2_service_iam_member" "public" {
  for_each = google_cloud_run_v2_service.job_engine

  name     = each.value.name
  location = each.value.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}
