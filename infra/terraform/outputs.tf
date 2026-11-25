# The values the GitHub repository variables need (docs/deploy.md B1.6); none is a secret.

output "GCP_PROJECT_ID" {
  value = var.project_id
}

output "GCP_PROJECT_NUMBER" {
  value = data.google_project.this.number
}

output "GCP_REGION" {
  value = var.region
}

output "GCP_WIF_PROVIDER" {
  value = google_iam_workload_identity_pool_provider.github.name
}

output "GCP_DEPLOY_SA" {
  value = google_service_account.deployer.email
}

output "service_urls" {
  value = local.service_url
}
