"""Tests for the reverse Showdown / PokePaste parser.

The parser is the inverse of `render_showdown`. Round-trip tests are the
backbone of the suite: anything the renderer can emit, the parser must
accept and reproduce as an equal `TeamData`. Error-case tests pin down
the user-facing failure messages so the Edit modal's re-open shows
something specific (and not, e.g., a Python exception type leaking
through).
"""

from __future__ import annotations

import pytest

from sketch.replica.extractor import PokemonEntry, TeamData
from sketch.replica.pokepaste_renderer import render_showdown
from sketch.replica.showdown_parser import ShowdownParseError, parse_showdown


def _entry(
    species: str = "Floette-Eternal",
    *,
    gender: str | None = "F",
    item: str | None = "Floettite",
    ability: str = "Flower Veil",
    nature: str = "Modest",
    evs: dict[str, int] | None = None,
    moves: list[str] | None = None,
) -> PokemonEntry:
    return PokemonEntry(
        species=species,
        gender=gender,
        item=item,
        ability=ability,
        nature=nature,
        evs=evs
        if evs is not None
        else {"hp": 32, "atk": 0, "def": 0, "spa": 32, "spd": 0, "spe": 2},
        moves=moves
        if moves is not None
        else ["Dazzling Gleam", "Moonblast", "Light of Ruin", "Protect"],
    )


def _team(*overrides: PokemonEntry, team_id: str | None = "QBXXWXL05U") -> TeamData:
    """Build a canonical 6-mon team, replacing slots from the left.

    Used by tests to anchor on a known baseline and override one or two
    slots for edge-case coverage (1-move Pokemon, genderless mons, etc.)
    without having to redeclare the full team each time.
    """
    base = [
        _entry("Floette-Eternal"),
        _entry("Aerodactyl", gender="M", item=None, ability="Pressure"),
        _entry("Incineroar", gender="M", ability="Intimidate"),
        _entry("Garchomp", gender="F", ability="Rough Skin"),
        _entry("Charizard", gender="M", ability="Solar Power"),
        _entry("Venusaur", gender="F", ability="Chlorophyll"),
    ]
    for i, override in enumerate(overrides):
        base[i] = override
    return TeamData(pokemon=base, team_id=team_id)


class TestRoundTrip:
    """`parse_showdown(render_showdown(team), team_id=...)` ≡ `team`."""

    def test_canonical_six_mon_team(self):
        team = _team()
        parsed = parse_showdown(render_showdown(team), team_id=team.team_id)
        assert parsed == team

    def test_genderless_no_item(self):
        # Klefki has no in-game gender symbol and may hold no item; the
        # renderer omits both, the parser must produce gender=None /
        # item=None rather than empty strings.
        klefki = _entry(
            "Klefki",
            gender=None,
            item=None,
            ability="Prankster",
            nature="Bold",
            evs={"hp": 32, "atk": 0, "def": 16, "spa": 0, "spd": 16, "spe": 0},
            moves=["Reflect", "Light Screen", "Foul Play", "Spikes"],
        )
        team = _team(klefki)
        parsed = parse_showdown(render_showdown(team), team_id=team.team_id)
        assert parsed.pokemon[0].gender is None
        assert parsed.pokemon[0].item is None
        assert parsed == team

    def test_one_move_pokemon(self):
        # Kangaskhan running just Fake Out + Last Resort is the canonical
        # < 4-moves Champions example. The schema's old `minItems: 4`
        # rejected this; the parser must accept down to 1 move.
        kanga = _entry(
            "Kangaskhan",
            gender="F",
            item="Silk Scarf",
            ability="Scrappy",
            nature="Adamant",
            evs={"hp": 4, "atk": 32, "def": 0, "spa": 0, "spd": 0, "spe": 28},
            moves=["Fake Out", "Last Resort"],
        )
        team = _team(kanga)
        parsed = parse_showdown(render_showdown(team), team_id=team.team_id)
        assert parsed.pokemon[0].moves == ["Fake Out", "Last Resort"]
        assert parsed == team

    def test_no_evs_block_round_trips_to_all_zeros(self):
        # Renderer omits the EVs line entirely when every stat is 0; the
        # parser must reconstitute that as a full all-zero dict so the
        # PokemonEntry shape stays consistent.
        bare = _entry(
            "Magnemite",
            gender=None,
            item=None,
            ability="Sturdy",
            nature="Modest",
            evs={k: 0 for k in ("hp", "atk", "def", "spa", "spd", "spe")},
            moves=["Thunderbolt"],
        )
        team = _team(bare)
        parsed = parse_showdown(render_showdown(team), team_id=team.team_id)
        assert parsed.pokemon[0].evs == bare.evs
        assert parsed == team

    def test_accepts_lf_line_endings(self):
        # Renderer emits CRLF (required by pokepast.es). Discord modal
        # input is whatever the user typed — usually LF on macOS / Linux
        # browsers. Parser must accept both.
        team = _team()
        crlf_paste = render_showdown(team)
        lf_paste = crlf_paste.replace("\r\n", "\n")
        parsed = parse_showdown(lf_paste, team_id=team.team_id)
        assert parsed == team

    def test_preserves_passed_in_team_id(self):
        team = _team(team_id="QBXXWXL05U")
        parsed = parse_showdown(render_showdown(team), team_id="DIFFERENT01")
        # The parser doesn't read team_id from the text — the caller
        # passes through the OCR-validated value.
        assert parsed.team_id == "DIFFERENT01"


