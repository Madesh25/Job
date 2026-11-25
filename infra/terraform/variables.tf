variable "project_id" {
  description = "The Google Cloud project for both services (dev and prod)."
  type        = string
  default     = "job-search-engine-509510"
}

variable "region" {
  description = "One region for Cloud Run, Artifact Registry and Cloud Scheduler (docs/deploy.md B1.1)."
  type        = string
  default     = "europe-west1"
}

variable "github_repository" {
  description = "The only GitHub repository allowed to deploy (Workload Identity Federation)."
  type        = string
  default     = "Madesh25/Job"
}

variable "prod_runtime_account_id" {
  description = "The prod runtime service account (it existed before this setup)."
  type        = string
  default     = "job-pipeline-runner"
}

variable "optional_secrets" {
  description = <<-EOT
    Optional secrets (local.optional_secret_readers) to manage here. List only the ones that
    already have a value: the deploy mounts every optional secret that exists, and a secret
    without a version would make that deploy fail.
  EOT
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for name in var.optional_secrets : contains(keys(local.optional_secret_readers), name)
    ])
    error_message = "Each optional secret must be one of local.optional_secret_readers (secrets.tf)."
  }
}

variable "scheduler_enabled" {
  description = <<-EOT
    false (the default): every Cloud Scheduler job is created paused, so nothing runs before
    go-live. true: each job follows its own `paused` value in scheduler.tf.
  EOT
  type        = bool
  default     = false
}
