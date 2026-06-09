"""Tests for sketch.convert.usage_priors (Tier 2 usage-stats prior)."""

from __future__ import annotations

import unittest

from sketch.convert.ev_model import CHAMPIONS
from sketch.convert.usage_priors import (
    SpreadEntry,
    UsagePriors,
    choose_usage_spread,
    load_usage_priors,
)
from sketch.team import STAT_KEYS, PokemonEntry


def _evs(**kwargs: int) -> dict[str, int]:
    base = {k: 0 for k in STAT_KEYS}
    base.update(kwargs)
    return base


def _mon(
    species: str = "Incineroar",
    *,
    nature: str = "Careful",
    evs: dict[str, int] | None = None,
) -> PokemonEntry:
    return PokemonEntry(
        species=species,
        gender=None,
        item="Assault Vest",
        ability="Intimidate",
        nature=nature,
        evs=evs or _evs(),
        moves=["Fake Out", "Knock Off", "Flare Blitz", "Parting Shot"],
    )


def _priors(*entries: SpreadEntry, species: str = "incineroar") -> UsagePriors:
    return UsagePriors(spreads={species: list(entries)})


class TestLoadUsagePriors(unittest.TestCase):
    def test_unknown_format_returns_none(self) -> None:
        self.assertIsNone(load_usage_priors("Reg ZZ"))

    def test_committed_artifact_loads(self) -> None:
        # The Reg M-A artifact ships in the repo; it should parse into a
        # non-empty per-species distribution.
        priors = load_usage_priors("Reg M-A")
        self.assertIsNotNone(priors)
        assert priors is not None
        self.assertGreater(len(priors.spreads), 0)
        entries = next(iter(priors.spreads.values()))
        self.assertIsInstance(entries[0], SpreadEntry)
        # EVs are on the Champions 0-32 stat-point scale.
        self.assertLessEqual(max(entries[0].evs.values()), CHAMPIONS.max_per_stat)


class TestChooseUsageSpread(unittest.TestCase):
    def test_species_absent_returns_none(self) -> None:
        priors = _priors(SpreadEntry("Careful", _evs(hp=32, spd=32), 100.0))
        self.assertIsNone(choose_usage_spread(_mon("Rillaboom"), priors, CHAMPIONS))

    def test_picks_highest_weight_nature_match(self) -> None:
        priors = _priors(
            SpreadEntry("Careful", _evs(hp=32, atk=2, spd=32), 30.0),
            SpreadEntry("Careful", _evs(hp=32, spd=32, spe=2), 70.0),
        )
        choice = choose_usage_spread(_mon(nature="Careful"), priors, CHAMPIONS)
        assert choice is not None
        self.assertEqual(choice.source, "usage")
        self.assertEqual(choice.evs, _evs(hp=32, spd=32, spe=2))

    def test_nature_filter_excludes_other_natures(self) -> None:
        priors = _priors(SpreadEntry("Careful", _evs(hp=32, spd=32), 100.0))
        self.assertIsNone(
            choose_usage_spread(_mon(nature="Adamant"), priors, CHAMPIONS)
        )

    def test_clamps_to_per_stat_cap(self) -> None:
        priors = _priors(SpreadEntry("Careful", _evs(hp=40, spd=32), 100.0))
        choice = choose_usage_spread(_mon(nature="Careful"), priors, CHAMPIONS)
        assert choice is not None
        self.assertEqual(choice.evs["hp"], CHAMPIONS.max_per_stat)

    def test_pin_consistent_spread_honored(self) -> None:
        priors = _priors(
            SpreadEntry("Careful", _evs(hp=32, spd=32), 70.0),
            SpreadEntry("Careful", _evs(hp=4, spd=32, spe=28), 30.0),
        )
        choice = choose_usage_spread(
            _mon(nature="Careful"), priors, CHAMPIONS, pins={"hp": 4}
        )
        assert choice is not None
        self.assertEqual(choice.evs["hp"], 4)

    def test_pin_inconsistent_returns_none(self) -> None:
        priors = _priors(SpreadEntry("Careful", _evs(hp=32, spd=32), 100.0))
        self.assertIsNone(
            choose_usage_spread(
                _mon(nature="Careful"), priors, CHAMPIONS, pins={"hp": 4}
            )
        )

    def test_confidence_high(self) -> None:
        priors = _priors(SpreadEntry("Careful", _evs(hp=32, spd=32), 100.0))
        choice = choose_usage_spread(_mon(nature="Careful"), priors, CHAMPIONS)
        assert choice is not None
        self.assertEqual(choice.confidence, "high")

    def test_confidence_medium(self) -> None:
        # Four equal-weight spreads → top share 0.25 → "medium".
        priors = _priors(
            SpreadEntry("Careful", _evs(hp=32, spd=32), 10.0),
            SpreadEntry("Careful", _evs(hp=32, atk=32), 10.0),
            SpreadEntry("Careful", _evs(hp=32, spe=32), 10.0),
            SpreadEntry("Careful", _evs(atk=32, spd=32), 10.0),
        )
        choice = choose_usage_spread(_mon(nature="Careful"), priors, CHAMPIONS)
        assert choice is not None
        self.assertEqual(choice.confidence, "medium")

    def test_confidence_low(self) -> None:
        # Six equal-weight spreads → top share ~0.167 → "low".
        priors = _priors(
            *[SpreadEntry("Careful", _evs(hp=v), 10.0) for v in range(1, 7)]
        )
        choice = choose_usage_spread(_mon(nature="Careful"), priors, CHAMPIONS)
        assert choice is not None
        self.assertEqual(choice.confidence, "low")


if __name__ == "__main__":
    unittest.main()
