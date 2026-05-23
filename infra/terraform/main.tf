provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone

  # `billing_project` + `user_project_override` make the provider send the
  # X-Goog-User-Project header on every API call, pinning the "consumer"
  # (the project that's billed and that needs the API enabled) to
  # var.project_id. Without these, WIF-impersonated calls from CI default
  # to a consumer the provider picks server-side, which manifests as
  # `SERVICE_DISABLED` 403s referring to a project number that isn't the
  # one we're managing. Local applies don't surface this because ADC from
  # `gcloud auth application-default login` already has a user project
  # pinned by gcloud config.
  billing_project       = var.project_id
  user_project_override = true
}

locals {
  repo_name      = "sketch"
  image_basename = "bot"
  image_url      = "${var.region}-docker.pkg.dev/${var.project_id}/${local.repo_name}/${local.image_basename}:${var.image_tag}"

  required_apis = [
    "sheets.googleapis.com",
    "secretmanager.googleapis.com",
    "iap.googleapis.com",
    "compute.googleapis.com",
    "artifactregistry.googleapis.com",
    # cloudresourcemanager and iam are tracked here so they're protected
    # against accidental disable, but the very first enablement of both has
    # to happen manually via gcloud — terraform's `google_project_service`
    # itself goes through Cloud Resource Manager, so it can't bootstrap CRM
    # from nothing, and `google_service_account` needs IAM API just to read
    # state. See infra/terraform/README.md §2 for the one-time gcloud
    # commands. After the manual bootstrap these stay terraform-managed.
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "sts.googleapis.com",
  ]
}

# ----------------------------------------------------------------------------
# APIs
# ----------------------------------------------------------------------------

resource "google_project_service" "enabled" {
  for_each = toset(local.required_apis)

  service            = each.value
  disable_on_destroy = false
}

# ----------------------------------------------------------------------------
# Service accounts
# ----------------------------------------------------------------------------

resource "google_service_account" "vm" {
  account_id   = "sketch-vm"
  display_name = "Sketch VM (runtime)"
  description  = "Attached to the GCE VM. Reads the Discord token from Secret Manager and the Pokepaste team data from the configured Google Sheet."
}

resource "google_service_account" "deployer" {
  account_id   = "sketch-deployer"
  display_name = "Sketch Deployer (GitHub Actions)"
  description  = "Impersonated by GitHub Actions via Workload Identity Federation. Pushes images and restarts the VM service."
}

# ----------------------------------------------------------------------------
# Secret Manager — Discord token
# ----------------------------------------------------------------------------

resource "google_secret_manager_secret" "discord_token" {
  secret_id = "sketch-discord-token"

  replication {
    auto {}
  }

  depends_on = [google_project_service.enabled]
}

resource "google_secret_manager_secret_iam_member" "vm_token_access" {
  secret_id = google_secret_manager_secret.discord_token.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.vm.email}"
}

# ----------------------------------------------------------------------------
# Artifact Registry — container images
# ----------------------------------------------------------------------------

resource "google_artifact_registry_repository" "sketch" {
  location      = var.region
  repository_id = local.repo_name
  description   = "Sketch bot container images."
  format        = "DOCKER"

  # KEEP rules ALWAYS take precedence over DELETE rules. So the :current tag
  # and the 5 most-recent images are protected even if they're older than 30
  # days. Only images that match the DELETE condition AND no KEEP rule are
  # actually removed.
  #
  # Net effect: :current is safe (it's a moving pointer, and whichever image
  # carries it is kept). The 5 most-recent SHA-tagged builds are safe for
  # rollback. Older orphaned tags get garbage-collected after 30 days. This
  # stays well under the Artifact Registry 0.5 GB free tier (each image is
  # ~80 MB compressed, so 5 + current ≈ 480 MB peak).
  #
  # To preview without deleting, set `cleanup_policy_dry_run = true` below;
  # AR will log what it would delete to Cloud Logging instead of acting.
  cleanup_policies {
    id     = "keep-current"
    action = "KEEP"
    condition {
      tag_state    = "TAGGED"
      tag_prefixes = ["current"]
    }
  }

  cleanup_policies {
    id     = "keep-recent-shas"
    action = "KEEP"
    most_recent_versions {
      keep_count = 5
    }
  }

  cleanup_policies {
    id     = "delete-old"
    action = "DELETE"
    condition {
      older_than = "2592000s" # 30 days
      tag_state  = "ANY"
    }
  }

  depends_on = [google_project_service.enabled]
}

