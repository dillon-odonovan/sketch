"""Tests for sketch.convert.ev_matcher.choose_evs."""

from __future__ import annotations

import unittest

from sketch.convert.bank import BankTeam, _norm_species
from sketch.convert.ev_matcher import EvChoice, choose_evs
from sketch.convert.ev_model import CHAMPIONS, EvModel
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


def _bank_team(url: str, *mons: PokemonEntry) -> BankTeam:
    return BankTeam(url=url, team=TeamData(pokemon=list(mons)))


def _choose(
    target: PokemonEntry,
    bank_teams: list[BankTeam],
    ots_species: set[str] | None = None,
    ev_model: EvModel = CHAMPIONS,
) -> EvChoice | None:
    if ots_species is None:
        ots_species = {_norm_species(target.species)}
    return choose_evs(target, bank_teams, ots_species, ev_model)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestChooseEvsEmpty(unittest.TestCase):
    def test_empty_bank_returns_none(self) -> None:
        self.assertIsNone(_choose(_mon(), []))

    def test_wrong_species_returns_none(self) -> None:
        bank = [_bank_team("url1", _mon("Snorlax", evs=_evs(hp=32)))]
        self.assertIsNone(_choose(_mon("Pikachu"), bank))


class TestChooseEvsSingleCandidate(unittest.TestCase):
    def test_single_candidate_returned(self) -> None:
        spread = _evs(hp=4, spe=32)
        bank = [_bank_team("url1", _mon("Pikachu", evs=spread))]
        result = _choose(_mon("Pikachu"), bank)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.evs, spread)
        self.assertEqual(result.source, "bank")

    def test_case_insensitive_species_match(self) -> None:
        spread = _evs(spe=32)
        bank = [_bank_team("u", _mon("PIKACHU", evs=spread))]
        result = _choose(_mon("pikachu"), bank)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.evs, spread)


class TestChooseEvsNatureGate(unittest.TestCase):
    """Nature acts as a pool gate, not a score component."""

    def test_same_nature_candidate_preferred(self) -> None:
        timid_spread = _evs(spe=32)
        adamant_spread = _evs(atk=32)
        bank = [
            _bank_team("a", _mon("Pikachu", nature="Timid", evs=timid_spread)),
            _bank_team("b", _mon("Pikachu", nature="Adamant", evs=adamant_spread)),
        ]
        result = _choose(_mon("Pikachu", nature="Timid"), bank)
        assert result is not None
        self.assertEqual(result.evs, timid_spread)

    def test_falls_back_to_wrong_nature_when_no_match(self) -> None:
        # Only an Adamant spread exists; Timid target still gets a result.
        spread = _evs(atk=32)
        bank = [_bank_team("a", _mon("Pikachu", nature="Adamant", evs=spread))]
        result = _choose(_mon("Pikachu", nature="Timid"), bank)
        self.assertIsNotNone(result)

    def test_high_quality_wrong_nature_beat_by_low_quality_correct_nature(self) -> None:
        # A same-nature candidate with no ability/item/moves match should
        # still beat a mismatched-nature candidate with everything else.
        correct_nature_spread = _evs(hp=4)
        wrong_nature_spread = _evs(spe=32)
        target = _mon(
            "Pikachu",
            nature="Timid",
            ability="Static",
            item="Light Ball",
            moves=["Thunderbolt", "Volt Tackle", "Iron Tail", "Quick Attack"],
        )
        bank = [
            # Correct nature, nothing else matches.
            _bank_team(
                "a",
                _mon(
                    "Pikachu",
                    nature="Timid",
                    ability="Lightning Rod",
                    item="Sitrus Berry",
                    moves=["Surf", "Blizzard", "Flamethrower", "Psychic"],
                    evs=correct_nature_spread,
                ),
            ),
            # Wrong nature, ability+item+moves all match.
            _bank_team(
                "b",
                _mon(
                    "Pikachu",
                    nature="Adamant",
                    ability="Static",
                    item="Light Ball",
                    moves=["Thunderbolt", "Volt Tackle", "Iron Tail", "Quick Attack"],
                    evs=wrong_nature_spread,
                ),
            ),
        ]
        result = _choose(target, bank)
        assert result is not None
        # The same-nature candidate wins because the pool is restricted to
        # same-nature entries first.
        self.assertEqual(result.evs, correct_nature_spread)


