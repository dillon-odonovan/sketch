"""Tests for the OTS → CTS conversion pipeline.

Covers:
  - ev_model.ev_model_for_format — known / unknown formats.
  - ev_matcher.choose_evs — ranking by nature/ability/item/moves/
      composition, frequency tiebreak, clamping, empty bank → None.
  - bank.load_bank_teams — species filtering, URL de-dup, parse-failure
      skip, fetch-failure skip.
  - converter.convert_ots_to_cts — bank match wins, LLM fallback for
      unmatched mons, pre-trained mons left untouched, non-EV fields
      preserved.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sketch.convert import bank as bank_mod
from sketch.convert import converter as converter_mod
from sketch.convert.bank import BankTeam, _norm_species
from sketch.convert.ev_matcher import EvChoice, choose_evs
from sketch.convert.ev_model import (
    CHAMPIONS,
    LEGACY,
    UnsupportedFormatError,
    ev_model_for_format,
)
from sketch.convert.llm_guess import EvGuessError
from sketch.storage.sheets_client import SearchSnapshot, TeamRow
from sketch.team import STAT_KEYS, PokemonEntry, TeamData

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _zero_evs() -> dict[str, int]:
    return {k: 0 for k in STAT_KEYS}


def _evs(**kwargs: int) -> dict[str, int]:
    base = _zero_evs()
    base.update(kwargs)
    return base


def _mon(
    species: str = "Pikachu",
    *,
    ability: str = "Static",
    item: str | None = "Light Ball",
    nature: str = "Timid",
    moves: list[str] | None = None,
    evs: dict[str, int] | None = None,
    gender: str | None = None,
) -> PokemonEntry:
    return PokemonEntry(
        species=species,
        gender=gender,
        item=item,
        ability=ability,
        nature=nature,
        evs=evs if evs is not None else _zero_evs(),
        moves=moves or ["Thunderbolt", "Volt Tackle", "Iron Tail", "Quick Attack"],
    )


def _team(*mons: PokemonEntry) -> TeamData:
    return TeamData(pokemon=list(mons))


def _bank_team(url: str, *mons: PokemonEntry) -> BankTeam:
    return BankTeam(url=url, team=_team(*mons))


# ---------------------------------------------------------------------------
# ev_model tests
# ---------------------------------------------------------------------------


class TestEvModelForFormat(unittest.TestCase):
    def test_champions_format_returns_champions_model(self) -> None:
        model = ev_model_for_format("Reg M-A")
        self.assertIs(model, CHAMPIONS)
        self.assertEqual(model.max_per_stat, 32)

    def test_unknown_format_raises(self) -> None:
        with self.assertRaises(UnsupportedFormatError):
            ev_model_for_format("Reg A")

    def test_legacy_model_has_correct_cap(self) -> None:
        # Not wired to any format yet, but the object must be correct.
        self.assertEqual(LEGACY.max_per_stat, 252)


# ---------------------------------------------------------------------------
# ev_matcher tests
# ---------------------------------------------------------------------------


class TestChooseEvs(unittest.TestCase):
    """Pure scoring with no I/O."""

    def _choose(
        self,
        target: PokemonEntry,
        bank_teams: list[BankTeam],
        ots_species: set[str] | None = None,
        max_per_stat: int = 32,
    ) -> EvChoice | None:
        if ots_species is None:
            ots_species = {_norm_species(target.species)}
        return choose_evs(target, bank_teams, ots_species, max_per_stat)

    def test_empty_bank_returns_none(self) -> None:
        self.assertIsNone(self._choose(_mon(), []))

    def test_wrong_species_returns_none(self) -> None:
        bank = [_bank_team("url1", _mon("Snorlax", evs=_evs(hp=32)))]
        self.assertIsNone(self._choose(_mon("Pikachu"), bank))

    def test_single_candidate_returned(self) -> None:
        spread = _evs(hp=4, spe=32)
        bank = [_bank_team("url1", _mon("Pikachu", evs=spread))]
        result = self._choose(_mon("Pikachu"), bank)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.evs, spread)
        self.assertEqual(result.source, "bank")

    def test_nature_match_wins_over_no_nature_match(self) -> None:
        # Two Pikachu: one with matching nature (Timid), one without (Adamant).
        timid_spread = _evs(spe=32)
        adamant_spread = _evs(atk=32)
        bank = [
            _bank_team("a", _mon("Pikachu", nature="Timid", evs=timid_spread)),
            _bank_team("b", _mon("Pikachu", nature="Adamant", evs=adamant_spread)),
        ]
        result = self._choose(_mon("Pikachu", nature="Timid"), bank)
        assert result is not None
        self.assertEqual(result.evs, timid_spread)

    def test_ability_match_beats_item_match(self) -> None:
        # Two candidates with matching nature but different ability/item alignment.
        ability_match_spread = _evs(spa=32, spe=16)
        item_match_spread = _evs(hp=32, def_=4)
        target = _mon("Pikachu", ability="Static", item="Light Ball", nature="Timid")
        bank = [
            # Matches ability, wrong item → should win.
            _bank_team(
                "a",
                _mon(
                    "Pikachu",
                    ability="Static",
                    item="Sitrus Berry",
                    nature="Timid",
                    evs=ability_match_spread,
                ),
            ),
            # Wrong ability, matches item.
            _bank_team(
                "b",
                _mon(
                    "Pikachu",
                    ability="Lightning Rod",
                    item="Light Ball",
                    nature="Timid",
                    evs=item_match_spread,
                ),
            ),
        ]
        result = self._choose(target, bank)
        assert result is not None
        self.assertEqual(result.evs, ability_match_spread)

    def test_item_match_beats_move_overlap(self) -> None:
        target = _mon(
            "Pikachu",
            ability="Static",
            item="Light Ball",
            nature="Timid",
            moves=["Thunderbolt", "Volt Tackle", "Iron Tail", "Quick Attack"],
        )
        item_spread = _evs(spa=16, spe=32)
        moves_spread = _evs(hp=32)
        bank = [
            # Item match, 0 moves overlap.
            _bank_team(
                "a",
                _mon(
                    "Pikachu",
                    ability="Static",
                    item="Light Ball",
                    nature="Timid",
                    moves=["Surf", "Blizzard", "Flamethrower", "Psychic"],
                    evs=item_spread,
                ),
            ),
            # No item match, 3 moves overlap.
            _bank_team(
                "b",
                _mon(
                    "Pikachu",
                    ability="Static",
                    item="Sitrus Berry",
                    nature="Timid",
                    moves=["Thunderbolt", "Volt Tackle", "Iron Tail", "Protect"],
                    evs=moves_spread,
                ),
            ),
        ]
        result = self._choose(target, bank)
        assert result is not None
        self.assertEqual(result.evs, item_spread)

    def test_composition_overlap_tiebreak(self) -> None:
        # Two Pikachu with same nature/ability/item/moves but from teams with
        # different composition overlap — higher overlap wins.
        low_spread = _evs(hp=4)
        high_spread = _evs(spe=32)
        target_species = {"pikachu", "raichu", "mimikyu", "electrode", "jolteon"}
        target = _mon("Pikachu", nature="Timid", ability="Static", item="Light Ball")
        bank = [
            # Overlap=1 (only Pikachu itself).
            _bank_team(
                "a",
                _mon(
                    "Pikachu",
                    nature="Timid",
                    ability="Static",
                    item="Light Ball",
                    evs=low_spread,
                ),
                _mon("Snorlax"),
                _mon("Gengar"),
                _mon("Blastoise"),
                _mon("Venusaur"),
                _mon("Charizard"),
            ),
            # Overlap=4 (Pikachu + Raichu + Mimikyu + Electrode).
            _bank_team(
                "b",
                _mon(
                    "Pikachu",
                    nature="Timid",
                    ability="Static",
                    item="Light Ball",
                    evs=high_spread,
                ),
                _mon("Raichu"),
                _mon("Mimikyu"),
                _mon("Electrode"),
                _mon("Snorlax"),
                _mon("Gengar"),
            ),
        ]
        result = self._choose(target, bank, ots_species=target_species)
        assert result is not None
        self.assertEqual(result.evs, high_spread)

    def test_frequency_tiebreak_picks_most_common_spread(self) -> None:
        # Three Pikachu candidates tied on all set signals; spread B appears
        # twice and should win.
        spread_a = _evs(hp=4, spe=32)
        spread_b = _evs(spa=32, spe=16)
        target = _mon("Pikachu", nature="Timid", ability="Static", item="Light Ball")
        bank = [
            _bank_team(
                "a",
                _mon(
                    "Pikachu",
                    nature="Timid",
                    ability="Static",
                    item="Light Ball",
                    evs=spread_a,
                ),
            ),
            _bank_team(
                "b",
                _mon(
                    "Pikachu",
                    nature="Timid",
                    ability="Static",
                    item="Light Ball",
                    evs=spread_b,
                ),
            ),
            _bank_team(
                "c",
                _mon(
                    "Pikachu",
                    nature="Timid",
                    ability="Static",
                    item="Light Ball",
                    evs=spread_b,
                ),
            ),
        ]
        result = self._choose(target, bank)
        assert result is not None
        self.assertEqual(result.evs, spread_b)

    def test_evs_clamped_to_max_per_stat(self) -> None:
        over_cap = _evs(hp=100, spe=200)
        bank = [_bank_team("u", _mon("Pikachu", evs=over_cap))]
        result = self._choose(_mon("Pikachu"), bank, max_per_stat=32)
        assert result is not None
        for v in result.evs.values():
            self.assertLessEqual(v, 32)

    def test_case_insensitive_species_match(self) -> None:
        spread = _evs(spe=32)
        # Bank has the mon stored as-is; target species uses different case.
        bank = [_bank_team("u", _mon("PIKACHU", evs=spread))]
        result = self._choose(_mon("pikachu"), bank)
        assert result is not None
        self.assertEqual(result.evs, spread)


# ---------------------------------------------------------------------------
# bank.load_bank_teams tests
# ---------------------------------------------------------------------------


def _fake_snapshot(rows: list[TeamRow]) -> SearchSnapshot:
    from sketch.search.text_search import DescriptionIndex

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
            [
                _row(
                    "https://pokepast.es/aaa",
                    ["Pikachu", "Raichu", "Mimikyu", "Electrode", "Snorlax", "Gengar"],
                )
            ]
        )
        # Build a valid 6-mon paste.
        paste = "\r\n\r\n".join([_PIKACHU_PASTE] * 6)
        with patch(
            "sketch.convert.bank.fetch_pokepaste_raw", new=AsyncMock(return_value=paste)
        ):
            result = await bank_mod.load_bank_teams(
                sheets, "Sheet1", {"pikachu"}, CHAMPIONS
            )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].url, "https://pokepast.es/aaa")

    async def test_fetch_failure_skips_row(self) -> None:
        sheets = AsyncMock()
        sheets.get_search_snapshot.return_value = _fake_snapshot(
            [
                _row(
                    "https://pokepast.es/bbb",
                    ["Pikachu", "Raichu", "Mimikyu", "Electrode", "Snorlax", "Gengar"],
                )
            ]
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
            [
                _row(
                    "https://pokepast.es/ccc",
                    ["Pikachu", "Raichu", "Mimikyu", "Electrode", "Snorlax", "Gengar"],
                )
            ]
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
        sheets = AsyncMock()
        url = "https://pokepast.es/dup"
        paste = "\r\n\r\n".join([_PIKACHU_PASTE] * 6)
        sheets.get_search_snapshot.return_value = _fake_snapshot(
            [
                _row(
                    url,
                    ["Pikachu", "Raichu", "Mimikyu", "Electrode", "Snorlax", "Gengar"],
                ),
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
        mock_fetch = AsyncMock(return_value=paste)
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


# ---------------------------------------------------------------------------
# converter.convert_ots_to_cts tests
# ---------------------------------------------------------------------------


def _make_anthropic_client(spreads: list[dict] | None = None) -> MagicMock:
    """Return a mock AsyncAnthropic client that yields canned submit_spreads."""
    client = MagicMock()

    if spreads is not None:
        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.name = "submit_spreads"
        tool_block.input = {"spreads": spreads}
        msg = MagicMock()
        msg.content = [tool_block]
        client.messages.create = AsyncMock(return_value=msg)
    else:
        client.messages.create = AsyncMock(side_effect=EvGuessError("llm down"))

    return client


_OTS_TEAM = _team(
    _mon("Pikachu", ability="Static", item="Light Ball", nature="Timid"),
    _mon("Snorlax", ability="Thick Fat", item="Leftovers", nature="Adamant"),
    _mon("Gengar", ability="Levitate", item="Choice Specs", nature="Timid"),
    _mon("Blastoise", ability="Torrent", item="White Herb", nature="Modest"),
    _mon("Venusaur", ability="Chlorophyll", item="Life Orb", nature="Modest"),
    _mon("Charizard", ability="Solar Power", item="Heat Rock", nature="Timid"),
)


class TestConvertOtsToCts(unittest.IsolatedAsyncioTestCase):
    async def _run(
        self,
        ots: TeamData = _OTS_TEAM,
        bank_teams: list[BankTeam] | None = None,
        llm_spreads: list[dict] | None = None,
    ) -> converter_mod.ConvertResult:
        sheets = AsyncMock()
        # load_bank_teams is patched to return canned bank teams.
        with patch(
            "sketch.convert.converter.load_bank_teams",
            new=AsyncMock(return_value=bank_teams or []),
        ):
            result = await converter_mod.convert_ots_to_cts(
                ots,
                sheets=sheets,
                sheet_name="Regulation M-A",
                fmt_name="Reg M-A",
                anthropic_client=_make_anthropic_client(llm_spreads),
            )
        return result

    async def test_bank_matched_mon_gets_bank_evs(self) -> None:
        bank_spread = _evs(spe=32, spa=16)
        bank = [
            _bank_team(
                "u",
                _mon(
                    "Pikachu",
                    ability="Static",
                    item="Light Ball",
                    nature="Timid",
                    evs=bank_spread,
                ),
                *[
                    _mon(s)
                    for s in ["Snorlax", "Gengar", "Blastoise", "Venusaur", "Charizard"]
                ],
            )
        ]
        result = await self._run(bank_teams=bank, llm_spreads=[])
        self.assertEqual(result.team.pokemon[0].evs, bank_spread)
        self.assertEqual(result.sources[0], "bank")

    async def test_unmatched_mon_gets_llm_evs(self) -> None:
        guessed_spread = _evs(atk=32, spe=32)
        llm_spreads = [{"slot": 1, "evs": {k: guessed_spread[k] for k in STAT_KEYS}}]
        result = await self._run(llm_spreads=llm_spreads)
        self.assertEqual(result.team.pokemon[0].evs, guessed_spread)
        self.assertEqual(result.sources[0], "estimated")

    async def test_pre_trained_mon_left_untouched(self) -> None:
        trained_spread = _evs(hp=32, def_=16)
        ots = _team(
            _mon("Pikachu", evs=trained_spread),
            *[
                _mon(s)
                for s in ["Snorlax", "Gengar", "Blastoise", "Venusaur", "Charizard"]
            ],
        )
        result = await self._run(
            ots=ots, llm_spreads=[{"slot": s, "evs": _zero_evs()} for s in range(2, 7)]
        )
        self.assertEqual(result.team.pokemon[0].evs, trained_spread)
        self.assertEqual(result.sources[0], "kept")

    async def test_non_ev_fields_preserved(self) -> None:
        target = _mon(
            "Pikachu",
            ability="Static",
            item="Light Ball",
            nature="Timid",
            moves=["Thunderbolt", "Volt Tackle", "Iron Tail", "Quick Attack"],
        )
        ots = _team(
            target,
            *[
                _mon(s)
                for s in ["Snorlax", "Gengar", "Blastoise", "Venusaur", "Charizard"]
            ],
        )
        guessed_spread = _evs(spe=32)
        llm_spreads = [
            {"slot": i + 1, "evs": {k: guessed_spread[k] for k in STAT_KEYS}}
            for i in range(6)
        ]
        result = await self._run(ots=ots, llm_spreads=llm_spreads)
        trained = result.team.pokemon[0]
        self.assertEqual(trained.species, target.species)
        self.assertEqual(trained.ability, target.ability)
        self.assertEqual(trained.item, target.item)
        self.assertEqual(trained.nature, target.nature)
        self.assertEqual(trained.moves, target.moves)

    async def test_sources_list_length_equals_team_size(self) -> None:
        result = await self._run(
            llm_spreads=[{"slot": i + 1, "evs": _zero_evs()} for i in range(6)]
        )
        self.assertEqual(len(result.sources), 6)

    async def test_unsupported_format_raises(self) -> None:
        sheets = AsyncMock()
        with self.assertRaises(UnsupportedFormatError):
            await converter_mod.convert_ots_to_cts(
                _OTS_TEAM,
                sheets=sheets,
                sheet_name="Regulation A",
                fmt_name="Reg A",
                anthropic_client=_make_anthropic_client(),
            )

    async def test_mixed_bank_and_llm_sources(self) -> None:
        """First mon matched by bank, second unmatched → LLM."""
        bank_spread = _evs(hp=4, spe=32)
        llm_spread = _evs(atk=32)
        bank = [
            _bank_team(
                "u",
                _mon(
                    "Pikachu",
                    nature="Timid",
                    ability="Static",
                    item="Light Ball",
                    evs=bank_spread,
                ),
                *[
                    _mon(s)
                    for s in ["Raichu", "Mimikyu", "Electrode", "Jolteon", "Lanturn"]
                ],
            )
        ]
        ots = _team(
            _mon("Pikachu", nature="Timid", ability="Static", item="Light Ball"),
            _mon("Snorlax", nature="Adamant", ability="Thick Fat", item="Leftovers"),
            *[_mon(s) for s in ["Gengar", "Blastoise", "Venusaur", "Charizard"]],
        )
        llm_spreads = [
            {"slot": 2, "evs": {k: llm_spread[k] for k in STAT_KEYS}},
            *[{"slot": i + 3, "evs": _zero_evs()} for i in range(4)],
        ]
        result = await self._run(ots=ots, bank_teams=bank, llm_spreads=llm_spreads)
        self.assertEqual(result.team.pokemon[0].evs, bank_spread)
        self.assertEqual(result.sources[0], "bank")
        self.assertEqual(result.team.pokemon[1].evs, llm_spread)
        self.assertEqual(result.sources[1], "estimated")


if __name__ == "__main__":
    unittest.main()