resource "google_artifact_registry_repository_iam_member" "deployer_writer" {
  location   = google_artifact_registry_repository.sketch.location
  repository = google_artifact_registry_repository.sketch.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_artifact_registry_repository_iam_member" "vm_reader" {
  location   = google_artifact_registry_repository.sketch.location
  repository = google_artifact_registry_repository.sketch.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.vm.email}"
}

# ----------------------------------------------------------------------------
# Compute — GCE VM
# ----------------------------------------------------------------------------

resource "google_compute_instance" "sketch" {
  name         = "sketch"
  machine_type = "e2-micro"
  zone         = var.zone

  tags = ["sketch"]

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
      size  = 10
      type  = "pd-standard"
    }
  }

  network_interface {
    network = "default"
    access_config {} # Ephemeral public IP for outbound to Discord/googleapis
  }

  service_account {
    email = google_service_account.vm.email
    # cloud-platform covers GCP APIs (Secret Manager, Artifact Registry).
    # The Sheets scope must be listed separately — Workspace APIs don't honor
    # cloud-platform. Removing it causes ACCESS_TOKEN_SCOPE_INSUFFICIENT.
    scopes = [
      "https://www.googleapis.com/auth/cloud-platform",
      "https://www.googleapis.com/auth/spreadsheets",
    ]
  }

  # OS Login lets the deployer service account SSH in via IAP without
  # managing SSH keys.
  #
  # `startup-script` is set as a key inside `metadata` (not via the top-level
  # `metadata_startup_script` attribute) because changes to entries inside the
  # `metadata` map are updated in-place by the GCP API, while changes to
  # `metadata_startup_script` are ForceNew and recreate the VM. Since the
  # startup script's content depends on guild_config, image_url, etc. — values
  # we expect to edit routinely — keeping the script in `metadata` lets those
  # edits propagate without destroying the running bot.
  metadata = {
    enable-oslogin = "TRUE"
    startup-script = templatefile("${path.module}/startup.sh.tftpl", {
      region       = var.region
      image_url    = local.image_url
      project_id   = var.project_id
      dev_guild_id = var.dev_guild_id
      # Drop unset broadcast_channel_id entries before encoding so the JSON the
      # Python validator parses doesn't carry an explicit null for guilds that
      # don't broadcast — keeps the env-var payload clean either way.
      guild_config_json = jsonencode({
        for guild_id, cfg in var.guild_config : guild_id => merge(
          { spreadsheet_id = cfg.spreadsheet_id },
          cfg.broadcast_channel_id != null ? { broadcast_channel_id = cfg.broadcast_channel_id } : {}
        )
      })
      sketch_service = file("${path.module}/../../deploy/sketch.service")
    })
  }

  allow_stopping_for_update = true

  depends_on = [
    google_project_service.enabled,
    google_artifact_registry_repository_iam_member.vm_reader,
    google_secret_manager_secret_iam_member.vm_token_access,
  ]
}

# ----------------------------------------------------------------------------
# Firewall — SSH only from IAP
# ----------------------------------------------------------------------------
#
# The rule below is the ONLY ingress rule Terraform manages: SSH from Google's
# IAP forwarding range, gated to the "sketch" target tag.
#
# Separately, GCP auto-creates `default-allow-ssh`, `default-allow-rdp`,
# `default-allow-icmp`, and `default-allow-internal` on the default VPC at
# project creation time. The first two expose ports 22/3389 to 0.0.0.0/0 and
# should be deleted manually once (see infra/terraform/README.md §7). Terraform
# can't import the auto-created rules cleanly, so we don't try — they're a
# one-time cleanup, not an ongoing concern.

