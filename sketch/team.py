"""Shared team data shape.

`TeamData` + `PokemonEntry` are the contract between any team *producer*
(Champions OCR, VRPaste fetcher, future sources) and any team *consumer*
(Pokepaste renderer, preview embed). They live at the top level rather
than under any one producer or consumer because none of those modules
owns the shape — it's the lingua franca.

`STAT_KEYS` is the canonical Showdown stat key order, used as the keys
of every `PokemonEntry.evs` dict and as the source-of-truth iteration
order for renderers that emit a stat line.
"""

from __future__ import annotations

from dataclasses import dataclass

STAT_KEYS = ("hp", "atk", "def", "spa", "spd", "spe")

# Display capitalization for each stat key (the canonical Showdown EV-line
# tags). Shared by the Showdown renderer and the `/convert-ots` provenance
# summary so both spell stats the same way ("SpA", not "spa").
STAT_DISPLAY = {
    "hp": "HP",
    "atk": "Atk",
    "def": "Def",
    "spa": "SpA",
    "spd": "SpD",
    "spe": "Spe",
}

# Per-stat EV cap for Pokemon Champions formats (Reg M-A, M-B, …). Mainline
# VGC formats cap at 252; that difference is what the format-driven `EvModel`
# in `sketch.convert.ev_model` exists to express. This constant is the single
# source of truth for the Champions bound — `showdown_parser` defaults its EV
# validation to it, and `EvModel.CHAMPIONS` reuses it — and lives here in the
# dependency-free lingua-franca module so neither importer creates a cycle.
CHAMPIONS_EV_MAX_PER_STAT = 32


def norm_species(name: str) -> str:
    """Casefold a species name for equality comparison and lookup keys.

    The single source of truth for "are these the same species" across the
    codebase: the bank matcher, the usage-prior keys, the OTS species set, and
    the `/search-teams` mon filter all compare species through this so they
    can never drift. Both sides are expected to already be canonical form
    names (e.g. `Charizard-Mega-Y`); this only strips surrounding whitespace
    and lowercases. It is distinct from `convert.normalize.normalize_species`,
    which *resolves* a base species + item into its battle form.
    """
    return name.strip().lower()


@dataclass(frozen=True)
class PokemonEntry:
    species: str
    gender: str | None  # "M", "F", or None for genderless / not displayed
    item: str | None
    ability: str
    nature: str  # canonical Showdown name (e.g. "Modest", "Adamant", "Serious")
    evs: dict[str, int]  # keys = STAT_KEYS, values >= 0
    moves: list[str]


@dataclass(frozen=True)
class TeamData:
    pokemon: list[PokemonEntry]
    # `team_id` is the 10-char alphanumeric code shown at the top of both
    # Champions Replica share-screen pages. Captured so the OCR command
    # handler can verify that the user-submitted code matches what the
    # screenshots actually show — protects against cache poisoning by
    # mismatched code/screenshot pairs. None for non-Champions sources
    # (VRPaste, etc.) and when the model couldn't read it.
    team_id: str | None = None
