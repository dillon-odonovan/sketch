"""Parse a Showdown / PokePaste export back into a TeamData.

Strict inverse of `render_showdown` in `pokepaste_renderer` — accepts the
same line-oriented format the renderer emits, plus reasonable variations
(LF or CRLF line endings, case-insensitive stat labels, lenient spacing
around `@`). Used by the `/add-team` Edit flow to round-trip a
user-edited paste back into the structured `TeamData` the cache writer
expects.

The parser is intentionally strict about *which* values it accepts — only
the 20 stat-arrow natures plus the canonical neutral "Serious", EVs
capped at 32 per stat — so the cache and the in-sheet TEAMDATAFROMPASTE
never see a shape this bot couldn't itself emit. Lenience lives at the
*syntax* layer (whitespace, line endings) so the user isn't tripped up
by typing differences.
"""

from __future__ import annotations

import re

from sketch.replica.extractor import (
    _NATURE_MAP,
    _NEUTRAL_NATURE,
    STAT_KEYS,
    PokemonEntry,
    TeamData,
)


class ShowdownParseError(Exception):
    """Raised when the input doesn't conform to the Showdown export shape.

    The message is user-facing — it travels into the Discord modal title
    and the input label on re-open, so it should be concise enough to fit
    Discord's 45-char caps after truncation and specific enough that the
    user can locate and fix the offending block.
    """


# Single source of truth for the canonical nature names this bot will
# accept and emit. Derived from `_NATURE_MAP` so renderer and parser
# never drift on which set of natures is supported.
_VALID_NATURES: frozenset[str] = frozenset(_NATURE_MAP.values()) | {_NEUTRAL_NATURE}

# Stat-label -> internal key. Renderer emits `HP / Atk / Def / SpA / SpD /
# Spe`; we accept any case so a user typing `hp` or `ATK` round-trips
# cleanly. Strict on which labels are recognized — unknown labels surface
# as an explicit error rather than silently zeroing the stat.
_STAT_LABEL_TO_KEY: dict[str, str] = {
    "hp": "hp",
    "atk": "atk",
    "def": "def",
    "spa": "spa",
    "spd": "spd",
    "spe": "spe",
}

# Per-line patterns. Each is anchored and matches one whole line — easy
# to verify in isolation on regex101 with no multiline flag. The species
# header is parsed step-by-step in `_parse_species_header` rather than
# with a mega-regex, because the lazy-quantifier interplay needed to
# carve species / gender / item out of a single match is hard to read.
_ABILITY_RE = re.compile(r"^Ability:\s*(?P<ability>.+?)\s*$")
_EVS_RE = re.compile(r"^EVs:\s*(?P<body>.+?)\s*$")
_MOVE_RE = re.compile(r"^-\s*(?P<move>.+?)\s*$")

# Trailing marker on a nature line, e.g. `Modest Nature`. We locate the
# line by suffix match (`endswith _NATURE_SUFFIX`) rather than regex so
# the lookup is obvious — the nature name is whatever sits before it,
# trimmed and validated against `_VALID_NATURES`.
_NATURE_SUFFIX = " Nature"

# EV entry: `<n> <Label>` e.g. `32 HP` or `4 SpA`.
_EV_ENTRY_RE = re.compile(r"^(?P<value>\d+)\s+(?P<label>[A-Za-z]+)$")


def parse_showdown(text: str, *, team_id: str | None = None) -> TeamData:
    """Parse Showdown export text into a `TeamData`.

    Accepts LF or CRLF line endings. `team_id` is passed through to the
    returned `TeamData` since the Showdown format doesn't carry it — the
    caller (the /add-team Edit flow) preserves the value from the team
    that was OCR'd and Team-ID-validated before the preview was shown.

    Raises `ShowdownParseError` with a user-facing message on any
    structural or validation failure.
    """
    if not text or not text.strip():
        raise ShowdownParseError(
            "Paste is empty. Please provide all 6 Pokemon in Showdown format."
        )

    # Split on a blank line separating blocks. `\s*` between newlines
    # tolerates stray whitespace on the blank line itself.
    raw_blocks = re.split(r"\r?\n\s*\r?\n", text.strip())
    blocks = [b for b in (b.strip() for b in raw_blocks) if b]

    if len(blocks) != 6:
        raise ShowdownParseError(f"Expected 6 Pokemon, got {len(blocks)}.")

    pokemon = [_parse_block(block, slot) for slot, block in enumerate(blocks, start=1)]
    return TeamData(pokemon=pokemon, team_id=team_id)


