"""Authoritative localized→English name lookup for Replica OCR.

The vision model echoes the raw on-screen text alongside its English guess for
every name field (see ``extractor.py``). This module maps that raw text to the
canonical English name using a committed PokeAPI-derived table, correcting the
model's translation-class misses (katakana loanwords, look-alike collisions,
weakly-recognized recent species). On a miss — Champions-custom content the
table doesn't know — we fall back to the model's own English guess.

The table is built offline by ``bin/build_pokeapi_names.py`` into
``data/pokeapi_names.json`` and ships in the image; nothing here touches the
network. ``normalize`` is shared with the build script so the keys written at
build time and the keys looked up at runtime can never drift.
"""

from __future__ import annotations

import gzip
import importlib.resources
import json
import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

_DATA_PACKAGE = "sketch.champions"
_DATA_RESOURCE = ("data", "pokeapi_names.json.gz")

# Categories present as top-level keys in the JSON data file.
_CATEGORIES = ("species", "items", "abilities", "moves")


def normalize(name: str) -> str:
    """Canonicalize a name for table keys and lookups.

    Strips all surrounding *and* internal whitespace — the share screen renders
    "구애 스카프" with a space while the PokeAPI CSV stores "구애스카프" — and
    casefolds so Latin-script spellings match regardless of case. ``str.split``
    treats every Unicode whitespace run (including the full-width U+3000 space)
    as a separator, so joining the pieces drops them all.
    """
    return "".join(name.split()).casefold()


@lru_cache(maxsize=1)
def _tables() -> dict[str, dict[str, str]]:
    """Load and cache the localized→English tables.

    Returns empty tables (and logs) if the data file is missing or malformed so
    a packaging slip degrades OCR to the model's own English guesses rather than
    crashing the extraction path outright.
    """
    try:
        resource = importlib.resources.files(_DATA_PACKAGE).joinpath(*_DATA_RESOURCE)
        raw = json.loads(gzip.decompress(resource.read_bytes()).decode("utf-8"))
    except (FileNotFoundError, ModuleNotFoundError, OSError, ValueError) as exc:
        logger.warning(
            "Could not load PokeAPI name table; OCR lookups disabled: %s", exc
        )
        return {category: {} for category in _CATEGORIES}
    return {category: dict(raw.get(category, {})) for category in _CATEGORIES}


def _resolve(category: str, raw: str | None, en: str) -> str:
    """Override ``en`` with the table's canonical English on a normalized hit."""
    if not raw:
        return en
    key = normalize(raw)
    if not key:
        return en
    return _tables()[category].get(key, en)


def resolve_item(raw: str | None, en: str) -> str:
    return _resolve("items", raw, en)


def resolve_ability(raw: str | None, en: str) -> str:
    return _resolve("abilities", raw, en)


def resolve_move(raw: str | None, en: str) -> str:
    return _resolve("moves", raw, en)


def resolve_species(raw: str | None, en: str) -> str:
    """Resolve a species name, preserving any form the model already produced.

    The PokeAPI species table holds base-species names only (no Hisui/Galar/Mega
    suffixes). When the model's English guess already carries a form suffix
    (a hyphen — ``Typhlosion-Hisui``, ``Calyrex-Shadow``), it derived that from
    type/ability/move signals; blindly overriding from a base-name read would
    strip the form. So we only apply the lookup to plain base names.
    """
    if "-" in (en or ""):
        return en
    return _resolve("species", raw, en)
