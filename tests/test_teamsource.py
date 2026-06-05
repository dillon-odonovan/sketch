"""Tests for sketch.teamsource URL classification and resolution."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from sketch.team import PokemonEntry, TeamData
from sketch.teamsource import (
    TeamUrlKind,
    UnsupportedTeamUrlError,
    classify_team_url,
    fetch_team_from_url,
    unsupported_team_url_message,
)

_POKEPASTE_URL = "https://pokepast.es/abc123"
_VRPASTE_URL = "https://www.vrpastes.com/abc123"


def _team() -> TeamData:
    return TeamData(
        pokemon=[
            PokemonEntry(
                species="Pikachu",
                gender=None,
                item=None,
                ability="Static",
                nature="Timid",
                evs={},
                moves=["Thunderbolt"],
            )
        ]
    )


class TestClassifyTeamUrl(unittest.TestCase):
    def test_pokepaste(self) -> None:
        self.assertIs(classify_team_url(_POKEPASTE_URL), TeamUrlKind.POKEPASTE)

    def test_vrpaste(self) -> None:
        self.assertIs(classify_team_url(_VRPASTE_URL), TeamUrlKind.VRPASTE)

    def test_unrecognized(self) -> None:
        self.assertIsNone(classify_team_url("https://example.com/whatever"))

    def test_empty(self) -> None:
        self.assertIsNone(classify_team_url(""))

    def test_message_names_both_services(self) -> None:
        msg = unsupported_team_url_message("https://example.com/x")
        self.assertIn("Pokepaste", msg)
        self.assertIn("VRPaste", msg)
        self.assertIn("https://example.com/x", msg)


class TestFetchTeamFromUrl(unittest.IsolatedAsyncioTestCase):
    async def test_vrpaste_delegates_to_fetch_vrpaste(self) -> None:
        team = _team()
        with patch(
            "sketch.teamsource.fetch_vrpaste",
            new=AsyncMock(return_value=team),
        ) as fetch_vrpaste:
            result = await fetch_team_from_url(_VRPASTE_URL)
        self.assertIs(result, team)
        fetch_vrpaste.assert_awaited_once_with(_VRPASTE_URL)

    async def test_pokepaste_fetches_raw_then_parses(self) -> None:
        team = _team()
        with (
            patch(
                "sketch.teamsource.fetch_pokepaste_raw",
                new=AsyncMock(return_value="raw showdown text"),
            ) as fetch_raw,
            patch(
                "sketch.teamsource.parse_showdown", return_value=team
            ) as parse_showdown,
        ):
            result = await fetch_team_from_url(_POKEPASTE_URL)
        self.assertIs(result, team)
        fetch_raw.assert_awaited_once_with(_POKEPASTE_URL)
        parse_showdown.assert_called_once_with("raw showdown text")

    async def test_unrecognized_raises(self) -> None:
        with self.assertRaises(UnsupportedTeamUrlError):
            await fetch_team_from_url("https://example.com/nope")


if __name__ == "__main__":
    unittest.main()
