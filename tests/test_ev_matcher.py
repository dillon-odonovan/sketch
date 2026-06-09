"""Tests for sketch.convert.ev_matcher.choose_evs."""

from __future__ import annotations

import unittest

from sketch.convert.bank import BankTeam
from sketch.convert.ev_matcher import EvChoice, choose_evs
from sketch.convert.ev_model import CHAMPIONS, EvModel
from sketch.team import STAT_KEYS, PokemonEntry, TeamData, norm_species

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
    pins: dict[str, int] | None = None,
) -> EvChoice | None:
    if ots_species is None:
        ots_species = {norm_species(target.species)}
    return choose_evs(target, bank_teams, ots_species, ev_model, pins=pins)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestChooseEvsEmpty(unittest.TestCase):
    def test_empty_bank_returns_none(self) -> None:
        self.assertIsNone(_choose(_mon(), []))

    def test_wrong_species_returns_none(self) -> None:
        bank = [_bank_team("url1", _mon("Snorlax", evs=_evs(hp=32)))]
        self.assertIsNone(_choose(_mon("Pikachu"), bank))

    def test_all_zero_ev_candidates_returns_none(self) -> None:
        # OTS pastes stored in the bank have all-zero EVs and should be
        # treated as no match — returning them would produce an
        # apparently trained mon with no EVs in the paste.
        bank = [_bank_team("url1", _mon("Pikachu", evs=_zero_evs()))]
        self.assertIsNone(_choose(_mon("Pikachu"), bank))

    def test_mixed_zero_and_trained_returns_trained(self) -> None:
        trained_spread = _evs(spe=32)
        bank = [
            _bank_team("u1", _mon("Pikachu", evs=_zero_evs())),
            _bank_team("u2", _mon("Pikachu", evs=trained_spread)),
        ]
        result = _choose(_mon("Pikachu"), bank)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.evs, trained_spread)


class TestChooseEvsSingleCandidate(unittest.TestCase):
    def test_single_candidate_returned(self) -> None:
        spread = _evs(hp=4, spe=32)
        bank = [_bank_team("https://pokepast.es/abc", _mon("Pikachu", evs=spread))]
        result = _choose(_mon("Pikachu"), bank)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.evs, spread)
        self.assertEqual(result.source, "bank")
        self.assertEqual(result.source_url, "https://pokepast.es/abc")

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
        # Values above the per-stat cap are clamped down. No total-budget
        # enforcement is applied — bank spreads come from real game teams,
        # which the game itself keeps within the aggregate budget.
        over_cap = _evs(hp=100, spe=200)
        bank = [_bank_team("u", _mon("Pikachu", evs=over_cap))]
        result = _choose(_mon("Pikachu"), bank)
        assert result is not None
        for v in result.evs.values():
            self.assertLessEqual(v, CHAMPIONS.max_per_stat)


class TestChooseEvsPins(unittest.TestCase):
    def test_pin_excludes_better_set_match(self) -> None:
        # Candidate A: perfect ability/item/move match but contradicts the
        # HP pin. Candidate B: weaker set signal but honors the pin. The pin
        # is a hard filter, so A is excluded and B wins.
        target = _mon("Pikachu", ability="Static", item="Light Ball", evs=_evs(hp=32))
        cand_a = _mon("Pikachu", ability="Static", item="Light Ball", evs=_evs(spe=32))
        cand_b = _mon(
            "Pikachu",
            ability="Lightning Rod",
            item="Focus Sash",
            moves=["Tackle"],
            evs=_evs(hp=32, spe=32),
        )
        bank = [_bank_team("u_a", cand_a), _bank_team("u_b", cand_b)]
        result = _choose(target, bank, pins={"hp": 32})
        assert result is not None
        self.assertEqual(result.evs["hp"], 32)
        self.assertEqual(result.source_url, "u_b")

    def test_no_honoring_candidate_returns_none(self) -> None:
        # Pins are confirmed ground truth: when no candidate honors the pin,
        # there is no valid bank match, so choose_evs returns None and the
        # caller falls back to the LLM (which keeps the pin).
        target = _mon("Pikachu", evs=_evs(hp=32))
        cand = _mon("Pikachu", evs=_evs(spe=32))
        bank = [_bank_team("u", cand)]
        self.assertIsNone(_choose(target, bank, pins={"hp": 32}))

    def test_partial_pin_match_excluded(self) -> None:
        # Two pins (hp + spe). A candidate honoring only one is excluded;
        # only a candidate honoring *all* pins is eligible.
        target = _mon("Pikachu", evs=_evs(hp=32, spe=16))
        partial = _mon("Pikachu", item="Focus Sash", evs=_evs(hp=32, spe=32))
        full = _mon("Pikachu", item="Choice Scarf", evs=_evs(hp=32, atk=18, spe=16))
        bank = [_bank_team("u_partial", partial), _bank_team("u_full", full)]
        result = _choose(target, bank, pins={"hp": 32, "spe": 16})
        assert result is not None
        self.assertEqual(result.source_url, "u_full")
        self.assertEqual(result.evs["hp"], 32)
        self.assertEqual(result.evs["spe"], 16)

    def test_no_candidate_honors_all_pins_returns_none(self) -> None:
        # Each candidate honors one pin but not both → none eligible → None.
        target = _mon("Pikachu", evs=_evs(hp=32, spe=16))
        only_hp = _mon("Pikachu", evs=_evs(hp=32, spe=32))
        only_spe = _mon("Pikachu", evs=_evs(hp=8, spe=16))
        bank = [_bank_team("u_hp", only_hp), _bank_team("u_spe", only_spe)]
        self.assertIsNone(_choose(target, bank, pins={"hp": 32, "spe": 16}))


if __name__ == "__main__":
    unittest.main()
