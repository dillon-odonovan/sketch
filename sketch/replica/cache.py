"""Global cache: normalized Replica Code -> rendered PokePaste + URL.

Replica Codes in Pokemon Champions are globally deterministic — the same
10-char code yields the same team for every player on every console. So
one OCR per code, across every guild on the bot, is enough. The cache
turns "first sighting of a code requires screenshots" into "every later
sighting is code-only," which is what makes the steady-state /add-team
invocation be just `code:` and nothing else.

Each entry stores both the rendered Showdown / PokePaste text and the
minted pokepast.es URL. The URL can be temporarily `None` if the
upload to pokepast.es failed after a successful OCR + user confirm —
the next /add-team call with the same code uses the cached paste text
to retry the upload, skipping the (expensive) OCR step.

Backed by a top-level Firestore collection (one doc per code, doc_id =
normalized code), with a per-process read-through dict in front so
repeated lookups don't pay Firestore-read cost. Writes go through
`create()` — the transactional fail-if-exists primitive — so two guilds
OCR'ing the same code at the same moment collapse cleanly: the loser
catches AlreadyExists, re-reads the winning doc, and uses that paste.
`set_url()` upgrades an entry's URL once the upload succeeds (or on
retry of a partial entry).

Layout mirrors `storage/guild_config.py`: a Protocol with two impls
(`InMemoryReplicaCacheStore` for tests, `FirestoreReplicaCacheStore`
for production). Sync methods on the store; callers wrap with
`asyncio.to_thread` from the slash-command handler so the event loop
isn't blocked on Firestore RPCs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from google.api_core import exceptions as gax_exceptions
from google.cloud import firestore

from sketch import config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReplicaCacheEntry:
    """One cached replica -> team mapping.

    `paste_text` is the rendered Showdown / PokePaste export — written
    at user-confirm time, before the pokepast.es upload is attempted.

    `pokepaste_url` is the canonical (https, no trailing slash) form
    produced by `canonicalize_pokepaste_url` once the upload succeeds.
    It's `None` between user-confirm and a successful upload, so a
    failed upload leaves a recoverable cache entry: the next /add-team
    for this code uses the stored `paste_text` to retry the upload
    without re-running OCR.

    `species` is the 6 species names, stored for human-readable Firestore
    console inspection — the sheet's TEAMDATAFROMPASTE formula is still
    the source of truth for what shows up in the team bank.

    The `created_by_*` audit fields trace bad cache entries back to the
    user/guild that seeded them.
    """

    paste_text: str
    pokepaste_url: str | None
    species: list[str]
    created_at: datetime
    created_by_user_id: int
    created_by_guild_id: int


class ReplicaCacheStore(Protocol):
    """`get` returns None for unknown codes. `create` is fail-if-exists
    and returns the canonical entry — the winner's value on race, the
    new value when there's no race. `set_url` upgrades an entry's
    `pokepaste_url` field in place (used to attach a freshly-minted URL
    to a previously-incomplete entry, or to recover from a failed
    upload).
    """

    def get(self, code: str) -> ReplicaCacheEntry | None: ...

    def create(self, code: str, entry: ReplicaCacheEntry) -> ReplicaCacheEntry: ...

    def set_url(self, code: str, url: str) -> ReplicaCacheEntry: ...


class InMemoryReplicaCacheStore:
    """Test double. Same contract as the Firestore-backed store.

    Not used in production. Kept independent of the Firestore module so
    tests can drive cache-hit, cache-miss, race-on-create, and
    partial-entry-then-set-url flows without standing up a fake
    `firestore.Client`.
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

    def set_url(self, code: str, url: str) -> ReplicaCacheEntry:
        existing = self._mapping.get(code)
        if existing is None:
            raise LookupError(
                f"Cannot set URL for replica {code}: no cache entry exists"
            )
        upgraded = replace(existing, pokepaste_url=url)
        self._mapping[code] = upgraded
        return upgraded


