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
2. Create a **service account**, download its JSON key.
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
- `DISCORD_GUILD_ID` — for instant slash-command sync in your dev server. Omit for global registration (~1 hour propagation).
- `GOOGLE_CREDENTIALS_JSON` — the entire service account JSON on a single line.
- `SPREADSHEET_ID` — the ID of the test sheet.

### 5. Run

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

`.env` and any `*.json` credentials are gitignored. The service-account JSON is
read from the `GOOGLE_CREDENTIALS_JSON` env var (the JSON content itself, not a
file path), so nothing sensitive ever lives on disk inside the repo.
