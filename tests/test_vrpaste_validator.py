import pytest

from sketch.pokepaste.validator import ValidationError
from sketch.vrpaste.validator import (
    canonicalize_vrpaste_url,
    extract_vrpaste_id,
)


class TestCanonicalizeVRPasteUrl:
    @pytest.mark.parametrize(
        "variant",
        [
            "https://www.vrpastes.com/gxmfscC1",
            "https://www.vrpastes.com/gxmfscC1/",
            "https://vrpastes.com/gxmfscC1",
            "http://www.vrpastes.com/gxmfscC1",
            "http://vrpastes.com/gxmfscC1/",
            "  https://www.vrpastes.com/gxmfscC1  ",
        ],
    )
    def test_equivalent_forms_collapse_to_canonical(self, variant):
        # The www-prefixed https form with no trailing slash is the
        # canonical we hand to find_row_by_url-style dedup.
        assert canonicalize_vrpaste_url(variant) == "https://www.vrpastes.com/gxmfscC1"

    def test_preserves_id_case(self):
        # VRPaste ids are case-sensitive (gxmfscC1 != gxmfscc1).
        assert canonicalize_vrpaste_url(
            "https://www.vrpastes.com/AbCdEf"
        ) != canonicalize_vrpaste_url("https://www.vrpastes.com/abcdef")

    @pytest.mark.parametrize(
        "value",
        [
            "https://example.com/abc",
            "https://vrpaste.com/abc",  # missing 's' in host
            "https://www.vrpastes.com/",  # no id
            "https://www.vrpastes.com/abc?query=1",  # query string disallowed
            "ftp://www.vrpastes.com/abc",
            "",
        ],
    )
    def test_rejects_malformed(self, value):
        with pytest.raises(ValidationError):
            canonicalize_vrpaste_url(value)


class TestExtractVRPasteId:
    def test_extracts_id_portion(self):
        assert extract_vrpaste_id("https://www.vrpastes.com/gxmfscC1") == "gxmfscC1"

    def test_extracts_id_from_non_www_host(self):
        assert extract_vrpaste_id("https://vrpastes.com/abc123") == "abc123"

    def test_extracts_id_with_trailing_slash(self):
        assert extract_vrpaste_id("https://www.vrpastes.com/abc/") == "abc"

    def test_rejects_malformed(self):
        with pytest.raises(ValidationError):
            extract_vrpaste_id("not a url")