class FirestoreReplicaCacheStore:
    """Production store. Lazy read-through dict in front of Firestore.

    Unlike `FirestoreGuildConfigStore` (which loads the full collection
    at startup), the replica collection can grow unbounded, so we don't
    preload. The dict fills on first read of each code and stays warm
    for the bot's lifetime — entries are tiny (~1 KB) and within a
    process the URL only ever transitions from None to a stable string,
    so cached entries are never silently wrong (the worst staleness is
    "we still think this entry has no URL after another process set it,"
    which triggers a redundant re-mint — wasteful but not incorrect).
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
            # Intentionally not negative-caching: a write between this
            # read and a subsequent get() must be visible. The 10-char
            # shape check on the slash command bounds the worst case to
            # one Firestore read per valid-looking submission.
            return None
        entry = _parse_doc(snap.id, snap.to_dict() or {})
        if entry is not None:
            self._cache[code] = entry
        return entry

    def create(self, code: str, entry: ReplicaCacheEntry) -> ReplicaCacheEntry:
        try:
            self._doc(code).create(_serialize(entry))
        except gax_exceptions.AlreadyExists:
            # Lost the race against another concurrent OCR for the same
            # code. The winning entry is now in Firestore; re-read it
            # and let the caller use that paste / URL. The loser's
            # parsed team is discarded (the winner's is the canonical
            # version going forward).
            existing = self.get(code)
            if existing is None:
                # AlreadyExists raised but the doc isn't readable. This
                # would require an external delete between create and
                # get — vanishingly rare. Surface the original error.
                logger.warning(
                    "Firestore create raised AlreadyExists for replica %s "
                    "but the doc isn't readable; surfacing as transient error",
                    code,
                )
                raise
            return existing
        # We won. Warm the local cache so the immediate next get()
        # doesn't round-trip just to confirm what we just wrote.
        self._cache[code] = entry
        return entry

    def set_url(self, code: str, url: str) -> ReplicaCacheEntry:
        # Use set(merge=True) — this updates only the pokepaste_url
        # field on the existing doc rather than rewriting the whole
        # thing. If two processes race on a re-mint (both minted a
        # different URL because of an earlier failure), the later
        # write wins — both URLs point at valid pastes of the same
        # team, so either is correct; the cache just settles on one.
        self._doc(code).set({"pokepaste_url": url}, merge=True)
        existing = self._cache.get(code)
        if existing is not None:
            upgraded = replace(existing, pokepaste_url=url)
            self._cache[code] = upgraded
            return upgraded
        # The in-memory cache was cold for this entry; re-read it from
        # Firestore so the caller gets the full entry shape back.
        snap = self._doc(code).get()
        if not snap.exists:
            raise LookupError(
                f"Cannot set URL for replica {code}: no cache entry exists"
            )
        entry = _parse_doc(snap.id, snap.to_dict() or {})
        if entry is None:
            raise LookupError(
                f"Cache entry for replica {code} failed to parse after set_url"
            )
        self._cache[code] = entry
        return entry

    def _doc(self, code: str):
        return self._client.collection(config.REPLICA_CACHE_COLLECTION).document(code)


def _serialize(entry: ReplicaCacheEntry) -> dict:
    """Convert an entry to the on-wire Firestore document shape.

    Snowflakes (Discord user/guild IDs) are stored as strings to dodge
    Firestore's silent int->float coercion on the read path (same
    pattern `FirestoreGuildConfigStore.set_broadcast_channel_id` uses
    for the broadcast_channel_id field).

    `pokepaste_url=None` (entry written before the upload succeeded)
    serializes as a null field; the parser accepts both null and an
    absent key.
    """
    return {
        "paste_text": entry.paste_text,
        "pokepaste_url": entry.pokepaste_url,
        "species": list(entry.species),
        "created_at": entry.created_at,
        "created_by_user_id": str(entry.created_by_user_id),
        "created_by_guild_id": str(entry.created_by_guild_id),
    }


def _parse_doc(doc_id: str, data: dict) -> ReplicaCacheEntry | None:
    """Build an entry from a Firestore doc, or log and skip on malformed
    input.

    `paste_text`, `created_at`, and both audit snowflakes are required;
    a doc missing any of them is dropped with a WARNING naming the bad
    field. `pokepaste_url` is optional (None / absent both mean "upload
    hasn't succeeded yet"). Dropping a bad doc isolates the damage to
    that one entry — the next cold OCR of the same code repopulates a
    valid one.
    """
    paste_text = data.get("paste_text")
    if not isinstance(paste_text, str) or not paste_text:
        logger.warning(
            "Skipping %s/%s: missing or non-string paste_text",
            config.REPLICA_CACHE_COLLECTION,
            doc_id,
        )
        return None

    raw_url = data.get("pokepaste_url")
    pokepaste_url: str | None
    if raw_url is None:
        pokepaste_url = None
    elif isinstance(raw_url, str) and raw_url:
        pokepaste_url = raw_url
    else:
        logger.warning(
            "Skipping %s/%s: pokepaste_url is present but not a non-empty "
            "string (got %r)",
            config.REPLICA_CACHE_COLLECTION,
            doc_id,
            raw_url,
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
        paste_text=paste_text,
        pokepaste_url=pokepaste_url,
        species=species,
        created_at=created_at,
        created_by_user_id=created_by_user_id,
        created_by_guild_id=created_by_guild_id,
    )
