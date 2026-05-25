import pytest

from sketch.champions.replica_validator import normalize_replica
from sketch.pokepaste.validator import ValidationError


class TestNormalizeReplica:
    def test_uppercases_valid_alphanumeric(self):
        assert normalize_replica("abcdef0123") == "ABCDEF0123"

    def test_passes_through_already_uppercase(self):
        assert normalize_replica("ABCDEF0123") == "ABCDEF0123"

    def test_accepts_mixed_case(self):
        assert normalize_replica("aB1cD2eF3a") == "AB1CD2EF3A"

    def test_accepts_non_hex_letters(self):
        assert normalize_replica("X8XJ7PDMJ2") == "X8XJ7PDMJ2"
        assert normalize_replica("x8xj7pdmj2") == "X8XJ7PDMJ2"

    def test_accepts_real_champions_team_id(self):
        # QBXXWXL05U is the in-game Team ID from the sample Replica share
        # screen. It contains Q/X/W/L which were rejected under the earlier
        # hex-only assumption — this guards against regressing to that.
        assert normalize_replica("QBXXWXL05U") == "QBXXWXL05U"

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "abc",
            "abcdef01234",  # 11 chars
            "abcdef012",  # 9 chars
            "abcdef 123",  # space inside
            "abcdef-123",  # punctuation inside
        ],
    )
    def test_rejects_invalid(self, value):
        with pytest.raises(ValidationError):
            normalize_replica(value)
