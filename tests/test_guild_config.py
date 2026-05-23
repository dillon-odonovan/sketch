"""Tests for the guild config store.

`StaticGuildConfigStore` covers the in-memory contract every command handler
depends on. `FirestoreGuildConfigStore` is tested with a fake Firestore
client (a stand-in for `google.cloud.firestore.Client`) so the suite never
needs network or credentials.
"""

from __future__ import annotations

from dataclasses import dataclass

from guild_config import (
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


# --- Firestore-backed store -------------------------------------------------


@dataclass
class _FakeDoc:
    id: str
    data: dict | None

    def to_dict(self) -> dict | None:
        return self.data


class _FakeCollection:
    def __init__(self, docs: list[_FakeDoc]) -> None:
        self._docs = docs

    def stream(self):
        return iter(self._docs)


class _FakeFirestoreClient:
    """Minimal stand-in for google.cloud.firestore.Client.

    Returns a fixed list of fake documents from `.collection(name).stream()`.
    The store doesn't use any other methods today, so we don't bother with a
    fuller protocol.
    """

    def __init__(self, docs: list[_FakeDoc]) -> None:
        self._docs = docs
        self.requested_collections: list[str] = []

    def collection(self, name: str) -> _FakeCollection:
        self.requested_collections.append(name)
        return _FakeCollection(self._docs)


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