resource "google_compute_firewall" "allow_ssh_from_iap" {
  name        = "allow-ssh-from-iap"
  network     = "default"
  description = "SSH to Sketch VM is reachable only through Identity-Aware Proxy."
  direction   = "INGRESS"

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  # https://cloud.google.com/iap/docs/using-tcp-forwarding#firewall — IAP's
  # forwarding range is fixed.
  source_ranges = ["35.235.240.0/20"]
  target_tags   = ["sketch"]

  depends_on = [google_project_service.enabled]
}

# ----------------------------------------------------------------------------
# IAM — deployer SA permissions on the project
# ----------------------------------------------------------------------------

# Allow the deployer to tunnel SSH through IAP.
resource "google_project_iam_member" "deployer_iap_tunneller" {
  project = var.project_id
  role    = "roles/iap.tunnelResourceAccessor"
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

# Grant OS Login + sudo on the VM so the deployer can `sudo systemctl restart sketch`.
resource "google_project_iam_member" "deployer_os_admin_login" {
  project = var.project_id
  role    = "roles/compute.osAdminLogin"
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

# `gcloud compute ssh` to an instance running under another service account
# requires actAs on that SA.
resource "google_service_account_iam_member" "deployer_acts_as_vm" {
  service_account_id = google_service_account.vm.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}

# `terraform plan` in CI needs to read every managed resource to refresh state.
# `roles/viewer` is the canonical project-wide read role; granting individual
# *.viewer roles per service would be ~8 bindings with no real safety benefit
# (none of them grant mutate perms).
resource "google_project_iam_member" "deployer_plan_viewer" {
  project = var.project_id
  role    = "roles/viewer"
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

# Required because the google provider runs with `user_project_override = true`
# (see the provider block above). With that header set, every API call from
# the deployer SA needs `serviceusage.services.use` on the billing project —
# which `roles/viewer` does not include.
resource "google_project_iam_member" "deployer_service_usage_consumer" {
  project = var.project_id
  role    = "roles/serviceusage.serviceUsageConsumer"
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

# Plan in CI runs `terraform init` against the GCS state backend and
# acquires the state lock — both are object-level operations covered by
# `storage.objectUser`.
resource "google_storage_bucket_iam_member" "deployer_tfstate_access" {
  bucket = "${var.project_id}-tfstate"
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.deployer.email}"
}

# Plan also has to refresh the IAM binding above, which calls
# `storage.buckets.getIamPolicy`. No stock GCP role grants only that
# permission — `roles/storage.admin` and `roles/storage.legacyBucketOwner`
# both include it but also grant bucket-delete and setIamPolicy, which the
# deployer SA does not need. A custom role keeps the permission set
# minimal: object IO via objectUser above, plus literally just this one
# read permission, with no ability to mutate the bucket or its IAM policy.
resource "google_project_iam_custom_role" "tfstate_iam_reader" {
  project     = var.project_id
  role_id     = "tfstateIamReader"
  title       = "Terraform tfstate Bucket IAM Reader"
  description = "Grants storage.buckets.getIamPolicy so terraform plan can refresh bucket-IAM binding state."
  permissions = ["storage.buckets.getIamPolicy"]
}

resource "google_storage_bucket_iam_member" "deployer_tfstate_iam_reader" {
  bucket = "${var.project_id}-tfstate"
  role   = google_project_iam_custom_role.tfstate_iam_reader.name
  member = "serviceAccount:${google_service_account.deployer.email}"
}

# ----------------------------------------------------------------------------
# Workload Identity Federation — GitHub Actions auth without long-lived keys
# ----------------------------------------------------------------------------

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github-pool"
  display_name              = "GitHub Actions"
  description               = "Federates GitHub Actions OIDC tokens into GCP service accounts."

  depends_on = [google_project_service.enabled]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-provider"
  display_name                       = "GitHub OIDC"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.actor"      = "assertion.actor"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }

  # Hard-pin to our repo so a stray OIDC token from any other GitHub repo
  # cannot impersonate the deployer SA.
  attribute_condition = "assertion.repository == \"${var.github_owner}/${var.github_repo}\""

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "deployer_wif" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_owner}/${var.github_repo}"
}
