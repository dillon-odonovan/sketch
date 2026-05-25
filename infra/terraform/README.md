# Sketch — Infrastructure (Terraform)

All GCP resources for Sketch live here. A single `terraform apply` provisions the GCE VM, the runtime and deployer service accounts, the Discord-token and Anthropic-API-key secret slots, the Firestore database that holds per-guild config, the Artifact Registry repository with its cleanup policy, the IAP-only SSH firewall rule, and the Workload Identity Federation pool that GitHub Actions uses to authenticate.

State is stored in a GCS bucket in the same project — there is no third-party SaaS dependency.

## Prerequisites

- GCP project with billing attached.
- Terraform `>= 1.6` and `gcloud` installed locally.
- You are logged in to `gcloud` against the target project: `gcloud auth login && gcloud config set project <PROJECT>`.
- Application-Default Credentials set up for Terraform: `gcloud auth application-default login`.
- A target Google Sheet that the bot will write to.
- A Discord bot token (Developer Portal → your app → Bot → Reset Token).
- An Anthropic API key for the `/add-team` OCR pipeline (Console → API Keys).

## Bootstrap (one-time per project)

### 1. Create the Terraform state bucket

The state bucket cannot itself be Terraform-managed (chicken-and-egg).

```bash
PROJECT=$(gcloud config get-value project)
gcloud storage buckets create gs://$PROJECT-tfstate \
  --location=us-west1 \
  --uniform-bucket-level-access \
  --soft-delete-duration=7d
gcloud storage buckets update gs://$PROJECT-tfstate --versioning
```

Versioning + soft-delete give you state-file rollback if a destructive apply ever lands by mistake.

### 2. Enable the bootstrap APIs

