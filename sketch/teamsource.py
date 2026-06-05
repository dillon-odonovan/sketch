"""Classify and resolve a team-sharing URL (Pokepaste or VRPaste).

`/convert-ots`, `/add-team`, and `/delete-team` all branch on whether a
user-supplied URL is a Pokepaste or a VRPaste. This module centralizes
that classification and the shared "unrecognized URL" message so the
branch shape and copy live in one place.

It also exposes `fetch_team_from_url` for callers that just want the
parsed team regardless of source — mirroring `fetch_vrpaste`'s
URL→`TeamData` contract for Pokepaste too (which otherwise only offers
`fetch_pokepaste_raw`, the raw-text form `/add-team` and the bank loader
still need).
"""

from __future__ import annotations

from enum import StrEnum

from sketch.champions.showdown_parser import parse_showdown
from sketch.pokepaste.fetcher import fetch_pokepaste_raw
from sketch.pokepaste.validator import ValidationError, is_pokepaste_url
from sketch.team import TeamData
from sketch.vrpaste.fetcher import fetch_vrpaste
from sketch.vrpaste.validator import is_vrpaste_url


class TeamUrlKind(StrEnum):
    """Which team-sharing service a URL points at."""

    POKEPASTE = "pokepaste"
    VRPASTE = "vrpaste"


class UnsupportedTeamUrlError(ValidationError):
    """Raised when a URL is neither a Pokepaste nor a VRPaste.

    Subclasses `ValidationError` so handlers already catching that type
    for URL-shaped input surface this the same way. Message is
    user-facing.
    """


def classify_team_url(url: str) -> TeamUrlKind | None:
    """Return the `TeamUrlKind` for `url`, or `None` if it's neither."""
    if is_vrpaste_url(url):
        return TeamUrlKind.VRPASTE
    if is_pokepaste_url(url):
        return TeamUrlKind.POKEPASTE
    return None


def unsupported_team_url_message(url: str) -> str:
    """The shared user-facing error for a URL we don't recognize."""
    return (
        f"`{url}` doesn't look like a Pokepaste or VRPaste URL. "
        "Expected something like `https://pokepast.es/abc123` or "
        "`https://www.vrpastes.com/abc123`."
    )


async def fetch_team_from_url(url: str) -> TeamData:
    """Resolve a Pokepaste or VRPaste `url` into a parsed `TeamData`.

    VRPaste resolves via `fetch_vrpaste`; Pokepaste fetches the raw
    Showdown export and parses it with `parse_showdown`. Raises
    `UnsupportedTeamUrlError` for anything else, and propagates the
    source-specific fetch/parse errors (`VRPasteFetchError`,
    `PokepasteFetchError`, `ValidationError`, `ShowdownParseError`)
    unchanged so callers can surface them to the user.
    """
    kind = classify_team_url(url)
    if kind is TeamUrlKind.VRPASTE:
        return await fetch_vrpaste(url)
    if kind is TeamUrlKind.POKEPASTE:
        return parse_showdown(await fetch_pokepaste_raw(url))
    raise UnsupportedTeamUrlError(unsupported_team_url_message(url))
