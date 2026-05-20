# Deploying Sketch to a GCE e2-micro instance

Production setup walkthrough for a free-tier `e2-micro` Compute Engine VM in `us-central1` (or another Always-Free-eligible US region). The bot runs under systemd. The Discord token lives in Google Secret Manager; Google Sheets auth uses the VM's attached service account via Application Default Credentials — no JSON key on the VM disk. The only persistent state is the spreadsheet itself.

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
  --zone=us-central1-a \
  --image-family=debian-12 \
  --image-project=debian-cloud \
  --boot-disk-size=10GB \
  --boot-disk-type=pd-standard \
  --scopes=cloud-platform
```

`--scopes=cloud-platform` lets the VM's default service account call any GCP API it has IAM bindings for (Secret Manager + Sheets, in our case). For tighter scoping, create a dedicated VM service account (`--service-account=sketch-vm@<project>.iam.gserviceaccount.com`) and grant it only the specific roles below.

## 3. Grant the VM access to the Discord token secret

```bash
VM_SA=$(gcloud compute instances describe sketch --zone=us-central1-a \
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

## 5. Bootstrap the VM

SSH in:

```bash
gcloud compute ssh sketch --zone=us-central1-a
```

Then on the VM:

```bash
# System packages
sudo apt update
sudo apt install -y python3-venv git unattended-upgrades

# Dedicated user (no shell, no home in /home)
sudo useradd --system --create-home --home-dir /opt/sketch --shell /usr/sbin/nologin sketch

# Clone the repo to /opt/sketch
sudo git clone https://github.com/dillon-odonovan/sketch.git /opt/sketch
sudo chown -R sketch:sketch /opt/sketch

# Virtualenv + dependencies
sudo -u sketch python3 -m venv /opt/sketch/.venv
sudo -u sketch /opt/sketch/.venv/bin/pip install -r /opt/sketch/requirements.txt

# Make the launcher executable
sudo chmod +x /opt/sketch/deploy/launch.sh
```

## 6. Configure non-secret environment

```bash
sudo mkdir -p /etc/sketch
sudo cp /opt/sketch/deploy/env.example /etc/sketch/env
sudo $EDITOR /etc/sketch/env       # fill in DISCORD_GUILD_ID and SPREADSHEET_ID
sudo chown root:sketch /etc/sketch/env
sudo chmod 640 /etc/sketch/env
```

## 7. Install and start the systemd service

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

## 8. Enable security updates

```bash
sudo dpkg-reconfigure -plow unattended-upgrades
```

Accept the default; Debian's `unattended-upgrades` will then auto-install security patches.

## Updating the bot

```bash
gcloud compute ssh sketch --zone=us-central1-a --command="\
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
gcloud compute ssh sketch --zone=us-central1-a --command="sudo systemctl restart sketch"
```

`launch.sh` always asks for `latest`, so no path needs updating.

## Crash / restart behavior

The bot is stateless — DEX is reloaded on every startup, the spreadsheet is the source of truth, and Discord slash command registration is idempotent. So:

- If the bot process crashes, `Restart=always` brings it back in 10 seconds.
- If the VM reboots (rare maintenance, manual restart, OS update), systemd starts the bot once the network is up.
- In-flight slash commands during a restart show "interaction failed" to the user; new commands work normally as soon as the gateway reconnects (1–3 seconds after process start).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `gcloud secrets versions access` returns `PERMISSION_DENIED` | VM service account missing `secretAccessor` on `sketch-discord-token` | Re-run step 3 |
| `discord.errors.LoginFailure: Improper token has been passed` | Wrong secret content (e.g., Client Secret instead of Bot Token) | Add a new version with the real bot token |
| `403 Missing Access` on `tree.sync` | `DISCORD_GUILD_ID` points at a guild the bot isn't installed in | Fix `/etc/sketch/env`, restart |
| `403 The caller does not have permission` from Sheets API | VM's service account isn't shared on the spreadsheet | Re-run step 4 with the email from `echo "$VM_SA"` |
| `google.auth.exceptions.DefaultCredentialsError` at startup | Running locally without ADC configured | `gcloud auth application-default login`, or set `GOOGLE_APPLICATION_CREDENTIALS` to a JSON key path |
| Bot starts but never `Logged in as …` | Outbound network blocked | Check VPC firewall — only outbound to discord.com:443 and googleapis.com:443 is required |
