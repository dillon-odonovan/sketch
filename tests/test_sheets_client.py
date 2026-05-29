"""Tests for `SheetsClient.get_search_snapshot` and its TTL cache.

We stub the Google API service object rather than spinning up the real
client — `_run` already offloads to a thread, so the underlying call only
needs to be a plain Python callable with the right shape.

Time is injected via the `now` kwarg on `SheetsClient.__init__`. We avoid
monkeypatching `time.monotonic` so concurrent tests don't perturb each
other.
"""

from unittest.mock import patch

import pytest

from sketch import config
from sketch.search.text_search import DescriptionIndex
from sketch.storage.guild_config import GuildConfig, StaticGuildConfigStore
from sketch.storage.sheets_client import (
    RowShiftedError,
    SearchSnapshot,
    SheetsClient,
    SheetsClientRegistry,
    TeamNotFoundError,
    TeamRow,
)


class _FakeRequest:
    """Stands in for a googleapiclient request object.

    The real flow is `service.spreadsheets().values().get(...).execute(...)`.
    `_run` wraps just the `.execute` callable, so this fake only needs to
    return a deterministic payload from `execute` and count the calls.
    """

    def __init__(self, parent: "_FakeService") -> None:
        self._parent = parent

    def execute(self, num_retries: int = 0) -> dict:
        self._parent.execute_count += 1
        return self._parent.response


class _FakeService:
    """Minimal stand-in for the Google Sheets API service object.

    Only the spreadsheets().values().get(...).execute pathway is implemented —
    that's all `search_rows` exercises. Other methods would raise
    AttributeError if accidentally used, which is the test signal we want.
    """

    def __init__(self, response: dict) -> None:
        self.response = response
        self.execute_count = 0
        self.get_calls: list[dict] = []

    def spreadsheets(self) -> "_FakeService":
        return self

    def values(self) -> "_FakeService":
        return self

    def get(self, **kwargs) -> _FakeRequest:
        self.get_calls.append(kwargs)
        return _FakeRequest(self)


class _Clock:
    """Mutable monotonic clock for cache tests. `advance` moves time forward."""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


# A two-row response shape mirroring what FORMATTED_VALUE returns from the
# !A{FIRST_DATA_ROW}:M range. Columns: A=url, B-F=misc, G=description,
# H-M=species. Padded to 13 columns so `search_rows` indexes safely.
_SAMPLE_RESPONSE = {
    "values": [
        [
            "https://pokepast.es/aaaa1111",
            "",
            "",
            "",
            "",
            "",
            "jsmithvgc — Calyrex-S balance",
            "Calyrex-Shadow",
            "Urshifu",
            "Amoonguss",
            "Rillaboom",
            "Incineroar",
            "Tornadus",
        ],
        [
            "https://pokepast.es/bbbb2222",
            "",
            "",
            "",
            "",
            "",
            "alice — Toxapex sun team",
            "Charizard",
            "Toxapex",
            "Tyranitar",
            "Garchomp",
            "Tapu Lele",
            "Greninja",
        ],
    ]
}


def _make_client(
    response: dict | None = None,
    ttl: float = 30.0,
    start_time: float = 1000.0,
) -> tuple[SheetsClient, _FakeService, _Clock]:
    service = _FakeService(response if response is not None else _SAMPLE_RESPONSE)
    clock = _Clock(start=start_time)
    client = SheetsClient(
        service,
        "test-spreadsheet-id",
        search_cache_ttl=ttl,
        now=clock.now,
    )
    return client, service, clock


