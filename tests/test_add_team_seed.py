"""Tests for the `/add-team` url+replica extras (issue #49):

  - `_seed_replica_cache_from_pokepaste_url`: seed the global Replica
    Cache from a resolved Pokepaste URL, existence-checked and
    best-effort.
  - `_check_replica_already_in_sheet`: reject a duplicate Team ID before
    any resolution / OCR work.

Both helpers are driven directly with a minimal fake interaction rather
than the full slash-command handler.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

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
from sketch.storage.sheets_client import TeamRow

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


@dataclass
class _FakeUser:
    id: int = 111


@dataclass
class _FakeInteraction:
    user: _FakeUser = field(default_factory=_FakeUser)
    guild_id: int | None = 222
    edits: list[dict[str, Any]] = field(default_factory=list)

    async def edit_original_response(self, **kwargs: Any) -> None:
        self.edits.append(kwargs)


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


class _FakeSheets:
    """Stub exposing only `find_row_by_replica`."""

    def __init__(self, row: TeamRow | None = None, *, error: bool = False) -> None:
        self._row = row
        self._error = error
        self.calls: list[tuple[str, str]] = []

    async def find_row_by_replica(
        self, sheet_name: str, replica: str
    ) -> TeamRow | None:
        self.calls.append((sheet_name, replica))
        if self._error:
            raise RuntimeError("boom")
        return self._row


class TestSeedReplicaCacheFromUrl:
    async def test_seeds_full_entry_when_absent(self):
        cache = InMemoryReplicaCacheStore()
        interaction = _FakeInteraction()

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
        interaction = _FakeInteraction()

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
        interaction = _FakeInteraction()

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
        sheets = _FakeSheets()
        interaction = _FakeInteraction()
        handled = await _check_replica_already_in_sheet(
            interaction, sheets, _inputs(replica=None)
        )
        assert handled is False
        assert sheets.calls == []  # no lookup without a code

    async def test_returns_false_when_code_absent_from_sheet(self):
        sheets = _FakeSheets(row=None)
        interaction = _FakeInteraction()
        handled = await _check_replica_already_in_sheet(interaction, sheets, _inputs())
        assert handled is False
        assert sheets.calls == [("Reg M-A Sheet", "QBXXWXL05U")]

    async def test_rejects_duplicate_code_with_message(self):
        row = TeamRow(
            row_number=7,
            url="https://pokepast.es/original",
            description="Miraidon balance",
            species=["Miraidon"],
            replica="QBXXWXL05U",
        )
        sheets = _FakeSheets(row=row)
        interaction = _FakeInteraction()
        handled = await _check_replica_already_in_sheet(interaction, sheets, _inputs())
        assert handled is True
        assert len(interaction.edits) == 1
        content = interaction.edits[0]["content"]
        assert "QBXXWXL05U" in content
        assert "row 7" in content
        assert "Miraidon balance" in content

    async def test_read_error_is_handled(self):
        sheets = _FakeSheets(error=True)
        interaction = _FakeInteraction()
        handled = await _check_replica_already_in_sheet(interaction, sheets, _inputs())
        # True so the caller stops; user was told via an edit.
        assert handled is True
        assert len(interaction.edits) == 1
