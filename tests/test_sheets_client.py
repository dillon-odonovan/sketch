"""Tests for `SheetsClient.get_search_snapshot` and its TTL cache.

We stub the Google API service object rather than spinning up the real
client — `_run` already offloads to a thread, so the underlying call only
needs to be a plain Python callable with the right shape.

Time is injected via the `now` kwarg on `SheetsClient.__init__`. We avoid
monkeypatching `time.monotonic` so concurrent tests don't perturb each
other.
"""

import pytest

from sketch.search.text_search import DescriptionIndex
from sketch.storage.sheets_client import SearchSnapshot, SheetsClient, TeamRow


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