class TestSnapshotShape:
    async def test_returns_snapshot_with_rows_and_index(self):
        client, service, _ = _make_client()
        snap = await client.get_search_snapshot("AnySheet")
        assert isinstance(snap, SearchSnapshot)
        assert len(snap.rows) == 2
        assert all(isinstance(r, TeamRow) for r in snap.rows)
        assert isinstance(snap.desc_index, DescriptionIndex)

    async def test_index_reflects_row_descriptions(self):
        # The description index must be built from the fetched rows — not
        # empty, not from some other corpus. Probe it with a query that
        # depends on the actual descriptions above.
        client, _, _ = _make_client()
        snap = await client.get_search_snapshot("AnySheet")
        # Row 0 has "jsmithvgc"; row 1 has "toxapex" (lowercased after tokenize).
        assert snap.desc_index.match("jsmithvgc") == {0}
        assert snap.desc_index.match("toxapex") == {1}

    async def test_match_rows_convenience_returns_in_sheet_order(self):
        client, _, _ = _make_client()
        snap = await client.get_search_snapshot("AnySheet")
        # "team" appears only in row 1 ("sun team"); "balance" only in row 0.
        assert [r.url for r in snap.match_rows("team")] == [
            "https://pokepast.es/bbbb2222"
        ]
        assert [r.url for r in snap.match_rows("balance")] == [
            "https://pokepast.es/aaaa1111"
        ]


class TestTTLCache:
    async def test_two_rapid_calls_share_one_fetch(self):
        client, service, _ = _make_client(ttl=30.0)
        snap1 = await client.get_search_snapshot("MySheet")
        snap2 = await client.get_search_snapshot("MySheet")
        # One Sheets fetch even though we queried twice.
        assert service.execute_count == 1
        # Identity: cache returns the same SearchSnapshot object so callers
        # can reuse its index without rebuilding.
        assert snap1 is snap2

    async def test_call_just_before_ttl_expires_uses_cache(self):
        client, service, clock = _make_client(ttl=30.0)
        await client.get_search_snapshot("MySheet")
        clock.advance(29.9)
        await client.get_search_snapshot("MySheet")
        assert service.execute_count == 1

    async def test_call_after_ttl_expires_refetches(self):
        client, service, clock = _make_client(ttl=30.0)
        snap1 = await client.get_search_snapshot("MySheet")
        clock.advance(30.1)
        snap2 = await client.get_search_snapshot("MySheet")
        assert service.execute_count == 2
        # New fetch produces a new SearchSnapshot object (identity differs).
        assert snap1 is not snap2

    async def test_separate_sheets_dont_share_cache_entries(self):
        client, service, _ = _make_client(ttl=30.0)
        await client.get_search_snapshot("SheetA")
        await client.get_search_snapshot("SheetB")
        # Different cache keys → two fetches even within the TTL window.
        assert service.execute_count == 2

    async def test_failure_is_not_cached(self):
        # If the Sheets fetch raises, the next call should retry rather than
        # serving a "failed" sentinel from cache.
        client, service, _ = _make_client(ttl=30.0)

        # Replace execute with a one-shot raiser, then restore the normal one.
        original_response = service.response

        def _raising_execute(num_retries: int = 0):
            raise RuntimeError("simulated Sheets outage")

        first_request = _FakeRequest(service)
        first_request.execute = _raising_execute  # type: ignore[method-assign]
        # Patch `get` so the FIRST call returns the raising request; subsequent
        # calls fall back to the normal flow.
        original_get = service.get
        calls = {"n": 0}

        def _flaky_get(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return first_request
            return original_get(**kwargs)

        service.get = _flaky_get  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="simulated Sheets outage"):
            await client.get_search_snapshot("MySheet")

        # Reset response to canonical sample (already is) and try again.
        service.response = original_response
        snap = await client.get_search_snapshot("MySheet")
        assert isinstance(snap, SearchSnapshot)
        # The failing attempt didn't increment execute_count (raised before
        # incrementing), so this is the first successful execute.
        assert service.execute_count == 1


