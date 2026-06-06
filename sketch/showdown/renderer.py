"""Render a TeamData (from any source) into Showdown / PokePaste export text.

`render_showdown(team)` produces the line-oriented format the in-sheet
TEAMDATAFROMPASTE AppsScript already knows how to parse and that
`sketch.showdown.parser.parse_showdown` reads back — the two are strict
inverses. Uploading that text to pokepast.es is a separate concern living in
`sketch.pokepaste.uploader`, so tests can golden-file the text without
touching the network.

The rendered shape: gender suffix on the species line, EVs line listing
only non-zero stats, Nature line, four `- Move` lines. No Tera Type,
Level, or IVs — Champions Replica share screens don't surface those,
VRPaste's API doesn't expose them, and the reference paste that
round-trips cleanly through pokepast.es and the in-sheet AppsScript
omits them.
"""

from __future__ import annotations

from sketch.team import STAT_KEYS, PokemonEntry, TeamData

# Display capitalization for the EV line. The Showdown / PokePaste parsers
# accept lowercase too, but the canonical form uses these tags and matching
# it keeps the resulting paste readable when an operator opens it manually.
_STAT_DISPLAY = {
    "hp": "HP",
    "atk": "Atk",
    "def": "Def",
    "spa": "SpA",
    "spd": "SpD",
    "spe": "Spe",
}

# CRLF line endings throughout the rendered paste. Pokemon Showdown's
# clipboard export uses CRLF (authored for Windows clipboard interchange),
# and pokepast.es's parser splits Pokemon blocks on `\r\n\r\n` specifically
# — emitting plain LF produces a paste that pokepast.es treats as one
# giant single-Pokemon entry instead of six separate ones. The in-sheet
# TEAMDATAFROMPASTE AppsScript fetches the same canonical raw text from
# pokepast.es, so CRLF round-trips through both renderers without surprise.
_LINE_END = "\r\n"


def _render_mon(p: PokemonEntry) -> str:
    """Render one Pokemon as a Showdown-export block.

    Field order follows the reference paste's convention exactly (species
    [+gender] [+item], Ability, EVs, Nature, four moves). Order is
    load-bearing because the in-sheet AppsScript `TEAMDATAFROMPASTE`
    pattern-matches against this shape — reorders here would silently
    break parsing for every replica-added team.

    TODO(champions-tera): when Pokemon Champions enables terastallization
    in-game, restore `tera_type` on `PokemonEntry` and emit a `Tera Type:`
    line between `Ability:` and `EVs:` (the canonical Showdown position).
    Level and IVs are intentionally omitted: Champions fixes level to 50
    and all IVs to 31, neither of which the share screen surfaces, so
    emitting them would synthesize data we didn't extract.
    """
    lines: list[str] = []

    gender_suffix = f" ({p.gender})" if p.gender else ""
    item_suffix = f" @ {p.item}" if p.item else ""
    lines.append(f"{p.species}{gender_suffix}{item_suffix}")

    lines.append(f"Ability: {p.ability}")

    ev_parts = [
        f"{p.evs[k]} {_STAT_DISPLAY[k]}" for k in STAT_KEYS if p.evs.get(k, 0) > 0
    ]
    if ev_parts:
        lines.append(f"EVs: {' / '.join(ev_parts)}")

    lines.append(f"{p.nature} Nature")

    for move in p.moves:
        lines.append(f"- {move}")

    return _LINE_END.join(lines)


def render_showdown(team: TeamData) -> str:
    """Render the full team as one PokePaste-compatible string.

    Each Pokemon block is separated by a blank line. CRLF line endings
    are required by pokepast.es's block-splitter — see `_LINE_END`. No
    trailing newline, matching Showdown's clipboard export shape.
    """
    return (_LINE_END * 2).join(_render_mon(p) for p in team.pokemon)
