"""Unit tests for the pure-function pieces of commands.py.

The Discord-facing slash command handlers themselves aren't exercised here —
they'd need extensive mocking of `discord.Interaction`, `CommandTree`, and the
closure capture inside `setup_commands`. Instead we lift the bug-prone bits
(currently the match-filter loop) into module-private helpers and test those.

The description filter changed shape in the tokenized-search rollout:
`_filter_team_rows` no longer takes a raw `description_query` string. It now
takes a precomputed `description_match_indices: set[int] | None` — positional
indices into `rows` whose descriptions passed the upstream
`DescriptionIndex.match` call. That keeps `_filter_team_rows` ignorant of
tokenization rules (which live in `text_search`) and ensures filter
composition stays a pure boolean operation. The matcher itself is covered by
`tests/test_text_search.py`.
"""

import pytest
from discord import app_commands

from sketch import config
from sketch.commands import _SPREADSHEET_ID_RE, _filter_team_rows, _spreadsheet_link
from sketch.commands._shared import _resolve_format
from sketch.search.text_search import DescriptionIndex
from sketch.storage.sheets_client import TeamRow


def _row(
    row_number: int,
    url: str,
    description: str,
    species: list[str],
    replica: str | None = None,
) -> TeamRow:
    return TeamRow(
        row_number=row_number,
        url=url,
        description=description,
        species=species,
        replica=replica,
    )


@pytest.fixture
def bank() -> list[TeamRow]:
    """A small representative team bank used across filter tests.

    Two of the three rows carry a replica code; the third leaves replica
    unset to exercise the "row has no replica" branch in the replica filter.
    """
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
            replica="QBXXWXL05U",
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
            replica="ABCD123456",
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
            description_match_indices=None,
            url_target=None,
            replica_target=None,
        )
        assert result == bank

    def test_returns_empty_list_for_empty_input(self):
        result = _filter_team_rows(
            [],
            resolved_groups=[],
            description_match_indices=None,
            url_target=None,
            replica_target=None,
        )
        assert result == []


class TestMonFilter:
    def test_single_group_matches_when_row_contains_the_species(self, bank):
        result = _filter_team_rows(
            bank,
            resolved_groups=[["Calyrex-Shadow"]],
            description_match_indices=None,
            url_target=None,
            replica_target=None,
        )
        assert [r.row_number for r in result] == [3]

    def test_species_match_is_case_insensitive(self, bank):
        result = _filter_team_rows(
            bank,
            resolved_groups=[["calyrex-shadow"]],
            description_match_indices=None,
            url_target=None,
            replica_target=None,
        )
        assert [r.row_number for r in result] == [3]

    def test_group_with_multiple_forms_matches_any(self, bank):
        # Mimics how `Charizard` resolves to base + Mega-X + Mega-Y: a row
        # passes the group if any form is present.
        result = _filter_team_rows(
            bank,
            resolved_groups=[["Charizard", "Charizard-Mega-X", "Charizard-Mega-Y"]],
            description_match_indices=None,
            url_target=None,
            replica_target=None,
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
            description_match_indices=None,
            url_target=None,
            replica_target=None,
        )
        assert sorted(r.row_number for r in result) == [4, 5]

    def test_AND_returns_empty_when_groups_dont_co_occur(self, bank):
        # No row has both Calyrex-Shadow AND Charizard.
        result = _filter_team_rows(
            bank,
            resolved_groups=[["Calyrex-Shadow"], ["Charizard"]],
            description_match_indices=None,
            url_target=None,
            replica_target=None,
        )
        assert result == []


class TestDescriptionFilter:
    """`description_match_indices` is positional into `rows`.

    These tests pass hand-built index sets to verify the boolean composition,
    not the tokenizer (that's in `tests/test_text_search.py`). See
    `TestDescriptionPipeline` below for end-to-end coverage that the index
    positions line up with the rows list passed in.
    """

    def test_includes_only_rows_whose_position_is_in_set(self, bank):
        # `bank` row at position 0 has row_number=3 (jsmithvgc — Calyrex-S).
        result = _filter_team_rows(
            bank,
            resolved_groups=[],
            description_match_indices={0},
            url_target=None,
            replica_target=None,
        )
        assert [r.row_number for r in result] == [3]

    def test_empty_match_set_excludes_every_row(self, bank):
        # `description_match_indices=set()` means "filter applied, zero
        # rows matched the query." All rows must drop, even though the other
        # filters are no-ops.
        result = _filter_team_rows(
            bank,
            resolved_groups=[],
            description_match_indices=set(),
            url_target=None,
            replica_target=None,
        )
        assert result == []

    def test_none_match_set_skips_description_filter_entirely(self, bank):
        # `description_match_indices=None` is the "no description filter"
        # sentinel — distinct from an empty set. All rows pass.
        result = _filter_team_rows(
            bank,
            resolved_groups=[],
            description_match_indices=None,
            url_target=None,
            replica_target=None,
        )
        assert [r.row_number for r in result] == [3, 4, 5]


