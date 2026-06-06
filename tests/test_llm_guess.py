"""Tests for sketch.convert.llm_guess._parse_spreads and _trim_to_budget."""

from __future__ import annotations

import unittest
from typing import Any

from sketch.convert.ev_model import CHAMPIONS
from sketch.convert.llm_guess import (
    _describe,
    _parse_spreads,
    _system_prompt,
    _trim_to_budget,
)
from sketch.team import STAT_KEYS, PokemonEntry


def _evs(**kwargs: int) -> dict[str, int]:
    base = {k: 0 for k in STAT_KEYS}
    base.update(kwargs)
    return base


class TestTrimToBudget(unittest.TestCase):
    """_trim_to_budget should reduce to exactly max_total without rounding loss."""

    def test_under_budget_unchanged(self) -> None:
        evs = _evs(hp=32, spe=32)
        result = _trim_to_budget(evs, 66)
        self.assertEqual(result, evs)

    def test_exactly_at_budget_unchanged(self) -> None:
        evs = _evs(hp=32, spe=32, atk=2)
        result = _trim_to_budget(evs, 66)
        self.assertEqual(result, evs)

    def test_one_over_budget_trims_smallest(self) -> None:
        # 3/32/32 = 67 — the off-by-one that produced 31s with proportional
        # scaling. _trim_to_budget should remove 1 from the smallest stat (hp:3)
        # giving 2/32/32 = 66, not 2/31/31 = 64.
        evs = _evs(hp=3, atk=32, spe=32)
        result = _trim_to_budget(evs, 66)
        self.assertEqual(sum(result.values()), 66)
        # The large stats should be preserved at 32.
        self.assertEqual(result["atk"], 32)
        self.assertEqual(result["spe"], 32)
        self.assertEqual(result["hp"], 2)

    def test_all_maxed_trims_to_budget(self) -> None:
        # 32*6 = 192; trim to 66 by removing 126 points from smallest-first.
        evs = _evs(**{k: 32 for k in STAT_KEYS})
        result = _trim_to_budget(evs, 66)
        self.assertEqual(sum(result.values()), 66)
        # All values still clamped within per-stat cap.
        for v in result.values():
            self.assertGreaterEqual(v, 0)
            self.assertLessEqual(v, 32)

    def test_protected_stat_not_trimmed(self) -> None:
        # hp is the smallest stat but pinned — the trim must take the
        # excess from an unprotected stat instead, leaving hp intact.
        evs = _evs(hp=4, atk=32, spe=32)  # 68, two over budget
        result = _trim_to_budget(evs, 66, protected={"hp"})
        self.assertEqual(result["hp"], 4)
        self.assertEqual(sum(result.values()), 66)


