"""Resolve pre-form species names in an OTS into their canonical forms.

`parse_showdown` returns the species verbatim, so a Showdown text export
that writes a Mega in its pre-Mega form with the stone on the same line
(`Charizard @ Charizardite Y`) yields the base species `Charizard`. The
team bank stores resolved forms (`Charizard-Mega-Y`), so the base name
never matches and the mon falls through to the LLM estimator instead of
reusing a known spread.

This module bridges that gap: infer the intended form from the held item
using stable game-mechanic rules, then validate the inferred name against
the guild's `DexIndex` so we only ever upgrade to a form that actually
exists. The DEX guard keeps the item rules safe to apply generically — a
stray item on a species without that form simply leaves the name
unchanged.
"""

from __future__ import annotations

import dataclasses
import re

from sketch.search.dex import DexIndex
from sketch.team import TeamData

# A Mega Stone is named `<stem>ite` with an optional ` X`/` Y`/` Z` variant
# suffix (Charizardite Y, Mewtwonite X, …). We don't parse the stem — the
# header species plus the DEX guard settle the species — so this only needs
# to recognize the shape and pull the variant letter.
_MEGA_STONE_RE = re.compile(r".+ite(?: ([xyz]))?$")

# The one common `…ite` held item that is NOT a Mega Stone.
_NON_MEGA_ITE_ITEMS: frozenset[str] = frozenset({"eviolite"})

# Form-changing held items keyed to their resulting form suffix. These are
# fixed game mechanics (not roster content): Primal reversion orbs, the
# Origin-forme items, and the Crowned-forme rusted weapons. The stat-boosting
# `Adamant Orb` / `Lustrous Orb` are deliberately absent — only the
# form-changing `Adamant Crystal` / `Lustrous Globe` belong here.
_ITEM_FORM_SUFFIX: dict[str, str] = {
    "red orb": "-Primal",
    "blue orb": "-Primal",
    "griseous orb": "-Origin",
    "griseous core": "-Origin",
    "adamant crystal": "-Origin",
    "lustrous globe": "-Origin",
    "rusted sword": "-Crowned",
    "rusted shield": "-Crowned",
}


def _form_from_item(species: str, item: str | None) -> str | None:
    """Return the item-implied form name for `species`, or None.

    Produces a candidate name only — the caller validates it against the
    DEX before trusting it, so these rules can be applied without knowing
    whether `species` is actually form-capable.
    """
    if not item:
        return None
    norm = item.strip().lower()
    if not norm:
        return None

    if norm not in _NON_MEGA_ITE_ITEMS and (m := _MEGA_STONE_RE.fullmatch(norm)):
        variant = m.group(1)
        suffix = f"-Mega-{variant.upper()}" if variant else "-Mega"
        return f"{species}{suffix}"

    suffix = _ITEM_FORM_SUFFIX.get(norm)
    if suffix is not None:
        return f"{species}{suffix}"

    return None


def normalize_species(species: str, item: str | None, dex: DexIndex) -> str:
    """Upgrade a base species to its item-implied form, if the DEX has it.

    Returns the canonical-cased form name when the held item implies a
    Mega/Primal/Origin/Crowned form and that form exists in `dex`;
    otherwise returns `species` unchanged.
    """
    candidate = _form_from_item(species, item)
    if candidate is None:
        return species
    matches = {m.lower(): m for m in dex.resolve(species).canonical_matches}
    return matches.get(candidate.lower(), species)


def normalize_team(team: TeamData, dex: DexIndex) -> TeamData:
    """Return `team` with every Pokemon's species resolved via the DEX."""
    new_pokemon = [
        dataclasses.replace(p, species=normalize_species(p.species, p.item, dex))
        for p in team.pokemon
    ]
    return dataclasses.replace(team, pokemon=new_pokemon)