class TestInvalidate:
    async def test_invalidate_forces_refetch_on_next_get(self):
        # The primary use case: /add-team invalidates after species settle,
        # the next /search-teams must rebuild the snapshot from a fresh
        # Sheets fetch even though the TTL hasn't expired.
        client, service, clock = _make_client(ttl=300.0)
        snap1 = await client.get_search_snapshot("MySheet")
        assert service.execute_count == 1

        client.invalidate_snapshot("MySheet")

        snap2 = await client.get_search_snapshot("MySheet")
        assert service.execute_count == 2
        assert snap1 is not snap2

    async def test_invalidate_noop_when_no_entry(self):
        # First /add-team after bot start may invalidate before anyone has
        # called /search-teams — should be a clean noop, not an error.
        client, service, _ = _make_client(ttl=300.0)
        client.invalidate_snapshot("NeverFetched")  # must not raise
        # And the cache stays cold afterwards: a subsequent get is the
        # first execute, not the second.
        await client.get_search_snapshot("NeverFetched")
        assert service.execute_count == 1

    async def test_invalidate_is_scoped_to_one_sheet_name(self):
        client, service, _ = _make_client(ttl=300.0)
        await client.get_search_snapshot("SheetA")
        await client.get_search_snapshot("SheetB")
        assert service.execute_count == 2

        # Invalidate only SheetA; SheetB should remain cached.
        client.invalidate_snapshot("SheetA")

        await client.get_search_snapshot("SheetA")  # refetches
        await client.get_search_snapshot("SheetB")  # still cached
        assert service.execute_count == 3


class TestDefaults:
    async def test_default_ttl_pulls_from_config(self):
        # When `search_cache_ttl` is omitted, the client should consult
        # `config.SEARCH_CACHE_TTL_SECONDS`. This guards against the default
        # silently regressing to e.g. 0 (no caching) on a refactor.
        from sketch import config

        service = _FakeService(_SAMPLE_RESPONSE)
        client = SheetsClient(service, "test-spreadsheet-id")
        assert client._search_cache_ttl == config.SEARCH_CACHE_TTL_SECONDS


class TestListTabNames:
    """`/register-sheet` probes a candidate spreadsheet with this before
    persisting, so the shape of the response (list of titles, exception on
    access denied / missing sheet) is part of the public API."""

    async def test_returns_titles_from_response(self):
        service = _FakeService(
            {
                "sheets": [
                    {"properties": {"title": "Reg M-A", "sheetId": 0}},
                    {"properties": {"title": "DEX", "sheetId": 1}},
                ]
            }
        )
        client = SheetsClient(service, "test-spreadsheet-id")
        assert await client.list_tab_names() == ["Reg M-A", "DEX"]

    async def test_skips_malformed_entries(self):
        # Defensive: if Google ever returns a sheets[] entry without
        # properties.title (or partials in a future API shape), don't
        # KeyError the whole probe — just drop that entry.
        service = _FakeService(
            {
                "sheets": [
                    {"properties": {"title": "Reg M-A"}},
                    {"properties": {}},
                    {},
                ]
            }
        )
        client = SheetsClient(service, "test-spreadsheet-id")
        assert await client.list_tab_names() == ["Reg M-A"]

    async def test_propagates_exceptions(self):
        # The probe lets HttpError / transport errors escape so commands.py
        # can translate to an actionable refusal ("sheet not shared with
        # the bot's service account"). Wrapping or swallowing here would
        # collapse access-denied and not-found into the same generic.
        service = _FakeService({})

        # `*args, **kwargs` so the patched function tolerates being called
        # both as a bound method (where Python prepends `self`) and as the
        # `execute(num_retries=N)` shape `_run` invokes.
        def _raise(*args, **kwargs):
            raise RuntimeError("403 Forbidden")

        with patch.object(_FakeRequest, "execute", _raise):
            client = SheetsClient(service, "test-spreadsheet-id")
            with pytest.raises(RuntimeError, match="403"):
                await client.list_tab_names()