class TestUrlFilter:
    def test_exact_canonical_match(self, bank):
        result = _filter_team_rows(
            bank,
            resolved_groups=[],
            description_match_indices=None,
            url_target="https://pokepast.es/aaaa1111",
            replica_target=None,
        )
        assert [r.row_number for r in result] == [3]

    def test_normalization_collapses_http_and_trailing_slash(self, bank):
        # Row 5's stored URL is `http://pokepast.es/cccc3333/` — different
        # scheme AND trailing slash. The target is the canonical form. The
        # helper should canonicalize the stored URL per-row and find a match.
        result = _filter_team_rows(
            bank,
            resolved_groups=[],
            description_match_indices=None,
            url_target="https://pokepast.es/cccc3333",
            replica_target=None,
        )
        assert [r.row_number for r in result] == [5]

    def test_paste_id_case_is_significant(self, bank):
        # pokepast.es IDs are case-sensitive — `aaaa1111` (row 3) must NOT
        # match `AAAA1111`.
        result = _filter_team_rows(
            bank,
            resolved_groups=[],
            description_match_indices=None,
            url_target="https://pokepast.es/AAAA1111",
            replica_target=None,
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
            description_match_indices=None,
            url_target="https://pokepast.es/abcd1234",
            replica_target=None,
        )
        assert [r.row_number for r in result] == [4]

    def test_short_circuits_pokemon_and_description(self, bank):
        # Pass a mon group and a description set that, together, would
        # exclude row 4 — but row 4's URL is the target. URL is uniquely
        # identifying, so mons and description must be ignored and row 4
        # comes back.
        result = _filter_team_rows(
            bank,
            resolved_groups=[["Calyrex-Shadow"]],
            description_match_indices=set(),
            url_target="https://pokepast.es/bbbb2222",
            replica_target=None,
        )
        assert [r.row_number for r in result] == [4]


class TestReplicaFilter:
    def test_exact_match(self, bank):
        # Row 3's replica is "QBXXWXL05U" — already uppercase.
        result = _filter_team_rows(
            bank,
            resolved_groups=[],
            description_match_indices=None,
            url_target=None,
            replica_target="QBXXWXL05U",
        )
        assert [r.row_number for r in result] == [3]

    def test_stored_lowercase_match_via_upper(self, bank):
        # Guard for rows written before consistent uppercasing was in place:
        # the stored value is lowercased but the target (already normalized)
        # is uppercase. The helper uppercases the stored side before compare.
        rows = list(bank)
        rows.append(
            _row(
                6,
                "https://pokepast.es/dddd4444",
                "lowercase-replica row",
                ["Pikachu"] * 6,
                replica="zzzz999999",
            )
        )
        result = _filter_team_rows(
            rows,
            resolved_groups=[],
            description_match_indices=None,
            url_target=None,
            replica_target="ZZZZ999999",
        )
        assert [r.row_number for r in result] == [6]

    def test_no_match_returns_empty(self, bank):
        # Well-formed 10-char code that isn't in the bank.
        result = _filter_team_rows(
            bank,
            resolved_groups=[],
            description_match_indices=None,
            url_target=None,
            replica_target="ZZZZZZZZZZ",
        )
        assert result == []

    def test_row_without_replica_does_not_match(self, bank):
        # Row 5 has replica=None. Filtering by any code must skip it
        # without raising on the None.upper() that doesn't get reached.
        result = _filter_team_rows(
            bank,
            resolved_groups=[],
            description_match_indices=None,
            url_target=None,
            replica_target="QBXXWXL05U",
        )
        # Row 5 not in the result; only the row whose replica matches is.
        assert 5 not in [r.row_number for r in result]

    def test_short_circuits_pokemon_and_description(self, bank):
        # Pass a mon group and an empty description set that would exclude
        # row 3 — but row 3 owns the replica code. Replica is uniquely
        # identifying, so mons and description must be ignored.
        result = _filter_team_rows(
            bank,
            resolved_groups=[["Charizard"]],
            description_match_indices=set(),
            url_target=None,
            replica_target="QBXXWXL05U",
        )
        assert [r.row_number for r in result] == [3]

    def test_replica_takes_priority_over_url(self, bank):
        # Replica points at row 3 (aaaa1111). URL points at row 4 (bbbb2222).
        # Replica wins outright — only row 3 comes back.
        result = _filter_team_rows(
            bank,
            resolved_groups=[],
            description_match_indices=None,
            url_target="https://pokepast.es/bbbb2222",
            replica_target="QBXXWXL05U",
        )
        assert [r.row_number for r in result] == [3]


