"""Tests for the guild config store.

`StaticGuildConfigStore` covers the in-memory contract every command handler
depends on. `FirestoreGuildConfigStore` is tested with a fake Firestore
client (a stand-in for `google.cloud.firestore.Client`) so the suite never
needs network or credentials.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from google.cloud import firestore as _firestore

from sketch.storage.guild_config import (
    GUILD_CONFIGS_COLLECTION,
    FirestoreGuildConfigStore,
    GuildConfig,
    StaticGuildConfigStore,
)


class TestStaticGuildConfigStore:
    def test_get_hit(self):
        store = StaticGuildConfigStore({42: GuildConfig(spreadsheet_id="sheet-A")})
        assert store.get(42) == GuildConfig(spreadsheet_id="sheet-A")

    def test_get_miss(self):
        store = StaticGuildConfigStore({42: GuildConfig(spreadsheet_id="sheet-A")})
        assert store.get(99) is None

    def test_configured_guild_ids(self):
        store = StaticGuildConfigStore(
            {
                1: GuildConfig(spreadsheet_id="A"),
                2: GuildConfig(spreadsheet_id="B"),
            }
        )
        assert sorted(store.configured_guild_ids()) == [1, 2]

    def test_empty_store(self):
        store = StaticGuildConfigStore({})
        assert store.get(1) is None
        assert store.configured_guild_ids() == []

    def test_default_constructor_is_empty(self):
        # Constructor accepts no argument for ergonomic use in admin-command
        # tests that build state up call by call.
        store = StaticGuildConfigStore()
        assert store.configured_guild_ids() == []

    def test_set_spreadsheet_id_creates_new_guild(self):
        store = StaticGuildConfigStore()
        new_cfg = store.set_spreadsheet_id(42, "sheet-A")
        assert new_cfg == GuildConfig(spreadsheet_id="sheet-A")
        assert store.get(42) == GuildConfig(spreadsheet_id="sheet-A")

    def test_set_spreadsheet_id_overwrites_preserving_broadcast(self):
        # `/register-sheet` on an already-configured guild (e.g. they're
        # migrating to a new sheet) must NOT silently wipe a working
        # broadcast channel.
        store = StaticGuildConfigStore(
            {42: GuildConfig(spreadsheet_id="old", broadcast_channel_id=999)}
        )
        new_cfg = store.set_spreadsheet_id(42, "new")
        assert new_cfg == GuildConfig(spreadsheet_id="new", broadcast_channel_id=999)

    def test_set_broadcast_channel_id_updates_existing(self):
        store = StaticGuildConfigStore({42: GuildConfig(spreadsheet_id="sheet-A")})
        new_cfg = store.set_broadcast_channel_id(42, 999)
        assert new_cfg == GuildConfig(
            spreadsheet_id="sheet-A", broadcast_channel_id=999
        )

    def test_set_broadcast_channel_id_raises_for_unconfigured_guild(self):
        # Defense in depth — commands.py guards with `store.get` first, but
        # the store still refuses to land a broadcast channel without a
        # spreadsheet, since `/add-team` wouldn't fire and the channel
        # would just be dead state.
        store = StaticGuildConfigStore()
        with pytest.raises(LookupError):
            store.set_broadcast_channel_id(42, 999)

    def test_clear_broadcast_channel_id_drops_the_channel(self):
        store = StaticGuildConfigStore(
            {42: GuildConfig(spreadsheet_id="sheet-A", broadcast_channel_id=999)}
        )
        new_cfg = store.clear_broadcast_channel_id(42)
        assert new_cfg == GuildConfig(
            spreadsheet_id="sheet-A", broadcast_channel_id=None
        )

    def test_clear_broadcast_channel_id_is_idempotent(self):
        store = StaticGuildConfigStore({42: GuildConfig(spreadsheet_id="sheet-A")})
        new_cfg = store.clear_broadcast_channel_id(42)
        assert new_cfg.broadcast_channel_id is None

    def test_clear_broadcast_channel_id_raises_for_unconfigured_guild(self):
        store = StaticGuildConfigStore()
        with pytest.raises(LookupError):
            store.clear_broadcast_channel_id(42)


# --- Firestore-backed store -------------------------------------------------


@dataclass
class _FakeDoc:
    id: str
    data: dict | None

    def to_dict(self) -> dict | None:
        return self.data


@dataclass
class _FakeDocRef:
    """Stand-in for a Firestore DocumentReference.

    `set(payload, merge=True)` is the only mutation the store performs.
    We model it the way real Firestore behaves with merge=True: missing
    keys are preserved, present keys overwrite, and `firestore.DELETE_FIELD`
    sentinels pop the key entirely. The reconstructed dict feeds the
    parent collection's doc list so subsequent `.stream()` iterations
    reflect the write — i.e., a fresh `FirestoreGuildConfigStore` built
    against the same fake client would observe the write.
    """

    id: str
    _docs: list[_FakeDoc]
    set_calls: list[tuple[dict, bool]] = field(default_factory=list)

    def set(self, payload: dict, *, merge: bool = False) -> None:
        self.set_calls.append((dict(payload), merge))
        existing = next((d for d in self._docs if d.id == self.id), None)
        if existing is None:
            existing = _FakeDoc(id=self.id, data={})
            self._docs.append(existing)
        data = dict(existing.data or {}) if merge else {}
        for k, v in payload.items():
            if v is _firestore.DELETE_FIELD:
                data.pop(k, None)
            else:
                data[k] = v
        existing.data = data


class _FakeCollection:
    def __init__(self, docs: list[_FakeDoc]) -> None:
        self._docs = docs
        self.doc_refs: dict[str, _FakeDocRef] = {}

    def stream(self):
        return iter(self._docs)

    def document(self, doc_id: str) -> _FakeDocRef:
        # Return the same DocumentReference per id so tests can inspect
        # `.set_calls` after the fact, matching how the real Firestore SDK
        # returns equivalent refs across `.document(id)` calls.
        ref = self.doc_refs.get(doc_id)
        if ref is None:
            ref = _FakeDocRef(id=doc_id, _docs=self._docs)
            self.doc_refs[doc_id] = ref
        return ref


class _FakeFirestoreClient:
    """Minimal stand-in for google.cloud.firestore.Client.

    Supports `.collection(name).stream()` for the load path and
    `.collection(name).document(id).set(payload, merge=True/False)` for the
    write path. A single underlying `_docs` list backs both so writes
    observed by a fresh store rebuild match what the in-memory map already
    has after the write-through.
    """

    def __init__(self, docs: list[_FakeDoc]) -> None:
        self._docs = docs
        self.requested_collections: list[str] = []
        self._collection: _FakeCollection | None = None

    def collection(self, name: str) -> _FakeCollection:
        self.requested_collections.append(name)
        # Reuse the same _FakeCollection across calls so `.document(id)`
        # returns the same _FakeDocRef on the second call and we can inspect
        # `.set_calls` from the test.
        if self._collection is None:
            self._collection = _FakeCollection(self._docs)
        return self._collection


def _client(*docs: _FakeDoc) -> _FakeFirestoreClient:
    return _FakeFirestoreClient(list(docs))


class TestFirestoreGuildConfigStore:
    def test_reads_from_guild_configs_collection(self):
        client = _client()
        FirestoreGuildConfigStore(client)
        assert client.requested_collections == [GUILD_CONFIGS_COLLECTION]

    def test_loads_spreadsheet_only(self):
        client = _client(
            _FakeDoc(id="123", data={"spreadsheet_id": "sheet-A"}),
        )
        store = FirestoreGuildConfigStore(client)
        assert store.get(123) == GuildConfig(spreadsheet_id="sheet-A")

    def test_loads_spreadsheet_and_broadcast_channel(self):
        client = _client(
            _FakeDoc(
                id="123",
                data={
                    "spreadsheet_id": "sheet-A",
                    "broadcast_channel_id": "987654321098765432",
                },
            ),
        )
        store = FirestoreGuildConfigStore(client)
        assert store.get(123) == GuildConfig(
            spreadsheet_id="sheet-A",
            broadcast_channel_id=987654321098765432,
        )

    def test_loads_many_guilds(self):
        client = _client(
            _FakeDoc(id="111", data={"spreadsheet_id": "A"}),
            _FakeDoc(id="222", data={"spreadsheet_id": "B"}),
            _FakeDoc(id="333", data={"spreadsheet_id": "C"}),
        )
        store = FirestoreGuildConfigStore(client)
        assert sorted(store.configured_guild_ids()) == [111, 222, 333]

    def test_empty_collection_is_fine(self):
        # An empty Firestore is the default at fresh provision time. The bot
        # must boot — it just refuses every command until guilds are seeded.
        store = FirestoreGuildConfigStore(_client())
        assert store.configured_guild_ids() == []
        assert store.get(1) is None

    def test_get_miss(self):
        client = _client(
            _FakeDoc(id="111", data={"spreadsheet_id": "A"}),
        )
        store = FirestoreGuildConfigStore(client)
        assert store.get(999) is None

    def test_doc_missing_spreadsheet_id_is_skipped(self, caplog):
        client = _client(
            _FakeDoc(id="123", data={"broadcast_channel_id": "987"}),
            _FakeDoc(id="456", data={"spreadsheet_id": "sheet-B"}),
        )
        with caplog.at_level("WARNING"):
            store = FirestoreGuildConfigStore(client)
        assert store.get(123) is None
        assert store.get(456) == GuildConfig(spreadsheet_id="sheet-B")
        assert any(
            "missing or non-string spreadsheet_id" in r.message for r in caplog.records
        )

    def test_doc_non_string_spreadsheet_id_is_skipped(self, caplog):
        client = _client(
            _FakeDoc(id="123", data={"spreadsheet_id": 12345}),
        )
        with caplog.at_level("WARNING"):
            store = FirestoreGuildConfigStore(client)
        assert store.get(123) is None

    def test_doc_empty_payload_is_skipped(self, caplog):
        client = _client(_FakeDoc(id="123", data=None))
        with caplog.at_level("WARNING"):
            store = FirestoreGuildConfigStore(client)
        assert store.get(123) is None

    def test_non_numeric_doc_id_is_skipped(self, caplog):
        client = _client(
            _FakeDoc(id="not-a-snowflake", data={"spreadsheet_id": "A"}),
            _FakeDoc(id="456", data={"spreadsheet_id": "B"}),
        )
        with caplog.at_level("WARNING"):
            store = FirestoreGuildConfigStore(client)
        assert store.configured_guild_ids() == [456]
        assert any("not a numeric guild_id" in r.message for r in caplog.records)

    def test_bad_broadcast_channel_id_drops_only_broadcast(self, caplog):
        # A malformed broadcast_channel_id must NOT drop the whole guild —
        # the bot can still read/write the sheet, broadcasts just stay off
        # until the value is fixed.
        client = _client(
            _FakeDoc(
                id="123",
                data={
                    "spreadsheet_id": "sheet-A",
                    "broadcast_channel_id": "not-a-snowflake",
                },
            ),
        )
        with caplog.at_level("WARNING"):
            store = FirestoreGuildConfigStore(client)
        cfg = store.get(123)
        assert cfg == GuildConfig(spreadsheet_id="sheet-A", broadcast_channel_id=None)
        assert any("broadcast_channel_id" in r.message for r in caplog.records)

    def test_broadcast_channel_id_absent(self):
        client = _client(_FakeDoc(id="123", data={"spreadsheet_id": "sheet-A"}))
        store = FirestoreGuildConfigStore(client)
        assert store.get(123) == GuildConfig(spreadsheet_id="sheet-A")

    def test_broadcast_channel_id_explicit_null(self):
        client = _client(
            _FakeDoc(
                id="123",
                data={"spreadsheet_id": "sheet-A", "broadcast_channel_id": None},
            ),
        )
        store = FirestoreGuildConfigStore(client)
        assert store.get(123) == GuildConfig(spreadsheet_id="sheet-A")


class TestFirestoreGuildConfigStoreWrites:
    """Cover the write paths backing /register-sheet and the broadcast-channel
    admin commands. Each test asserts both:

    1. The in-memory map updates immediately (no restart needed — the whole
       point of the write-through design), and
    2. The fake Firestore doc gets the right payload + merge flag so the
       persistence shape matches what `bin/seed_guilds.py` produces on the
       same collection. If these two diverge, a bot restart would mutate
       the in-memory state.
    """

    def test_set_spreadsheet_id_writes_through_for_new_guild(self):
        client = _client()
        store = FirestoreGuildConfigStore(client)
        new_cfg = store.set_spreadsheet_id(42, "sheet-A")
        # In-memory: routable immediately.
        assert store.get(42) == GuildConfig(spreadsheet_id="sheet-A")
        assert new_cfg == GuildConfig(spreadsheet_id="sheet-A")
        # Firestore: doc set with merge=True so a pre-existing broadcast
        # channel on the same doc wouldn't be wiped.
        collection = client._collection
        assert collection is not None
        doc_ref = collection.doc_refs["42"]
        assert doc_ref.set_calls == [({"spreadsheet_id": "sheet-A"}, True)]

    def test_set_spreadsheet_id_preserves_existing_broadcast_in_memory(self):
        # Migration scenario: guild has a working broadcast channel, admin
        # registers a new sheet. broadcast_channel_id must survive.
        client = _client(
            _FakeDoc(
                id="42",
                data={
                    "spreadsheet_id": "old",
                    "broadcast_channel_id": "999",
                },
            ),
        )
        store = FirestoreGuildConfigStore(client)
        new_cfg = store.set_spreadsheet_id(42, "new")
        assert new_cfg == GuildConfig(spreadsheet_id="new", broadcast_channel_id=999)
        assert store.get(42) == new_cfg

    def test_set_spreadsheet_id_uses_string_doc_id(self):
        # Firestore doc IDs are strings; the store accepts int guild_ids and
        # must stringify them. If this regresses, a guild_id=42 write and a
        # guild_id="42" hand-edit would land on different docs.
        client = _client()
        store = FirestoreGuildConfigStore(client)
        store.set_spreadsheet_id(42, "sheet-A")
        assert "42" in client._collection.doc_refs

    def test_set_broadcast_channel_id_writes_string_form(self):
        # bin/seed_guilds.py stores broadcast_channel_id as a string (per
        # _parse_doc's validation), so the bot's write path must match. If
        # they diverge, the bot restart after a slash-command set would log
        # a "not a snowflake" warning and drop the channel.
        client = _client(_FakeDoc(id="42", data={"spreadsheet_id": "sheet-A"}))
        store = FirestoreGuildConfigStore(client)
        new_cfg = store.set_broadcast_channel_id(42, 999)
        assert new_cfg == GuildConfig(
            spreadsheet_id="sheet-A", broadcast_channel_id=999
        )
        doc_ref = client._collection.doc_refs["42"]
        assert doc_ref.set_calls == [({"broadcast_channel_id": "999"}, True)]

    def test_set_broadcast_channel_id_refuses_unconfigured_guild(self):
        client = _client()
        store = FirestoreGuildConfigStore(client)
        with pytest.raises(LookupError):
            store.set_broadcast_channel_id(42, 999)
        # No Firestore write should have happened either — otherwise an
        # orphaned broadcast_channel_id doc would survive a restart and
        # `_parse_doc` would warn and discard it on reload.
        assert client._collection is None or "42" not in (
            client._collection.doc_refs if client._collection else {}
        )

    def test_clear_broadcast_channel_id_uses_delete_field_sentinel(self):
        # merge=True with `firestore.DELETE_FIELD` removes the field rather
        # than setting it to None — leaving the field absent so future
        # `_parse_doc` runs see "broadcast_channel_id absent" instead of
        # the explicit-null case.
        client = _client(
            _FakeDoc(
                id="42",
                data={
                    "spreadsheet_id": "sheet-A",
                    "broadcast_channel_id": "999",
                },
            ),
        )
        store = FirestoreGuildConfigStore(client)
        new_cfg = store.clear_broadcast_channel_id(42)
        assert new_cfg == GuildConfig(spreadsheet_id="sheet-A")
        doc_ref = client._collection.doc_refs["42"]
        # The fake collapses DELETE_FIELD into an absent key on the doc
        # data — so a fresh store rebuild against the same fake client
        # would parse this as "no broadcast channel" rather than a malformed
        # explicit value.
        rebuilt = FirestoreGuildConfigStore(client)
        assert rebuilt.get(42) == GuildConfig(spreadsheet_id="sheet-A")
        # And the on-the-wire payload uses the sentinel, not a None.
        assert len(doc_ref.set_calls) == 1
        payload, merge = doc_ref.set_calls[0]
        assert merge is True
        assert payload["broadcast_channel_id"] is _firestore.DELETE_FIELD

    def test_clear_broadcast_channel_id_refuses_unconfigured_guild(self):
        client = _client()
        store = FirestoreGuildConfigStore(client)
        with pytest.raises(LookupError):
            store.clear_broadcast_channel_id(42)