class TestErrorCases:
    """Each validation rule surfaces a specific user-facing message."""

    def test_empty_paste(self):
        with pytest.raises(ShowdownParseError, match="empty"):
            parse_showdown("")

    def test_five_pokemon(self):
        team = _team()
        # Drop the trailing block (and its preceding separator) from the
        # canonical paste to land at 5 blocks.
        blocks = render_showdown(team).split("\r\n\r\n")
        partial = "\r\n\r\n".join(blocks[:5])
        with pytest.raises(ShowdownParseError, match="Expected 6 Pokemon, got 5"):
            parse_showdown(partial)

    def test_seven_pokemon(self):
        team = _team()
        blocks = render_showdown(team).split("\r\n\r\n")
        extra = "\r\n\r\n".join(blocks + [blocks[0]])
        with pytest.raises(ShowdownParseError, match="Expected 6 Pokemon, got 7"):
            parse_showdown(extra)

    def test_five_moves(self):
        team = _team()
        paste = render_showdown(team)
        # Inject a 5th move on the first mon by duplicating the last move
        # line of the first block.
        first_block, rest = paste.split("\r\n\r\n", 1)
        first_block_with_5_moves = first_block + "\r\n- Protect"
        broken = first_block_with_5_moves + "\r\n\r\n" + rest
        with pytest.raises(ShowdownParseError, match="too many moves"):
            parse_showdown(broken)

    def test_ev_value_over_cap(self):
        team = _team()
        paste = render_showdown(team).replace("32 HP", "33 HP", 1)
        with pytest.raises(ShowdownParseError, match="out of range"):
            parse_showdown(paste)

    def test_unknown_nature_hardy_suggests_serious(self):
        # Hardy is a Showdown-recognized neutral, but this bot only
        # emits / accepts "Serious" for neutral natures. The error must
        # explicitly tell the user to use Serious.
        team = _team()
        paste = render_showdown(team).replace("Modest Nature", "Hardy Nature", 1)
        with pytest.raises(ShowdownParseError, match="Serious"):
            parse_showdown(paste)

    def test_unknown_nature_typo(self):
        team = _team()
        paste = render_showdown(team).replace("Modest Nature", "Mdoest Nature", 1)
        with pytest.raises(ShowdownParseError, match="Mdoest"):
            parse_showdown(paste)

    def test_missing_ability_line(self):
        team = _team()
        paste = render_showdown(team)
        broken = paste.replace("Ability: Flower Veil\r\n", "", 1)
        with pytest.raises(ShowdownParseError, match="Ability"):
            parse_showdown(broken)

    def test_missing_nature_line(self):
        team = _team()
        paste = render_showdown(team)
        broken = paste.replace("Modest Nature\r\n", "", 1)
        with pytest.raises(ShowdownParseError, match="Nature"):
            parse_showdown(broken)

    def test_no_moves(self):
        # Stripping all four move lines from the first block leaves a
        # mon with zero moves — disallowed by the 1-move minimum.
        team = _team()
        first_block, rest = render_showdown(team).split("\r\n\r\n", 1)
        header_lines = [
            ln for ln in first_block.split("\r\n") if not ln.startswith("-")
        ]
        broken = "\r\n".join(header_lines) + "\r\n\r\n" + rest
        with pytest.raises(ShowdownParseError, match="no moves"):
            parse_showdown(broken)

    def test_malformed_species_header_with_unmatched_paren(self):
        team = _team()
        paste = render_showdown(team)
        broken = paste.replace("Floette-Eternal (F)", "Floette-Eternal (F", 1)
        with pytest.raises(ShowdownParseError):
            parse_showdown(broken)
