# Sketch

A Discord bot that lets a server's members add Pokepaste teams to a shared Google Sheet and search the catalog by format + Pokémon.

Named after [Sketch](<https://bulbapedia.bulbagarden.net/wiki/Sketch_(move)>), Smeargle's signature move — which permanently records a move into its moveset, the way this bot permanently records a team into the bank.

The sheet's AppsScript handles all Pokepaste parsing; the bot just writes URLs and reads the spilled species.

## Commands

- `/add-team url:<paste> description:<text> [format] [replica] [paste_type]` — add a paste to the sheet.
- `/search-teams mon1:<name> [mon2:…] … [mon6:…] [format]` — find teams containing all listed Pokémon.
- `/help` — usage summary.

Pokémon names support **prefix-group matching** against the DEX sheet:
`Charizard` matches base + both megas; `Charizard-Mega-Y` matches only Mega-Y.
Typos return a "did you mean" suggestion.

### Admin commands (Manage Server)

Server admins can self-configure the bot without any operator intervention. All four are gated with Discord's **Manage Server** permission and are hidden in DMs.

- `/register-sheet spreadsheet_id:<id>` — register or replace the Google Sheet this server writes to. Required after the first install. The bot probes the sheet for access *before* persisting, so a typo or missing share doesn't break the guild.
- `/set-broadcast-channel channel:<#channel>` — announce every successful `/add-team` in `<#channel>`. Requires a registered sheet.
- `/clear-broadcast-channel` — stop broadcasting `/add-team` announcements.
- `/show-config` — display the server's current spreadsheet + broadcast channel.

## Setup

### 1. Discord application

1. Create an app at <https://discord.com/developers/applications>.
2. Add a **Bot** user and copy the token.
3. Under **OAuth2 → URL Generator**, select scopes `bot` + `applications.commands`, generate the invite URL, and invite the bot to your server.
4. Grab your server's guild ID (Discord → User Settings → Advanced → enable Developer Mode → right-click server → Copy ID).

### 2. Google service account

1. In Google Cloud Console, create (or use) a project and enable the **Google Sheets API**.
2. Create a **service account** (no need to download a JSON key for normal local dev — see step 3 for auth).
3. Open the target Sheet (use a **test copy** during development) and share it with the service account's email (`...@...iam.gserviceaccount.com`) as **Editor**.

### 3. Local environment

```bash
cd sketch
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`:

- `DISCORD_TOKEN` — from step 1.
- `DEV_GUILD_ID` — for instant slash-command sync in your dev server. Omit for global registration (~1 hour propagation). Note: this controls *where commands are registered for fast iteration*, not which guilds the bot serves — see "Per-guild configuration" below.
- `GCP_PROJECT` — your Google Cloud project ID. The bot reads guild config and the Discord token from this project. Locally, ADC's quota project is used as the fallback if you don't set this.

#### Per-guild configuration (Firestore)

Per-guild routing (`spreadsheet_id`, optional `broadcast_channel_id`) lives in a Firestore collection named `guild_configs`, one document per guild (doc ID = guild ID). The bot refuses commands from any guild that doesn't have a doc, and each listed spreadsheet must be shared with the bot's service account as Editor.

The **canonical path** to configure a guild is the admin slash commands above (`/register-sheet`, `/set-broadcast-channel`, `/clear-broadcast-channel`) — they write through to Firestore *and* update the bot's in-memory cache in the same call, so changes take effect immediately without a restart.

The seed script (`bin/seed_guilds.py`) is kept as an operator backstop for the rare cases the slash commands can't cover — for example, bootstrapping a new install before anyone has Manage Server access, or recovering a guild whose admins lost the permission:

```bash
python bin/seed_guilds.py 123456789012345678 \
  --spreadsheet-id 1AbCd-Your_Sheet_Id \
  --broadcast-channel-id 112233445566778899
```

The bot serves multiple guilds from one process — install the same bot in a personal debug server and a shared production server, mapped to different sheets, and one container handles both. Spreadsheet IDs are restricted to `[A-Za-z0-9_-]`; the same validation runs in both write paths.

`broadcast_channel_id` is **optional**. When set, every successful `/add-team` posts a public embed to that channel announcing the new team (regardless of which channel the command was invoked from). The bot needs **Send Messages** and **Embed Links** in the broadcast channel; if it doesn't, the write still succeeds and a warning is logged.

Seed-script writes still require a restart for the bot to pick them up (the in-memory cache is loaded once at startup); slash-command writes do not.

Set up Google auth via **Application Default Credentials**. Pick one:

- **Recommended**: `gcloud auth application-default login` and accept impersonation of the service account from step 2 if prompted. The Google client library finds these credentials automatically; no env var needed.
- **Alternative**: download the service-account JSON key from step 2 to a local path *outside the repo*, then set `GOOGLE_APPLICATION_CREDENTIALS=/absolute/path/to/sa-key.json` in your `.env`. ADC reads it from there.

### 4. Run

```bash
python -m sketch
```

Look for `Logged in as <bot>` and `Synced commands to guild <id>`. Slash commands should appear in your Discord server immediately.

### 5. (Optional) Install pre-commit hooks

```bash
pip install -r requirements-dev.txt
pre-commit install
```

`ruff` lints and formats on commit; the same hooks run in CI as a backstop.

## Running tests

```bash
pip install -r requirements-dev.txt
pytest
```

## Deploying

The bot ships as a container running under systemd on a GCE `e2-micro`. Infrastructure is fully described in Terraform; deploys are driven by GitHub Actions.

- **First-time setup**: see [infra/terraform/README.md](infra/terraform/README.md) — a `terraform apply` provisions the VM, secret, Firestore database, Artifact Registry, and Workload Identity Federation.
- **Updating the bot**: merge to `main`. The `Deploy` workflow builds the image, pushes to Artifact Registry, and restarts the VM service via IAP SSH.
- **Adding or updating a guild**: have a server admin run `/register-sheet`, `/set-broadcast-channel`, etc. — no operator intervention or restart needed. The `bin/seed_guilds.py` script is kept around for backstop cases (see "Per-guild configuration" above); it writes to the same collection but requires a `sudo systemctl restart sketch` to take effect.
- **Rolling back**: trigger the `Deploy` workflow via *Run workflow* in the Actions tab and provide a previous `:sha-<sha>` tag as `image_tag`.

## Adding a new format / sheet tab

Edit `FORMAT_SHEETS` in [`sketch/config.py`](sketch/config.py):

```python
FORMAT_SHEETS = {
    "Reg M-A": "Regulation M-A",
    "Reg F":   "Regulation F",
}
```

The key is the user-facing dropdown label in Discord; the value must match the tab name in the spreadsheet exactly. Restart the bot — the new format appears as a dropdown option on both commands.

## Security

`.env` and any `*.json` credentials are gitignored. Google auth uses
Application Default Credentials, so in production no JSON key lives on the
VM disk — the bot picks up the attached service account via GCE's metadata
server. The Discord bot token is the only true bearer secret; it lives in
Google Secret Manager and is fetched by `sketch/__main__.py` at container start.
GitHub Actions authenticates to GCP via Workload Identity Federation — no
long-lived JSON keys exist anywhere.
