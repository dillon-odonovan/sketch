"""Tests for the tokenizer and DescriptionIndex in `text_search`.

The matcher is a pure in-memory function with no I/O, so all tests run
synchronously without fixtures (mirrors `tests/test_dex_index.py`).
"""

import pytest

from sketch.search.text_search import (
    SUBSTRING_MIN_TOKEN_LENGTH,
    DescriptionIndex,
    tokenize,
    tokenize_query,
)


class TestTokenize:
    def test_basic_words_split_on_whitespace(self):
        assert tokenize("hello world") == ["hello", "world"]

    def test_case_is_folded_to_lower(self):
        assert tokenize("Hello WORLD") == ["hello", "world"]

    def test_runs_of_whitespace_collapse(self):
        assert tokenize("multiple   spaces\there") == [
            "multiple",
            "spaces",
            "here",
        ]

    def test_parentheses_act_as_delimiters(self):
        assert tokenize("Shiliang Tang (tsltang0508) Dual Weather") == [
            "shiliang",
            "tang",
            "tsltang0508",
            "dual",
            "weather",
        ]

    def test_hyphens_act_as_delimiters(self):
        # Differs from `dex.py`, where hyphens are name-form boundaries.
        # In descriptions hyphens are just punctuation.
        assert tokenize("Calyrex-Shadow") == ["calyrex", "shadow"]

    def test_slashes_periods_commas_act_as_delimiters(self):
        assert tokenize("Hello/World.Foo,bar") == ["hello", "world", "foo", "bar"]

    def test_digits_are_kept_inside_tokens(self):
        # Gamertags and format codes routinely mix letters + digits.
        assert tokenize("tsltang0508 vgc23") == ["tsltang0508", "vgc23"]

    def test_accented_letters_strip_to_base(self):
        # NFKD-decompose then drop combining marks: "é" → "e", "ñ" → "n".
        assert tokenize("Pokémon Café Niño") == ["pokemon", "cafe", "nino"]

    def test_empty_string_returns_empty_list(self):
        assert tokenize("") == []

    def test_punctuation_only_returns_empty_list(self):
        assert tokenize("()(...)") == []
        assert tokenize("---") == []
        assert tokenize("   ") == []

    def test_non_latin_script_drops_to_empty(self):
        # Documented limitation: CJK / Cyrillic descriptions don't tokenize.
        # If we ever need this, swap the delimiter regex for a Unicode-aware
        # one (e.g. \W with re.UNICODE) and add a CJK segmenter.
        assert tokenize("こんにちは") == []

    def test_tokenize_query_matches_tokenize(self):
        # tokenize_query is the alias-hook for future synonyms; today it must
        # be observable as a passthrough. If this test starts failing, the
        # alias map landed — update accordingly.
        assert tokenize_query("Hello WORLD") == tokenize("Hello WORLD")


class TestDescriptionIndexConstruction:
    def test_empty_corpus_has_no_tokens(self):
        idx = DescriptionIndex.from_descriptions([])
        assert len(idx) == 0

    def test_distinct_tokens_deduplicated_across_rows(self):
        # "calyrex" appears in both rows; the index stores it once.
        idx = DescriptionIndex.from_descriptions(["calyrex shadow", "calyrex ice"])
        # Tokens: {calyrex, shadow, ice} → 3 distinct.
        assert len(idx) == 3

    def test_row_index_is_positional(self):
        idx = DescriptionIndex.from_descriptions(
            [
                "first row tokens",
                "second row tokens",
                "third row tokens",
            ]
        )
        # "first" is unique to row 0; "row" appears in all three.
        assert idx.match("first") == {0}
        assert idx.match("row") == {0, 1, 2}