def _parse_block(block: str, slot: int) -> PokemonEntry:
    """Parse one Pokemon block (between blank-line separators)."""
    lines = [ln.rstrip() for ln in re.split(r"\r?\n", block) if ln.strip()]
    if not lines:
        raise ShowdownParseError(f"Pokemon {slot}: empty block.")

    species, gender, item = _parse_species_header(lines[0], slot)

    # The renderer's field order is fixed: Ability, [EVs], Nature, then
    # moves. We walk the remaining lines in order, accepting Ability /
    # EVs / Nature in any order before the first move line, then
    # collect move lines until the block ends. This gives the user a
    # little flexibility in case they reorder header lines while
    # editing, but still requires every required line to be present.
    ability: str | None = None
    evs: dict[str, int] | None = None
    nature: str | None = None
    moves: list[str] = []

    idx = 1
    while idx < len(lines):
        line = lines[idx]
        if line.startswith("-"):
            break

        if (m := _ABILITY_RE.match(line)) and ability is None:
            ability = m.group("ability").strip()
        elif (m := _EVS_RE.match(line)) and evs is None:
            evs = _parse_evs(m.group("body"), slot)
        elif line.endswith(_NATURE_SUFFIX) and nature is None:
            nature = line[: -len(_NATURE_SUFFIX)].strip()
            if nature not in _VALID_NATURES:
                raise ShowdownParseError(
                    f"Pokemon {slot}: unknown nature `{nature}`. Use one of "
                    f"the 20 stat-shifting natures (Adamant, Modest, Jolly, "
                    f"Timid, …) or `Serious` for a neutral nature."
                )
        else:
            raise ShowdownParseError(f"Pokemon {slot}: unexpected line `{line}`.")
        idx += 1

    while idx < len(lines):
        line = lines[idx]
        if not (m := _MOVE_RE.match(line)):
            raise ShowdownParseError(
                f"Pokemon {slot}: expected a `- Move` line, got `{line}`."
            )
        moves.append(m.group("move").strip())
        idx += 1

    if ability is None:
        raise ShowdownParseError(f"Pokemon {slot}: missing `Ability:` line.")
    if nature is None:
        raise ShowdownParseError(f"Pokemon {slot}: missing `<Name> Nature` line.")
    if not moves:
        raise ShowdownParseError(f"Pokemon {slot}: no moves listed.")
    if len(moves) > 4:
        raise ShowdownParseError(
            f"Pokemon {slot}: too many moves ({len(moves)}); max 4."
        )

    if evs is None:
        # Renderer omits the EVs line entirely when every stat is zero.
        # Mirror that by defaulting to an all-zero dict so PokemonEntry
        # still satisfies its `evs: dict[str, int]` shape.
        evs = {k: 0 for k in STAT_KEYS}

    return PokemonEntry(
        species=species,
        gender=gender,
        item=item,
        ability=ability,
        nature=nature,
        evs=evs,
        moves=moves,
    )


def _parse_species_header(line: str, slot: int) -> tuple[str, str | None, str | None]:
    """Pull species / gender / item out of the first line of a block.

    Header shape (from `pokepaste_renderer._render_mon`):
        `{species}[ ({M|F})][ @ {item}]`

    Carved step by step rather than with one regex because the three
    fields stack as suffixes — peeling them off the right end leaves
    the species exposed without juggling lazy quantifiers.
    """
    rest = line.strip()
    if not rest:
        raise ShowdownParseError(f"Pokemon {slot}: empty species line.")

    # Peel the item off the right first, if any. The renderer uses
    # ` @ ` (space-at-space); accept any whitespace around `@` so a
    # user-typed `Mimikyu @Life Orb` also works.
    item: str | None = None
    at_match = re.search(r"\s*@\s*", rest)
    if at_match:
        item_raw = rest[at_match.end() :].strip()
        item = item_raw or None
        rest = rest[: at_match.start()].rstrip()
        # An unmatched `(` after stripping the item half points at a
        # malformed gender group like `Floette-Eternal (F` — easier to
        # reject explicitly here than to let the species capture
        # silently absorb the stray paren.
        if "(" in rest and ")" not in rest:
            raise ShowdownParseError(
                f"Pokemon {slot}: malformed species line `{line}`."
            )

    # Peel the gender off the right next, if any. Must be at the very
    # end and exactly one capital letter — anything else (e.g. `(F-`)
    # is a malformed header and not a valid gender marker.
    gender: str | None = None
    gender_match = re.search(r"\s*\((M|F)\)\s*$", rest)
    if gender_match:
        gender = gender_match.group(1)
        rest = rest[: gender_match.start()].rstrip()
    elif "(" in rest or ")" in rest:
        raise ShowdownParseError(f"Pokemon {slot}: malformed species line `{line}`.")

    species = rest.strip()
    if not species:
        raise ShowdownParseError(f"Pokemon {slot}: missing species name.")
    return species, gender, item


def _parse_evs(body: str, slot: int) -> dict[str, int]:
    """Parse the body of an `EVs:` line into a stat dict.

    Format: `32 HP / 32 Atk / 4 Spe`. Missing stats default to zero (so
    a paste with only nonzero stats listed still produces a full
    six-key dict).
    """
    evs = {k: 0 for k in STAT_KEYS}
    parts = [p.strip() for p in body.split("/") if p.strip()]
    if not parts:
        raise ShowdownParseError(f"Pokemon {slot}: `EVs:` line has no values.")
    for part in parts:
        m = _EV_ENTRY_RE.match(part)
        if not m:
            raise ShowdownParseError(
                f"Pokemon {slot}: can't parse EV entry `{part}`. "
                f"Expected `<number> <Stat>`, e.g. `32 HP`."
            )
        value = int(m.group("value"))
        label = m.group("label").lower()
        key = _STAT_LABEL_TO_KEY.get(label)
        if key is None:
            raise ShowdownParseError(
                f"Pokemon {slot}: unknown stat `{m.group('label')}`. "
                f"Use HP / Atk / Def / SpA / SpD / Spe."
            )
        if value < 0 or value > 32:
            raise ShowdownParseError(
                f"Pokemon {slot}: EV value {value} out of range. Pokemon "
                f"Champions caps EVs at 32 per stat."
            )
        evs[key] = value
    return evs
