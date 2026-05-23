"""Per-guild configuration: which Google Sheet does each Discord guild write
to, and where (if anywhere) should successful /add-team calls broadcast?

Backed by a Firestore collection — one document per guild, doc_id = the
Discord guild_id (as a string). Terraform provisions the empty database; the
contents are populated by the `/register-sheet` and broadcast-channel slash
commands (`commands.py`). `bin/seed_guilds.py` is kept around as an
operator backstop for the rare case where the bot can't perform the write
itself (e.g. first-install bootstrapping under unusual permissions).

The `GuildConfigStore` Protocol is the contract command handlers depend on.
The bot constructs `FirestoreGuildConfigStore` at startup; tests use
`StaticGuildConfigStore` so they don't need a Firestore client.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
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
    """Lookup + mutation of `guild_id -> GuildConfig`.

    Reads return None for unconfigured guilds. Writes are synchronous from
    the caller's perspective (the Firestore-backed impl performs a blocking
    RPC); admin slash-command handlers wrap them in `asyncio.to_thread` so
    the event loop isn't blocked while the network call is in flight.

    The broadcast-channel mutators require the guild to already have a
    spreadsheet_id — without one, the bot refuses every command and a
    broadcast channel would be meaningless. Callers should check `get()`
    first and refuse with a friendly message; the store raises `LookupError`
    as defense in depth.
    """

    def get(self, guild_id: int) -> GuildConfig | None: ...

    def set_spreadsheet_id(self, guild_id: int, spreadsheet_id: str) -> GuildConfig: ...

    def set_broadcast_channel_id(
        self, guild_id: int, channel_id: int
    ) -> GuildConfig: ...

    def clear_broadcast_channel_id(self, guild_id: int) -> GuildConfig: ...


class StaticGuildConfigStore:
    """In-memory store. Mutable; used by tests that need to exercise the write
    methods without standing up a Firestore client.

    Not used in production today — `FirestoreGuildConfigStore` is the
    canonical implementation. Kept compatible with the full protocol so a
    future test against `setup_commands` can drive admin commands end-to-end
    with no Firestore dependency.
    """

    def __init__(self, mapping: dict[int, GuildConfig] | None = None) -> None:
        self._mapping: dict[int, GuildConfig] = dict(mapping or {})

    def get(self, guild_id: int) -> GuildConfig | None:
        return self._mapping.get(guild_id)

    def configured_guild_ids(self) -> list[int]:
        """For diagnostic logging at startup."""
        return list(self._mapping.keys())

    def set_spreadsheet_id(self, guild_id: int, spreadsheet_id: str) -> GuildConfig:
        existing = self._mapping.get(guild_id)
        new_cfg = (
            replace(existing, spreadsheet_id=spreadsheet_id)
            if existing is not None
            else GuildConfig(spreadsheet_id=spreadsheet_id)
        )
        self._mapping[guild_id] = new_cfg
        return new_cfg

    def set_broadcast_channel_id(self, guild_id: int, channel_id: int) -> GuildConfig:
        existing = self._mapping.get(guild_id)
        if existing is None:
            raise LookupError(
                f"Cannot set broadcast channel for unconfigured guild {guild_id}"
            )
        new_cfg = replace(existing, broadcast_channel_id=channel_id)
        self._mapping[guild_id] = new_cfg
        return new_cfg

    def clear_broadcast_channel_id(self, guild_id: int) -> GuildConfig:
        existing = self._mapping.get(guild_id)
        if existing is None:
            raise LookupError(
                f"Cannot clear broadcast channel for unconfigured guild {guild_id}"
            )
        new_cfg = replace(existing, broadcast_channel_id=None)
        self._mapping[guild_id] = new_cfg
        return new_cfg


class FirestoreGuildConfigStore:
    """Loads every guild_configs doc into an in-memory dict at construction,
    and write-through updates both Firestore and the in-memory dict in one
    call so newly-registered guilds become routable immediately.

    Trade-off vs. read-through-on-every-call: per-lookup RPCs would push
    5–50ms of latency into every slash command, which is too much for a
    hot-path like /search-teams. Write-through means we keep the in-memory
    speed without a cross-instance invalidation problem (single process) and
    without a TTL-based refresh that would defer config changes by an
    unpredictable interval.

    Malformed documents are logged and skipped at load time rather than
    crashing the bot. Strict crash-at-startup made sense for hand-edited env
    JSON; for a datastore, refusing to boot over one bad doc would block
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

    def set_spreadsheet_id(self, guild_id: int, spreadsheet_id: str) -> GuildConfig:
        # merge=True so we don't wipe a pre-existing broadcast_channel_id on
        # the same doc.
        self._doc(guild_id).set({"spreadsheet_id": spreadsheet_id}, merge=True)
        existing = self._mapping.get(guild_id)
        new_cfg = (
            replace(existing, spreadsheet_id=spreadsheet_id)
            if existing is not None
            else GuildConfig(spreadsheet_id=spreadsheet_id)
        )
        self._mapping[guild_id] = new_cfg
        return new_cfg

    def set_broadcast_channel_id(self, guild_id: int, channel_id: int) -> GuildConfig:
        existing = self._mapping.get(guild_id)
        if existing is None:
            raise LookupError(
                f"Cannot set broadcast channel for unconfigured guild {guild_id}"
            )
        # Stored as a string to match the shape bin/seed_guilds.py writes,
        # so _parse_doc's snowflake-string parsing handles both paths
        # identically on the next bot restart.
        self._doc(guild_id).set({"broadcast_channel_id": str(channel_id)}, merge=True)
        new_cfg = replace(existing, broadcast_channel_id=channel_id)
        self._mapping[guild_id] = new_cfg
        return new_cfg

    def clear_broadcast_channel_id(self, guild_id: int) -> GuildConfig:
        # Lazy import: the module-level TYPE_CHECKING guard keeps
        # google.cloud.firestore out of import-time deps so tests using
        # only StaticGuildConfigStore don't need the package installed.
        from google.cloud import firestore as _firestore

        existing = self._mapping.get(guild_id)
        if existing is None:
            raise LookupError(
                f"Cannot clear broadcast channel for unconfigured guild {guild_id}"
            )
        self._doc(guild_id).set(
            {"broadcast_channel_id": _firestore.DELETE_FIELD}, merge=True
        )
        new_cfg = replace(existing, broadcast_channel_id=None)
        self._mapping[guild_id] = new_cfg
        return new_cfg

    def _doc(self, guild_id: int):
        return self._client.collection(GUILD_CONFIGS_COLLECTION).document(str(guild_id))


def _parse_doc(doc_id: str, data: dict) -> GuildConfig | None:
    """Build a GuildConfig from a Firestore doc, or return None + log on
    malformed input. Validation is intentionally lenient — write-time
    validation in the slash-command handlers (and bin/seed_guilds.py) is
    the real gate; this is defense in depth against direct console edits
    or schema drift.
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
