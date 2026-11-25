# Terraform's state (what it created) lives in a Cloud Storage bucket. Terraform cannot create
# its own state bucket, so it is made once by hand (infra/terraform/README.md, step 1).
# A backend block cannot use variables: change the bucket name here if yours differs, or give
# it at init time: terraform init -backend-config="bucket=<name>".
terraform {
  backend "gcs" {
    bucket = "job-search-engine-509510-tfstate"
    prefix = "job-engine"
  }
}
