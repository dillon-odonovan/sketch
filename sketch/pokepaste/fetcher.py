"""Fetch a Pokepaste's raw Showdown export text.

pokepast.es serves the canonical, parseable export of any paste at
`<paste-url>/raw` (CRLF line endings preserved). That raw text is exactly
the Showdown shape the rest of the bot already understands — the in-sheet
`TEAMDATAFROMPASTE` formula, the replica cache's `paste_text`, and
`sketch.showdown.renderer` all speak it.

Used by `/add-team` to seed the global Replica Cache when a user supplies
a Pokepaste URL alongside a Champions Team ID: rather than OCR screenshots
we already have the team's paste, so we fetch it directly and cache it
keyed on the code.
"""

from __future__ import annotations

import logging

import aiohttp

from sketch.pokepaste.validator import canonicalize_pokepaste_url
from sketch.showdown.parser import parse_showdown
from sketch.team import TeamData

logger = logging.getLogger(__name__)


class PokepasteFetchError(Exception):
    """Raised when we couldn't read a Pokepaste's raw text.

    Callers treat fetching as best-effort, so this is logged at the call
    site rather than surfaced to the user — diagnostic detail (URL, HTTP
    status) goes to WARNING-level logs.
    """


async def fetch_pokepaste_raw(url: str) -> str:
    """Return the raw Showdown export text for a Pokepaste `url`.

    Canonicalizes `url` (so trailing-slash / scheme variants converge),
    then GETs the `/raw` view. CRLF line endings are preserved — they're
    required for re-minting via `post_to_pokepaste`. Raises
    `PokepasteFetchError` on a non-200 status or transport failure.
    """
    raw_url = f"{canonicalize_pokepaste_url(url)}/raw"
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                raw_url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp,
        ):
            if resp.status != 200:
                logger.warning(
                    "Pokepaste /raw returned HTTP %s for %s", resp.status, raw_url
                )
                raise PokepasteFetchError(
                    f"Could not fetch `{raw_url}`: HTTP {resp.status}."
                )
            return await resp.text()
    except aiohttp.ClientError as exc:
        logger.warning("Pokepaste /raw transport error for %s: %s", raw_url, exc)
        raise PokepasteFetchError(f"Could not fetch `{raw_url}`: {exc}") from exc


async def fetch_pokepaste(url: str) -> TeamData:
    """Resolve a Pokepaste `url` into a parsed `TeamData`.

    The URL→`TeamData` counterpart to `sketch.vrpaste.fetcher.fetch_vrpaste`:
    fetches the raw Showdown export via `fetch_pokepaste_raw` and parses it
    with `parse_showdown`. Callers that need the raw text (the replica-cache
    seed in `/add-team`, the bank loader) use `fetch_pokepaste_raw` directly.

    Raises `PokepasteFetchError` (fetch), `ValidationError` (URL shape), or
    `ShowdownParseError` (parse).
    """
    return parse_showdown(await fetch_pokepaste_raw(url))
