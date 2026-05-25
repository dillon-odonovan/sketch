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
ANTHROPIC_API_KEY = _required("ANTHROPIC_API_KEY")

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
    "Reg M-A": "Regulation M-A",
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

# How long a `/search-teams` result snapshot (sheet rows + the tokenized
# description index built over them) stays valid in process memory before
# the next /search-teams call re-fetches from Sheets.
#
# Realistic hit profile: /add-team is infrequent (one can only add so many
# distinct teams) and /search-teams sessions are typically minutes apart,
# so MOST /search-teams calls will be cache misses. The cache exists to
# amortize *bursts* — e.g., a user iterating queries within one session,
# or a user running /add-team and then immediately /search-teams to
# verify. Across separate sessions the TTL almost always expires first.
#
# Invalidation on /add-team (see SheetsClient.invalidate_snapshot) makes
# the "add then search" pattern correct even when within a cached window.
# The TTL itself serves two secondary roles: bound the staleness window
# for direct-Sheet edits (Google UI bypassing the bot), and cap the
# in-process memory held by long-idle guilds.
#
# 5 minutes captures session-length bursts comfortably while keeping
# direct-Sheet-edit staleness bounded. Could go higher (15-30 min) if
# we ever observe that within-session bursts routinely span longer.
SEARCH_CACHE_TTL_SECONDS = 300.0

# --- /replica feature -------------------------------------------------------

# Top-level Firestore collection that maps a normalized Replica Code (10-char
# uppercase alphanumeric) to the PokePaste URL we minted from its OCR'd team.
# Global (cross-guild) — codes are deterministic across all players, so a
# guild's OCR work benefits every other guild on the bot.
REPLICA_CACHE_COLLECTION = "replica_codes"

# Vision model for the /replica OCR pipeline. Sonnet 4.6 handles the Champions
# share-screen UI well at ~5x lower cost than Opus; tool-use forces schema
# conformance so we're not relying on the model's free-text JSON discipline.
# Promote to claude-opus-4-7 if Confirm-rate telemetry shows accuracy issues.
REPLICA_OCR_MODEL = "claude-sonnet-4-6"

# How long the preview embed (Confirm / Cancel buttons) stays interactive
# before timing out. Long enough that a user can step away briefly; short
# enough that a stale embed doesn't sit forever holding a deferred response.
REPLICA_PREVIEW_TIMEOUT_SECONDS = 300.0
