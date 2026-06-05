"""Tests for sketch.convert.llm_guess._parse_spreads and _trim_to_budget."""

from __future__ import annotations

import unittest
from typing import Any

from sketch.convert.ev_model import CHAMPIONS
from sketch.convert.llm_guess import _parse_spreads, _trim_to_budget
from sketch.team import STAT_KEYS


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


if __name__ == "__main__":
    unittest.main()
