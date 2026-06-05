"""Tests for `sketch.pokepaste.fetcher.fetch_pokepaste_raw`.

Network is mocked via `aioresponses`. The fetcher canonicalizes the URL
and reads the `<paste-url>/raw` view, preserving CRLF line endings (which
the renderer requires for re-minting).
"""

import pytest
from aioresponses import aioresponses

from sketch.pokepaste.fetcher import (
    PokepasteFetchError,
    fetch_pokepaste,
    fetch_pokepaste_raw,
)

_RAW_BODY = (
    "Garganacl @ Sitrus Berry\r\n"
    "Ability: Purifying Salt\r\n"
    "EVs: 196 HP / 76 Def / 236 SpD\r\n"
    "Careful Nature\r\n"
    "- Salt Cure\r\n"
    "- Recover\r\n"
    "- Wide Guard\r\n"
    "- Protect"
)


@pytest.mark.asyncio
async def test_fetch_returns_raw_text_with_crlf_preserved():
    with aioresponses() as mock:
        mock.get("https://pokepast.es/abc123/raw", status=200, body=_RAW_BODY)
        text = await fetch_pokepaste_raw("https://pokepast.es/abc123")
    assert text == _RAW_BODY
    assert "\r\n" in text


@pytest.mark.asyncio
async def test_fetch_canonicalizes_before_appending_raw():
    # Trailing slash + http scheme should canonicalize to the https,
    # no-trailing-slash form before `/raw` is appended.
    with aioresponses() as mock:
        mock.get("https://pokepast.es/abc123/raw", status=200, body=_RAW_BODY)
        text = await fetch_pokepaste_raw("http://pokepast.es/abc123/")
    assert text == _RAW_BODY


@pytest.mark.asyncio
async def test_fetch_non_200_raises():
    with aioresponses() as mock:
        mock.get("https://pokepast.es/missing/raw", status=404, body="not found")
        with pytest.raises(PokepasteFetchError):
            await fetch_pokepaste_raw("https://pokepast.es/missing")


@pytest.mark.asyncio
async def test_fetch_pokepaste_returns_parsed_teamdata():
    # An OTS block (no EV line); parse_showdown requires a full 6-Pokemon team.
    ots_block = (
        "Garganacl @ Sitrus Berry\r\n"
        "Ability: Purifying Salt\r\n"
        "Careful Nature\r\n"
        "- Salt Cure\r\n"
        "- Recover\r\n"
        "- Wide Guard\r\n"
        "- Protect"
    )
    six_mon_body = "\r\n\r\n".join([ots_block] * 6)
    with aioresponses() as mock:
        mock.get("https://pokepast.es/abc123/raw", status=200, body=six_mon_body)
        team = await fetch_pokepaste("https://pokepast.es/abc123")
    assert len(team.pokemon) == 6
    assert team.pokemon[0].species == "Garganacl"
