# Secret Manager (docs/deploy.md B2): the secret containers and who may read them. Never the
# values: they are added by hand (gcloud secrets versions add NAME --data-file=-), so no key
# ever sits in this code or in Terraform's state.
#
# The dev runtime account never reads a prod secret.

locals {
  # name -> the runtime accounts that read it ("dev", "prod")
  required_secret_readers = {
    "notion-token-dev"             = ["dev"]
    "telegram-token-dev"           = ["dev"]
    "telegram-webhook-secret-dev"  = ["dev"]
    "anthropic-key-dev"            = ["dev"]
    "notion-token-prod"            = ["prod"]
    "telegram-token-prod"          = ["prod"]
    "telegram-webhook-secret-prod" = ["prod"]
    "anthropic-key-prod"           = ["prod"]
    "telegram-chat-id"             = ["dev", "prod"]
    "gmail-m02-token"              = ["dev", "prod"]
    "gmail-main-token"             = ["prod"]
    "adzuna-app-id"                = ["dev", "prod"]
    "adzuna-app-key"               = ["dev", "prod"]
    "apollo-api-key"               = ["prod"]
    "hunter-api-key"               = ["prod"]
    "snov-client-id"               = ["prod"]
    "snov-client-secret"           = ["prod"]
  }

  # Mounted by the deploy only when they exist (var.optional_secrets picks the ones managed).
  optional_secret_readers = {
    "gmail-mm-token"       = ["dev", "prod"]
    "jooble-api-key"       = ["dev", "prod"]
    "github-token"         = ["dev", "prod"]
    "prospeo-api-key"      = ["prod"]
    "tomba-api-key"        = ["prod"]
    "tomba-api-secret"     = ["prod"]
    "lusha-api-key"        = ["prod"]
    "gemini-api-key"       = ["prod"]
    "deepseek-api-key"     = ["prod"]
    "apollo-api-key-2"     = ["prod"]
    "hunter-api-key-2"     = ["prod"]
    "snov-client-id-2"     = ["prod"]
    "snov-client-secret-2" = ["prod"]
    "prospeo-api-key-2"    = ["prod"]
    "tomba-api-key-2"      = ["prod"]
    "tomba-api-secret-2"   = ["prod"]
  }

  secret_readers = merge(
    local.required_secret_readers,
    { for name in var.optional_secrets : name => local.optional_secret_readers[name] },
  )

  runtime_accounts = {
    dev  = google_service_account.dev_runner.email
    prod = google_service_account.prod_runner.email
  }

  # One entry per (secret, reader): "notion-token-dev/dev" -> {secret, account}
  secret_access = merge([
    for name, readers in local.secret_readers : {
      for reader in readers : "${name}/${reader}" => {
        secret  = name
        account = local.runtime_accounts[reader]
      }
    }
  ]...)
}

resource "google_secret_manager_secret" "this" {
  for_each = local.secret_readers

  secret_id = each.key

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "runtime_reads" {
  for_each = local.secret_access

  secret_id = google_secret_manager_secret.this[each.value.secret].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${each.value.account}"
}
