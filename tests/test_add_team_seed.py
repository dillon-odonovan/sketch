"""Tests for the `/add-team` url+replica extras (issue #49):

  - `_seed_replica_cache_from_pokepaste_url`: seed the global Replica
    Cache from a resolved Pokepaste URL, existence-checked and
    best-effort.
  - `_check_replica_already_in_sheet`: reject a duplicate Team ID; now
    called from inside `_commit_team_row` alongside the URL dedup check.

Helpers are driven directly with a minimal fake interaction and
AsyncMock/MagicMock per the project's unittest.mock convention.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from aioresponses import aioresponses

from sketch.champions.replica_cache import (
    InMemoryReplicaCacheStore,
    ReplicaCacheEntry,
)
from sketch.commands.add_team import (
    _AddTeamInputs,
    _check_replica_already_in_sheet,
    _seed_replica_cache_from_pokepaste_url,
)
from sketch.storage.sheets_client import SheetsClient, TeamRow

_RAW_BODY = (
    "Miraidon @ Choice Specs\r\n"
    "Ability: Hadron Engine\r\n"
    "EVs: 4 HP / 32 SpA / 30 Spe\r\n"
    "Modest Nature\r\n"
    "- Electro Drift\r\n\r\n"
    "Flutter Mane @ Booster Energy\r\n"
    "Ability: Protosynthesis\r\n"
    "Timid Nature\r\n"
    "- Moonblast"
)


def _make_interaction() -> MagicMock:
    interaction = MagicMock()
    interaction.user.id = 111
    interaction.guild_id = 222
    interaction.edit_original_response = AsyncMock()
    return interaction


def _inputs(*, replica: str | None = "QBXXWXL05U") -> _AddTeamInputs:
    return _AddTeamInputs(
        description="desc",
        fmt_name="Reg M-A",
        sheet_name="Reg M-A Sheet",
        paste_type_value="Exact",
        url="https://pokepast.es/abc123",
        replica=replica,
        page1=None,
        page2=None,
    )


class TestSeedReplicaCacheFromUrl:
    async def test_seeds_full_entry_when_absent(self):
        cache = InMemoryReplicaCacheStore()
        interaction = _make_interaction()

        with aioresponses() as mock:
            mock.get("https://pokepast.es/abc123/raw", status=200, body=_RAW_BODY)
            await _seed_replica_cache_from_pokepaste_url(
                interaction,
                cache,
                _inputs(),
                pokepaste_url="https://pokepast.es/abc123",
            )

        entry = cache.get("QBXXWXL05U")
        assert entry is not None
        assert entry.paste_text == _RAW_BODY
        assert entry.pokepaste_url == "https://pokepast.es/abc123"
        assert entry.species == ["Miraidon", "Flutter Mane"]
        assert entry.created_by_user_id == 111
        assert entry.created_by_guild_id == 222

    async def test_skips_fetch_and_write_when_already_cached(self):
        existing = ReplicaCacheEntry(
            paste_text="cached paste",
            pokepaste_url="https://pokepast.es/original",
            species=["Koraidon"],
            created_at=datetime.now(timezone.utc),
            created_by_user_id=999,
            created_by_guild_id=888,
        )
        cache = InMemoryReplicaCacheStore({"QBXXWXL05U": existing})
        interaction = _make_interaction()

        # No HTTP mocks: a fetch would raise inside aioresponses, but the
        # existence check must short-circuit before any network call.
        with aioresponses():
            await _seed_replica_cache_from_pokepaste_url(
                interaction,
                cache,
                _inputs(),
                pokepaste_url="https://pokepast.es/abc123",
            )

        # Entry untouched (no clobber).
        assert cache.get("QBXXWXL05U") is existing

    async def test_fetch_failure_is_swallowed_and_writes_nothing(self):
        cache = InMemoryReplicaCacheStore()
        interaction = _make_interaction()

        with aioresponses() as mock:
            mock.get("https://pokepast.es/abc123/raw", status=404, body="nope")
            await _seed_replica_cache_from_pokepaste_url(
                interaction,
                cache,
                _inputs(),
                pokepaste_url="https://pokepast.es/abc123",
            )

        assert cache.get("QBXXWXL05U") is None


class TestCheckReplicaAlreadyInSheet:
    async def test_returns_false_when_no_replica(self):
        sheets = AsyncMock(spec=SheetsClient)
        interaction = _make_interaction()
        handled = await _check_replica_already_in_sheet(
            interaction, sheets, _inputs(replica=None)
        )
        assert handled is False
        sheets.find_row_by_replica.assert_not_called()

    async def test_returns_false_when_code_absent_from_sheet(self):
        sheets = AsyncMock(spec=SheetsClient)
        sheets.find_row_by_replica.return_value = None
        interaction = _make_interaction()
        handled = await _check_replica_already_in_sheet(interaction, sheets, _inputs())
        assert handled is False
        sheets.find_row_by_replica.assert_called_once_with(
            "Reg M-A Sheet", "QBXXWXL05U"
        )

    async def test_rejects_duplicate_code_with_message(self):
        row = TeamRow(
            row_number=7,
            url="https://pokepast.es/original",
            description="Miraidon balance",
            species=["Miraidon"],
            replica="QBXXWXL05U",
        )
        sheets = AsyncMock(spec=SheetsClient)
        sheets.find_row_by_replica.return_value = row
        interaction = _make_interaction()
        handled = await _check_replica_already_in_sheet(interaction, sheets, _inputs())
        assert handled is True
        interaction.edit_original_response.assert_called_once()
        content = interaction.edit_original_response.call_args.kwargs["content"]
        assert "QBXXWXL05U" in content
        assert "row 7" in content
        assert "Miraidon balance" in content

    async def test_read_error_is_handled(self):
        sheets = AsyncMock(spec=SheetsClient)
        sheets.find_row_by_replica.side_effect = RuntimeError("boom")
        interaction = _make_interaction()
        handled = await _check_replica_already_in_sheet(interaction, sheets, _inputs())
        assert handled is True
        interaction.edit_original_response.assert_called_once()
