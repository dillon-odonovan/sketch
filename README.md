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
- `DEV_GUILD_ID` — for instant slash-command sync in your dev server. Omit for global registration (~1 hour propagation). Note: this controls *where commands are registered for fast iteration*, not which guilds the bot serves — see `GUILD_CONFIG_JSON`.
- `GUILD_CONFIG_JSON` — JSON map of `guild_id → {spreadsheet_id}`. The bot refuses commands from any guild not listed here. Each spreadsheet must be shared with the bot's service account as Editor. Example:

  ```bash
  GUILD_CONFIG_JSON='{"123456789012345678": {"spreadsheet_id": "1AbCd-Your_Sheet_Id"}, "987654321098765432": {"spreadsheet_id": "1XyZw-Another_Sheet"}}'
  ```

  In shells that strip JSON escapes, single-quote the whole value as shown. Spreadsheet IDs are restricted to `[A-Za-z0-9_-]`. The bot serves multiple guilds from one process — install the same bot in both a personal debug server and a shared production server, mapped to different sheets, and one container handles both.

Set up Google auth via **Application Default Credentials**. Pick one:

- **Recommended**: `gcloud auth application-default login` and accept impersonation of the service account from step 2 if prompted. The Google client library finds these credentials automatically; no env var needed.
- **Alternative**: download the service-account JSON key from step 2 to a local path *outside the repo*, then set `GOOGLE_APPLICATION_CREDENTIALS=/absolute/path/to/sa-key.json` in your `.env`. ADC reads it from there.

### 4. Run

```bash
python bot.py
```

Look for `Logged in as <bot>` and `Synced commands to guild <id>`. Slash commands should appear in your Discord server immediately.

## Adding a new format / sheet tab

Edit `FORMAT_SHEETS` in [`config.py`](config.py):

```python
FORMAT_SHEETS = {
    "Reg M-A": "TeamBank Parser V1",
    "Reg M-B": "TeamBank Parser V2",
}
```

Restart the bot — the new format appears as a dropdown option on both commands.

## Security

`.env` and any `*.json` credentials are gitignored. Google auth uses
Application Default Credentials, so in production no JSON key lives on the
VM disk — the bot picks up the attached service account via GCE's metadata
server. The Discord bot token is the only true bearer secret; in the deploy
scaffold it lives in Google Secret Manager and is materialized as an env var
at process start.
