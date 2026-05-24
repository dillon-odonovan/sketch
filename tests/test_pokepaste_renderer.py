"""Tests for the Showdown renderer and pokepast.es uploader.

The renderer is a pure function — golden-file comparison covers it
exhaustively. The uploader is mocked with `aioresponses` so the suite
never makes a real POST.

The golden assertions matter because the in-sheet `TEAMDATAFROMPASTE`
AppsScript depends on this format being parseable: any drift here would
silently break the species columns for every replica-added team.
"""

from __future__ import annotations

import pytest
from aioresponses import aioresponses

from sketch.replica.extractor import PokemonEntry, TeamData
from sketch.replica.pokepaste_renderer import (
    RenderError,
    post_to_pokepaste,
    render_showdown,
)


def _entry(
    *,
    species: str = "Iron Hands",
    item: str | None = "Assault Vest",
    ability: str = "Quark Drive",
    tera_type: str = "Water",
    nature: str = "Adamant",
    evs: dict | None = None,
    ivs: dict | None = None,
    moves: list[str] | None = None,
    level: int = 50,
) -> PokemonEntry:
    return PokemonEntry(
        species=species,
        item=item,
        ability=ability,
        tera_type=tera_type,
        nature=nature,
        evs=evs or {"hp": 252, "atk": 252, "def": 0, "spa": 0, "spd": 4, "spe": 0},
        ivs=ivs,
        moves=moves or ["Fake Out", "Drain Punch", "Wild Charge", "Heavy Slam"],
        level=level,
    )


class TestRenderShowdown:
    def test_single_mon_basic_block(self):
        team = TeamData(pokemon=[_entry()])
        expected = "\n".join(
            [
                "Iron Hands @ Assault Vest",
                "Ability: Quark Drive",
                "Level: 50",
                "Tera Type: Water",
                "EVs: 252 HP / 252 Atk / 4 SpD",
                "Adamant Nature",
                "- Fake Out",
                "- Drain Punch",
                "- Wild Charge",
                "- Heavy Slam",
            ]
        )
        assert render_showdown(team) == expected

    def test_no_item_omits_at_suffix(self):
        # The "@ item" suffix is optional in Showdown export. None must
        # render as a clean species line, not "Iron Hands @ None".
        team = TeamData(pokemon=[_entry(item=None)])
        rendered = render_showdown(team)
        assert rendered.splitlines()[0] == "Iron Hands"

    def test_zero_ev_stats_are_omitted_from_evs_line(self):
        # A 252/252/0/0/4/0 spread should produce only the non-zero stats
        # on the EVs line — the canonical Showdown convention.
        team = TeamData(
            pokemon=[
                _entry(
                    evs={
                        "hp": 252,
                        "atk": 252,
                        "def": 0,
                        "spa": 0,
                        "spd": 4,
                        "spe": 0,
                    }
                )
            ]
        )
        rendered = render_showdown(team)
        assert "EVs: 252 HP / 252 Atk / 4 SpD" in rendered
        assert " 0 " not in rendered

    def test_full_zero_ev_team_skips_evs_line(self):
        # A team with no EV investment at all (edge case: a fresh-caught
        # mon dropped into Champions) should omit the EVs line entirely
        # rather than render an empty "EVs: ".
        team = TeamData(
            pokemon=[
                _entry(evs={"hp": 0, "atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0})
            ]
        )
        rendered = render_showdown(team)
        assert "EVs:" not in rendered

    def test_ivs_line_omitted_when_all_31(self):
        # The extractor returns ivs=None when all six are 31; an explicit
        # dict of 31s shouldn't render an IVs line either (the renderer
        # checks each stat, not just the None sentinel).
        team = TeamData(
            pokemon=[
                _entry(
                    ivs={
                        "hp": 31,
                        "atk": 31,
                        "def": 31,
                        "spa": 31,
                        "spd": 31,
                        "spe": 31,
                    }
                )
            ]
        )
        rendered = render_showdown(team)
        assert "IVs:" not in rendered

    def test_ivs_line_lists_only_non_31_stats(self):
        # 0 Atk IV on a special attacker is the most common non-31 case;
        # only the offending stat should appear on the IVs line.
        team = TeamData(
            pokemon=[
                _entry(
                    ivs={"hp": 31, "atk": 0, "def": 31, "spa": 31, "spd": 31, "spe": 31}
                )
            ]
        )
        rendered = render_showdown(team)
        assert "IVs: 0 Atk" in rendered

    def test_level_always_emitted(self):
        # PokePaste and Showdown both default to level 100 when the line
        # is absent. VGC is level 50, so the line must always be present.
        team = TeamData(pokemon=[_entry(level=50)])
        rendered = render_showdown(team)
        assert "Level: 50" in rendered

    def test_six_mons_separated_by_blank_lines(self):
        team = TeamData(pokemon=[_entry() for _ in range(6)])
        rendered = render_showdown(team)
        blocks = rendered.split("\n\n")
        assert len(blocks) == 6

    def test_no_trailing_newline(self):
        # Showdown's clipboard export and pokepast.es both produce text
        # with no trailing newline. Match the convention so a round-trip
        # comparison would succeed.
        team = TeamData(pokemon=[_entry()])
        assert not render_showdown(team).endswith("\n")


class TestPostToPokepaste:
    async def test_returns_canonical_url_from_redirect(self):
        with aioresponses() as mock:
            mock.post(
                "https://pokepast.es/create",
                status=303,
                headers={"Location": "/abc123def"},
            )
            url = await post_to_pokepaste("paste text", "Replica AAAA111122")
        # Relative redirect promoted to absolute and run through the
        # canonicalizer (which the rest of the bot uses for dedup).
        assert url == "https://pokepast.es/abc123def"

    async def test_accepts_absolute_redirect(self):
        with aioresponses() as mock:
            mock.post(
                "https://pokepast.es/create",
                status=302,
                headers={"Location": "https://pokepast.es/xyz789"},
            )
            url = await post_to_pokepaste("paste text", "Replica BBBB222233")
        assert url == "https://pokepast.es/xyz789"

    async def test_non_redirect_status_raises_render_error(self):
        with aioresponses() as mock:
            mock.post(
                "https://pokepast.es/create",
                status=500,
                body="Internal Server Error",
            )
            with pytest.raises(RenderError, match="pokepast.es"):
                await post_to_pokepaste("paste text", "Replica CCCC333344")

    async def test_missing_location_header_raises(self):
        # A 302 without a Location header is a malformed response; the
        # uploader must catch this rather than silently returning "".
        with aioresponses() as mock:
            mock.post("https://pokepast.es/create", status=302, body="")
            with pytest.raises(RenderError, match="pokepast.es"):
                await post_to_pokepaste("paste text", "Replica DDDD444455")
