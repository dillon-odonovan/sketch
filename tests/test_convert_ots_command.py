"""Tests for sketch.commands.convert_ots helpers."""

from __future__ import annotations

import unittest

from sketch.commands.convert_ots import _source_summary
from sketch.convert.converter import ConvertResult
from sketch.team import STAT_KEYS, PokemonEntry, TeamData


def _zero_evs() -> dict[str, int]:
    return {k: 0 for k in STAT_KEYS}


def _mon(species: str) -> PokemonEntry:
    return PokemonEntry(
        species=species,
        gender=None,
        item=None,
        ability="Static",
        nature="Timid",
        evs=_zero_evs(),
        moves=["Tackle"],
    )


def _result(
    sources: list[str],
    source_urls: list[str | None] | None = None,
    species: list[str] | None = None,
) -> tuple[ConvertResult, list[str]]:
    n = len(sources)
    sp = species or [f"Mon{i}" for i in range(n)]
    urls = source_urls or [None] * n
    team = TeamData(pokemon=[_mon(s) for s in sp])
    return ConvertResult(team=team, sources=sources, source_urls=urls), sp


class TestSourceSummary(unittest.TestCase):
    def test_all_from_bank_with_urls(self) -> None:
        urls = [f"https://pokepast.es/abc{i}" for i in range(6)]
        r, names = _result(["bank"] * 6, source_urls=urls)
        out = _source_summary(r, names)
        self.assertIn("6 from bank", out)
        self.assertIn("Trained 6 mons", out)
        # Each mon should appear with its shortened URL.
        for i in range(6):
            self.assertIn(f"pokepast.es/abc{i}", out)

    def test_all_estimated(self) -> None:
        r, names = _result(["estimated"] * 6)
        out = _source_summary(r, names)
        self.assertIn("6 estimated", out)
        # Each mon listed as estimated.
        for name in names:
            self.assertIn(f"• {name} — estimated", out)

    def test_all_kept(self) -> None:
        r, names = _result(["kept"] * 6)
        out = _source_summary(r, names)
        self.assertIn("6 already trained", out)
        # Kept mons are not individually listed.
        self.assertNotIn("•", out)

    def test_mixed_sources(self) -> None:
        sources = ["bank", "bank", "estimated", "estimated", "bank", "kept"]
        urls = [
            "https://pokepast.es/a1",
            "https://pokepast.es/a2",
            None,
            None,
            "https://pokepast.es/a3",
            None,
        ]
        names = [
            "Venusaur",
            "Charizard",
            "Garchomp",
            "Incineroar",
            "Floette-Eternal",
            "Sinistcha",
        ]
        r, _ = _result(sources, source_urls=urls, species=names)
        out = _source_summary(r, names)
        self.assertIn("3 from bank", out)
        self.assertIn("2 estimated", out)
        self.assertIn("1 already trained", out)
        self.assertIn("Trained 6 mons", out)
        self.assertIn("pokepast.es/a1", out)
        self.assertIn("pokepast.es/a2", out)
        self.assertIn("pokepast.es/a3", out)
        self.assertIn("• Garchomp — estimated", out)

    def test_empty(self) -> None:
        r, names = _result([])
        out = _source_summary(r, names)
        self.assertIn("0 matched", out)


if __name__ == "__main__":
    unittest.main()
