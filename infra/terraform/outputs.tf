output "vm_service_account_email" {
  description = "Email of the VM's service account. Share the target spreadsheet with this address (Editor) as a one-time manual step after the first apply."
  value       = google_service_account.vm.email
}

output "deployer_service_account_email" {
  description = "Email of the GitHub Actions deployer SA. Set this as the GCP_DEPLOYER_SA GitHub repository variable."
  value       = google_service_account.deployer.email
}

output "workload_identity_provider" {
  description = "Full WIF provider resource name. Set this as the GCP_WIF_PROVIDER GitHub repository variable."
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "artifact_registry_url" {
  description = "Fully-qualified base URL for pushing/pulling images. Image URL = <this>/bot:<tag>."
  value       = "${google_artifact_registry_repository.sketch.location}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.sketch.repository_id}"
}

output "image_url_current" {
  description = "Fully-qualified image URL the systemd unit pulls."
  value       = local.image_url
}

output "discord_token_secret" {
  description = "Resource name of the Discord token secret. Add the token value with: gcloud secrets versions add <name> --data-file=-"
  value       = google_secret_manager_secret.discord_token.id
}

output "anthropic_api_key_secret" {
  description = "Resource name of the Anthropic API key secret. Add the key value with: gcloud secrets versions add <name> --data-file=-"
  value       = google_secret_manager_secret.anthropic_api_key.id
}