class TestParseSpreads(unittest.TestCase):
    """_parse_spreads should respect inclusive bounds and trim correctly."""

    def _spread(self, **kwargs: int) -> dict[str, Any]:
        evs: dict[str, Any] = {k: 0 for k in STAT_KEYS}
        evs.update(kwargs)
        return {"slot": 1, "evs": evs}

    def test_valid_spread_returned_unchanged(self) -> None:
        tool = {"spreads": [self._spread(hp=32, spe=32)]}
        result = _parse_spreads(tool, CHAMPIONS)
        self.assertEqual(result[1]["hp"], 32)
        self.assertEqual(result[1]["spe"], 32)

    def test_max_per_stat_is_inclusive(self) -> None:
        # 32 is a valid value, not over the cap.
        tool = {"spreads": [self._spread(hp=32)]}
        result = _parse_spreads(tool, CHAMPIONS)
        self.assertEqual(result[1]["hp"], 32)

    def test_over_per_stat_clamped(self) -> None:
        tool = {"spreads": [self._spread(hp=100)]}
        result = _parse_spreads(tool, CHAMPIONS)
        self.assertEqual(result[1]["hp"], 32)

    def test_one_over_total_budget_trimmed_exactly(self) -> None:
        # 3/32/32 = 67; should become 2/32/32 = 66, not 2/31/31 = 64.
        tool = {"spreads": [self._spread(hp=3, atk=32, spe=32)]}
        result = _parse_spreads(tool, CHAMPIONS)
        total = sum(result[1].values())
        self.assertLessEqual(total, CHAMPIONS.max_total)
        self.assertEqual(result[1]["atk"], 32)
        self.assertEqual(result[1]["spe"], 32)

    def test_all_maxed_trimmed_to_budget(self) -> None:
        tool = {"spreads": [self._spread(**{k: 32 for k in STAT_KEYS})]}
        result = _parse_spreads(tool, CHAMPIONS)
        total = sum(result[1].values())
        self.assertLessEqual(total, CHAMPIONS.max_total)

    def test_pinned_stat_protected_during_trim(self) -> None:
        # hp=4 is the smallest stat but pinned for this slot; the over-budget
        # trim should preserve it and cut an unpinned stat instead.
        tool = {"spreads": [self._spread(hp=4, atk=32, spe=32)]}  # 68
        result = _parse_spreads(tool, CHAMPIONS, pins_by_slot={1: {"hp": 4}})
        self.assertEqual(result[1]["hp"], 4)
        self.assertLessEqual(sum(result[1].values()), CHAMPIONS.max_total)

    def test_pin_value_enforced_when_model_violates_it(self) -> None:
        # Pins are confirmed ground truth: even if the model returns a
        # different value for a pinned stat, _parse_spreads overlays the pin
        # and re-trims the rest so the output honors it exactly.
        tool = {"spreads": [self._spread(hp=0, atk=32, spe=32)]}  # ignores HP pin
        result = _parse_spreads(tool, CHAMPIONS, pins_by_slot={1: {"hp": 32}})
        self.assertEqual(result[1]["hp"], 32)
        self.assertLessEqual(sum(result[1].values()), CHAMPIONS.max_total)


def _mon(**kwargs: Any) -> PokemonEntry:
    base: dict[str, Any] = {
        "species": "Pikachu",
        "gender": None,
        "item": "Light Ball",
        "ability": "Static",
        "nature": "Timid",
        "evs": {k: 0 for k in STAT_KEYS},
        "moves": ["Thunderbolt"],
    }
    base.update(kwargs)
    return PokemonEntry(**base)


class TestDescribe(unittest.TestCase):
    def test_no_pins_omits_known_evs(self) -> None:
        line = _describe(1, _mon())
        self.assertNotIn("Known EVs", line)

    def test_pins_rendered_with_display_names(self) -> None:
        line = _describe(1, _mon(), pins={"hp": 32, "spe": 16})
        self.assertIn("Known EVs (fixed, keep exactly):", line)
        self.assertIn("HP=32", line)
        self.assertIn("Spe=16", line)


class TestSystemPrompt(unittest.TestCase):
    def test_mentions_fixed_known_evs(self) -> None:
        prompt = _system_prompt("Reg M-A", CHAMPIONS)
        self.assertIn("Known EVs (fixed)", prompt)

    def test_directs_investment_in_nature_boosted_stat(self) -> None:
        # The prompt must steer the model toward the nature-boosted stat and,
        # in particular, real Speed for speed-boosting natures (the Hydreigon
        # / Froslass under-investment fix).
        prompt = _system_prompt("Reg M-A", CHAMPIONS)
        self.assertIn("nature boosts", prompt)
        self.assertIn("Timid", prompt)
        self.assertIn("Speed", prompt)

    def test_warns_against_singles_style_spreads(self) -> None:
        # Doubles/VGC framing: discourage the singles 252/252 (here 32/32)
        # two-maxed-stats pattern.
        prompt = _system_prompt("Reg M-A", CHAMPIONS)
        self.assertIn("252/252", prompt)
        self.assertIn("not singles", prompt)


if __name__ == "__main__":
    unittest.main()
