"""Global cache: normalized Replica Code -> PokePaste URL.

Replica Codes in Pokemon Champions are globally deterministic — the same
10-char hex code yields the same team for every player on every console.
So one OCR per code, across every guild on the bot, is enough. The cache
turns "first sighting of a code requires screenshots" into "every later
sighting is code-only," which is what makes the steady-state `/replica`
invocation be just `code:` and nothing else.

Backed by a top-level Firestore collection (one doc per code, doc_id =
normalized hex), with a per-process read-through dict in front so repeated
lookups don't pay Firestore-read cost. Writes go through `create()` — the
transactional fail-if-exists primitive — so two guilds OCR'ing the same
code at the same moment collapse cleanly: the loser catches AlreadyExists,
re-reads the winning doc, and uses that URL. The duplicate OCR work is
accepted as rare and cheap.

Layout mirrors `storage/guild_config.py`: a Protocol with two impls
(`InMemoryReplicaCacheStore` for tests, `FirestoreReplicaCacheStore` for
production). Sync methods on the store; callers wrap with
`asyncio.to_thread` from the slash-command handler so the event loop isn't
blocked on Firestore RPCs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from google.api_core import exceptions as gax_exceptions
from google.cloud import firestore

from sketch import config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReplicaCacheEntry:
    """One cached replica -> team mapping.

    `pokepaste_url` is the canonical (https, no trailing slash) form
    produced by `canonicalize_pokepaste_url` at write time.

    `species` is the 6 species names as parsed by the renderer at OCR time.
    Stored on the cache entry purely for human readability when the Firestore
    console is opened by an operator — the sheet's TEAMDATAFROMPASTE formula
    is still the source of truth for what shows up in the team bank.

    The `created_by_*` audit fields exist so a future admin command can
    trace bad cache entries back to the user/guild that seeded them.
    """

    pokepaste_url: str
    species: list[str]
    created_at: datetime
    created_by_user_id: int
    created_by_guild_id: int


class ReplicaCacheStore(Protocol):
    """`get` returns None for unknown codes. `create` is fail-if-exists and
    returns the canonical entry — the winner's value on race, the new value
    when there's no race. Callers don't need to distinguish; either way the
    return value is the URL to use next.
    """

    def get(self, code: str) -> ReplicaCacheEntry | None: ...

    def create(self, code: str, entry: ReplicaCacheEntry) -> ReplicaCacheEntry: ...


class InMemoryReplicaCacheStore:
    """Test double. Same contract as the Firestore-backed store.

    Not used in production. Kept independent of the Firestore module so
    tests can drive cache-hit, cache-miss, and race-on-create flows without
    standing up a fake `firestore.Client`.
    """

    def __init__(self, mapping: dict[str, ReplicaCacheEntry] | None = None) -> None:
        self._mapping: dict[str, ReplicaCacheEntry] = dict(mapping or {})

    def get(self, code: str) -> ReplicaCacheEntry | None:
        return self._mapping.get(code)

    def create(self, code: str, entry: ReplicaCacheEntry) -> ReplicaCacheEntry:
        existing = self._mapping.get(code)
        if existing is not None:
            return existing
        self._mapping[code] = entry
        return entry


class FirestoreReplicaCacheStore:
    """Production store. Lazy read-through dict in front of Firestore.

    Unlike `FirestoreGuildConfigStore` (which loads the full collection at
    startup), the replica collection can grow unbounded, so we don't preload.
    The dict fills on first read of each code and stays warm for the bot's
    lifetime — codes are tiny (10-char key, ~200-byte value) and re-OCR of a
    cached code is impossible by construction, so cached entries never go
    stale within the process.
    """

    def __init__(self, client: firestore.Client) -> None:
        self._client = client
        self._cache: dict[str, ReplicaCacheEntry] = {}

    def get(self, code: str) -> ReplicaCacheEntry | None:
        cached = self._cache.get(code)
        if cached is not None:
            return cached
        snap = self._doc(code).get()
        if not snap.exists:
            # Intentionally not negative-caching: a write between this read
            # and a subsequent get() must be visible. The 10-char hex shape
            # check on the slash command bounds the worst case to one
            # Firestore read per valid-looking submission.
            return None
        entry = _parse_doc(snap.id, snap.to_dict() or {})
        if entry is not None:
            self._cache[code] = entry
        return entry

    def create(self, code: str, entry: ReplicaCacheEntry) -> ReplicaCacheEntry:
        try:
            self._doc(code).create(_serialize(entry))
        except gax_exceptions.AlreadyExists:
            # Lost the race against another concurrent OCR for the same code.
            # The winning entry is now in Firestore; re-read it and let the
            # caller use that URL. The loser's freshly-minted pokepast.es URL
            # is orphaned, which is acceptable waste at the expected rate.
            existing = self.get(code)
            if existing is None:
                # AlreadyExists raised but the doc isn't readable. This would
                # require an external delete between create and get — vanishingly
                # rare. Surface the original error rather than papering over it.
                logger.warning(
                    "Firestore create raised AlreadyExists for replica %s but "
                    "the doc isn't readable; surfacing as transient error",
                    code,
                )
                raise
            return existing
        # We won. Warm the local cache so the immediate next get() doesn't
        # round-trip just to confirm what we just wrote.
        self._cache[code] = entry
        return entry

    def _doc(self, code: str):
        return self._client.collection(config.REPLICA_CACHE_COLLECTION).document(code)


def _serialize(entry: ReplicaCacheEntry) -> dict:
    """Convert an entry to the on-wire Firestore document shape.

    Snowflakes (Discord user/guild IDs) are stored as strings to dodge
    Firestore's silent int->float coercion on the read path (same pattern
    `FirestoreGuildConfigStore.set_broadcast_channel_id` uses for the
    broadcast_channel_id field).
    """
    return {
        "pokepaste_url": entry.pokepaste_url,
        "species": list(entry.species),
        "created_at": entry.created_at,
        "created_by_user_id": str(entry.created_by_user_id),
        "created_by_guild_id": str(entry.created_by_guild_id),
    }


def _parse_doc(doc_id: str, data: dict) -> ReplicaCacheEntry | None:
    """Build an entry from a Firestore doc, or log and skip on malformed
    input.

    Strict on every required field — a doc missing the URL, the timestamp,
    or either audit snowflake is dropped with a WARNING naming what was
    wrong. The earlier lenient version substituted epoch / 0 defaults,
    which papered over real data corruption and produced cache entries
    with nonsense audit columns. Dropping the bad doc isolates damage to
    that one entry; the next cold OCR of the same code will repopulate it
    with valid fields.
    """
    url = data.get("pokepaste_url")
    if not isinstance(url, str) or not url:
        logger.warning(
            "Skipping %s/%s: missing or non-string pokepaste_url",
            config.REPLICA_CACHE_COLLECTION,
            doc_id,
        )
        return None

    raw_species = data.get("species") or []
    species = [str(s) for s in raw_species if isinstance(s, str)]

    created_at = data.get("created_at")
    if not isinstance(created_at, datetime):
        logger.warning(
            "Skipping %s/%s: missing or non-datetime created_at (got %r)",
            config.REPLICA_CACHE_COLLECTION,
            doc_id,
            created_at,
        )
        return None

    def _parse_snowflake(field: str) -> int | None:
        raw = data.get(field)
        if isinstance(raw, str) and raw.isdigit():
            return int(raw)
        if isinstance(raw, int):
            return raw
        return None

    created_by_user_id = _parse_snowflake("created_by_user_id")
    created_by_guild_id = _parse_snowflake("created_by_guild_id")
    if created_by_user_id is None or created_by_guild_id is None:
        logger.warning(
            "Skipping %s/%s: missing or malformed audit snowflake "
            "(user_id=%r guild_id=%r)",
            config.REPLICA_CACHE_COLLECTION,
            doc_id,
            data.get("created_by_user_id"),
            data.get("created_by_guild_id"),
        )
        return None

    return ReplicaCacheEntry(
        pokepaste_url=url,
        species=species,
        created_at=created_at,
        created_by_user_id=created_by_user_id,
        created_by_guild_id=created_by_guild_id,
    )