class TestSheetsClientRegistry:
    """Cache-keyed-by-guild behavior, plus the invalidate hook used by
    `/register-sheet` to drop a client whose spreadsheet_id changed."""

    def _make_registry(self, store):
        # SheetsClientRegistry.__init__ wires up google.auth + the discovery
        # service. We don't want to touch the network — patch both and let
        # the registry hold a stub service. The stub's identity is what we
        # assert SheetsClients are built against.
        sentinel_service = object()
        with (
            patch("sketch.storage.sheets_client.google.auth.default") as auth_mock,
            patch("sketch.storage.sheets_client.build") as build_mock,
        ):
            auth_mock.return_value = (object(), None)
            build_mock.return_value = sentinel_service
            registry = SheetsClientRegistry(store)
        return registry, sentinel_service

    def test_get_returns_none_for_unconfigured_guild(self):
        store = StaticGuildConfigStore()
        registry, _ = self._make_registry(store)
        assert registry.get(99) is None

    def test_get_caches_per_guild(self):
        store = StaticGuildConfigStore({42: GuildConfig(spreadsheet_id="sheet-A")})
        registry, _ = self._make_registry(store)
        first = registry.get(42)
        second = registry.get(42)
        # Same instance both calls — building per-call would drop the
        # tab-id / DEX / snapshot caches on every slash command.
        assert first is second

    def test_invalidate_drops_cached_client(self):
        # Drives the /register-sheet path: store updates first, then the
        # registry is invalidated, then the next get rebuilds against the
        # fresh spreadsheet_id from the store.
        store = StaticGuildConfigStore({42: GuildConfig(spreadsheet_id="old")})
        registry, _ = self._make_registry(store)
        old_client = registry.get(42)
        assert old_client is not None
        assert old_client._spreadsheet_id == "old"

        store.set_spreadsheet_id(42, "new")
        registry.invalidate(42)

        new_client = registry.get(42)
        assert new_client is not None
        assert new_client is not old_client
        assert new_client._spreadsheet_id == "new"

    def test_invalidate_unconfigured_guild_is_noop(self):
        store = StaticGuildConfigStore()
        registry, _ = self._make_registry(store)
        registry.invalidate(99)  # must not raise even with empty cache

    def test_build_probe_client_does_not_pollute_cache(self):
        # /register-sheet probes a candidate ID before persisting — that
        # probe MUST NOT end up in `_clients`, or a failed probe would
        # leave the registry pinned to a sheet the bot can't read.
        store = StaticGuildConfigStore({42: GuildConfig(spreadsheet_id="real")})
        registry, _ = self._make_registry(store)
        probe = registry.build_probe_client("candidate-id")
        assert probe._spreadsheet_id == "candidate-id"
        # Real lookup still hits the cache and returns the configured
        # client, not the probe.
        real = registry.get(42)
        assert real is not None
        assert real._spreadsheet_id == "real"
        assert real is not probe


# -- find_row_by_url / find_row_by_replica -----------------------------------


class _RoutingService:
    """Programmable Sheets service stub.

    Routes each `values().get(range=...)` to a registered payload and records
    `batchUpdate` bodies so delete-flow assertions can inspect them. Keeps
    each test self-describing — the sample bank is built per-test rather
    than reused across the file's other suites.
    """

    def __init__(self) -> None:
        self.get_responses: dict[str, dict] = {}
        self.metadata_response: dict = {
            "sheets": [{"properties": {"title": "Reg M-A Sheet", "sheetId": 7}}]
        }
        self.batch_update_bodies: list[dict] = []
        self.get_calls: list[dict] = []

    def spreadsheets(self):
        return _RoutingSpreadsheets(self)


class _RoutingSpreadsheets:
    def __init__(self, parent: _RoutingService) -> None:
        self._parent = parent

    def values(self):
        return _RoutingValues(self._parent)

    def get(self, **kwargs):
        # Tab-id discovery: spreadsheets().get(fields="...sheetId...").
        return _CannedRequest(self._parent.metadata_response)

    def batchUpdate(self, *, spreadsheetId, body):
        self._parent.batch_update_bodies.append(body)
        return _CannedRequest({})


