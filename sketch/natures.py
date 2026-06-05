"""Canonical Pokemon nature lookup tables.

Shared between the share-screen extractor (which resolves a nature from the
boosted/reduced stat arrows) and the Showdown parser (which validates the set
of nature names it will accept). Lives at the top level rather than under any
one consumer because neither owns the data — it's the lingua franca for
nature names, the same role `team.py` plays for the team shape.
"""

from __future__ import annotations

# (boosted_stat, reduced_stat) -> canonical nature name. The deterministic
# 20-entry mapping is the only safe way to translate the share-screen arrows
# since the screen never spells the nature out.
NATURE_MAP: dict[tuple[str, str], str] = {
    ("Attack", "Defense"): "Lonely",
    ("Attack", "Sp. Atk"): "Adamant",
    ("Attack", "Sp. Def"): "Naughty",
    ("Attack", "Speed"): "Brave",
    ("Defense", "Attack"): "Bold",
    ("Defense", "Sp. Atk"): "Impish",
    ("Defense", "Sp. Def"): "Lax",
    ("Defense", "Speed"): "Relaxed",
    ("Sp. Atk", "Attack"): "Modest",
    ("Sp. Atk", "Defense"): "Mild",
    ("Sp. Atk", "Sp. Def"): "Rash",
    ("Sp. Atk", "Speed"): "Quiet",
    ("Sp. Def", "Attack"): "Calm",
    ("Sp. Def", "Defense"): "Gentle",
    ("Sp. Def", "Sp. Atk"): "Careful",
    ("Sp. Def", "Speed"): "Sassy",
    ("Speed", "Attack"): "Timid",
    ("Speed", "Defense"): "Hasty",
    ("Speed", "Sp. Atk"): "Jolly",
    ("Speed", "Sp. Def"): "Naive",
}

# "Serious" is the canonical Showdown / PokePaste neutral — by convention,
# pastes that didn't specify a nature use Serious, while Hardy implies the
# mon was actually drawn from in-game wild encounters with a Hardy nature.
# The extractor sees "no arrows" on the share screen, which carries no
# information either way, so the canonical-neutral output is the honest one.
NEUTRAL_NATURE = "Serious"
