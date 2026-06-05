"""Tests for sketch.convert.converter.convert_ots_to_cts."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sketch.convert import converter as converter_mod
from sketch.convert.bank import BankTeam
from sketch.convert.ev_model import UnsupportedFormatError
from sketch.convert.llm_guess import EvGuessError
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
) -> PokemonEntry:
    return PokemonEntry(
        species=species,
        gender=None,
        item=item,
        ability=ability,
        nature=nature,
        evs=evs if evs is not None else _zero_evs(),
        moves=moves or ["Thunderbolt", "Volt Tackle", "Iron Tail", "Quick Attack"],
    )


_FILLERS = ["Snorlax", "Gengar", "Blastoise", "Venusaur", "Charizard"]


def _ots(*mons: PokemonEntry) -> TeamData:
    return TeamData(pokemon=list(mons))


_SIX_FILLER_OTS = _ots(
    _mon("Pikachu"),
    *[_mon(s) for s in _FILLERS],
)


def _make_anthropic_client(spreads: list[dict] | None = None) -> MagicMock:
    """Mock AsyncAnthropic client that yields canned submit_spreads output."""
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


def _all_llm_spreads(ev: dict[str, int] | None = None) -> list[dict]:
    ev = ev or _zero_evs()
    return [{"slot": i + 1, "evs": {k: ev[k] for k in STAT_KEYS}} for i in range(6)]


class TestConvertOtsToCts(unittest.IsolatedAsyncioTestCase):
    async def _run(
        self,
        ots: TeamData = _SIX_FILLER_OTS,
        bank_teams: list | None = None,
        llm_spreads: list[dict] | None = None,
    ) -> converter_mod.ConvertResult:
        sheets = AsyncMock()
        with patch(
            "sketch.convert.converter.load_bank_teams",
            new=AsyncMock(return_value=bank_teams or []),
        ):
            return await converter_mod.convert_ots_to_cts(
                ots,
                sheets=sheets,
                sheet_name="Regulation M-A",
                fmt_name="Reg M-A",
                anthropic_client=_make_anthropic_client(llm_spreads),
            )

    async def test_unmatched_mon_gets_llm_evs(self) -> None:
        guessed = _evs(atk=32, spe=32)
        llm_spreads = [{"slot": 1, "evs": {k: guessed[k] for k in STAT_KEYS}}]
        llm_spreads += [{"slot": i, "evs": _zero_evs()} for i in range(2, 7)]
        result = await self._run(llm_spreads=llm_spreads)
        self.assertEqual(result.team.pokemon[0].evs, guessed)
        self.assertEqual(result.sources[0], "estimated")

    async def test_pre_trained_mon_left_untouched(self) -> None:
        trained = _evs(hp=32, def_=16)
        ots = _ots(_mon("Pikachu", evs=trained), *[_mon(s) for s in _FILLERS])
        result = await self._run(ots=ots, llm_spreads=_all_llm_spreads())
        self.assertEqual(result.team.pokemon[0].evs, trained)
        self.assertEqual(result.sources[0], "kept")

    async def test_non_ev_fields_preserved(self) -> None:
        target = _mon(
            "Pikachu",
            ability="Static",
            item="Light Ball",
            nature="Timid",
            moves=["Thunderbolt", "Volt Tackle", "Iron Tail", "Quick Attack"],
        )
        ots = _ots(target, *[_mon(s) for s in _FILLERS])
        result = await self._run(ots=ots, llm_spreads=_all_llm_spreads(_evs(spe=32)))
        trained = result.team.pokemon[0]
        self.assertEqual(trained.species, target.species)
        self.assertEqual(trained.ability, target.ability)
        self.assertEqual(trained.item, target.item)
        self.assertEqual(trained.nature, target.nature)
        self.assertEqual(trained.moves, target.moves)

    async def test_sources_length_equals_team_size(self) -> None:
        result = await self._run(llm_spreads=_all_llm_spreads())
        self.assertEqual(len(result.sources), 6)

    async def test_unsupported_format_raises(self) -> None:
        sheets = AsyncMock()
        with self.assertRaises(UnsupportedFormatError):
            await converter_mod.convert_ots_to_cts(
                _SIX_FILLER_OTS,
                sheets=sheets,
                sheet_name="Regulation A",
                fmt_name="Reg A",
                anthropic_client=_make_anthropic_client(),
            )

    async def test_mixed_bank_and_llm_sources(self) -> None:
        bank_spread = _evs(hp=4, spe=32)
        llm_spread = _evs(atk=32)
        bank_pikachu = _mon(
            "Pikachu",
            nature="Timid",
            ability="Static",
            item="Light Ball",
            evs=bank_spread,
        )
        bank_team = BankTeam(
            url="u",
            team=TeamData(
                pokemon=[
                    bank_pikachu,
                    *[
                        _mon(s)
                        for s in [
                            "Raichu",
                            "Mimikyu",
                            "Electrode",
                            "Jolteon",
                            "Lanturn",
                        ]
                    ],
                ]
            ),
        )
        ots = _ots(
            _mon("Pikachu", nature="Timid", ability="Static", item="Light Ball"),
            _mon("Snorlax", nature="Adamant", ability="Thick Fat", item="Leftovers"),
            *[_mon(s) for s in ["Gengar", "Blastoise", "Venusaur", "Charizard"]],
        )
        llm_spreads = [
            {"slot": i + 2, "evs": {k: llm_spread[k] for k in STAT_KEYS}}
            for i in range(5)
        ]
        result = await self._run(
            ots=ots, bank_teams=[bank_team], llm_spreads=llm_spreads
        )
        self.assertEqual(result.team.pokemon[0].evs, bank_spread)
        self.assertEqual(result.sources[0], "bank")
        self.assertEqual(result.team.pokemon[1].evs, llm_spread)
        self.assertEqual(result.sources[1], "estimated")


if __name__ == "__main__":
    unittest.main()
