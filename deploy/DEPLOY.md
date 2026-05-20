# Deploying Sketch to a GCE e2-micro instance

Production setup walkthrough for a free-tier `e2-micro` Compute Engine VM in `us-central1` (or another Always-Free-eligible US region). The bot runs under systemd, secrets come from Google Secret Manager, and the only persistent state is the spreadsheet itself.

## Prerequisites

- A GCP project with billing attached (Always Free is "free up to the quota," but still requires a billing account).
- The **Google Sheets API** and **Secret Manager API** enabled in that project (`gcloud services enable sheets.googleapis.com secretmanager.googleapis.com`).
- A **service account** with Editor access on the target spreadsheet — the same one the bot uses locally. Download its JSON key once; it goes into Secret Manager, not onto the VM disk.
- A Discord bot token (Developer Portal → your app → Bot → Reset Token).
- A Discord guild ID for the dev or production server.

## 1. Store the secrets

Two secrets only — everything else is non-sensitive config.

```bash
# From your laptop, with gcloud authed to the right project
gcloud secrets create sketch-discord-token --replication-policy=automatic
echo -n 'YOUR_BOT_TOKEN_HERE' | gcloud secrets versions add sketch-discord-token --data-file=-

gcloud secrets create sketch-google-credentials-json --replication-policy=automatic
gcloud secrets versions add sketch-google-credentials-json --data-file=/path/to/service-account.json
```

After these succeed, delete the local service-account JSON. Secret Manager is now the only copy.

To rotate later: `gcloud secrets versions add <secret> --data-file=-` adds a new version. Disable the old one to stay within the 6-active-version free tier:

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

`--scopes=cloud-platform` lets the VM's default service account use Secret Manager (along with any other GCP API). For tighter scoping you can create a dedicated VM service account and grant only `roles/secretmanager.secretAccessor` on the two secrets.

## 3. Grant the VM access to the secrets

```bash
VM_SA=$(gcloud compute instances describe sketch --zone=us-central1-a \
        --format='value(serviceAccounts[0].email)')

for SECRET in sketch-discord-token sketch-google-credentials-json; do
  gcloud secrets add-iam-policy-binding "$SECRET" \
    --member="serviceAccount:$VM_SA" \
    --role="roles/secretmanager.secretAccessor"
done
```

## 4. Bootstrap the VM

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

## 5. Configure non-secret environment

```bash
sudo mkdir -p /etc/sketch
sudo cp /opt/sketch/deploy/env.example /etc/sketch/env
sudo $EDITOR /etc/sketch/env       # fill in DISCORD_GUILD_ID and SPREADSHEET_ID
sudo chown root:sketch /etc/sketch/env
sudo chmod 640 /etc/sketch/env
```

## 6. Install and start the systemd service

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

## 7. Enable security updates

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

Restart picks up code changes and re-reads secrets from Secret Manager. There's no on-disk state to migrate.

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
| `gcloud secrets versions access` returns `PERMISSION_DENIED` | VM service account missing `secretAccessor` | Re-run step 3 |
| `discord.errors.LoginFailure: Improper token has been passed` | Wrong secret content (e.g., Client Secret instead of Bot Token) | Add a new version with the real bot token |
| `403 Missing Access` on `tree.sync` | `DISCORD_GUILD_ID` points at a guild the bot isn't installed in | Fix `/etc/sketch/env`, restart |
| Bot starts but never `Logged in as …` | Outbound network blocked | Check VPC firewall — only outbound to discord.com:443 and googleapis.com:443 is required |
