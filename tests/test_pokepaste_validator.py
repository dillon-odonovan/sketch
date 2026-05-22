import pytest
from aioresponses import aioresponses

from pokepaste_validator import (
    ValidationError,
    normalize_replica,
    validate_pokepaste_url,
)


class TestNormalizeReplica:
    def test_uppercases_valid_hex(self):
        assert normalize_replica("abcdef0123") == "ABCDEF0123"

    def test_passes_through_already_uppercase(self):
        assert normalize_replica("ABCDEF0123") == "ABCDEF0123"

    def test_accepts_mixed_case(self):
        assert normalize_replica("aB1cD2eF3a") == "AB1CD2EF3A"

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "abc",
            "abcdefghij",  # non-hex letters
            "abcdef01234",  # 11 chars
            "abcdef012",  # 9 chars
            "abcdef 123",  # space
        ],
    )
    def test_rejects_invalid(self, value):
        with pytest.raises(ValidationError):
            normalize_replica(value)


class TestValidatePokepasteUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/abc",
            "http://pokepaste.es/abc",  # wrong host (missing the dot)
            "https://pokepast.es/",  # no id
            "https://pokepast.es/abc?query",  # query string disallowed
            "ftp://pokepast.es/abc",
            "",
        ],
    )
    async def test_rejects_malformed_url(self, url):
        with pytest.raises(ValidationError):
            await validate_pokepaste_url(url)

    async def test_accepts_valid_url_when_fetch_succeeds(self):
        url = "https://pokepast.es/abc123"
        with aioresponses() as mock:
            mock.get(url, status=200)
            await validate_pokepaste_url(url)

    async def test_accepts_trailing_slash(self):
        url = "https://pokepast.es/abc123/"
        with aioresponses() as mock:
            mock.get(url, status=200)
            await validate_pokepaste_url(url)

    async def test_rejects_when_fetch_returns_non_200(self):
        url = "https://pokepast.es/abc123"
        with aioresponses() as mock:
            mock.get(url, status=404)
            with pytest.raises(ValidationError, match="HTTP 404"):
                await validate_pokepaste_url(url)
