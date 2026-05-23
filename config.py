import os

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _optional(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


DISCORD_TOKEN = _required("DISCORD_TOKEN")

# Dev-only slash-command sync target. When set, slash commands are registered
# against just this guild for instant (~5s) iteration during development. When
# unset, commands sync globally (Discord propagates to every guild the bot is
# in over ~1h — the production posture for multi-guild). This is independent
# of the Firestore-backed guild config, which controls *which guilds the bot
# serves at runtime*.
DEV_GUILD_ID = _optional("DEV_GUILD_ID")

# Google auth is handled at use-time via Application Default Credentials
# (see sheets_client.py and bot.py). No env var is needed here. The Firestore
# client picks up the project from ADC (`gcloud auth application-default
# login` locally; metadata server on GCE).

FORMAT_SHEETS: dict[str, str] = {
    "Reg M-A": "TeamBank Parser V1",
}

DEX_SHEET_NAME = "DEX"
DEX_NAME_RANGE = f"{DEX_SHEET_NAME}!B3:B"

# Row 1 is the header; row 2 is a formatting spacer; data begins at row 3.
FIRST_DATA_ROW = 3
LAST_TEMPLATE_COLUMN = "S"

PASTE_TYPE_CHOICES = ["Exact", "Recreated", "Unspecified"]
PASTE_TYPE_DEFAULT = "Unspecified"

POLL_INTERVAL_SECONDS = 1.0
POLL_TIMEOUT_SECONDS = 10.0

SEARCH_RESULT_LIMIT = 15
