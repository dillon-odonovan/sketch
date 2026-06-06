"""Tests for the localized→English name lookup (issue #46).

These run against the committed `pokeapi_names.json` table, so they double as a
smoke test that the build script produced a usable file with the expected
fixtures from the issue.
"""

from __future__ import annotations

from sketch.champions import name_lookup
from sketch.champions.name_lookup import (
    normalize,
    resolve_ability,
    resolve_item,
    resolve_move,
    resolve_species,
)


class TestNormalize:
    def test_strips_surrounding_whitespace(self):
        assert normalize("  Choice Scarf  ") == "choicescarf"

    def test_strips_internal_whitespace(self):
        # Screen "구애 스카프" must match CSV "구애스카프".
        assert normalize("구애 스카프") == normalize("구애스카프")

    def test_strips_full_width_ideographic_space(self):
        assert normalize("구애　스카프") == normalize("구애스카프")

    def test_casefolds_latin(self):
        assert normalize("CALM mind") == normalize("Calm Mind")

    def test_cjk_unchanged_apart_from_spacing(self):
        assert normalize("명상") == "명상"


class TestResolveOverridesOnHit:
    def test_move_katakana_loanword(self):
        # アクアブレイク phonetically "Aqua Break" → official "Liquidation".
        assert resolve_move("アクアブレイク", "Aqua Break") == "Liquidation"

    def test_move_lookalike_collision(self):
        assert resolve_move("명상", "Meditate") == "Calm Mind"

    def test_move_word_order(self):
        assert resolve_move("アームハンマー", "Arm Hammer") == "Hammer Arm"

    def test_item_with_internal_space(self):
        assert resolve_item("구애 스카프", "Gooey Scarf") == "Choice Scarf"

    def test_item_japanese(self):
        assert resolve_item("こだわりスカーフ", "Choice Scarf") == "Choice Scarf"

    def test_species_recent_gen9(self):
        # Korean Sinistcha; model substituted a familiar look-alike.
        assert resolve_species("그우린차", "Polteageist") == "Sinistcha"

    def test_species_base_lookalike(self):
        assert resolve_species("마폭시", "Blaziken") == "Delphox"


class TestResolveFallsBackOnMiss:
    def test_unknown_raw_keeps_model_english(self):
        assert resolve_move("ﾅﾆｺﾚ", "Glacial Lance") == "Glacial Lance"

    def test_empty_raw_keeps_model_english(self):
        assert resolve_item("", "Leftovers") == "Leftovers"

    def test_none_raw_keeps_model_english(self):
        assert resolve_ability(None, "Intimidate") == "Intimidate"

    def test_champions_custom_item_kept(self):
        # Not a real PokeAPI item — the model's guess is all we have.
        assert resolve_item("플로엣타이트", "Floettite") == "Floettite"


class TestSpeciesFormGuard:
    def test_hyphenated_form_is_preserved(self):
        # Base-name `raw` must not strip a form the model derived from kit.
        assert resolve_species("바쿠퐁", "Typhlosion-Hisui") == "Typhlosion-Hisui"

    def test_calyrex_form_preserved_even_with_english_raw(self):
        assert resolve_species("Calyrex", "Calyrex-Shadow") == "Calyrex-Shadow"


class TestTablesLoad:
    def test_all_categories_present_and_populated(self):
        tables = name_lookup._tables()
        for category in ("species", "items", "abilities", "moves"):
            assert tables[category], f"{category} table is empty"