class _RoutingValues:
    def __init__(self, parent: _RoutingService) -> None:
        self._parent = parent

    def get(self, **kwargs):
        self._parent.get_calls.append(kwargs)
        rng = kwargs.get("range", "")
        payload = self._parent.get_responses.get(rng, {"values": []})
        return _CannedRequest(payload)


class _CannedRequest:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def execute(self, num_retries: int = 0):
        return self._payload


_SHEET = "Reg M-A Sheet"
_SCAN_RANGE = f"{_SHEET}!A{config.FIRST_DATA_ROW}:M"
# Range used by delete_row's CAS guard read (A:G covers URL + replica cols).
_CAS_RANGE = f"{_SHEET}!A{config.FIRST_DATA_ROW}:G{config.FIRST_DATA_ROW}"


def _bank_row(
    url: str = "https://pokepast.es/aaaa1111",
    replica: str = "QBXXWXL05U",
    description: str = "jsmithvgc — Calyrex-S balance",
    species: list[str] | None = None,
) -> list[str]:
    species = species or [
        "Calyrex-Shadow",
        "Urshifu",
        "Amoonguss",
        "Rillaboom",
        "Incineroar",
        "Tornadus",
    ]
    return [url, "", "", replica, "Exact", "", description, *species]


class TestFindRow:
    async def test_find_row_by_url_returns_match_with_species(self):
        svc = _RoutingService()
        svc.get_responses[_SCAN_RANGE] = {"values": [_bank_row()]}
        client = SheetsClient(svc, "ssid")

        row = await client.find_row_by_url(_SHEET, "https://pokepast.es/aaaa1111")

        assert row is not None
        assert row.row_number == config.FIRST_DATA_ROW
        assert row.url == "https://pokepast.es/aaaa1111"
        assert row.description == "jsmithvgc — Calyrex-S balance"
        assert "Calyrex-Shadow" in row.species

    async def test_find_row_by_url_skips_malformed_stored_url(self):
        svc = _RoutingService()
        svc.get_responses[_SCAN_RANGE] = {
            "values": [
                _bank_row(url="not-a-pokepaste-url"),
                _bank_row(),
            ]
        }
        client = SheetsClient(svc, "ssid")

        row = await client.find_row_by_url(_SHEET, "https://pokepast.es/aaaa1111")

        assert row is not None
        assert row.row_number == config.FIRST_DATA_ROW + 1

    async def test_find_row_by_url_returns_none_on_miss(self):
        svc = _RoutingService()
        svc.get_responses[_SCAN_RANGE] = {"values": [_bank_row()]}
        client = SheetsClient(svc, "ssid")

        assert await client.find_row_by_url(_SHEET, "https://pokepast.es/never") is None

    async def test_find_row_by_replica_case_insensitive(self):
        svc = _RoutingService()
        svc.get_responses[_SCAN_RANGE] = {"values": [_bank_row(replica="QBXXWXL05U")]}
        client = SheetsClient(svc, "ssid")

        row = await client.find_row_by_replica(_SHEET, "qbxxwxl05u")

        assert row is not None
        assert row.url == "https://pokepast.es/aaaa1111"
        assert row.row_number == config.FIRST_DATA_ROW

    async def test_find_row_by_replica_returns_none_on_miss(self):
        svc = _RoutingService()
        svc.get_responses[_SCAN_RANGE] = {"values": [_bank_row()]}
        client = SheetsClient(svc, "ssid")

        assert await client.find_row_by_replica(_SHEET, "DOESNOTEXST") is None

    async def test_find_row_skips_blank_target_column(self):
        # A row with no replica code should not match a replica lookup.
        svc = _RoutingService()
        svc.get_responses[_SCAN_RANGE] = {
            "values": [
                _bank_row(replica=""),
                _bank_row(replica="HIT0000000"),
            ]
        }
        client = SheetsClient(svc, "ssid")

        row = await client.find_row_by_replica(_SHEET, "HIT0000000")
        assert row is not None
        assert row.row_number == config.FIRST_DATA_ROW + 1

    async def test_find_row_loading_species_returns_empty_list(self):
        # Just-added row whose TEAMDATAFROMPASTE is still resolving.
        svc = _RoutingService()
        svc.get_responses[_SCAN_RANGE] = {
            "values": [
                _bank_row(species=["Loading...", "", "", "", "", ""]),
            ]
        }
        client = SheetsClient(svc, "ssid")

        row = await client.find_row_by_url(_SHEET, "https://pokepast.es/aaaa1111")
        assert row is not None
        assert row.species == []


