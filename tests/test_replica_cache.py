"""Tests for the replica-code cache.

`InMemoryReplicaCacheStore` covers the contract slash-command handlers
depend on. `FirestoreReplicaCacheStore` is tested against a hand-rolled
fake Firestore client (same pattern as `test_guild_config.py`) so the
suite never needs network or credentials.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest
from google.api_core import exceptions as gax_exceptions

from sketch.replica.cache import (
    FirestoreReplicaCacheStore,
    InMemoryReplicaCacheStore,
    ReplicaCacheEntry,
)


def _entry(
    paste_text: str = "Pikachu @ Light Ball\nAbility: Static\n- Volt Tackle",
    url: str | None = "https://pokepast.es/abc123",
    species: list[str] | None = None,
    user_id: int = 1,
    guild_id: int = 99,
) -> ReplicaCacheEntry:
    return ReplicaCacheEntry(
        paste_text=paste_text,
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
        # The "lost the race" contract: a second create against the same
        # key returns the FIRST entry, not the loser's value, and does
        # not overwrite it. The on-Firestore version depends on this.
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

    def test_create_with_null_url_then_set_url_upgrades(self):
        # The cache-resilience contract: a partial entry (paste_text
        # cached, pokepaste_url=None) gets upgraded with the URL once a
        # later mint succeeds. The replica-OCR confirm gate writes the
        # partial entry first; the mint then either fills the URL or
        # leaves it null for the next /add-team to retry.
        store = InMemoryReplicaCacheStore()
        store.create("AAAA111122", _entry(url=None))
        upgraded = store.set_url("AAAA111122", "https://pokepast.es/new")
        assert upgraded.pokepaste_url == "https://pokepast.es/new"
        # The other fields are preserved exactly.
        assert upgraded.paste_text.startswith("Pikachu")
        assert store.get("AAAA111122").pokepaste_url == "https://pokepast.es/new"

    def test_set_url_raises_when_entry_missing(self):
        # set_url is for upgrading an existing entry, not for creating
        # one. If callers reach this path with a missing entry, that's
        # a logic bug — surface it loudly.
        store = InMemoryReplicaCacheStore()
        with pytest.raises(LookupError):
            store.set_url("AAAA111122", "https://pokepast.es/abc")


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

    Methods exercised by the store:
    - `.get()` for reads
    - `.create(payload)` for transactional fail-if-exists writes
    - `.set(payload, merge=True)` for the set_url upgrade
    """

    id: str
    _docs: dict[str, dict]
    create_calls: list[dict] = field(default_factory=list)
    set_calls: list[tuple[dict, bool]] = field(default_factory=list)

    def get(self) -> _FakeSnap:
        return _FakeSnap(id=self.id, _data=self._docs.get(self.id))

    def create(self, payload: dict) -> None:
        self.create_calls.append(dict(payload))
        if self.id in self._docs:
            raise gax_exceptions.AlreadyExists(f"Document {self.id} already exists")
        self._docs[self.id] = dict(payload)

    def set(self, payload: dict, *, merge: bool = False) -> None:
        self.set_calls.append((dict(payload), merge))
        existing = dict(self._docs.get(self.id, {})) if merge else {}
        existing.update(payload)
        self._docs[self.id] = existing


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


