"""Tests for sketch.commands.convert_ots helpers."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from discord import app_commands

from sketch import config
from sketch.commands import convert_ots
from sketch.commands.convert_ots import _source_summary
from sketch.convert.converter import ConvertedSlot, ConvertResult, SlotSource
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
    pinned: list[tuple[str, ...]] | None = None,
    confidence: list[str | None] | None = None,
) -> tuple[ConvertResult, list[str]]:
    n = len(sources)
    sp = species or [f"Mon{i}" for i in range(n)]
    urls = source_urls or [None] * n
    pins = pinned or [()] * n
    conf = confidence or [None] * n
    slots = [
        ConvertedSlot(
            pokemon=_mon(name),
            source=SlotSource(label=label, url=url, pinned=pin, confidence=c),
        )
        for name, label, url, pin, c in zip(sp, sources, urls, pins, conf, strict=True)
    ]
    return ConvertResult(slots=slots), sp


class TestSourceSummary(unittest.TestCase):
    def test_all_from_bank_with_urls(self) -> None:
        urls = [f"https://pokepast.es/abc{i}" for i in range(6)]
        r, _ = _result(["bank"] * 6, source_urls=urls)
        out = _source_summary(r)
        self.assertIn("6 from bank", out)
        self.assertIn("Trained 6 mons", out)
        # Full URLs rendered so Discord turns them into hyperlinks.
        for i in range(6):
            self.assertIn(f"https://pokepast.es/abc{i}", out)

    def test_all_estimated(self) -> None:
        r, names = _result(["estimated"] * 6)
        out = _source_summary(r)
        self.assertIn("6 estimated", out)
        # Each mon listed as estimated.
        for name in names:
            self.assertIn(f"• {name} — estimated", out)

    def test_all_kept(self) -> None:
        r, _ = _result(["kept"] * 6)
        out = _source_summary(r)
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
        out = _source_summary(r)
        self.assertIn("3 from bank", out)
        self.assertIn("2 estimated", out)
        self.assertIn("1 already trained", out)
        self.assertIn("Trained 6 mons", out)
        self.assertIn("https://pokepast.es/a1", out)
        self.assertIn("https://pokepast.es/a2", out)
        self.assertIn("https://pokepast.es/a3", out)
        self.assertIn("• Garchomp — estimated", out)

    def test_usage_rendered_with_confidence(self) -> None:
        r, _ = _result(
            ["usage", "usage"],
            species=["Incineroar", "Rillaboom"],
            confidence=["high", "low"],
        )
        out = _source_summary(r)
        self.assertIn("2 from usage stats", out)
        self.assertIn("• Incineroar — usage stats (high)", out)
        self.assertIn("• Rillaboom — usage stats (low)", out)

    def test_empty(self) -> None:
        r, _ = _result([])
        out = _source_summary(r)
        self.assertIn("0 matched", out)

    def test_pinned_stats_rendered_as_suffix(self) -> None:
        r, _ = _result(
            ["bank", "estimated"],
            source_urls=["https://pokepast.es/a1", None],
            species=["Venusaur", "Charizard"],
            pinned=[("hp",), ("hp", "spe")],
        )
        out = _source_summary(r)
        self.assertIn("• Venusaur — https://pokepast.es/a1 (HP pinned)", out)
        self.assertIn("• Charizard — estimated (HP, Spe pinned)", out)

    def test_no_pin_suffix_when_unpinned(self) -> None:
        r, _ = _result(["estimated"], species=["Garchomp"])
        out = _source_summary(r)
        self.assertIn("• Garchomp — estimated", out)
        self.assertNotIn("pinned", out)


def _capture_convert_ots_callback(registry, anthropic_client):
    """Register `/convert-ots` on a stub tree and return the raw callback.

    The real `@app_commands.choices` / `@app_commands.describe` decorators run
    against the coroutine and return it unchanged; the stub `tree.command`
    captures it so the handler body can be invoked directly.
    """
    captured: dict[str, object] = {}

    class _Tree:
        def command(self, **_kwargs):
            def deco(fn):
                captured["fn"] = fn
                return fn

            return deco

    convert_ots.register(_Tree(), registry, anthropic_client=anthropic_client)
    return captured["fn"]


class TestConvertOtsVRPasteRouting:
    """The URL path must route a VRPaste URL through the shared dispatcher.

    Regression guard: an earlier convert-ots validated the input as a
    Pokepaste URL and rejected VRPaste links with "doesn't look like a
    Pokepaste URL". The handler now defers to `fetch_team_from_url`, which
    classifies and fetches both sources.
    """

    async def test_vrpaste_url_is_fetched_and_converted(self) -> None:
        registry = MagicMock()
        anthropic_client = MagicMock()
        callback = _capture_convert_ots_callback(registry, anthropic_client)

        interaction = MagicMock()
        interaction.user.id = 111
        interaction.guild_id = 222
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()

        fmt_name = next(iter(config.FORMAT_SHEETS))
        choice = app_commands.Choice(name=fmt_name, value=fmt_name)

        ots = TeamData(pokemon=[_mon(f"Mon{i}") for i in range(6)])
        url = "https://www.vrpastes.com/rHYxiZMf"
        sheets = MagicMock()

        with (
            patch.object(
                convert_ots, "_resolve_guild_sheets", AsyncMock(return_value=sheets)
            ),
            patch.object(
                convert_ots, "fetch_team_from_url", AsyncMock(return_value=ots)
            ) as fetch,
            patch.object(convert_ots, "_run_conversion", AsyncMock()) as run,
        ):
            await callback(interaction, choice, url=url)

        # The VRPaste URL goes through the shared classifier/fetcher, not a
        # Pokepaste-only validator, and flows into the conversion pipeline.
        fetch.assert_awaited_once_with(url)
        run.assert_awaited_once()
        assert run.call_args.kwargs["ots"] is ots
        # No user-facing validation error was emitted.
        interaction.followup.send.assert_not_called()
        interaction.response.defer.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
