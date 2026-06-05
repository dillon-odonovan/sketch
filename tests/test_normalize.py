"""Tests for sketch.convert.normalize."""

from __future__ import annotations

import unittest

from sketch.convert.normalize import (
    _form_from_item,
    normalize_species,
    normalize_team,
)
from sketch.search.dex import DexIndex
from sketch.team import STAT_KEYS, PokemonEntry, TeamData

# A DEX fixture covering the form families exercised below. `Blastoise` is
# intentionally present WITHOUT a `-Mega` so the DEX-guard path is testable.
_DEX = DexIndex(
    [
        "Charizard",
        "Charizard-Mega-X",
        "Charizard-Mega-Y",
        "Venusaur",
        "Venusaur-Mega",
        "Blastoise",
        "Groudon",
        "Groudon-Primal",
        "Giratina",
        "Giratina-Origin",
        "Dialga",
        "Dialga-Origin",
        "Zacian",
        "Zacian-Crowned",
        "Pikachu",
        "Snorlax",
    ]
)


def _zero_evs() -> dict[str, int]:
    return {k: 0 for k in STAT_KEYS}


def _mon(
    species: str = "Pikachu",
    *,
    item: str | None = "Light Ball",
    ability: str = "Static",
    nature: str = "Timid",
    moves: list[str] | None = None,
) -> PokemonEntry:
    return PokemonEntry(
        species=species,
        gender=None,
        item=item,
        ability=ability,
        nature=nature,
        evs=_zero_evs(),
        moves=moves or ["Thunderbolt", "Volt Tackle", "Iron Tail", "Quick Attack"],
    )


def _team(*mons: PokemonEntry) -> TeamData:
    return TeamData(pokemon=list(mons))


class TestFormFromItem(unittest.TestCase):
    """Pure item→form rule layer, independent of any DEX."""

    def test_mega_stone_no_variant(self) -> None:
        self.assertEqual(_form_from_item("Venusaur", "Venusaurite"), "Venusaur-Mega")

    def test_mega_stone_x_variant(self) -> None:
        self.assertEqual(
            _form_from_item("Charizard", "Charizardite X"), "Charizard-Mega-X"
        )

    def test_mega_stone_y_variant(self) -> None:
        self.assertEqual(
            _form_from_item("Charizard", "Charizardite Y"), "Charizard-Mega-Y"
        )

    def test_mega_stone_z_variant(self) -> None:
        self.assertEqual(_form_from_item("Foo", "Fooite Z"), "Foo-Mega-Z")

    def test_eviolite_is_not_a_mega_stone(self) -> None:
        self.assertIsNone(_form_from_item("Chansey", "Eviolite"))

    def test_primal_red_orb(self) -> None:
        self.assertEqual(_form_from_item("Groudon", "Red Orb"), "Groudon-Primal")

    def test_primal_blue_orb(self) -> None:
        self.assertEqual(_form_from_item("Kyogre", "Blue Orb"), "Kyogre-Primal")

    def test_origin_griseous_orb(self) -> None:
        self.assertEqual(_form_from_item("Giratina", "Griseous Orb"), "Giratina-Origin")

    def test_origin_griseous_core(self) -> None:
        self.assertEqual(
            _form_from_item("Giratina", "Griseous Core"), "Giratina-Origin"
        )

    def test_origin_adamant_crystal(self) -> None:
        self.assertEqual(_form_from_item("Dialga", "Adamant Crystal"), "Dialga-Origin")

    def test_origin_lustrous_globe(self) -> None:
        self.assertEqual(_form_from_item("Palkia", "Lustrous Globe"), "Palkia-Origin")

    def test_stat_boosting_adamant_orb_ignored(self) -> None:
        self.assertIsNone(_form_from_item("Dialga", "Adamant Orb"))

    def test_stat_boosting_lustrous_orb_ignored(self) -> None:
        self.assertIsNone(_form_from_item("Palkia", "Lustrous Orb"))

    def test_crowned_rusted_sword(self) -> None:
        self.assertEqual(_form_from_item("Zacian", "Rusted Sword"), "Zacian-Crowned")

    def test_crowned_rusted_shield(self) -> None:
        self.assertEqual(
            _form_from_item("Zamazenta", "Rusted Shield"), "Zamazenta-Crowned"
        )

    def test_no_item_returns_none(self) -> None:
        self.assertIsNone(_form_from_item("Pikachu", None))

    def test_empty_item_returns_none(self) -> None:
        self.assertIsNone(_form_from_item("Pikachu", ""))

    def test_ordinary_item_returns_none(self) -> None:
        self.assertIsNone(_form_from_item("Pikachu", "Light Ball"))

    def test_case_insensitive(self) -> None:
        self.assertEqual(
            _form_from_item("Charizard", "charizardite y"), "Charizard-Mega-Y"
        )