# -- delete_row --------------------------------------------------------------


class TestDeleteRow:
    async def test_delete_with_no_guard_issues_batch_update(self):
        svc = _RoutingService()
        client = SheetsClient(svc, "ssid")

        deleted = await client.delete_row(_SHEET, 5)
        assert deleted is True
        # No guard read should have been issued (only the tab-id meta read).
        assert all(":" not in call.get("range", "") for call in svc.get_calls)
        assert len(svc.batch_update_bodies) == 1
        body = svc.batch_update_bodies[0]
        request = body["requests"][0]["deleteDimension"]
        assert request["range"]["sheetId"] == 7
        assert request["range"]["dimension"] == "ROWS"
        assert request["range"]["startIndex"] == 4
        assert request["range"]["endIndex"] == 5

    async def test_delete_with_expected_url_match_deletes(self):
        svc = _RoutingService()
        svc.get_responses[f"{_SHEET}!A5:G5"] = {
            "values": [
                ["https://pokepast.es/abc", "", "", "ZZZ0000000", "", "", "desc"]
            ]
        }
        client = SheetsClient(svc, "ssid")

        deleted = await client.delete_row(
            _SHEET, 5, expected_url="https://pokepast.es/abc"
        )
        assert deleted is True
        assert len(svc.batch_update_bodies) == 1

    async def test_delete_with_expected_url_mismatch_does_not_delete(self):
        svc = _RoutingService()
        svc.get_responses[f"{_SHEET}!A5:G5"] = {
            "values": [
                [
                    "https://pokepast.es/somethingelse",
                    "",
                    "",
                    "ZZZ0000000",
                    "",
                    "",
                    "desc",
                ]
            ]
        }
        client = SheetsClient(svc, "ssid")

        deleted = await client.delete_row(
            _SHEET, 5, expected_url="https://pokepast.es/abc"
        )
        assert deleted is False
        assert svc.batch_update_bodies == []

    async def test_delete_with_expected_replica_match_deletes(self):
        svc = _RoutingService()
        svc.get_responses[f"{_SHEET}!A5:G5"] = {
            "values": [
                ["https://pokepast.es/abc", "", "", "qbxxwxl05u", "", "", "desc"]
            ]
        }
        client = SheetsClient(svc, "ssid")

        # Uppercase the expected value the way the handler does.
        deleted = await client.delete_row(_SHEET, 5, expected_replica="QBXXWXL05U")
        assert deleted is True
        assert len(svc.batch_update_bodies) == 1

    async def test_delete_with_expected_replica_mismatch_does_not_delete(self):
        svc = _RoutingService()
        svc.get_responses[f"{_SHEET}!A5:G5"] = {
            "values": [
                ["https://pokepast.es/abc", "", "", "OTHER00000", "", "", "desc"]
            ]
        }
        client = SheetsClient(svc, "ssid")

        deleted = await client.delete_row(_SHEET, 5, expected_replica="QBXXWXL05U")
        assert deleted is False
        assert svc.batch_update_bodies == []

    async def test_delete_with_expected_url_empty_row_does_not_delete(self):
        # If the guard read returns nothing (row already gone), bail out
        # rather than running a no-op delete that would shift unrelated
        # rows up.
        svc = _RoutingService()
        svc.get_responses[f"{_SHEET}!A5:G5"] = {"values": []}
        client = SheetsClient(svc, "ssid")

        deleted = await client.delete_row(
            _SHEET, 5, expected_url="https://pokepast.es/abc"
        )
        assert deleted is False
        assert svc.batch_update_bodies == []


