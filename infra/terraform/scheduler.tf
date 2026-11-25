# Cloud Scheduler (docs/deploy.md B4): HTTP POST with an OIDC token of job-engine-scheduler,
# audience = the service URL. Every job is created paused until var.scheduler_enabled is true
# (go-live); then each follows its own `paused` value.

locals {
  # The service URL is fixed by name, project number and region (the deploy uses the same).
  service_url = {
    for env in ["dev", "prod"] :
    env => "https://job-engine-${env}-${data.google_project.this.number}.${var.region}.run.app"
  }

  india  = "Asia/Kolkata"
  warsaw = "Europe/Warsaw"

  scheduler_jobs = {
    # Run by hand: gcloud scheduler jobs run je-daily-dev
    "je-daily-dev"      = { env = "dev", path = "/tasks/daily", schedule = "30 7 * * *", tz = local.india, deadline = "900s", retries = 1, paused = true }
    "je-daily-prod"     = { env = "prod", path = "/tasks/daily", schedule = "30 7,21 * * *", tz = local.india, deadline = "900s", retries = 1, paused = false }
    "je-digest-prod"    = { env = "prod", path = "/tasks/digest", schedule = "0 20 * * 0", tz = local.india, deadline = "900s", retries = 1, paused = true }
    "je-sweep-prod"     = { env = "prod", path = "/tasks/sweep", schedule = "0 8 * * *", tz = local.india, deadline = "900s", retries = 1, paused = false }
    "je-autopilot-prod" = { env = "prod", path = "/tasks/autopilot", schedule = "*/30 7-9,19-22 * * *", tz = local.india, deadline = "60s", retries = 1, paused = false }
    "je-check-prod"     = { env = "prod", path = "/tasks/check", schedule = "*/30 15-16 * * *", tz = local.india, deadline = "60s", retries = 1, paused = false }
    "je-mail-prod"      = { env = "prod", path = "/tasks/mail", schedule = "*/10 7-12 * * 1-5", tz = local.warsaw, deadline = "300s", retries = 1, paused = false }
    "je-replies-prod"   = { env = "prod", path = "/tasks/replies", schedule = "*/15 7-23 * * *", tz = local.india, deadline = "300s", retries = 0, paused = false }
    "je-health-prod"    = { env = "prod", path = "/tasks/health", schedule = "30 6 * * *", tz = local.india, deadline = "900s", retries = 1, paused = false }
    "je-report-prod"    = { env = "prod", path = "/tasks/report", schedule = "45 21 * * *", tz = local.india, deadline = "900s", retries = 1, paused = false }
    "je-apply-prod"     = { env = "prod", path = "/tasks/apply", schedule = "*/15 7-23 * * *", tz = local.india, deadline = "900s", retries = 1, paused = false }
    "je-nudge-prod"     = { env = "prod", path = "/tasks/nudge", schedule = "*/30 22-23,0 * * *", tz = local.india, deadline = "900s", retries = 1, paused = false }
  }
}

resource "google_cloud_scheduler_job" "tasks" {
  for_each = local.scheduler_jobs

  name             = each.key
  region           = var.region
  schedule         = each.value.schedule
  time_zone        = each.value.tz
  attempt_deadline = each.value.deadline
  paused           = !var.scheduler_enabled || each.value.paused

  retry_config {
    retry_count = each.value.retries
  }

  http_target {
    http_method = "POST"
    uri         = "${local.service_url[each.value.env]}${each.value.path}"

    oidc_token {
      service_account_email = google_service_account.scheduler.email
      audience              = local.service_url[each.value.env]
    }
  }

  depends_on = [google_project_service.apis]
}
