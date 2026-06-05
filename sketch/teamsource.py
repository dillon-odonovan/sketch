"""Classify and resolve a team-sharing URL (Pokepaste or VRPaste).

`/convert-ots`, `/add-team`, and `/delete-team` all branch on whether a
user-supplied URL is a Pokepaste or a VRPaste. This module centralizes
that classification and the shared "unrecognized URL" message so the
branch shape and copy live in one place.

`fetch_team_from_url` dispatches to the source-specific URL→`TeamData`
fetchers (`fetch_pokepaste` / `fetch_vrpaste`) for callers that just want
the parsed team regardless of source.
"""

from __future__ import annotations

from enum import StrEnum

from sketch.pokepaste.fetcher import fetch_pokepaste
from sketch.pokepaste.validator import ValidationError, is_pokepaste_url
from sketch.team import TeamData
from sketch.vrpaste.fetcher import fetch_vrpaste
from sketch.vrpaste.validator import is_vrpaste_url


class TeamUrlSource(StrEnum):
    """Which team-sharing service a URL points at."""

    POKEPASTE = "pokepaste"
    VRPASTE = "vrpaste"


class UnsupportedTeamUrlError(ValidationError):
    """Raised when a URL is neither a Pokepaste nor a VRPaste.

    Subclasses `ValidationError` so handlers already catching that type
    for URL-shaped input surface this the same way. Message is
    user-facing.
    """


def classify_team_url(url: str) -> TeamUrlSource | None:
    """Return the `TeamUrlSource` for `url`, or `None` if it's neither."""
    if is_vrpaste_url(url):
        return TeamUrlSource.VRPASTE
    if is_pokepaste_url(url):
        return TeamUrlSource.POKEPASTE
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

    Dispatches to `fetch_pokepaste` / `fetch_vrpaste`. Raises
    `UnsupportedTeamUrlError` for anything else, and propagates the
    source-specific fetch/parse errors (`VRPasteFetchError`,
    `PokepasteFetchError`, `ValidationError`, `ShowdownParseError`)
    unchanged so callers can surface them to the user.
    """
    source = classify_team_url(url)
    if source is TeamUrlSource.VRPASTE:
        return await fetch_vrpaste(url)
    if source is TeamUrlSource.POKEPASTE:
        return await fetch_pokepaste(url)
    raise UnsupportedTeamUrlError(unsupported_team_url_message(url))