def _full_doc(
    *,
    paste_text: str = "Pikachu @ Light Ball\nAbility: Static\n- Volt Tackle",
    url: str | None = "https://pokepast.es/abc123",
    user_id: str = "111",
    guild_id: str = "222",
) -> dict:
    """Build a well-formed Firestore doc payload (mirroring `_serialize`'s
    output) for tests that need to seed the fake client."""
    return {
        "paste_text": paste_text,
        "pokepaste_url": url,
        "species": ["Pikachu"] * 6,
        "created_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
        "created_by_user_id": user_id,
        "created_by_guild_id": guild_id,
    }


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
        # Cache warm: next get returns the entry without re-reading
        # Firestore. We test this by mutating the underlying _docs dict
        # after the write; a re-fetched value would observe the
        # mutation, a cached value would not.
        client._docs["AAAA111122"] = _full_doc(url="https://pokepast.es/other")
        assert store.get("AAAA111122") == entry

    def test_create_on_existing_returns_winner(self):
        # Race-loser path: another writer beat us to this doc. The
        # store catches AlreadyExists, re-reads, and returns the
        # winner's entry rather than raising into the caller.
        winner_payload = _full_doc(url="https://pokepast.es/winner")
        client = _FakeFirestoreClient({"AAAA111122": winner_payload})
        store = FirestoreReplicaCacheStore(client)
        loser = _entry(url="https://pokepast.es/loser")
        returned = store.create("AAAA111122", loser)
        assert returned.pokepaste_url == "https://pokepast.es/winner"

    def test_get_parses_string_snowflakes(self):
        # The store serializes user/guild IDs as strings to dodge
        # Firestore's silent int->float coercion. The parser reads
        # them back as ints so callers don't see surprise floats.
        client = _FakeFirestoreClient(
            {
                "AAAA111122": _full_doc(
                    user_id="987654321098765432",
                    guild_id="123456789012345678",
                )
            }
        )
        store = FirestoreReplicaCacheStore(client)
        cached = store.get("AAAA111122")
        assert cached is not None
        assert cached.created_by_user_id == 987654321098765432
        assert cached.created_by_guild_id == 123456789012345678

    def test_get_skips_doc_missing_paste_text(self, caplog):
        # paste_text is required — a doc lacking it can't drive a
        # mint retry (the whole point of caching), so it's not useful.
        bad = _full_doc()
        del bad["paste_text"]
        client = _FakeFirestoreClient({"AAAA111122": bad})
        store = FirestoreReplicaCacheStore(client)
        with caplog.at_level("WARNING"):
            assert store.get("AAAA111122") is None
        assert any("paste_text" in r.message for r in caplog.records)

    def test_get_skips_doc_with_malformed_url(self, caplog):
        # pokepaste_url is OPTIONAL (null means "upload hasn't happened
        # yet") but if present must be a non-empty string. Numbers,
        # empty strings, etc. drop the entry.
        client = _FakeFirestoreClient({"AAAA111122": _full_doc(url=42)})
        store = FirestoreReplicaCacheStore(client)
        with caplog.at_level("WARNING"):
            assert store.get("AAAA111122") is None
        assert any("pokepaste_url" in r.message for r in caplog.records)

    def test_get_accepts_null_url_as_partial_entry(self):
        # The whole point of the cache-resilience design: a partial
        # entry (paste_text set, pokepaste_url=None) parses cleanly and
        # is returned to the caller, which then retries the mint.
        client = _FakeFirestoreClient({"AAAA111122": _full_doc(url=None)})
        store = FirestoreReplicaCacheStore(client)
        cached = store.get("AAAA111122")
        assert cached is not None
        assert cached.pokepaste_url is None
        assert cached.paste_text.startswith("Pikachu")

    def test_get_skips_doc_missing_created_at(self, caplog):
        # Docs missing the timestamp drop with a WARNING — defense
        # against hand-edits / schema drift.
        bad = _full_doc()
        del bad["created_at"]
        client = _FakeFirestoreClient({"AAAA111122": bad})
        store = FirestoreReplicaCacheStore(client)
        with caplog.at_level("WARNING"):
            assert store.get("AAAA111122") is None
        assert any("created_at" in r.message for r in caplog.records)

    def test_get_skips_doc_missing_audit_snowflake(self, caplog):
        # Missing or malformed audit snowflake drops the entry rather
        # than emitting `0` as the user_id / guild_id, which would
        # surface bogus values in the cache inspector.
        bad = _full_doc()
        del bad["created_by_guild_id"]
        client = _FakeFirestoreClient({"AAAA111122": bad})
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
        assert payload["paste_text"].startswith("Pikachu")
        # Round-trip: a fresh store against the same fake client reads
        # the same int values back.
        fresh = FirestoreReplicaCacheStore(client)
        cached = fresh.get("AAAA111122")
        assert cached is not None
        assert cached.created_by_user_id == 999
        assert cached.created_by_guild_id == 888

    def test_get_caches_after_first_firestore_read(self):
        # A cache-miss read populates the in-memory dict so the next
        # get() doesn't pay Firestore-read cost. We assert this by
        # mutating the backing dict after the first read; a re-fetched
        # value would observe the mutation, a cached value would not.
        client = _FakeFirestoreClient(
            {"AAAA111122": _full_doc(url="https://pokepast.es/v1")}
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

    def test_set_url_upgrades_partial_entry(self):
        # The cache-resilience flow: a previous /add-team wrote the
        # paste with URL=None (mint failed); a later /add-team retries
        # the mint and calls set_url to upgrade the entry.
        client = _FakeFirestoreClient()
        store = FirestoreReplicaCacheStore(client)
        store.create("AAAA111122", _entry(url=None))
        upgraded = store.set_url("AAAA111122", "https://pokepast.es/minted")
        assert upgraded.pokepaste_url == "https://pokepast.es/minted"
        # The Firestore-side call uses set(merge=True) so only the URL
        # field is written — paste_text, species, etc. are preserved.
        ref = client._collection.doc_refs["AAAA111122"]
        assert ref.set_calls == [
            ({"pokepaste_url": "https://pokepast.es/minted"}, True)
        ]
        # In-memory cache is upgraded too — next get returns the new URL
        # without re-reading Firestore.
        client._docs["AAAA111122"]["pokepaste_url"] = "https://pokepast.es/other"
        assert store.get("AAAA111122").pokepaste_url == "https://pokepast.es/minted"

    def test_set_url_rereads_when_local_cache_cold(self):
        # If a process restarts between create and set_url, the in-
        # memory cache is cold but Firestore still has the partial
        # entry. set_url must re-read it before returning so the caller
        # gets a fully-typed `ReplicaCacheEntry`, not a fragment.
        client = _FakeFirestoreClient({"AAAA111122": _full_doc(url=None)})
        store = FirestoreReplicaCacheStore(client)
        # NOTE: deliberately did NOT pre-warm the cache via .get()
        upgraded = store.set_url("AAAA111122", "https://pokepast.es/minted")
        assert upgraded.pokepaste_url == "https://pokepast.es/minted"
        assert upgraded.paste_text.startswith("Pikachu")

    def test_set_url_raises_when_entry_missing(self):
        client = _FakeFirestoreClient()
        store = FirestoreReplicaCacheStore(client)
        with pytest.raises(LookupError):
            store.set_url("AAAA111122", "https://pokepast.es/abc")
