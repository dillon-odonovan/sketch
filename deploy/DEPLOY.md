# Deploying Sketch to a GCE e2-micro instance

Production setup walkthrough for a free-tier `e2-micro` Compute Engine VM in `us-west1` (or another Always-Free-eligible US region). The bot runs under systemd. The Discord token lives in Google Secret Manager; Google Sheets auth uses the VM's attached service account via Application Default Credentials — no JSON key on the VM disk. The only persistent state is the spreadsheet itself.

## Prerequisites

- A GCP project with billing attached (Always Free is "free up to the quota," but still requires a billing account).
- The **Google Sheets API** and **Secret Manager API** enabled in that project (`gcloud services enable sheets.googleapis.com secretmanager.googleapis.com`).
- A target Google Sheet that the bot will write to (use a test copy during initial setup). We'll share it with the VM's service account in step 4.
- A Discord bot token (Developer Portal → your app → Bot → Reset Token).
- A Discord guild ID for the dev or production server.

## 1. Store the Discord token

Only one true secret to manage. Google Sheets auth is handled via the VM's service account in step 4.

```bash
# From your laptop, with gcloud authed to the right project
gcloud secrets create sketch-discord-token --replication-policy=automatic
echo -n 'YOUR_BOT_TOKEN_HERE' | gcloud secrets versions add sketch-discord-token --data-file=-
```

To rotate later: `gcloud secrets versions add sketch-discord-token --data-file=-` adds a new version. Disable the old one to stay within the 6-active-version free tier:

```bash
gcloud secrets versions disable <OLD_VERSION_NUMBER> --secret=sketch-discord-token
```

## 2. Create the VM

```bash
gcloud compute instances create sketch \
  --machine-type=e2-micro \
  --zone=us-west1-a \
  --image-family=debian-12 \
  --image-project=debian-cloud \
  --boot-disk-size=10GB \
  --boot-disk-type=pd-standard \
  --scopes=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/spreadsheets
```

**Why both scopes**: `cloud-platform` covers all _Google Cloud Platform_ APIs (Compute, Storage, Secret Manager, BigQuery, etc.), and it's the recommended default for GCP workloads — actual permission narrowing happens via IAM bindings (next steps), not via narrower scopes. **However**, the Sheets API is a _Google Workspace_ API, not a Cloud Platform API, and it does not honor `cloud-platform` as a scope. Workspace APIs require their own scopes (`spreadsheets`, `drive`, etc.), so the Sheets-specific scope has to be listed alongside cloud-platform. Without it, the bot will fail with `ACCESS_TOKEN_SCOPE_INSUFFICIENT` on its first DEX read.

For per-app isolation, create a dedicated service account and pass it via `--service-account=sketch-vm@<project>.iam.gserviceaccount.com` — but **always keep both scopes alongside it**. Using `--service-account` without `--scopes` falls back to a restricted default scope set that excludes both Secret Manager and Sheets. The actual permission narrowing happens in steps 3 and 4 (granting only `secretAccessor` on the one secret, and Editor on the one sheet).

If you've already created the VM without the right scopes, recover with:

```bash
gcloud compute instances stop sketch --zone=us-west1-a
gcloud compute instances set-service-account sketch \
  --zone=us-west1-a \
  --service-account=<SA_EMAIL> \
  --scopes=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/spreadsheets
gcloud compute instances start sketch --zone=us-west1-a
```

## 3. Grant the VM access to the Discord token secret

```bash
VM_SA=$(gcloud compute instances describe sketch --zone=us-west1-a \
        --format='value(serviceAccounts[0].email)')

gcloud secrets add-iam-policy-binding sketch-discord-token \
  --member="serviceAccount:$VM_SA" \
  --role="roles/secretmanager.secretAccessor"
```

## 4. Share the spreadsheet with the VM's service account

This is the equivalent of attaching an IAM role to an EC2 instance — the bot's Sheets access flows from the VM's identity rather than from a downloaded key.

```bash
echo "$VM_SA"   # copy this email address
```

