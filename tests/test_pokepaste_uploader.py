"""Tests for the pokepast.es uploader.

The uploader is mocked with `aioresponses` so the suite never makes a
real POST.
"""

from __future__ import annotations

import pytest
from aioresponses import aioresponses

from sketch.pokepaste.uploader import PokepasteUploadError, post_to_pokepaste


class TestPostToPokepaste:
    async def test_returns_canonical_url_from_relative_location(self):
        # /create responds with a 303 whose Location header is the new
        # paste's path (typically relative, e.g. "/abc123def"). We promote
        # to absolute and canonicalize.
        with aioresponses() as mock:
            mock.post(
                "https://pokepast.es/create",
                status=303,
                headers={"Location": "/abc123def"},
            )
            url = await post_to_pokepaste("paste text", "Replica QBXXWXL05U")
        assert url == "https://pokepast.es/abc123def"

    async def test_canonicalizes_absolute_location(self):
        # Server might respond with the full URL, http scheme, or trailing
        # slash — the canonicalizer collapses to the dedup-shape.
        with aioresponses() as mock:
            mock.post(
                "https://pokepast.es/create",
                status=302,
                headers={"Location": "http://pokepast.es/xyz789/"},
            )
            url = await post_to_pokepaste("paste text", "Replica BBBB222233")
        assert url == "https://pokepast.es/xyz789"

    async def test_non_redirect_status_raises(self):
        # 200 / 4xx / 5xx are all unexpected — pokepast.es should always
        # 30x on a successful create.
        with aioresponses() as mock:
            mock.post(
                "https://pokepast.es/create",
                status=404,
                body="404 page not found",
            )
            with pytest.raises(PokepasteUploadError, match="pokepast.es"):
                await post_to_pokepaste("paste text", "Replica CCCC333344")

    async def test_5xx_status_raises(self):
        with aioresponses() as mock:
            mock.post(
                "https://pokepast.es/create",
                status=500,
                body="Internal Server Error",
            )
            with pytest.raises(PokepasteUploadError, match="pokepast.es"):
                await post_to_pokepaste("paste text", "Replica DDDD444455")

    async def test_redirect_without_location_raises(self):
        # A 30x without a Location header is malformed; surface the error
        # rather than returning an empty / nonsensical URL.
        with aioresponses() as mock:
            mock.post(
                "https://pokepast.es/create",
                status=303,
                headers={},
            )
            with pytest.raises(PokepasteUploadError, match="pokepast.es"):
                await post_to_pokepaste("paste text", "Replica EEEE555566")
