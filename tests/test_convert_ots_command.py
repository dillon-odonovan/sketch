"""Tests for sketch.commands.convert_ots helpers."""

from __future__ import annotations

import unittest

from sketch.commands.convert_ots import _source_summary


class TestSourceSummary(unittest.TestCase):
    def test_all_from_bank(self) -> None:
        result = _source_summary(["bank"] * 6)
        self.assertIn("6 from bank", result)
        self.assertIn("Trained 6 mons", result)

    def test_all_estimated(self) -> None:
        result = _source_summary(["estimated"] * 6)
        self.assertIn("6 estimated", result)

    def test_all_kept(self) -> None:
        result = _source_summary(["kept"] * 6)
        self.assertIn("6 already trained", result)

    def test_mixed_sources(self) -> None:
        sources = ["bank", "bank", "estimated", "estimated", "bank", "kept"]
        result = _source_summary(sources)
        self.assertIn("3 from bank", result)
        self.assertIn("2 estimated", result)
        self.assertIn("1 already trained", result)
        self.assertIn("Trained 6 mons", result)

    def test_no_matches(self) -> None:
        # Shouldn't happen in practice, but the function handles it gracefully.
        result = _source_summary([])
        self.assertIn("0 matched", result)


if __name__ == "__main__":
    unittest.main()