Two APIs cannot be enabled by Terraform itself: `cloudresourcemanager.googleapis.com` (Terraform's own `google_project_service` resource depends on it) and `iam.googleapis.com` (required to read/manage service accounts during state refresh). Enable both manually, once, before the first `terraform apply`:

```bash
PROJECT=$(gcloud config get-value project)
gcloud services enable cloudresourcemanager.googleapis.com --project="$PROJECT"
gcloud services enable iam.googleapis.com --project="$PROJECT"
```

Both APIs are free. From this point on Terraform manages them via `required_apis` in `main.tf` — re-enabling them in code protects against an accidental disable, but the very first enablement has to happen out-of-band because of the chicken-and-egg.

You may not have hit this if you previously created any service accounts via the gcloud CLI or the Cloud Console (those flows auto-enable both APIs as a side effect). It only surfaces on a truly fresh project, or when Terraform routes calls through a CI service account that has never used these APIs before.

### 3. Configure variables

```bash
cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars   # fill in project_id, github_owner, optional overrides
```

Per-guild routing (spreadsheet IDs, broadcast channel IDs) is not a Terraform variable — it lives in Firestore. You'll seed it in step 6 below, after `terraform apply` creates the database.

### 4. Initialise and apply

```bash
PROJECT=$(gcloud config get-value project)
terraform init -backend-config="bucket=$PROJECT-tfstate"
terraform apply
```

On the first apply you may need to re-run if API enablement is slow to propagate (Google occasionally returns `accessNotConfigured` briefly even after `google_project_service` succeeds).

### 5. Drop the secrets into Secret Manager

The secret slots are Terraform-managed; their **values** are supplied out-of-band so neither ever enters Terraform state. The container will crash-loop until both versions exist.

```bash
echo -n 'YOUR_BOT_TOKEN'      | gcloud secrets versions add sketch-discord-token     --data-file=-
echo -n 'YOUR_ANTHROPIC_KEY'  | gcloud secrets versions add sketch-anthropic-api-key --data-file=-
```

### 6. Seed Firestore with the first guild(s)

Terraform creates the empty `(default)` Firestore database; guild documents are written by `bin/seed_guilds.py`. Run it once per guild the bot should serve. The script uses ADC, so it works under `gcloud auth application-default login`.

```bash
python ../../bin/seed_guilds.py 123456789012345678 \
  --spreadsheet-id 1AbCd-Your_Sheet_Id \
  --broadcast-channel-id 112233445566778899
```

Omit `--broadcast-channel-id` for guilds that shouldn't broadcast new teams. To remove an existing broadcast channel later, pass `--clear-broadcast`. The script overwrites the named fields and leaves anything else on the doc untouched (`set(..., merge=True)`).

### 7. Share each spreadsheet with the VM's service account

```bash
terraform output vm_service_account_email
```

Copy that email, open every Sheet listed in your seeded guild config → **Share** → paste → **Editor** → uncheck "Notify people" → **Share**.

### 8. Configure GitHub repository variables

In the repo's Settings → Secrets and variables → Actions → **Variables**:

| Name                  | Value                                            | Used by    |
| --------------------- | ------------------------------------------------ | ---------- |
| `GCP_PROJECT_ID`      | your project ID                                  | deploy, plan |
| `GCP_REGION`          | `us-west1` (or whatever you set)                 | deploy     |
| `GCP_ZONE`            | Same value as `zone` in your tfvars, e.g. `us-west1-b`. Must match where the VM actually lives — the deploy workflow SSHes into it. | deploy |
| `GCP_WIF_PROVIDER`    | `terraform output workload_identity_provider`    | deploy, plan |
| `GCP_DEPLOYER_SA`     | `terraform output deployer_service_account_email`| deploy, plan |
| `GCP_ARTIFACT_REPO`   | `terraform output artifact_registry_url`         | deploy     |
| `TF_DEV_GUILD_ID`     | Same value as `dev_guild_id` in your tfvars (often empty). | plan |

None of these are secrets — they're all public identifiers. `TF_DEV_GUILD_ID` only exists because `terraform.tfvars` is gitignored and the plan workflow needs the same variable values that your local `apply` uses. Per-guild config is no longer a Terraform variable — it lives in Firestore (see step 6).

### 9. Remove GCP's auto-created public SSH/RDP rules (one-time)

Default GCE firewall rules expose port 22 to the public internet. Terraform doesn't import them cleanly, so delete them by hand:

```bash
gcloud compute firewall-rules delete default-allow-ssh default-allow-rdp --quiet
```

After this, SSH only flows through IAP (via `gcloud compute ssh --tunnel-through-iap`).

### 10. Push to `main` to deploy the first image

Once GitHub Actions has its WIF credentials, `git push origin main` triggers the deploy workflow, which builds the image, pushes it, and restarts the VM service. The first push will succeed even though no image exists yet — the VM's systemd unit has `ExecStartPre=-docker pull` (the `-` makes pull non-fatal), so it will fail-fast on first boot until the deploy workflow has pushed an image. Run the deploy workflow once and the VM picks up the image on next restart.

## Updating the bot

Just merge to `main`. The deploy workflow:

1. Builds the image, tags it `:current` and `:sha-<sha>`, pushes both.
2. SSHes via IAP to the VM and runs `sudo systemctl restart sketch`.
3. The unit's `ExecStartPre=docker pull` picks up the new `:current`.
4. Smoke-checks the service status.

## Adding or updating a guild

Guild routing lives in Firestore, not Terraform. Run the seed script under ADC:

```bash
python bin/seed_guilds.py <guild_id> --spreadsheet-id <id> [--broadcast-channel-id <id>]
```

Then restart the bot so it re-reads the collection:

```bash
gcloud compute ssh sketch --tunnel-through-iap --zone=us-west1-a \
  --command="sudo systemctl restart sketch"
```

The in-memory cache is populated once at boot, so new/edited guild docs need a restart to be picked up. (When `/register-guild` ships it will write through both Firestore and the cache, so server owners won't need a restart.) Don't forget to share each spreadsheet with `terraform output vm_service_account_email` as Editor — the Sheets API won't fail until first write, but it *will* fail.

## Updating infrastructure

Infra changes (anything under `infra/terraform/**`) go through a PR:

1. Open a PR. The **Terraform Plan** workflow runs `fmt`, `validate`, and `plan`, then posts the plan as a PR comment.
2. Review the plan — particularly any `# … will be destroyed` lines.
3. Merge.
4. Pull `main` locally and run `terraform apply`. There is no auto-apply: a bad merge could otherwise destroy the VM, and the blast radius of CD'ing apply isn't worth the small ergonomic win at one-VM scale.

## Rolling back

In GitHub Actions, run the **deploy** workflow via `workflow_dispatch` with the `image_tag` input set to a previous tag (e.g. `sha-abc1234`). The workflow re-tags that SHA as `:current` and restarts the service.

You can also roll back via Terraform itself:

```bash
terraform apply -var="image_tag=sha-abc1234"
gcloud compute ssh sketch --tunnel-through-iap --zone=us-west1-a --command="sudo systemctl restart sketch"
```

## Operational runbook

### IAP SSH alias (recommended)

`gcloud` doesn't have a config setting to make `--tunnel-through-iap` the default. Add an alias to your shell:

```bash
alias gcssh='gcloud compute ssh --tunnel-through-iap'
```

Then `gcssh sketch --zone=us-west1-a` works as a drop-in for the public-internet version.

### Rotating a secret (Discord token or Anthropic API key)

```bash
# Pick the secret name: sketch-discord-token  OR  sketch-anthropic-api-key
SECRET=sketch-discord-token
echo -n 'NEW_VALUE' | gcloud secrets versions add "$SECRET" --data-file=-
gcloud secrets versions disable OLD_VERSION --secret="$SECRET"
gcloud compute ssh sketch --tunnel-through-iap --zone=us-west1-a \
  --command="sudo systemctl restart sketch"
```

`sketch/__main__.py` always asks Secret Manager for `latest`, so no path needs updating.

### Inspecting the running container

```bash
gcloud compute ssh sketch --tunnel-through-iap --zone=us-west1-a
# Then on the VM:
sudo systemctl status sketch
sudo journalctl -u sketch -f
sudo docker logs sketch
sudo docker images
```

### Forcing a re-pull without changing the tag

```bash
gcloud compute ssh sketch --tunnel-through-iap --zone=us-west1-a \
  --command="sudo systemctl restart sketch"
```

`ExecStartPre=docker pull` runs every restart.

## Crash / restart behavior

- **Container crash**: `Restart=always`, `RestartSec=10` — back up in 10 seconds.
- **VM reboot** (kernel updates via `unattended-upgrades`, manual reset): systemd starts the unit after `network-online.target` and `docker.service`. The `ExecStartPre` pull is best-effort; if Artifact Registry is briefly unreachable, the cached image still runs.
- **Bot is stateless**. The spreadsheet is the source of truth. DEX is reloaded at every startup; slash command registration is idempotent.

## Troubleshooting

| Symptom                                                                           | Likely cause                                                                                | Fix                                                                                                              |
| --------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `terraform apply` fails with `accessNotConfigured`                                | API enablement hasn't propagated yet                                                        | Wait ~30 seconds and re-run                                                                                      |
| `docker pull` fails with `denied: Unauthenticated`                                | VM SA missing `artifactregistry.reader` on the repo                                         | Check `google_artifact_registry_repository_iam_member.vm_reader` exists; re-apply                                |
| Bot fails at startup with `PERMISSION_DENIED` on `accessSecretVersion`            | VM SA missing `secretmanager.secretAccessor` on the secret                                  | Check `google_secret_manager_secret_iam_member.vm_token_access` and `…vm_anthropic_api_key_access` exist; re-apply |
| Bot fails at startup with `FailedPrecondition: secret has no versions`            | Created the secret slot via `terraform apply` but never added a version                     | Run the `gcloud secrets versions add` commands from §5 for whichever secret is missing                           |
| Sheets API returns `ACCESS_TOKEN_SCOPE_INSUFFICIENT`                              | VM created without the `spreadsheets` scope alongside `cloud-platform`                      | The Terraform sets both; if the VM pre-dates Terraform, recreate it (or `set-service-account` with both scopes)  |
| `discord.errors.LoginFailure: Improper token has been passed`                     | Wrong secret content (e.g., Client Secret instead of Bot Token)                             | Add a new version of `sketch-discord-token` with the real bot token; restart                                     |
| `403 Missing Access` on `tree.sync`                                               | `dev_guild_id` points at a guild the bot isn't installed in                                 | Fix `terraform.tfvars`, re-apply, reboot the VM                                                                  |
| `403 The caller does not have permission` from Sheets API                         | VM's service account isn't shared on the spreadsheet                                        | Re-run step 7 with the email from `terraform output vm_service_account_email`                                    |
| `gcloud compute ssh` times out after firewall lockdown                            | Missing `--tunnel-through-iap`, IAP API disabled, or user missing `iap.tunnelResourceAccessor`| Add the flag; run the project IAM binding from §8 / make sure your user has `roles/iap.tunnelResourceAccessor`   |
| GitHub Actions auth fails: `Unable to acquire impersonation credentials`          | WIF principal binding mismatched (wrong repo or branch) or `iam.workloadIdentityUser` not granted | Verify `attribute_condition` in `main.tf` matches the repo; confirm the `deployer_wif` binding exists            |

## Why these choices

- **GCS state backend, not HCP Terraform**: keeps the deployment surface entirely inside the GCP IAM boundary; no external account to manage; well under the 5 GB GCS free tier.
- **VM e2-micro, not Cloud Run**: stays in the Always-Free tier; the bot is a long-running gateway connection and benefits from a pinned host.
- **Single VM, single container under systemd**: the user is the supervisor we trust. Docker is the packaging format we want for reproducibility. systemd unifies the two.
- **Workload Identity Federation, not service-account JSON keys**: GitHub Actions auth without long-lived keys means no rotation chore and a smaller blast radius.
- **Cleanup policy on Artifact Registry**: keeps storage under the 0.5 GB free tier indefinitely (each image is ~80 MB compressed and we cap at ~6 images = ~480 MB).
