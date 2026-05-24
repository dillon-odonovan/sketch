"""Tests for the replica-code cache.

`InMemoryReplicaCacheStore` covers the contract slash-command handlers
depend on. `FirestoreReplicaCacheStore` is tested against a hand-rolled
fake Firestore client (same pattern as `test_guild_config.py`) so the
suite never needs network or credentials.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from google.api_core import exceptions as gax_exceptions

from sketch.replica.cache import (
    FirestoreReplicaCacheStore,
    InMemoryReplicaCacheStore,
    ReplicaCacheEntry,
)


def _entry(
    url: str = "https://pokepast.es/abc123",
    species: list[str] | None = None,
    user_id: int = 1,
    guild_id: int = 99,
) -> ReplicaCacheEntry:
    return ReplicaCacheEntry(
        pokepaste_url=url,
        species=species or ["Pikachu"] * 6,
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
        created_by_user_id=user_id,
        created_by_guild_id=guild_id,
    )


class TestInMemoryReplicaCacheStore:
    def test_get_miss_returns_none(self):
        store = InMemoryReplicaCacheStore()
        assert store.get("AAAA111122") is None

    def test_create_then_get(self):
        store = InMemoryReplicaCacheStore()
        entry = _entry()
        returned = store.create("AAAA111122", entry)
        assert returned is entry
        assert store.get("AAAA111122") == entry

    def test_create_returns_existing_on_collision(self):
        # The "lost the race" contract: a second create against the same key
        # returns the FIRST entry, not the loser's value, and does not
        # overwrite it. This is what makes the on-Firestore version safe.
        store = InMemoryReplicaCacheStore()
        winner = _entry(url="https://pokepast.es/winner")
        loser = _entry(url="https://pokepast.es/loser")
        store.create("AAAA111122", winner)
        returned = store.create("AAAA111122", loser)
        assert returned == winner
        assert store.get("AAAA111122") == winner

    def test_seed_via_constructor(self):
        store = InMemoryReplicaCacheStore({"AAAA111122": _entry()})
        assert store.get("AAAA111122") is not None


# --- Firestore-backed store -------------------------------------------------


@dataclass
class _FakeSnap:
    """Stand-in for a Firestore DocumentSnapshot."""

    id: str
    _data: dict | None

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict | None:
        return self._data


@dataclass
class _FakeDocRef:
    """Stand-in for a Firestore DocumentReference.

    The store uses two methods on this: `.get()` for reads, `.create(payload)`
    for writes. `.create` raises AlreadyExists if a doc with this id has
    already been created — the primitive the race-loser branch depends on.
    """

    id: str
    _docs: dict[str, dict]
    create_calls: list[dict] = field(default_factory=list)

    def get(self) -> _FakeSnap:
        return _FakeSnap(id=self.id, _data=self._docs.get(self.id))

    def create(self, payload: dict) -> None:
        self.create_calls.append(dict(payload))
        if self.id in self._docs:
            raise gax_exceptions.AlreadyExists(f"Document {self.id} already exists")
        self._docs[self.id] = dict(payload)


class _FakeCollection:
    def __init__(self, docs: dict[str, dict]) -> None:
        self._docs = docs
        self.doc_refs: dict[str, _FakeDocRef] = {}

    def document(self, doc_id: str) -> _FakeDocRef:
        ref = self.doc_refs.get(doc_id)
        if ref is None:
            ref = _FakeDocRef(id=doc_id, _docs=self._docs)
            self.doc_refs[doc_id] = ref
        return ref


class _FakeFirestoreClient:
    def __init__(self, docs: dict[str, dict] | None = None) -> None:
        self._docs = dict(docs or {})
        self._collection: _FakeCollection | None = None
        self.requested_collections: list[str] = []

    def collection(self, name: str) -> _FakeCollection:
        self.requested_collections.append(name)
        if self._collection is None:
            self._collection = _FakeCollection(self._docs)
        return self._collection


class TestFirestoreReplicaCacheStore:
    def test_get_miss_returns_none(self):
        store = FirestoreReplicaCacheStore(_FakeFirestoreClient())
        assert store.get("AAAA111122") is None

    def test_create_writes_through_and_warms_cache(self):
        client = _FakeFirestoreClient()
        store = FirestoreReplicaCacheStore(client)
        entry = _entry()
        returned = store.create("AAAA111122", entry)
        assert returned == entry
        # Cache warm: next get returns the entry without re-reading Firestore.
        # We test this by mutating the underlying _docs dict and verifying
        # get still returns the cached value. The Firestore mutation would
        # be ignored, proving the read hit the dict.
        client._docs["AAAA111122"] = {"pokepaste_url": "https://pokepast.es/other"}
        assert store.get("AAAA111122") == entry

    def test_create_on_existing_returns_winner(self):
        # Race-loser path: another writer beat us to this doc. The store
        # must catch AlreadyExists, re-read, and return the winner's entry
        # rather than raising into the caller.
        winner_payload = {
            "pokepaste_url": "https://pokepast.es/winner",
            "species": ["Charizard"] * 6,
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "created_by_user_id": "111",
            "created_by_guild_id": "222",
        }
        client = _FakeFirestoreClient({"AAAA111122": winner_payload})
        store = FirestoreReplicaCacheStore(client)
        loser = _entry(url="https://pokepast.es/loser")
        returned = store.create("AAAA111122", loser)
        assert returned.pokepaste_url == "https://pokepast.es/winner"

    def test_get_parses_string_snowflakes(self):
        # The store serializes user/guild IDs as strings to dodge Firestore's
        # int->float coercion. The parser must read them back as ints so
        # callers don't see surprise floats.
        client = _FakeFirestoreClient(
            {
                "AAAA111122": {
                    "pokepaste_url": "https://pokepast.es/abc",
                    "species": ["Pikachu"] * 6,
                    "created_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
                    "created_by_user_id": "987654321098765432",
                    "created_by_guild_id": "123456789012345678",
                }
            }
        )
        store = FirestoreReplicaCacheStore(client)
        cached = store.get("AAAA111122")
        assert cached is not None
        assert cached.created_by_user_id == 987654321098765432
        assert cached.created_by_guild_id == 123456789012345678

    def test_get_skips_doc_missing_url(self, caplog):
        # Defense against operator hand-edits / schema drift: a doc with no
        # pokepaste_url logs a warning and returns None rather than crashing
        # the read path on every call for that code.
        client = _FakeFirestoreClient({"AAAA111122": {"species": ["Pikachu"] * 6}})
        store = FirestoreReplicaCacheStore(client)
        with caplog.at_level("WARNING"):
            assert store.get("AAAA111122") is None
        assert any("pokepaste_url" in r.message for r in caplog.records)

    def test_get_skips_doc_missing_created_at(self, caplog):
        # The earlier lenient parser substituted epoch for missing timestamps,
        # which papered over real data corruption. The strict parser now drops
        # such docs with a WARNING.
        client = _FakeFirestoreClient(
            {
                "AAAA111122": {
                    "pokepaste_url": "https://pokepast.es/abc",
                    "species": ["Pikachu"] * 6,
                    "created_by_user_id": "1",
                    "created_by_guild_id": "2",
                    # No created_at field
                }
            }
        )
        store = FirestoreReplicaCacheStore(client)
        with caplog.at_level("WARNING"):
            assert store.get("AAAA111122") is None
        assert any("created_at" in r.message for r in caplog.records)

    def test_get_skips_doc_missing_audit_snowflake(self, caplog):
        # Same contract: missing or malformed audit snowflake drops the entry
        # rather than emitting `0` as the user_id / guild_id (which would
        # surface bogus values to whoever inspects the cache).
        client = _FakeFirestoreClient(
            {
                "AAAA111122": {
                    "pokepaste_url": "https://pokepast.es/abc",
                    "species": ["Pikachu"] * 6,
                    "created_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
                    "created_by_user_id": "1",
                    # No created_by_guild_id field
                }
            }
        )
        store = FirestoreReplicaCacheStore(client)
        with caplog.at_level("WARNING"):
            assert store.get("AAAA111122") is None
        assert any("audit snowflake" in r.message for r in caplog.records)

    def test_create_serializes_snowflakes_as_strings(self):
        client = _FakeFirestoreClient()
        store = FirestoreReplicaCacheStore(client)
        store.create("AAAA111122", _entry(user_id=999, guild_id=888))
        ref = client._collection.doc_refs["AAAA111122"]
        assert len(ref.create_calls) == 1
        payload = ref.create_calls[0]
        assert payload["created_by_user_id"] == "999"
        assert payload["created_by_guild_id"] == "888"
        # Round-trip: a fresh store against the same fake client should read
        # the same int values back.
        fresh = FirestoreReplicaCacheStore(client)
        cached = fresh.get("AAAA111122")
        assert cached is not None
        assert cached.created_by_user_id == 999
        assert cached.created_by_guild_id == 888

    def test_get_caches_after_first_firestore_read(self):
        # A cache-miss read should populate the in-memory dict so the second
        # get() doesn't pay Firestore-read cost. We assert this by mutating
        # the backing dict after the first read; a re-fetched value would
        # observe the mutation, a cached value would not.
        client = _FakeFirestoreClient(
            {
                "AAAA111122": {
                    "pokepaste_url": "https://pokepast.es/v1",
                    "species": ["Pikachu"] * 6,
                    "created_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
                    "created_by_user_id": "111",
                    "created_by_guild_id": "222",
                }
            }
        )
        store = FirestoreReplicaCacheStore(client)
        first = store.get("AAAA111122")
        client._docs["AAAA111122"]["pokepaste_url"] = "https://pokepast.es/v2"
        second = store.get("AAAA111122")
        assert first is not None and second is not None
        assert first.pokepaste_url == "https://pokepast.es/v1"
        assert second.pokepaste_url == "https://pokepast.es/v1"

    def test_writes_to_replica_codes_collection(self):
        client = _FakeFirestoreClient()
        store = FirestoreReplicaCacheStore(client)
        store.create("AAAA111122", _entry())
        assert "replica_codes" in client.requested_collections
