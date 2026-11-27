# Terraform for the Google Cloud setup of the Job Engine (docs/deploy.md B1, B2, B4).
# Written 25 Nov from the manual steps in docs/deploy.md; nothing here holds a secret value.
terraform {
  # 1.9: a variable check may use a local (variables.tf optional_secrets); 1.7: import blocks
  # with for_each (imports.tf.example).
  required_version = ">= 1.9"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

data "google_project" "this" {
  project_id = var.project_id
}