Open the target spreadsheet in Google Sheets → **Share** → paste the service account email → choose **Editor** → uncheck "Notify people" → **Share**.

If you have other shared sheets or DEX references the bot needs to read, share each of them with this same service account.

## 5. Lock down inbound network access

Default GCE firewall rules expose SSH (and RDP) to the public internet. Replace public-internet SSH with [Identity-Aware Proxy](https://cloud.google.com/iap/docs/using-tcp-forwarding) tunneling so port 22 is only reachable from Google's authenticated proxy range, then remove the public rules.

```bash
PROJECT=$(gcloud config get-value project)
USER_EMAIL=$(gcloud config get-value account)

# Enable the IAP API
gcloud services enable iap.googleapis.com

# Grant your user the IAP tunnel role on the project
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="user:$USER_EMAIL" \
  --role="roles/iap.tunnelResourceAccessor"

# Allow SSH only from Google's IAP forwarding range
gcloud compute firewall-rules create allow-ssh-from-iap \
  --direction=INGRESS \
  --action=ALLOW \
  --rules=tcp:22 \
  --source-ranges=35.235.240.0/20

# Remove the public-internet SSH and RDP rules
gcloud compute firewall-rules delete default-allow-ssh --quiet
gcloud compute firewall-rules delete default-allow-rdp --quiet
```

After this, all SSH access must go through IAP. `gcloud` doesn't expose a property to make `--tunnel-through-iap` the default, so the flag has to be passed on every `gcloud compute ssh` invocation. To save typing, add a shell alias to your `~/.zshrc` or `~/.bashrc`:

```bash
alias gcssh='gcloud compute ssh --tunnel-through-iap'
```

Then `gcssh sketch --zone=us-west1-a` works as a drop-in replacement. Direct `ssh user@<public-ip>` connections are dropped at the firewall — which is the goal.

Verify the inbound surface is what you expect:

```bash
gcloud compute firewall-rules list --filter="direction:INGRESS"
```

Should show `allow-ssh-from-iap` (TCP 22 from 35.235.240.0/20), `default-allow-internal` (intra-VPC), and `default-allow-icmp` (optional — delete if you don't want ping reachability). Nothing else.

## 6. Bootstrap the VM

SSH in (every `gcloud compute ssh` invocation in this doc assumes the IAP lockdown from step 5; if you skipped it, drop the `--tunnel-through-iap` flag):

```bash
gcloud compute ssh sketch --zone=us-west1-a --tunnel-through-iap
```

Then on the VM:

```bash
# System packages
sudo apt update
sudo apt install -y python3-venv git unattended-upgrades

# Dedicated system user (nologin shell; home points at /opt/sketch but
# isn't pre-created so the next step's git clone can populate the directory)
sudo useradd --system --home-dir /opt/sketch --shell /usr/sbin/nologin sketch

# Clone the repo to /opt/sketch (creates the directory)
sudo git clone https://github.com/dillon-odonovan/sketch.git /opt/sketch
sudo chown -R sketch:sketch /opt/sketch

# Virtualenv + dependencies
sudo -u sketch python3 -m venv /opt/sketch/.venv
sudo -u sketch /opt/sketch/.venv/bin/pip install -r /opt/sketch/requirements.txt

# Make the launcher executable
sudo chmod +x /opt/sketch/deploy/launch.sh
```

## 7. Configure non-secret environment

```bash
sudo mkdir -p /etc/sketch
sudo cp /opt/sketch/deploy/env.example /etc/sketch/env
sudo $EDITOR /etc/sketch/env       # fill in DISCORD_GUILD_ID and SPREADSHEET_ID
sudo chown root:sketch /etc/sketch/env
sudo chmod 640 /etc/sketch/env
```

## 8. Install and start the systemd service

```bash
sudo cp /opt/sketch/deploy/sketch.service /etc/systemd/system/sketch.service
sudo systemctl daemon-reload
sudo systemctl enable --now sketch
```

Verify:

```bash
systemctl status sketch
journalctl -u sketch -f
```

You should see `Loaded N DEX species names`, `Synced commands to guild …`, and `Logged in as Sketch`.

## 9. Enable security updates

```bash
sudo dpkg-reconfigure -plow unattended-upgrades
```

Accept the default; Debian's `unattended-upgrades` will then auto-install security patches.

## Updating the bot

```bash
gcloud compute ssh sketch --zone=us-west1-a --tunnel-through-iap --command="\
  cd /opt/sketch && \
  sudo -u sketch git pull && \
  sudo -u sketch /opt/sketch/.venv/bin/pip install -r requirements.txt && \
  sudo systemctl restart sketch"
```

Restart picks up code changes and re-reads the Discord token from Secret Manager. Google API access tokens are refreshed by the client library on the fly via the metadata server. There's no on-disk state to migrate.

## Rotating a secret

Add the new version, then disable the old:

```bash
echo -n 'NEW_TOKEN' | gcloud secrets versions add sketch-discord-token --data-file=-
gcloud secrets versions disable OLD_VERSION --secret=sketch-discord-token
gcloud compute ssh sketch --zone=us-west1-a --tunnel-through-iap --command="sudo systemctl restart sketch"
```

`launch.sh` always asks for `latest`, so no path needs updating.

## Crash / restart behavior

The bot is stateless — DEX is reloaded on every startup, the spreadsheet is the source of truth, and Discord slash command registration is idempotent. So:

- If the bot process crashes, `Restart=always` brings it back in 10 seconds.
- If the VM reboots (rare maintenance, manual restart, OS update), systemd starts the bot once the network is up.
- In-flight slash commands during a restart show "interaction failed" to the user; new commands work normally as soon as the gateway reconnects (1–3 seconds after process start).

## Troubleshooting

| Symptom                                                                                             | Likely cause                                                                                              | Fix                                                                                                               |
| --------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `gcloud secrets versions access` returns `PERMISSION_DENIED` with `ACCESS_TOKEN_SCOPE_INSUFFICIENT` | VM created without cloud-platform scope                                                                   | See the recovery snippet in step 2                                                                                |
| `gcloud secrets versions access` returns `PERMISSION_DENIED` (no scope error)                       | VM service account missing `secretAccessor` on `sketch-discord-token`                                     | Re-run step 3                                                                                                     |
| Sheets API returns `ACCESS_TOKEN_SCOPE_INSUFFICIENT` despite cloud-platform scope being set         | Workspace APIs (Sheets/Drive/Docs) don't accept `cloud-platform`; they need their own scope               | Use the recovery snippet in step 2 to add `https://www.googleapis.com/auth/spreadsheets` alongside cloud-platform |
| `discord.errors.LoginFailure: Improper token has been passed`                                       | Wrong secret content (e.g., Client Secret instead of Bot Token)                                           | Add a new version with the real bot token                                                                         |
| `403 Missing Access` on `tree.sync`                                                                 | `DISCORD_GUILD_ID` points at a guild the bot isn't installed in                                           | Fix `/etc/sketch/env`, restart                                                                                    |
| `403 The caller does not have permission` from Sheets API                                           | VM's service account isn't shared on the spreadsheet                                                      | Re-run step 4 with the email from `echo "$VM_SA"`                                                                 |
| `google.auth.exceptions.DefaultCredentialsError` at startup                                         | Running locally without ADC configured                                                                    | `gcloud auth application-default login`, or set `GOOGLE_APPLICATION_CREDENTIALS` to a JSON key path               |
| Bot starts but never `Logged in as …`                                                               | Outbound network blocked                                                                                  | Check VPC firewall — only outbound to discord.com:443 and googleapis.com:443 is required                          |
| `gcloud compute ssh` times out after firewall lockdown                                              | Missing `--tunnel-through-iap` flag, IAP API disabled, or user missing `roles/iap.tunnelResourceAccessor` | Add the flag; otherwise re-run step 5                                                                             |