class TestCombinedFilters:
    def test_mon_and_description_AND(self, bank):
        # alice's row is at position 1 (row_number=4). Position 1 alone in
        # the description set ANDed with the Charizard-family mon group must
        # return that single row.
        result = _filter_team_rows(
            bank,
            resolved_groups=[["Charizard", "Charizard-Mega-X", "Charizard-Mega-Y"]],
            description_match_indices={1},
            url_target=None,
            replica_target=None,
        )
        assert [r.row_number for r in result] == [4]


class TestDescriptionPipeline:
    """End-to-end check that `DescriptionIndex.match` positions line up with
    the `rows` list passed to `_filter_team_rows`.

    This is the regression guard for the integration: if someone shuffles
    `rows` between building the index and filtering, the positional indices
    drift silently and every description-filtered query returns garbage.
    """

    def test_full_pipeline_caly_zama_matches_calyrex_zamazenta(self):
        # Mix the descriptions in deliberately non-obvious order so the test
        # would fail if anyone re-sorted rows on the way through the pipeline.
        rows = [
            _row(7, "https://pokepast.es/aaaa", "alice — sun team", ["Pikachu"] * 6),
            _row(
                8,
                "https://pokepast.es/bbbb",
                "Calyrex Zamazenta balance",
                ["Pikachu"] * 6,
            ),
            _row(9, "https://pokepast.es/cccc", "Toxapex pivot", ["Pikachu"] * 6),
        ]
        index = DescriptionIndex.from_descriptions(r.description for r in rows)

        # Per-token prefix that subsumes the substring kernel: caly + zama.
        match_indices = index.match("caly zama")
        result = _filter_team_rows(
            rows,
            resolved_groups=[],
            description_match_indices=match_indices,
            url_target=None,
            replica_target=None,
        )
        assert [r.row_number for r in result] == [8]

    def test_full_pipeline_pex_matches_toxapex(self):
        rows = [
            _row(3, "https://pokepast.es/x", "Calyrex Shadow balance", ["Pikachu"] * 6),
            _row(4, "https://pokepast.es/y", "Toxapex pivot core", ["Pikachu"] * 6),
        ]
        index = DescriptionIndex.from_descriptions(r.description for r in rows)
        match_indices = index.match("pex")
        result = _filter_team_rows(
            rows,
            resolved_groups=[],
            description_match_indices=match_indices,
            url_target=None,
            replica_target=None,
        )
        assert [r.row_number for r in result] == [4]


class TestSpreadsheetIdValidation:
    """The /register-sheet gate. Same charset bin/seed_guilds.py enforces —
    if these diverge, an admin could register via slash command an ID the
    operator backstop would reject, surprising whoever runs the seed
    script next."""

    @pytest.mark.parametrize(
        "good",
        [
            "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
            "abc_def-ghi",
            "A",
        ],
    )
    def test_accepts_well_formed_ids(self, good):
        assert _SPREADSHEET_ID_RE.match(good) is not None

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            # Most common paste-the-whole-URL mistake.
            "https://docs.google.com/spreadsheets/d/abc/edit",
            # Whitespace anywhere.
            " abc",
            "abc ",
            "ab c",
            # Punctuation outside the URL-safe set.
            "abc/def",
            "abc.def",
        ],
    )
    def test_rejects_malformed_ids(self, bad):
        assert _SPREADSHEET_ID_RE.match(bad) is None


class TestSpreadsheetLink:
    def test_renders_canonical_docs_url(self):
        # The link shape is what users click after /register-sheet and
        # /show-config; keep this stable so we don't surprise admins with
        # a different host (e.g. a future /edit#gid= variant).
        assert (
            _spreadsheet_link("abc123")
            == "https://docs.google.com/spreadsheets/d/abc123"
        )


class TestResolveFormat:
    def test_none_resolves_to_current_default(self):
        assert _resolve_format(None) == config.DEFAULT_FORMAT

    def test_explicit_choice_resolves_to_its_value(self):
        choice = app_commands.Choice(name="Reg M-A", value="Reg M-A")
        assert _resolve_format(choice) == "Reg M-A"

    def test_default_is_a_registered_format(self):
        # Guards against bumping DEFAULT_FORMAT to a value with no sheet
        # mapping, which would KeyError on FORMAT_SHEETS[fmt_name].
        assert config.DEFAULT_FORMAT in config.FORMAT_SHEETS
