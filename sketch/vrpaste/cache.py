"""Global cache: VRPaste id -> minted Pokepaste URL.

A VRPaste's id is the slug portion of `https://www.vrpastes.com/<id>`.
Caching that → minted-Pokepaste mapping turns repeat submissions of the
same VRPaste into a cheap lookup (no re-fetch, no re-mint) AND restores
sheet-level dedup: without this cache, every submission would mint a
fresh pokepast.es URL, so two submissions of the same VRPaste would
land on different sheet rows.

Writes go through `create()` — the transactional fail-if-exists
primitive — so two guilds (or two users in the same guild) racing on
the same VRPaste converge on a single Pokepaste URL. Both submissions
may mint distinct pokepast.es URLs locally; whichever lands first
wins the cache slot, and the loser reads the winner's URL out and
uses it. The loser's minted URL becomes a harmless orphan paste on
pokepast.es — but the sheet sees one URL, so dedup catches the
second row.

Simpler than `sketch.champions.replica_cache` even with the race
semantics: only one stored field (the Pokepaste URL), no
partial-cache-hit retry, no `set_url`. If a mint failed, the user
reruns and we hit pokepast.es again; there's no intermediate state to
preserve like the OCR + confirm + render the Champions flow
accumulates before the mint.

Backed by a top-level Firestore collection (one doc per VRPaste id,
doc_id = the id verbatim — id case is preserved so two ids that
differ only in case don't collide). Sync methods; callers wrap with
`asyncio.to_thread` to avoid blocking the event loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from google.api_core import exceptions as gax_exceptions
from google.cloud import firestore

from sketch import config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VRPasteCacheEntry:
    """One cached VRPaste id -> Pokepaste URL mapping.

    `pokepaste_url` is the canonical (https, no trailing slash) form
    that `sketch.pokepaste.renderer.post_to_pokepaste` returns after
    `canonicalize_pokepaste_url`. Always set — unlike the Champions
    replica cache, we don't allow None URLs because there's no
    multi-step flow to recover from; the cache is only written *after*
    a successful mint.

    The `created_by_*` audit fields trace bad cache entries back to
    the user/guild that seeded them.
    """

    pokepaste_url: str
    created_at: datetime
    created_by_user_id: int
    created_by_guild_id: int


class VRPasteCacheStore(Protocol):
    """`get` returns None for unknown ids. `create` is fail-if-exists
    and returns the canonical entry — the winner's value on race, the
    new value when there's no race. Callers should always use the
    returned entry's `pokepaste_url` rather than the URL they passed
    in: on a lost race that's the winner's URL, which is what the
    sheet write needs to converge on for dedup.
    """

    def get(self, vrpaste_id: str) -> VRPasteCacheEntry | None: ...

    def create(
        self,
        vrpaste_id: str,
        pokepaste_url: str,
        *,
        user_id: int,
        guild_id: int,
    ) -> VRPasteCacheEntry: ...


class InMemoryVRPasteCacheStore:
    """Test double. Same contract as the Firestore-backed store."""

    def __init__(self, mapping: dict[str, VRPasteCacheEntry] | None = None) -> None:
        self._mapping: dict[str, VRPasteCacheEntry] = dict(mapping or {})

    def get(self, vrpaste_id: str) -> VRPasteCacheEntry | None:
        return self._mapping.get(vrpaste_id)

    def create(
        self,
        vrpaste_id: str,
        pokepaste_url: str,
        *,
        user_id: int,
        guild_id: int,
    ) -> VRPasteCacheEntry:
        existing = self._mapping.get(vrpaste_id)
        if existing is not None:
            return existing
        entry = VRPasteCacheEntry(
            pokepaste_url=pokepaste_url,
            created_at=datetime.now(timezone.utc),
            created_by_user_id=user_id,
            created_by_guild_id=guild_id,
        )
        self._mapping[vrpaste_id] = entry
        return entry


class FirestoreVRPasteCacheStore:
    """Production store. Lazy read-through dict in front of Firestore.

    Same caching pattern as `FirestoreReplicaCacheStore`: don't preload
    (the collection can grow unbounded), warm a per-process dict on
    first read of each id. Within a process the URL only ever
    transitions from unset → set (or set → another valid URL on
    re-mint races), so cached entries are never silently wrong; the
    worst case is a redundant mint that immediately settles.
    """

    def __init__(self, client: firestore.Client) -> None:
        self._client = client
        self._cache: dict[str, VRPasteCacheEntry] = {}

    def get(self, vrpaste_id: str) -> VRPasteCacheEntry | None:
        cached = self._cache.get(vrpaste_id)
        if cached is not None:
            return cached
        snap = self._doc(vrpaste_id).get()
        if not snap.exists:
            return None
        entry = _parse_doc(snap.id, snap.to_dict() or {})
        if entry is not None:
            self._cache[vrpaste_id] = entry
        return entry

    def create(
        self,
        vrpaste_id: str,
        pokepaste_url: str,
        *,
        user_id: int,
        guild_id: int,
    ) -> VRPasteCacheEntry:
        entry = VRPasteCacheEntry(
            pokepaste_url=pokepaste_url,
            created_at=datetime.now(timezone.utc),
            created_by_user_id=user_id,
            created_by_guild_id=guild_id,
        )
        try:
            self._doc(vrpaste_id).create(_serialize(entry))
        except gax_exceptions.AlreadyExists:
            # Lost the race against another concurrent submission. The
            # winning entry is now in Firestore; re-read it and let the
            # caller use that URL so the sheet writes converge on a
            # single Pokepaste URL. Our minted URL is wasted but harmless
            # (it's a valid paste of the same team) and only happens
            # when two users hit the same VRPaste in the same instant.
            existing = self.get(vrpaste_id)
            if existing is None:
                # AlreadyExists raised but the doc isn't readable — would
                # require an external delete between create and get.
                # Surface as transient and let the user retry.
                logger.warning(
                    "Firestore create raised AlreadyExists for vrpaste id=%s "
                    "but the doc isn't readable; surfacing as transient error",
                    vrpaste_id,
                )
                raise
            return existing
        # We won the race. Warm the local cache so the immediate next
        # get() doesn't round-trip just to confirm what we just wrote.
        self._cache[vrpaste_id] = entry
        return entry

    def _doc(self, vrpaste_id: str):
        return self._client.collection(config.VRPASTE_CACHE_COLLECTION).document(
            vrpaste_id
        )


def _serialize(entry: VRPasteCacheEntry) -> dict:
    """Convert an entry to the on-wire Firestore document shape.

    Snowflakes (Discord user/guild IDs) are stored as strings to dodge
    Firestore's silent int->float coercion on the read path — the same
    pattern `FirestoreReplicaCacheStore._serialize` uses.
    """
    return {
        "pokepaste_url": entry.pokepaste_url,
        "created_at": entry.created_at,
        "created_by_user_id": str(entry.created_by_user_id),
        "created_by_guild_id": str(entry.created_by_guild_id),
    }


def _parse_doc(doc_id: str, data: dict) -> VRPasteCacheEntry | None:
    """Build an entry from a Firestore doc, or log and skip on malformed input.

    Dropping a bad doc isolates the damage to that one entry — the next
    /add-team for the same VRPaste re-fetches and re-mints, repopulating
    a valid entry on the same key.
    """
    pokepaste_url = data.get("pokepaste_url")
    if not isinstance(pokepaste_url, str) or not pokepaste_url:
        logger.warning(
            "Skipping %s/%s: missing or non-string pokepaste_url",
            config.VRPASTE_CACHE_COLLECTION,
            doc_id,
        )
        return None

    created_at = data.get("created_at")
    if not isinstance(created_at, datetime):
        logger.warning(
            "Skipping %s/%s: missing or non-datetime created_at (got %r)",
            config.VRPASTE_CACHE_COLLECTION,
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
            config.VRPASTE_CACHE_COLLECTION,
            doc_id,
            data.get("created_by_user_id"),
            data.get("created_by_guild_id"),
        )
        return None

    return VRPasteCacheEntry(
        pokepaste_url=pokepaste_url,
        created_at=created_at,
        created_by_user_id=created_by_user_id,
        created_by_guild_id=created_by_guild_id,
    )
