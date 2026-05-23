"""Unit tests for the pure-function pieces of commands.py.

The Discord-facing slash command handlers themselves aren't exercised here —
they'd need extensive mocking of `discord.Interaction`, `CommandTree`, and the
closure capture inside `setup_commands`. Instead we lift the bug-prone bits
(currently the match-filter loop) into module-private helpers and test those.
"""

import pytest

from commands import _filter_team_rows
from sheets_client import TeamRow


def _row(
    row_number: int,
    url: str,
    description: str,
    species: list[str],
) -> TeamRow:
    return TeamRow(
        row_number=row_number,
        url=url,
        description=description,
        species=species,
    )


@pytest.fixture
def bank() -> list[TeamRow]:
    """A small representative team bank used across filter tests."""
    return [
        _row(
            3,
            "https://pokepast.es/aaaa1111",
            "jsmithvgc — Calyrex-S balance",
            [
                "Calyrex-Shadow",
                "Urshifu",
                "Amoonguss",
                "Rillaboom",
                "Incineroar",
                "Tornadus",
            ],
        ),
        _row(
            4,
            "https://pokepast.es/bbbb2222",
            "alice — Charizard hyper offense",
            [
                "Charizard-Mega-Y",
                "Tyranitar",
                "Garchomp",
                "Tapu Lele",
                "Greninja",
                "Heatran",
            ],
        ),
        _row(
            5,
            "http://pokepast.es/cccc3333/",
            "bob — sun team",
            ["Charizard", "Venusaur", "Excadrill", "Tyranitar", "Latios", "Cresselia"],
        ),
    ]


class TestNoFilters:
    def test_returns_all_rows_when_no_filters_applied(self, bank):
        result = _filter_team_rows(
            bank,
            resolved_groups=[],
            description_query=None,
            url_target=None,
        )
        assert result == bank

    def test_returns_empty_list_for_empty_input(self):
        result = _filter_team_rows(
            [],
            resolved_groups=[],
            description_query=None,
            url_target=None,
        )
        assert result == []


class TestMonFilter:
    def test_single_group_matches_when_row_contains_the_species(self, bank):
        result = _filter_team_rows(
            bank,
            resolved_groups=[["Calyrex-Shadow"]],
            description_query=None,
            url_target=None,
        )
        assert [r.row_number for r in result] == [3]

    def test_species_match_is_case_insensitive(self, bank):
        result = _filter_team_rows(
            bank,
            resolved_groups=[["calyrex-shadow"]],
            description_query=None,
            url_target=None,
        )
        assert [r.row_number for r in result] == [3]

    def test_group_with_multiple_forms_matches_any(self, bank):
        # Mimics how `Charizard` resolves to base + Mega-X + Mega-Y: a row
        # passes the group if any form is present.
        result = _filter_team_rows(
            bank,
            resolved_groups=[["Charizard", "Charizard-Mega-X", "Charizard-Mega-Y"]],
            description_query=None,
            url_target=None,
        )
        assert sorted(r.row_number for r in result) == [4, 5]

    def test_multiple_groups_must_all_match_AND(self, bank):
        # alice's team has both Charizard-Mega-Y AND Tyranitar.
        # bob's team has Charizard (base) AND Tyranitar.
        # Both should match an AND of (Charizard-family) and (Tyranitar).
        result = _filter_team_rows(
            bank,
            resolved_groups=[
                ["Charizard", "Charizard-Mega-X", "Charizard-Mega-Y"],
                ["Tyranitar"],
            ],
            description_query=None,
            url_target=None,
        )
        assert sorted(r.row_number for r in result) == [4, 5]

    def test_AND_returns_empty_when_groups_dont_co_occur(self, bank):
        # No row has both Calyrex-Shadow AND Charizard.
        result = _filter_team_rows(
            bank,
            resolved_groups=[["Calyrex-Shadow"], ["Charizard"]],
            description_query=None,
            url_target=None,
        )
        assert result == []


class TestDescriptionFilter:
    def test_substring_match_case_insensitive(self, bank):
        result = _filter_team_rows(
            bank,
            resolved_groups=[],
            description_query="JSMITHVGC",
            url_target=None,
        )
        assert [r.row_number for r in result] == [3]

    def test_substring_match_returns_no_rows_when_absent(self, bank):
        result = _filter_team_rows(
            bank,
            resolved_groups=[],
            description_query="nonexistent-player",
            url_target=None,
        )
        assert result == []


class TestUrlFilter:
    def test_exact_canonical_match(self, bank):
        result = _filter_team_rows(
            bank,
            resolved_groups=[],
            description_query=None,
            url_target="https://pokepast.es/aaaa1111",
        )
        assert [r.row_number for r in result] == [3]

    def test_normalization_collapses_http_and_trailing_slash(self, bank):
        # Row 5's stored URL is `http://pokepast.es/cccc3333/` — different
        # scheme AND trailing slash. The target is the canonical form. The
        # helper should canonicalize the stored URL per-row and find a match.
        result = _filter_team_rows(
            bank,
            resolved_groups=[],
            description_query=None,
            url_target="https://pokepast.es/cccc3333",
        )
        assert [r.row_number for r in result] == [5]

    def test_paste_id_case_is_significant(self, bank):
        # pokepast.es IDs are case-sensitive — `aaaa1111` (row 3) must NOT
        # match `AAAA1111`.
        result = _filter_team_rows(
            bank,
            resolved_groups=[],
            description_query=None,
            url_target="https://pokepast.es/AAAA1111",
        )
        assert result == []

    def test_malformed_stored_url_falls_through(self):
        # If a row has a malformed URL stored (e.g., =HYPERLINK display text
        # leaking through FORMATTED_VALUE), the helper should treat that row
        # as non-matching rather than letting ValidationError escape.
        rows = [
            _row(3, "not a url at all", "garbage row", ["Pikachu"] * 6),
            _row(4, "https://pokepast.es/abcd1234", "real row", ["Pikachu"] * 6),
        ]
        result = _filter_team_rows(
            rows,
            resolved_groups=[],
            description_query=None,
            url_target="https://pokepast.es/abcd1234",
        )
        assert [r.row_number for r in result] == [4]


class TestCombinedFilters:
    def test_mon_and_description_AND(self, bank):
        result = _filter_team_rows(
            bank,
            resolved_groups=[["Charizard", "Charizard-Mega-X", "Charizard-Mega-Y"]],
            description_query="alice",
            url_target=None,
        )
        assert [r.row_number for r in result] == [4]

    def test_mon_and_url_AND_returns_one_when_both_match(self, bank):
        result = _filter_team_rows(
            bank,
            resolved_groups=[["Tyranitar"]],
            description_query=None,
            url_target="https://pokepast.es/bbbb2222",
        )
        assert [r.row_number for r in result] == [4]

    def test_mon_and_url_AND_returns_empty_when_mon_absent_from_url_row(self, bank):
        # Row 4 (bbbb2222) has Tyranitar but not Calyrex-Shadow.
        result = _filter_team_rows(
            bank,
            resolved_groups=[["Calyrex-Shadow"]],
            description_query=None,
            url_target="https://pokepast.es/bbbb2222",
        )
        assert result == []

    def test_all_three_filters_AND(self, bank):
        result = _filter_team_rows(
            bank,
            resolved_groups=[["Charizard", "Charizard-Mega-X", "Charizard-Mega-Y"]],
            description_query="alice",
            url_target="https://pokepast.es/bbbb2222",
        )
        assert [r.row_number for r in result] == [4]