class TestNormalizeSpecies(unittest.TestCase):
    """Item rules validated against the DEX guard."""

    def test_mega_x_resolves(self) -> None:
        self.assertEqual(
            normalize_species("Charizard", "Charizardite X", _DEX), "Charizard-Mega-X"
        )

    def test_mega_y_resolves(self) -> None:
        self.assertEqual(
            normalize_species("Charizard", "Charizardite Y", _DEX), "Charizard-Mega-Y"
        )

    def test_single_mega_resolves(self) -> None:
        self.assertEqual(
            normalize_species("Venusaur", "Venusaurite", _DEX), "Venusaur-Mega"
        )

    def test_primal_resolves(self) -> None:
        self.assertEqual(
            normalize_species("Groudon", "Red Orb", _DEX), "Groudon-Primal"
        )

    def test_origin_griseous_orb_resolves(self) -> None:
        self.assertEqual(
            normalize_species("Giratina", "Griseous Orb", _DEX), "Giratina-Origin"
        )

    def test_origin_adamant_crystal_resolves(self) -> None:
        self.assertEqual(
            normalize_species("Dialga", "Adamant Crystal", _DEX), "Dialga-Origin"
        )

    def test_crowned_resolves(self) -> None:
        self.assertEqual(
            normalize_species("Zacian", "Rusted Sword", _DEX), "Zacian-Crowned"
        )

    def test_form_absent_from_dex_left_unchanged(self) -> None:
        # `Blastoise` has no `-Mega` in the fixture → DEX guard rejects the
        # inferred form and returns the base species unchanged.
        self.assertEqual(
            normalize_species("Blastoise", "Blastoisinite", _DEX), "Blastoise"
        )

    def test_wrong_species_for_form_item_unchanged(self) -> None:
        # Pikachu + Rusted Sword: no `Pikachu-Crowned` in DEX → unchanged.
        self.assertEqual(normalize_species("Pikachu", "Rusted Sword", _DEX), "Pikachu")

    def test_eviolite_left_unchanged(self) -> None:
        self.assertEqual(normalize_species("Charizard", "Eviolite", _DEX), "Charizard")

    def test_no_item_left_unchanged(self) -> None:
        self.assertEqual(normalize_species("Pikachu", None, _DEX), "Pikachu")

    def test_already_a_form_left_unchanged(self) -> None:
        self.assertEqual(
            normalize_species("Charizard-Mega-Y", "Charizardite Y", _DEX),
            "Charizard-Mega-Y",
        )

    def test_returns_canonical_casing(self) -> None:
        self.assertEqual(
            normalize_species("charizard", "Charizardite Y", _DEX), "Charizard-Mega-Y"
        )

    def test_unknown_species_left_unchanged(self) -> None:
        self.assertEqual(
            normalize_species("Missingno", "Missingnoite", _DEX), "Missingno"
        )


class TestNormalizeTeam(unittest.TestCase):
    def test_resolves_species_and_preserves_other_fields(self) -> None:
        target = _mon("Charizard", ability="Solar Power", item="Charizardite Y")
        team = _team(target, _mon("Pikachu"))
        out = normalize_team(team, _DEX)
        self.assertEqual(out.pokemon[0].species, "Charizard-Mega-Y")
        # Item is preserved — the stone still appears in the CTS paste.
        self.assertEqual(out.pokemon[0].item, target.item)
        self.assertEqual(out.pokemon[0].ability, target.ability)
        self.assertEqual(out.pokemon[1].species, "Pikachu")

    def test_unresolvable_species_left_unchanged(self) -> None:
        target = _mon("Blastoise", item="Blastoisinite")
        out = normalize_team(_team(target), _DEX)
        self.assertEqual(out.pokemon[0].species, "Blastoise")


if __name__ == "__main__":
    unittest.main()
