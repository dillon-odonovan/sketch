#!/usr/bin/env bash
# Launcher invoked by systemd. Fetches the two real secrets from Google Secret
# Manager and exports them as environment variables, then execs the bot. Keeps
# the bot code portable: locally you populate `.env`; in production systemd +
# this script + Secret Manager provide the same variable names.
#
# Prerequisites on the VM:
#  - gcloud CLI installed (default on GCE Debian/Ubuntu images)
#  - The VM's attached service account has roles/secretmanager.secretAccessor
#    on the secrets below
#  - Non-secret env vars (DISCORD_GUILD_ID, SPREADSHEET_ID) come from
#    /etc/sketch/env via systemd's EnvironmentFile= directive
set -euo pipefail

# Names of the secrets in Secret Manager. Override via /etc/sketch/env if you
# named them differently (e.g., per-environment suffixes).
: "${SKETCH_SECRET_DISCORD_TOKEN:=sketch-discord-token}"
: "${SKETCH_SECRET_GOOGLE_CREDS:=sketch-google-credentials-json}"

fetch() {
  # `gcloud secrets versions access latest` returns the raw secret payload on
  # stdout. We capture it via $() and let the caller export it.
  gcloud secrets versions access latest --secret="$1"
}

DISCORD_TOKEN="$(fetch "$SKETCH_SECRET_DISCORD_TOKEN")"
GOOGLE_CREDENTIALS_JSON="$(fetch "$SKETCH_SECRET_GOOGLE_CREDS")"
export DISCORD_TOKEN GOOGLE_CREDENTIALS_JSON

# Hand off to the bot. exec replaces this shell so systemd's process tree
# tracks the Python interpreter directly (clean shutdowns, accurate PID).
exec /opt/sketch/.venv/bin/python /opt/sketch/bot.py