class TestChooseEvsRanking(unittest.TestCase):
    """Ranking within the nature-gated pool: ability → item → moves → composition."""

    def test_ability_match_beats_item_match(self) -> None:
        ability_match_spread = _evs(spa=32, spe=16)
        item_match_spread = _evs(hp=32, def_=4)
        target = _mon("Pikachu", ability="Static", item="Light Ball", nature="Timid")
        bank = [
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
        result = _choose(target, bank)
        assert result is not None
        self.assertEqual(result.evs, ability_match_spread)

    def test_item_match_beats_move_overlap(self) -> None:
        item_spread = _evs(spa=16, spe=32)
        moves_spread = _evs(hp=32)
        target = _mon(
            "Pikachu",
            ability="Static",
            item="Light Ball",
            nature="Timid",
            moves=["Thunderbolt", "Volt Tackle", "Iron Tail", "Quick Attack"],
        )
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
        result = _choose(target, bank)
        assert result is not None
        self.assertEqual(result.evs, item_spread)

    def test_composition_overlap_tiebreak(self) -> None:
        low_spread = _evs(hp=4)
        high_spread = _evs(spe=32)
        ots_species = {
            "pikachu",
            "raichu",
            "mimikyu",
            "electrode",
            "jolteon",
            "lanturn",
        }
        target = _mon("Pikachu", nature="Timid", ability="Static", item="Light Ball")
        bank = [
            # Overlap=1 (only Pikachu).
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
        result = _choose(target, bank, ots_species=ots_species)
        assert result is not None
        self.assertEqual(result.evs, high_spread)

    def test_break_after_first_species_match_per_team(self) -> None:
        """Each team contributes at most one entry per species (break after match)."""
        spread = _evs(spe=32)
        # Two Pikachus on the same team — only the first should be counted.
        bank = [
            _bank_team(
                "a",
                _mon("Pikachu", evs=spread),
                _mon("Pikachu", evs=_evs(atk=32)),  # should be ignored
            )
        ]
        result = _choose(_mon("Pikachu"), bank)
        assert result is not None
        self.assertEqual(result.evs, spread)


class TestChooseEvsFrequencyTiebreak(unittest.TestCase):
    def test_most_common_spread_wins(self) -> None:
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
        result = _choose(target, bank)
        assert result is not None
        self.assertEqual(result.evs, spread_b)


class TestChooseEvsClamping(unittest.TestCase):
    def test_evs_clamped_to_max_per_stat(self) -> None:
        over_cap = _evs(hp=100, spe=200)
        bank = [_bank_team("u", _mon("Pikachu", evs=over_cap))]
        result = _choose(_mon("Pikachu"), bank)
        assert result is not None
        for v in result.evs.values():
            self.assertLessEqual(v, CHAMPIONS.max_per_stat)

    def test_total_budget_enforced(self) -> None:
        # Spread that exceeds CHAMPIONS_EV_MAX_TOTAL per stat clamped per-stat
        # but still over total → _clamp should scale down.
        big_spread = {k: CHAMPIONS.max_per_stat for k in STAT_KEYS}  # 32*6=192
        bank = [_bank_team("u", _mon("Pikachu", evs=big_spread))]
        result = _choose(_mon("Pikachu"), bank)
        assert result is not None
        total = sum(result.evs.values())
        if CHAMPIONS.max_total is not None:
            self.assertLessEqual(total, CHAMPIONS.max_total)


if __name__ == "__main__":
    unittest.main()
