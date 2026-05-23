"""Per-guild configuration: which Google Sheet does each Discord guild write
to, and where (if anywhere) should successful /add-team calls broadcast?

Backed by a Firestore collection — one document per guild, doc_id = the
Discord guild_id (as a string). Terraform provisions the empty database; the
contents are populated by `bin/seed_guilds.py` today and by a future
`/register-guild` slash command. Keeping guild data out of Terraform means
server owners can self-register without anyone running `terraform apply`,
and removes the "tfvars edit / apply / VM reset" trap that earlier env-var
plumbing fell into.

The `GuildConfigStore` Protocol is the contract command handlers depend on.
The bot constructs `FirestoreGuildConfigStore` at startup; tests use
`StaticGuildConfigStore` so they don't need a Firestore client.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from google.cloud import firestore

logger = logging.getLogger(__name__)

# Firestore collection name. Single shared collection because the (default)
# database is project-singleton — if a second feature ever wants Firestore in
# this project it shares the DB, so namespacing happens at the collection
# level. Doc IDs are guild_id-as-string.
GUILD_CONFIGS_COLLECTION = "guild_configs"


@dataclass(frozen=True)
class GuildConfig:
    """Configuration for a single guild. Frozen so it can be cached safely."""

    spreadsheet_id: str
    broadcast_channel_id: int | None = None


class GuildConfigStore(Protocol):
    """Lookup of `guild_id -> GuildConfig`. Returns None for unconfigured guilds.

    Sync by design: callers run inside discord.py's event loop and the
    in-memory dict lookup is microsecond-fast. The Firestore-backed impl
    loads all docs at startup into an in-process dict to preserve that
    property — per-lookup RPCs would push 5-50ms of network latency into
    every slash command.
    """

    def get(self, guild_id: int) -> GuildConfig | None: ...


class StaticGuildConfigStore:
    """In-memory store. Immutable after construction. Used by tests and as the
    backing store for FirestoreGuildConfigStore's snapshot."""

    def __init__(self, mapping: dict[int, GuildConfig]) -> None:
        self._mapping = dict(mapping)

    def get(self, guild_id: int) -> GuildConfig | None:
        return self._mapping.get(guild_id)

    def configured_guild_ids(self) -> list[int]:
        """For diagnostic logging at startup."""
        return list(self._mapping.keys())


class FirestoreGuildConfigStore:
    """Loads every guild_configs doc into an in-memory dict at construction.

    Trade-off: the bot never re-fetches during a session, so a guild added
    via `bin/seed_guilds.py` requires a restart to take effect. That's
    intentional for now — single-process bot, no cross-instance invalidation
    problem, no background refresh complexity. When `/register-guild` lands
    it will write through to both Firestore and `self._mapping` in the same
    call (`register(guild_id, config)`), so newly-registered guilds become
    routable immediately without a restart.

    Malformed documents are logged and skipped rather than crashing the bot.
    Strict crash-at-startup made sense for hand-edited env JSON; for a
    datastore, refusing to boot the whole bot over one bad doc would block
    every other guild for no good reason.
    """

    def __init__(self, client: firestore.Client) -> None:
        self._client = client
        self._mapping = self._load()
        logger.info(
            "Loaded guild config from Firestore for %d guild(s): %s",
            len(self._mapping),
            self.configured_guild_ids(),
        )

    def _load(self) -> dict[int, GuildConfig]:
        out: dict[int, GuildConfig] = {}
        for doc in self._client.collection(GUILD_CONFIGS_COLLECTION).stream():
            cfg = _parse_doc(doc.id, doc.to_dict() or {})
            if cfg is None:
                continue
            try:
                guild_id = int(doc.id)
            except ValueError:
                logger.warning(
                    "Skipping guild_configs/%s: doc id is not a numeric guild_id",
                    doc.id,
                )
                continue
            out[guild_id] = cfg
        return out

    def get(self, guild_id: int) -> GuildConfig | None:
        return self._mapping.get(guild_id)

    def configured_guild_ids(self) -> list[int]:
        return list(self._mapping.keys())


def _parse_doc(doc_id: str, data: dict) -> GuildConfig | None:
    """Build a GuildConfig from a Firestore doc, or return None + log on
    malformed input. Validation is intentionally lenient — write-time
    validation in bin/seed_guilds.py is the real gate; this is defense in
    depth against direct console edits or schema drift.
    """
    spreadsheet_id = data.get("spreadsheet_id")
    if not isinstance(spreadsheet_id, str) or not spreadsheet_id:
        logger.warning(
            "Skipping guild_configs/%s: missing or non-string spreadsheet_id",
            doc_id,
        )
        return None

    raw_channel = data.get("broadcast_channel_id")
    broadcast_channel_id: int | None = None
    if raw_channel is not None:
        if not isinstance(raw_channel, str) or not raw_channel.isdigit():
            logger.warning(
                "Skipping broadcast_channel_id for guild_configs/%s: expected "
                "numeric string, got %r",
                doc_id,
                raw_channel,
            )
            # Keep the spreadsheet routing — the guild can still use the bot,
            # broadcasts just stay off until the bad value is fixed.
        else:
            broadcast_channel_id = int(raw_channel)

    return GuildConfig(
        spreadsheet_id=spreadsheet_id,
        broadcast_channel_id=broadcast_channel_id,
    )
