#!/usr/bin/env bash
# Launcher invoked by systemd. Fetches the Discord bot token from Google
# Secret Manager and exports it before exec'ing the bot. Google API auth is
# handled by Application Default Credentials at the bot's use-time via the
# VM's attached service account — no JSON key fetch needed here.
#
# Prerequisites on the VM:
#  - gcloud CLI installed (default on GCE Debian/Ubuntu images)
#  - The VM's attached service account has:
#      * roles/secretmanager.secretAccessor on $SKETCH_SECRET_DISCORD_TOKEN
#      * Editor access on the target Google Sheet
#  - Non-secret env vars (DISCORD_GUILD_ID, SPREADSHEET_ID) come from
#    /etc/sketch/env via systemd's EnvironmentFile= directive
set -euo pipefail

# Name of the Discord token secret in Secret Manager. Override via
# /etc/sketch/env if you named it differently.
: "${SKETCH_SECRET_DISCORD_TOKEN:=sketch-discord-token}"

# `gcloud secrets versions access latest` returns the raw secret payload on
# stdout. We capture it via $() and export it.
DISCORD_TOKEN="$(gcloud secrets versions access latest --secret="$SKETCH_SECRET_DISCORD_TOKEN")"
export DISCORD_TOKEN

# Hand off to the bot. exec replaces this shell so systemd's process tree
# tracks the Python interpreter directly (clean shutdowns, accurate PID).
exec /opt/sketch/.venv/bin/python /opt/sketch/bot.py
