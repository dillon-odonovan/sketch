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
