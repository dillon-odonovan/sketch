"""Pick the best bank EV spread for one OTS Pokemon.

Pure, side-effect-free scoring over the candidate `BankTeam`s loaded by
`sketch.convert.bank`. Given one target OTS Pokemon, gather every bank
Pokemon of the same species and rank them by a single lexicographic key,
documented in `_score` and easy to retune in one place:

  1. nature / stat-alignment match  (validity gate — a spread is only
     meaningful under its intended nature; `32 Atk / 32 Spe` reads as
     Adamant *or* Jolly, and the nature disambiguates which stats the
     investment was actually for)
  2. ability match                  (highest-precedence set signal)
  3. item match
  4. move-overlap count
  5. team-composition overlap        (more of the OTS's six mons present
     on the source team ⇒ more representative archetype)

Among candidates tied on that key, the *most frequent* exact spread wins
— so a common ability+item+moves Pokemon with several different spreads
in the bank converges on the consensus one rather than an outlier.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from sketch.convert.bank import BankTeam, _norm_species
from sketch.team import STAT_KEYS, PokemonEntry


@dataclass(frozen=True)
class EvChoice:
    """A chosen EV spread plus where it came from.

    `source` is the coarse provenance for the user-facing summary
    (`"bank"` here; the converter uses `"estimated"` for LLM guesses and
    `"kept"` for already-trained mons). `detail` is a short log-only note
    on how strong the match was.
    """

    evs: dict[str, int]
    source: str
    detail: str


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def _move_set(moves: list[str]) -> frozenset[str]:
    return frozenset(_norm(m) for m in moves)


@dataclass(frozen=True)
class _Candidate:
    entry: PokemonEntry
    overlap: int  # how many OTS species appear on this candidate's team


def _score(target: PokemonEntry, cand: _Candidate) -> tuple[int, int, int, int, int]:
    """Lexicographic ranking key (higher is better) for one candidate."""
    target_moves = _move_set(target.moves)
    return (
        int(_norm(cand.entry.nature) == _norm(target.nature)),
        int(_norm(cand.entry.ability) == _norm(target.ability)),
        int(_norm(cand.entry.item) == _norm(target.item)),
        len(target_moves & _move_set(cand.entry.moves)),
        cand.overlap,
    )


def _clamp(evs: dict[str, int], max_per_stat: int) -> dict[str, int]:
    return {k: max(0, min(int(evs.get(k, 0)), max_per_stat)) for k in STAT_KEYS}


def choose_evs(
    target: PokemonEntry,
    bank_teams: list[BankTeam],
    ots_species: set[str],
    max_per_stat: int,
) -> EvChoice | None:
    """Return the best bank spread for `target`, or None if no bank match.

    `ots_species` is the normalized set of the OTS's species (used to
    compute each candidate team's composition overlap). The returned
    spread is clamped to `max_per_stat`.
    """
    target_species = _norm_species(target.species)
    candidates: list[_Candidate] = []
    for bt in bank_teams:
        overlap = len(ots_species & {_norm_species(p.species) for p in bt.team.pokemon})
        for entry in bt.team.pokemon:
            if _norm_species(entry.species) == target_species:
                candidates.append(_Candidate(entry=entry, overlap=overlap))

    if not candidates:
        return None

    best_key = max(_score(target, c) for c in candidates)
    top = [c for c in candidates if _score(target, c) == best_key]

    # Frequency tiebreak: among the equally-ranked candidates, pick the
    # spread that shows up most often. Counter.most_common preserves
    # insertion order on ties, so the first-seen spread wins a true tie.
    spread_counts = Counter(
        tuple(_clamp(c.entry.evs, max_per_stat)[k] for k in STAT_KEYS) for c in top
    )
    winning_tuple, freq = spread_counts.most_common(1)[0]
    evs = dict(zip(STAT_KEYS, winning_tuple, strict=False))

    nature_ok, ability_ok, item_ok, move_overlap, overlap = best_key
    detail = (
        f"nature={'y' if nature_ok else 'n'} ability={'y' if ability_ok else 'n'} "
        f"item={'y' if item_ok else 'n'} moves={move_overlap} "
        f"composition={overlap} (from {len(candidates)} candidate(s), "
        f"spread freq {freq}/{len(top)})"
    )
    return EvChoice(evs=evs, source="bank", detail=detail)
