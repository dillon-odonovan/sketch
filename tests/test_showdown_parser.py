"""Tests for the reverse Showdown / PokePaste parser.

Each test feeds the parser an explicit Showdown text string and asserts
on the resulting `TeamData` (or the error message). A single round-trip
test at the top of the file confirms the parser and renderer remain
mutual inverses; the rest of the suite pins the parser's *contract*
(what text shape it accepts, what it produces, what it rejects)
independent of any other module.

`_FULL_PASTE` is the workhorse fixture — a hand-written 6-mon paste
exercising every parser path (gender + item, gender alone, item alone,
neither; 4 / 3 / 2 / 1 moves; EVs present and absent). Every happy-path
test asserts on a different slot of this paste; error-case tests build
their broken inputs by string-replacing into it, keeping the broken
shape obvious from the test source.
"""

from __future__ import annotations

import pytest

from sketch.showdown.parser import (
    ShowdownParseError,
    extract_species,
    parse_showdown,
)
from sketch.showdown.renderer import render_showdown
from sketch.team import PokemonEntry, TeamData

# Canonical 6-mon paste — hand-written so the parser's contract is
# pinned independently of `render_showdown`. CRLF line endings match
# the renderer's output (and what pokepast.es requires), but the
# `test_accepts_lf_line_endings` test confirms LF works too.
_FULL_PASTE = (
    # Slot 0: gender + item, 4 moves, EVs present.
    "Floette-Eternal (F) @ Floettite\r\n"
    "Ability: Flower Veil\r\n"
    "EVs: 32 HP / 32 SpA / 2 Spe\r\n"
    "Modest Nature\r\n"
    "- Dazzling Gleam\r\n"
    "- Moonblast\r\n"
    "- Light of Ruin\r\n"
    "- Protect\r\n"
    "\r\n"
    # Slot 1: gender, no item.
    "Aerodactyl (M)\r\n"
    "Ability: Pressure\r\n"
    "EVs: 4 HP / 32 Atk / 28 Spe\r\n"
    "Jolly Nature\r\n"
    "- Rock Slide\r\n"
    "- Dual Wingbeat\r\n"
    "- Tailwind\r\n"
    "- Protect\r\n"
    "\r\n"
    # Slot 2: no gender, no item.
    "Klefki\r\n"
    "Ability: Prankster\r\n"
    "EVs: 32 HP / 16 Def / 16 SpD\r\n"
    "Bold Nature\r\n"
    "- Reflect\r\n"
    "- Light Screen\r\n"
    "- Foul Play\r\n"
    "- Spikes\r\n"
    "\r\n"
    # Slot 3: gender + item, only 2 moves (Kangaskhan Fake Out + Last Resort).
    "Kangaskhan (F) @ Silk Scarf\r\n"
    "Ability: Scrappy\r\n"
    "EVs: 4 HP / 32 Atk / 28 Spe\r\n"
    "Adamant Nature\r\n"
    "- Fake Out\r\n"
    "- Last Resort\r\n"
    "\r\n"
    # Slot 4: no gender, no item, no EVs line, single move (floor of the range).
    "Magnemite\r\n"
    "Ability: Sturdy\r\n"
    "Modest Nature\r\n"
    "- Thunderbolt\r\n"
    "\r\n"
    # Slot 5: gender + item, 3 moves.
    "Garchomp (F) @ Life Orb\r\n"
    "Ability: Rough Skin\r\n"
    "EVs: 32 Atk / 4 SpD / 28 Spe\r\n"
    "Jolly Nature\r\n"
    "- Earthquake\r\n"
    "- Dragon Claw\r\n"
    "- Protect"
)


