"""Tests for sketch.convert.ev_model."""

from __future__ import annotations

import unittest

from sketch import config
from sketch.convert.ev_model import (
    CHAMPIONS,
    LEGACY,
    Format,
    UnsupportedFormatError,
    ev_model_for_format,
)


class TestFormat(unittest.TestCase):
    def test_format_is_str(self) -> None:
        self.assertEqual(Format.REG_M_A, "Reg M-A")

    def test_format_lookup_works_with_plain_string(self) -> None:
        # StrEnum equality means a plain-string key still resolves correctly.
        model = ev_model_for_format("Reg M-A")
        self.assertIs(model, CHAMPIONS)


class TestEvModelForFormat(unittest.TestCase):
    def test_champions_format_returns_champions_model(self) -> None:
        model = ev_model_for_format(Format.REG_M_A)
        self.assertIs(model, CHAMPIONS)
        self.assertEqual(model.max_per_stat, 32)

    def test_reg_m_c_returns_champions_model(self) -> None:
        model = ev_model_for_format(Format.REG_M_C)
        self.assertIs(model, CHAMPIONS)

    def test_champions_model_has_total_budget(self) -> None:
        self.assertIsNotNone(CHAMPIONS.max_total)
        self.assertGreater(CHAMPIONS.max_total, 0)  # type: ignore[operator]

    def test_unknown_format_raises(self) -> None:
        with self.assertRaises(UnsupportedFormatError):
            ev_model_for_format("Reg A")

    def test_legacy_model_has_correct_caps(self) -> None:
        self.assertEqual(LEGACY.max_per_stat, 252)
        self.assertIsNotNone(LEGACY.max_total)

    def test_every_registered_format_has_an_ev_model(self) -> None:
        # Guards against registering a new format in config.FORMAT_SHEETS
        # without also wiring its EvModel here, which would raise
        # UnsupportedFormatError the first time /convert-ots hit it.
        for fmt_name in config.FORMAT_SHEETS:
            ev_model_for_format(fmt_name)


if __name__ == "__main__":
    unittest.main()
