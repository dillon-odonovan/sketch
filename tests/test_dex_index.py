from sketch.search.dex import DexIndex

DEX_FIXTURE = [
    "Charizard",
    "Charizard-Mega-X",
    "Charizard-Mega-Y",
    "Calyrex-Shadow",
    "Calyrex-Ice",
    "Urshifu",
    "Urshifu-Rapid-Strike",
    "Pikachu",
]


def test_exact_match_returns_self():
    dex = DexIndex(DEX_FIXTURE)
    result = dex.resolve("Pikachu")
    assert result.canonical_matches == ["Pikachu"]
    assert result.suggestions == []


def test_match_is_case_insensitive():
    dex = DexIndex(DEX_FIXTURE)
    assert dex.resolve("PIKACHU").canonical_matches == ["Pikachu"]
    assert dex.resolve("pikachu").canonical_matches == ["Pikachu"]


def test_prefix_group_matches_base_and_forms():
    dex = DexIndex(DEX_FIXTURE)
    result = dex.resolve("Charizard")
    assert set(result.canonical_matches) == {
        "Charizard",
        "Charizard-Mega-X",
        "Charizard-Mega-Y",
    }


def test_specific_form_narrows_match():
    dex = DexIndex(DEX_FIXTURE)
    result = dex.resolve("Charizard-Mega-Y")
    assert result.canonical_matches == ["Charizard-Mega-Y"]


def test_partial_token_without_boundary_does_not_match():
    # "char" has no full-name boundary so it should fall through to suggestions.
    dex = DexIndex(DEX_FIXTURE)
    result = dex.resolve("char")
    assert result.canonical_matches == []


def test_misspelling_returns_suggestions():
    dex = DexIndex(DEX_FIXTURE)
    result = dex.resolve("Pikachuu")
    assert result.canonical_matches == []
    assert "Pikachu" in result.suggestions


def test_empty_query_returns_nothing():
    dex = DexIndex(DEX_FIXTURE)
    result = dex.resolve("")
    assert result.canonical_matches == []
    assert result.suggestions == []


def test_whitespace_query_returns_nothing():
    dex = DexIndex(DEX_FIXTURE)
    result = dex.resolve("   ")
    assert result.canonical_matches == []
    assert result.suggestions == []


def test_unknown_query_with_no_close_match():
    dex = DexIndex(DEX_FIXTURE)
    result = dex.resolve("XYZNotAPokemon")
    assert result.canonical_matches == []
    assert result.suggestions == []