# -- delete_by_url / delete_by_replica ----------------------------------------


def _cas_response(
    url: str = "https://pokepast.es/aaaa1111",
    replica: str = "QBXXWXL05U",
    description: str = "desc",
) -> dict:
    """Fake guard-read response for delete_row's CAS check (A:G shape)."""
    return {"values": [[url, "", "", replica, "", "", description]]}


class TestDeleteBy:
    async def test_delete_by_url_returns_deleted_row(self):
        svc = _RoutingService()
        svc.get_responses[_SCAN_RANGE] = {"values": [_bank_row()]}
        svc.get_responses[_CAS_RANGE] = _cas_response()
        client = SheetsClient(svc, "ssid")

        row = await client.delete_by_url(_SHEET, "https://pokepast.es/aaaa1111")

        assert row.url == "https://pokepast.es/aaaa1111"
        assert row.row_number == config.FIRST_DATA_ROW
        assert len(svc.batch_update_bodies) == 1

    async def test_delete_by_url_raises_team_not_found(self):
        svc = _RoutingService()
        svc.get_responses[_SCAN_RANGE] = {"values": [_bank_row()]}
        client = SheetsClient(svc, "ssid")

        with pytest.raises(TeamNotFoundError):
            await client.delete_by_url(_SHEET, "https://pokepast.es/does-not-exist")

        assert svc.batch_update_bodies == []

    async def test_delete_by_url_raises_row_shifted(self):
        # Lookup returns a row, but the CAS guard read at that row_number
        # finds a different URL (concurrent delete shifted rows up).
        svc = _RoutingService()
        # Scan finds the row at FIRST_DATA_ROW.
        svc.get_responses[_SCAN_RANGE] = {"values": [_bank_row()]}
        # CAS read at that same row returns a *different* URL.
        svc.get_responses[_CAS_RANGE] = _cas_response(
            url="https://pokepast.es/shifted-in"
        )
        client = SheetsClient(svc, "ssid")

        with pytest.raises(RowShiftedError):
            await client.delete_by_url(_SHEET, "https://pokepast.es/aaaa1111")

        assert svc.batch_update_bodies == []

    async def test_delete_by_replica_returns_deleted_row(self):
        svc = _RoutingService()
        svc.get_responses[_SCAN_RANGE] = {"values": [_bank_row()]}
        svc.get_responses[_CAS_RANGE] = _cas_response()
        client = SheetsClient(svc, "ssid")

        row = await client.delete_by_replica(_SHEET, "QBXXWXL05U")

        assert row.url == "https://pokepast.es/aaaa1111"
        assert len(svc.batch_update_bodies) == 1

    async def test_delete_by_replica_raises_team_not_found(self):
        svc = _RoutingService()
        svc.get_responses[_SCAN_RANGE] = {"values": [_bank_row()]}
        client = SheetsClient(svc, "ssid")

        with pytest.raises(TeamNotFoundError):
            await client.delete_by_replica(_SHEET, "NOTINSHEET0")

        assert svc.batch_update_bodies == []

    async def test_delete_by_replica_raises_row_shifted(self):
        svc = _RoutingService()
        svc.get_responses[_SCAN_RANGE] = {"values": [_bank_row()]}
        # CAS guard sees a different replica code at that row.
        svc.get_responses[_CAS_RANGE] = _cas_response(replica="DIFFERENTT0")
        client = SheetsClient(svc, "ssid")

        with pytest.raises(RowShiftedError):
            await client.delete_by_replica(_SHEET, "QBXXWXL05U")

        assert svc.batch_update_bodies == []