def test_renderer_and_parser_remain_inverses():
    """Single round-trip sanity check: `parse(render(team)) == team`.

    The rest of the suite pins the parser's contract against explicit
    strings; this one test catches drift if the renderer's output shape
    changes without a matching parser update (or vice versa). One pass
    over a representative team is enough — every individual parser path
    has a dedicated explicit-string test below.
    """
    team = TeamData(
        pokemon=[
            PokemonEntry(
                species="Floette-Eternal",
                gender="F",
                item="Floettite",
                ability="Flower Veil",
                nature="Modest",
                evs={"hp": 32, "atk": 0, "def": 0, "spa": 32, "spd": 0, "spe": 2},
                moves=["Dazzling Gleam", "Moonblast", "Light of Ruin", "Protect"],
            ),
            PokemonEntry(
                species="Aerodactyl",
                gender="M",
                item=None,
                ability="Pressure",
                nature="Jolly",
                evs={"hp": 4, "atk": 32, "def": 0, "spa": 0, "spd": 0, "spe": 28},
                moves=["Rock Slide", "Dual Wingbeat", "Tailwind", "Protect"],
            ),
            PokemonEntry(
                species="Klefki",
                gender=None,
                item=None,
                ability="Prankster",
                nature="Bold",
                evs={"hp": 32, "atk": 0, "def": 16, "spa": 0, "spd": 16, "spe": 0},
                moves=["Reflect", "Light Screen", "Foul Play", "Spikes"],
            ),
            PokemonEntry(
                species="Kangaskhan",
                gender="F",
                item="Silk Scarf",
                ability="Scrappy",
                nature="Adamant",
                evs={"hp": 4, "atk": 32, "def": 0, "spa": 0, "spd": 0, "spe": 28},
                moves=["Fake Out", "Last Resort"],
            ),
            PokemonEntry(
                species="Magnemite",
                gender=None,
                item=None,
                ability="Sturdy",
                nature="Modest",
                evs={"hp": 0, "atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0},
                moves=["Thunderbolt"],
            ),
            PokemonEntry(
                species="Garchomp",
                gender="F",
                item="Life Orb",
                ability="Rough Skin",
                nature="Jolly",
                evs={"hp": 0, "atk": 32, "def": 0, "spa": 0, "spd": 4, "spe": 28},
                moves=["Earthquake", "Dragon Claw", "Protect"],
            ),
        ],
        team_id="QBXXWXL05U",
    )
    assert parse_showdown(render_showdown(team), team_id=team.team_id) == team


class TestHappyPath:
    """Each test asserts the parser's output for one slot of `_FULL_PASTE`."""

    def test_returns_six_pokemon(self):
        result = parse_showdown(_FULL_PASTE, team_id="QBXXWXL05U")
        assert len(result.pokemon) == 6
        assert result.team_id == "QBXXWXL05U"

    def test_species_gender_and_item_all_present(self):
        floette = parse_showdown(_FULL_PASTE).pokemon[0]
        assert floette.species == "Floette-Eternal"
        assert floette.gender == "F"
        assert floette.item == "Floettite"

    def test_species_gender_no_item(self):
        aerodactyl = parse_showdown(_FULL_PASTE).pokemon[1]
        assert aerodactyl.species == "Aerodactyl"
        assert aerodactyl.gender == "M"
        assert aerodactyl.item is None

    def test_species_no_gender_no_item(self):
        # The parser must distinguish "no gender" from "no item" — neither
        # field should bleed into the other when both are absent.
        klefki = parse_showdown(_FULL_PASTE).pokemon[2]
        assert klefki.species == "Klefki"
        assert klefki.gender is None
        assert klefki.item is None

    def test_kangaskhan_two_moves(self):
        # Canonical < 4-moves Champions set. Pokemon can run fewer
        # than 4 moves (e.g. Kangaskhan with Fake Out + Last Resort —
        # Last Resort requires every other move used at least once,
        # so a 2-move set is the cap there); the parser must accept it.
        kanga = parse_showdown(_FULL_PASTE).pokemon[3]
        assert kanga.moves == ["Fake Out", "Last Resort"]

    def test_magnemite_single_move(self):
        # Floor of the accepted move-count range.
        magnemite = parse_showdown(_FULL_PASTE).pokemon[4]
        assert magnemite.moves == ["Thunderbolt"]

    def test_garchomp_three_moves(self):
        garchomp = parse_showdown(_FULL_PASTE).pokemon[5]
        assert garchomp.moves == ["Earthquake", "Dragon Claw", "Protect"]

    def test_evs_listed_stats_parsed_others_default_to_zero(self):
        floette = parse_showdown(_FULL_PASTE).pokemon[0]
        # `EVs: 32 HP / 32 SpA / 2 Spe` — the other three default to 0.
        assert floette.evs == {
            "hp": 32,
            "atk": 0,
            "def": 0,
            "spa": 32,
            "spd": 0,
            "spe": 2,
        }

    def test_missing_evs_block_defaults_to_all_zero(self):
        # Magnemite's block omits the `EVs:` line entirely. The parser
        # synthesizes a full zero dict so PokemonEntry's `evs` field is
        # always shaped the same.
        magnemite = parse_showdown(_FULL_PASTE).pokemon[4]
        assert magnemite.evs == {
            "hp": 0,
            "atk": 0,
            "def": 0,
            "spa": 0,
            "spd": 0,
            "spe": 0,
        }

    def test_natures_extracted(self):
        result = parse_showdown(_FULL_PASTE)
        assert result.pokemon[0].nature == "Modest"
        assert result.pokemon[1].nature == "Jolly"
        assert result.pokemon[2].nature == "Bold"
        assert result.pokemon[3].nature == "Adamant"

    def test_abilities_extracted(self):
        result = parse_showdown(_FULL_PASTE)
        assert result.pokemon[0].ability == "Flower Veil"
        assert result.pokemon[5].ability == "Rough Skin"

    def test_accepts_lf_line_endings(self):
        # Renderer emits CRLF (required by pokepast.es) but Discord modal
        # input returns whatever the user typed — typically LF on macOS
        # / Linux browsers. Parser must accept both.
        lf_paste = _FULL_PASTE.replace("\r\n", "\n")
        assert parse_showdown(lf_paste).pokemon[0].species == "Floette-Eternal"

    def test_preserves_passed_in_team_id(self):
        # The Showdown format doesn't carry the Team ID. The caller
        # (Edit-flow command handler) passes through the OCR-validated
        # value; the parser doesn't read or invent it.
        assert parse_showdown(_FULL_PASTE, team_id="ABCD12EFGH").team_id == "ABCD12EFGH"

    def test_default_team_id_is_none(self):
        assert parse_showdown(_FULL_PASTE).team_id is None


class TestErrorCases:
    """Each test asserts the user-facing error message for one validation
    rule. The broken paste is built by replacing into `_FULL_PASTE` so
    the malformation is visible at the test source."""

    def test_empty_paste(self):
        with pytest.raises(ShowdownParseError, match="empty"):
            parse_showdown("")

    def test_five_pokemon(self):
        # Strip the trailing block.
        broken = "\r\n\r\n".join(_FULL_PASTE.split("\r\n\r\n")[:5])
        with pytest.raises(ShowdownParseError, match="Expected 6 Pokemon, got 5"):
            parse_showdown(broken)

    def test_seven_pokemon(self):
        first_block = _FULL_PASTE.split("\r\n\r\n")[0]
        broken = _FULL_PASTE + "\r\n\r\n" + first_block
        with pytest.raises(ShowdownParseError, match="Expected 6 Pokemon, got 7"):
            parse_showdown(broken)

    def test_too_many_moves(self):
        # Inject a 5th move into Floette's block.
        broken = _FULL_PASTE.replace(
            "- Protect\r\n\r\nAerodactyl",
            "- Protect\r\n- Substitute\r\n\r\nAerodactyl",
            1,
        )
        with pytest.raises(ShowdownParseError, match="too many moves"):
            parse_showdown(broken)

    def test_no_moves(self):
        # Build a paste where the first block has header lines but no
        # `- Move` lines.
        moveless_block = (
            "Floette-Eternal (F) @ Floettite\r\nAbility: Flower Veil\r\nModest Nature"
        )
        rest = "\r\n\r\n".join(_FULL_PASTE.split("\r\n\r\n")[1:])
        broken = moveless_block + "\r\n\r\n" + rest
        with pytest.raises(ShowdownParseError, match="no moves"):
            parse_showdown(broken)

    def test_ev_value_over_cap(self):
        # Champions caps EVs at 32; 33 should be rejected as out-of-range.
        broken = _FULL_PASTE.replace("EVs: 32 HP", "EVs: 33 HP", 1)
        with pytest.raises(ShowdownParseError, match="out of range"):
            parse_showdown(broken)

    def test_unknown_nature_hardy_suggests_serious(self):
        # Hardy is a Showdown-recognized neutral but this bot only emits
        # / accepts "Serious" for neutral natures. Error must mention
        # Serious so the user knows the canonical fix.
        broken = _FULL_PASTE.replace("Modest Nature", "Hardy Nature", 1)
        with pytest.raises(ShowdownParseError, match="Serious"):
            parse_showdown(broken)

    def test_unknown_nature_typo_named_in_error(self):
        # The error names the offending value so the user can locate
        # the typo in their paste.
        broken = _FULL_PASTE.replace("Modest Nature", "Mdoest Nature", 1)
        with pytest.raises(ShowdownParseError, match="Mdoest"):
            parse_showdown(broken)

    def test_missing_ability_line(self):
        broken = _FULL_PASTE.replace("Ability: Flower Veil\r\n", "", 1)
        with pytest.raises(ShowdownParseError, match="Ability"):
            parse_showdown(broken)

    def test_missing_nature_line(self):
        broken = _FULL_PASTE.replace("Modest Nature\r\n", "", 1)
        with pytest.raises(ShowdownParseError, match="Nature"):
            parse_showdown(broken)

    def test_malformed_species_unmatched_paren(self):
        broken = _FULL_PASTE.replace("Floette-Eternal (F)", "Floette-Eternal (F", 1)
        with pytest.raises(ShowdownParseError, match="malformed"):
            parse_showdown(broken)


class TestIgnoredLines:
    """Standard Showdown / PokePaste exports carry informational lines the
    Champions format doesn't model (Level, Tera Type, IVs, …). The parser
    skips them rather than erroring, so a real-world OTS paste round-trips."""

    # The reportworm "Copy" output from the bug report: a full 6-mon OTS
    # paste with a `Level: 50` line on every Pokemon.
    _REPORTWORM_PASTE = (
        "Venusaur @ Focus Sash\r\n"
        "Ability: Chlorophyll\r\n"
        "Level: 50\r\n"
        "Timid Nature\r\n"
        "- Sleep Powder\r\n"
        "- Sludge Bomb\r\n"
        "- Earth Power\r\n"
        "- Protect\r\n"
        "\r\n"
        "Charizard @ Charizardite Y\r\n"
        "Ability: Blaze\r\n"
        "Level: 50\r\n"
        "Modest Nature\r\n"
        "- Heat Wave\r\n"
        "- Solar Beam\r\n"
        "- Weather Ball\r\n"
        "- Protect\r\n"
        "\r\n"
        "Garchomp @ Choice Scarf\r\n"
        "Ability: Rough Skin\r\n"
        "Level: 50\r\n"
        "Adamant Nature\r\n"
        "- Earthquake\r\n"
        "- Rock Slide\r\n"
        "- Stomping Tantrum\r\n"
        "- Dragon Claw\r\n"
        "\r\n"
        "Incineroar @ Sitrus Berry\r\n"
        "Ability: Intimidate\r\n"
        "Level: 50\r\n"
        "Careful Nature\r\n"
        "- Fake Out\r\n"
        "- Flare Blitz\r\n"
        "- Parting Shot\r\n"
        "- Throat Chop\r\n"
        "\r\n"
        "Floette-Eternal @ Floettite\r\n"
        "Ability: Flower Veil\r\n"
        "Level: 50\r\n"
        "Modest Nature\r\n"
        "- Moonblast\r\n"
        "- Dazzling Gleam\r\n"
        "- Calm Mind\r\n"
        "- Protect\r\n"
        "\r\n"
        "Sinistcha @ Kasib Berry\r\n"
        "Ability: Hospitality\r\n"
        "Level: 50\r\n"
        "Relaxed Nature\r\n"
        "- Matcha Gotcha\r\n"
        "- Rage Powder\r\n"
        "- Trick Room\r\n"
        "- Protect\r\n"
    )

    def test_level_lines_are_skipped(self):
        team = parse_showdown(self._REPORTWORM_PASTE)
        assert [p.species for p in team.pokemon] == [
            "Venusaur",
            "Charizard",
            "Garchomp",
            "Incineroar",
            "Floette-Eternal",
            "Sinistcha",
        ]
        # The skipped Level lines don't disturb the parsed fields.
        assert team.pokemon[0].ability == "Chlorophyll"
        assert team.pokemon[0].nature == "Timid"
        assert team.pokemon[0].item == "Focus Sash"
        assert team.pokemon[0].moves == [
            "Sleep Powder",
            "Sludge Bomb",
            "Earth Power",
            "Protect",
        ]

    def test_full_informational_set_is_skipped(self):
        # Tera Type / IVs / Shiny / Happiness all appear before the moves;
        # each must be skipped without disturbing the parsed fields.
        block = (
            "Garganacl @ Sitrus Berry\r\n"
            "Ability: Purifying Salt\r\n"
            "Level: 50\r\n"
            "Tera Type: Poison\r\n"
            "Shiny: Yes\r\n"
            "Happiness: 0\r\n"
            "IVs: 0 Atk\r\n"
            "EVs: 32 HP / 16 Def / 16 SpD\r\n"
            "Careful Nature\r\n"
            "- Salt Cure\r\n"
            "- Protect\r\n"
            "- Recover\r\n"
            "- Iron Defense"
        )
        rest = "\r\n\r\n".join(_FULL_PASTE.split("\r\n\r\n")[1:])
        team = parse_showdown(block + "\r\n\r\n" + rest)
        first = team.pokemon[0]
        assert first.species == "Garganacl"
        assert first.ability == "Purifying Salt"
        assert first.nature == "Careful"
        assert first.evs["hp"] == 32
        assert first.moves == ["Salt Cure", "Protect", "Recover", "Iron Defense"]

    def test_unrecognized_header_line_still_errors(self):
        # The skip allowlist must not become a catch-all: an unknown
        # header line still surfaces the explicit "unexpected line" error.
        broken = _FULL_PASTE.replace(
            "Ability: Flower Veil\r\n",
            "Ability: Flower Veil\r\nGarbage: foo\r\n",
            1,
        )
        with pytest.raises(ShowdownParseError, match="unexpected line"):
            parse_showdown(broken)


class TestExtractSpecies:
    """`extract_species` is the lenient, never-raising counterpart to
    `parse_showdown`: it pulls species off each block header and tolerates
    any body (including non-Champions VGC pastes the strict parser
    rejects), since its only consumer is an audit-only cache field."""

    def test_extracts_all_six_from_full_paste(self):
        species = extract_species(_FULL_PASTE)
        assert len(species) == 6
        # Gender + item suffixes are stripped from the header.
        assert species[0] == "Floette-Eternal"
        assert species[1] == "Aerodactyl"

    def test_tolerates_standard_vgc_paste(self):
        # EVs above the Champions cap would make `parse_showdown` raise —
        # `extract_species` ignores the body entirely and still returns the
        # species. (`Level:` / `Tera Type:` lines, by contrast, are now
        # skipped by `parse_showdown` too — see TestIgnoredLines.)
        paste = (
            "Garganacl @ Sitrus Berry\r\n"
            "Ability: Purifying Salt\r\n"
            "Level: 50\r\n"
            "Tera Type: Poison\r\n"
            "EVs: 196 HP / 76 Def / 236 SpD\r\n"
            "Careful Nature\r\n"
            "- Salt Cure\r\n\r\n"
            "Volcarona @ Leftovers\r\n"
            "Ability: Flame Body\r\n"
            "Level: 50\r\n"
            "EVs: 244 HP / 212 Def / 52 Spe\r\n"
            "Bold Nature\r\n"
            "- Quiver Dance"
        )
        assert extract_species(paste) == ["Garganacl", "Volcarona"]

    def test_accepts_lf_line_endings(self):
        paste = "Incineroar @ Safety Goggles\nAbility: Intimidate\n- Fake Out"
        assert extract_species(paste) == ["Incineroar"]

    def test_empty_input_returns_empty_list(self):
        assert extract_species("") == []
        assert extract_species("   \n  ") == []

    def test_skips_block_with_malformed_header(self):
        # A block whose first line can't be a species header (unmatched
        # paren) is silently skipped; the valid block still comes through.
        paste = (
            "Floette-Eternal (F\nAbility: Flower Veil\r\n\r\n"
            "Miraidon\nAbility: Hadron Engine"
        )
        assert extract_species(paste) == ["Miraidon"]