class TestDescriptionIndexMatching:
    """The behavioural spec: every requirement from the plan goes here."""

    @pytest.fixture
    def bank(self) -> DescriptionIndex:
        """Hand-built corpus mirroring real /search-teams descriptions."""
        self._descriptions = [
            "Shiliang Tang (tsltang0508) Dual Weather",  # 0
            "Calyrex Zamazenta balance",  # 1
            "alice — Charizard hyper offense",  # 2
            "bob — sun team with Toxapex pivot",  # 3
            "Garchomp rain core",  # 4
            "Pokémon Café — sweets squad",  # 5
        ]
        return DescriptionIndex.from_descriptions(self._descriptions)

    # ── Order-independence ────────────────────────────────────────────
    def test_order_independent_multi_token_match(self, bank):
        # Tokens appear in a different order in the description; AND should
        # still succeed because every query token has a candidate.
        assert bank.match("shiliang dual weather") == {0}

    # ── Per-token prefix (a subset of substring) ──────────────────────
    def test_prefix_match_per_token(self, bank):
        # "caly" is a prefix of "calyrex"; "zama" is a prefix of "zamazenta".
        assert bank.match("caly zama") == {1}

    # ── Per-token infix / suffix ──────────────────────────────────────
    def test_infix_match_pex_finds_toxapex(self, bank):
        # The motivating user case: "pex" is a suffix of "toxapex".
        assert bank.match("pex") == {3}

    def test_infix_match_chomp_finds_garchomp(self, bank):
        assert bank.match("chomp") == {4}

    # ── AND semantics across query tokens ─────────────────────────────
    def test_and_combines_query_tokens(self, bank):
        # Only row 1 has both *caly* and *zama* tokens. Row 0 has neither.
        assert bank.match("caly zama") == {1}

    def test_and_returns_empty_when_one_token_unmatched(self, bank):
        # "calyrex" matches row 1, "nonexistent" matches nothing → empty.
        assert bank.match("calyrex nonexistent") == set()

    # ── Min-length-3 floor on query tokens ────────────────────────────
    def test_short_query_token_requires_exact_match(self):
        # SUBSTRING_MIN_TOKEN_LENGTH = 3. A 2-char query token must match
        # an indexed token exactly, not via substring. Without this floor,
        # "to" would substring-hit "toxapex" and "totem" alike.
        assert SUBSTRING_MIN_TOKEN_LENGTH == 3
        idx = DescriptionIndex.from_descriptions(
            [
                "on call relay",  # 0 — has the literal "on" token
                "Toxapex pivot",  # 1 — has "toxapex" only
            ]
        )
        # Exact-token "on" matches row 0; does NOT substring-hit row 1.
        assert idx.match("on") == {0}
        # Below the floor, no substring match into "toxapex".
        assert idx.match("to") == set()
        # At or above the floor, substring kicks in.
        assert idx.match("tox") == {1}

    # ── Empty / punctuation-only queries ──────────────────────────────
    def test_empty_query_returns_empty(self, bank):
        assert bank.match("") == set()

    def test_punctuation_only_query_returns_empty(self, bank):
        assert bank.match("()(...)") == set()
        assert bank.match("   ") == set()

    # ── Delimiter tokenization on stored side ─────────────────────────
    def test_parens_in_stored_description_are_delimiters(self, bank):
        # The parens around "(tsltang0508)" are delimiters at index build,
        # so the bare "tsltang0508" query matches.
        assert bank.match("tsltang0508") == {0}

    # ── Unicode folding ───────────────────────────────────────────────
    def test_accented_descriptions_match_ascii_queries(self, bank):
        # "Pokémon Café" tokenizes to ["pokemon", "cafe"], so an ASCII
        # query reaches it.
        assert bank.match("pokemon cafe") == {5}

    # ── Multiple matches ──────────────────────────────────────────────
    def test_query_can_match_multiple_rows(self):
        idx = DescriptionIndex.from_descriptions(
            [
                "balance core",  # 0
                "balance offense",  # 1
                "weather core",  # 2
            ]
        )
        # "balance" matches rows 0 and 1.
        assert idx.match("balance") == {0, 1}
        # "core" matches rows 0 and 2.
        assert idx.match("core") == {0, 2}
        # AND of both narrows to the row that has both.
        assert idx.match("balance core") == {0}

    # ── Backwards-compat-ish: the old substring needle still wins ─────
    def test_full_word_query_matches_as_substring(self, bank):
        # The legacy "JSMITHVGC" style query — single full word — still
        # works under the new matcher.
        assert bank.match("toxapex") == {3}


class TestExtensionHooks:
    """Pin down the documented v2 extension points so future changes are
    visible diffs rather than silent behaviour drift."""

    def test_csr_alias_is_not_yet_active(self):
        # Aliases are out of scope for v1. "csr" must tokenize as a single
        # token and only substring-match descriptions that literally contain
        # "csr". If a future PR adds the alias map, this test should be
        # updated and moved into the alias-coverage suite.
        idx = DescriptionIndex.from_descriptions(
            [
                "Calyrex Shadow Rider balance",  # 0 — no literal "csr"
                "csr — quick reference",  # 1 — literal "csr"
            ]
        )
        assert idx.match("csr") == {1}

    def test_concatenated_decomposition_is_not_yet_active(self):
        # "calyzama" is one token to the index; without v2 decomposition we
        # only match descriptions that literally contain "calyzama" as a
        # substring of some token.
        idx = DescriptionIndex.from_descriptions(["Calyrex Zamazenta"])
        assert idx.match("calyzama") == set()
