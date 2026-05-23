"""Pokémon DEX (species index) for fuzzy/prefix-group name resolution.

Moved out of commands.py so SheetsClient can return DexIndex from its lazy
`get_dex()` method without creating a circular import with commands.py.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass


@dataclass
class ResolveResult:
    canonical_matches: list[str]
    suggestions: list[str]


class DexIndex:
    def __init__(self, names: list[str]):
        self._lower_to_canonical = {n.lower(): n for n in names}

    def __len__(self) -> int:
        return len(self._lower_to_canonical)

    def resolve(self, query: str) -> ResolveResult:
        norm = query.strip().lower()
        if not norm:
            return ResolveResult([], [])
        # Prefix-group rule: a DEX name matches the query if it equals the
        # query OR starts with `query + "-"`. So "charizard" matches
        # Charizard / Charizard-Mega-X / Charizard-Mega-Y, but "char" matches
        # nothing (no full-name boundary). Letting users type the base form
        # is the natural search behavior; typing a specific form (e.g.
        # "charizard-mega-y") narrows to just that one.
        matches = [
            self._lower_to_canonical[k]
            for k in self._lower_to_canonical
            if k == norm or k.startswith(norm + "-")
        ]
        if matches:
            return ResolveResult(canonical_matches=matches, suggestions=[])
        close = difflib.get_close_matches(
            norm, list(self._lower_to_canonical.keys()), n=5, cutoff=0.6
        )
        return ResolveResult(
            canonical_matches=[],
            suggestions=[self._lower_to_canonical[k] for k in close],
        )
