"""Tests for sketch.convert.bank.load_bank_teams."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from sketch.convert import bank as bank_mod
from sketch.convert.ev_model import CHAMPIONS
from sketch.search.text_search import DescriptionIndex
from sketch.storage.sheets_client import SearchSnapshot, TeamRow

_PIKACHU_PASTE = "\r\n".join(
    [
        "Pikachu @ Light Ball",
        "Ability: Static",
        "EVs: 32 Spe",
        "Timid Nature",
        "- Thunderbolt",
        "- Volt Tackle",
        "- Iron Tail",
        "- Quick Attack",
    ]
)

_SIX_MON_PASTE = "\r\n\r\n".join([_PIKACHU_PASTE] * 6)


def _fake_snapshot(rows: list[TeamRow]) -> SearchSnapshot:
    return SearchSnapshot(
        rows=rows,
        desc_index=DescriptionIndex.from_descriptions(r.description for r in rows),
    )


def _row(url: str, species: list[str]) -> TeamRow:
    return TeamRow(
        row_number=3,
        url=url,
        description="test",
        species=species,
        replica=None,
    )


_SIX_SPECIES = ["Pikachu", "Raichu", "Mimikyu", "Electrode", "Snorlax", "Gengar"]


class TestLoadBankTeams(unittest.IsolatedAsyncioTestCase):
    async def test_empty_snapshot_returns_empty(self) -> None:
        sheets = AsyncMock()
        sheets.get_search_snapshot.return_value = _fake_snapshot([])
        result = await bank_mod.load_bank_teams(
            sheets, "Sheet1", {"pikachu"}, CHAMPIONS
        )
        self.assertEqual(result, [])

    async def test_row_with_no_matching_species_skipped(self) -> None:
        sheets = AsyncMock()
        sheets.get_search_snapshot.return_value = _fake_snapshot(
            [
                _row(
                    "https://pokepast.es/aaa",
                    [
                        "Snorlax",
                        "Gengar",
                        "Blastoise",
                        "Venusaur",
                        "Charizard",
                        "Raichu",
                    ],
                )
            ]
        )
        result = await bank_mod.load_bank_teams(
            sheets, "Sheet1", {"pikachu"}, CHAMPIONS
        )
        self.assertEqual(result, [])

    async def test_matching_row_fetched_and_parsed(self) -> None:
        sheets = AsyncMock()
        sheets.get_search_snapshot.return_value = _fake_snapshot(
            [_row("https://pokepast.es/aaa", _SIX_SPECIES)]
        )
        with patch(
            "sketch.convert.bank.fetch_pokepaste_raw",
            new=AsyncMock(return_value=_SIX_MON_PASTE),
        ):
            result = await bank_mod.load_bank_teams(
                sheets, "Sheet1", {"pikachu"}, CHAMPIONS
            )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].url, "https://pokepast.es/aaa")

    async def test_fetch_failure_skips_row(self) -> None:
        sheets = AsyncMock()
        sheets.get_search_snapshot.return_value = _fake_snapshot(
            [_row("https://pokepast.es/bbb", _SIX_SPECIES)]
        )
        with patch(
            "sketch.convert.bank.fetch_pokepaste_raw",
            new=AsyncMock(side_effect=Exception("network error")),
        ):
            result = await bank_mod.load_bank_teams(
                sheets, "Sheet1", {"pikachu"}, CHAMPIONS
            )
        self.assertEqual(result, [])

    async def test_parse_failure_skips_row(self) -> None:
        sheets = AsyncMock()
        sheets.get_search_snapshot.return_value = _fake_snapshot(
            [_row("https://pokepast.es/ccc", _SIX_SPECIES)]
        )
        with patch(
            "sketch.convert.bank.fetch_pokepaste_raw",
            new=AsyncMock(return_value="not valid showdown text"),
        ):
            result = await bank_mod.load_bank_teams(
                sheets, "Sheet1", {"pikachu"}, CHAMPIONS
            )
        self.assertEqual(result, [])

    async def test_duplicate_urls_fetched_once(self) -> None:
        url = "https://pokepast.es/dup"
        sheets = AsyncMock()
        sheets.get_search_snapshot.return_value = _fake_snapshot(
            [
                _row(url, _SIX_SPECIES),
                _row(
                    url,
                    [
                        "Pikachu",
                        "Gengar",
                        "Blastoise",
                        "Venusaur",
                        "Charizard",
                        "Mewtwo",
                    ],
                ),
            ]
        )
        mock_fetch = AsyncMock(return_value=_SIX_MON_PASTE)
        with patch("sketch.convert.bank.fetch_pokepaste_raw", new=mock_fetch):
            result = await bank_mod.load_bank_teams(
                sheets, "Sheet1", {"pikachu"}, CHAMPIONS
            )
        mock_fetch.assert_called_once()
        self.assertEqual(len(result), 1)

    async def test_snapshot_read_failure_returns_empty(self) -> None:
        sheets = AsyncMock()
        sheets.get_search_snapshot.side_effect = RuntimeError("sheets down")
        result = await bank_mod.load_bank_teams(
            sheets, "Sheet1", {"pikachu"}, CHAMPIONS
        )
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
