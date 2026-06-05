"""Pick the best bank EV spread for one OTS Pokemon.

Pure, side-effect-free scoring over the candidate `BankTeam`s loaded by
`sketch.convert.bank`. Given one target OTS Pokemon, gather every bank
Pokemon of the same species and select the best EV spread in two stages:

Stage 1 — nature gate:
  Prefer candidates whose nature matches the OTS mon's nature (same stat
  alignment). If any same-nature candidates exist, restrict the pool to
  them. Only fall through to mismatched-nature candidates when the bank
  has no same-nature entry — so a high-quality ability+item+moves match
  isn't thrown away just because the nature differs.

Stage 2 — ranking (within the selected pool):
  Rank by a single lexicographic key so the comparator lives in one place:
    1. ability match                (highest-precedence set signal)
    2. item match
    3. move-overlap count           (desc)
    4. team-composition overlap     (desc — more of the OTS's six mons on
                                     the source team ⇒ more representative)

Frequency tiebreak:
  Among the top-ranked candidates (same key value), the *most common full
  spread* wins — so a popular archetype spread rises above one-off
  outliers rather than picking the first encountered. "Full spread" means
  the complete 6-tuple (hp, atk, def, spa, spd, spe), clamped to the
  format's per-stat cap; this de-duplicates spreads that happen to look
  identical after clamping.

Zero-EV filtering:
  Candidates whose EVs are all zero are excluded before ranking. OTS
  pastes stored in the bank have zero EVs and provide no useful spread
  information — returning them as a "bank match" would produce a team
  that looks trained in the source summary but has no EVs written to the
  paste. Mons whose only bank entries have zero EVs fall through to the
  LLM fallback instead.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from sketch.convert.bank import BankTeam, _norm_species
from sketch.convert.ev_model import EvModel
from sketch.team import STAT_KEYS, PokemonEntry


@dataclass(frozen=True)
class EvChoice:
    """A chosen EV spread plus where it came from.

    `source` is the coarse provenance for the user-facing summary
    (`"bank"` here; the converter uses `"estimated"` for LLM guesses and
    `"kept"` for already-trained mons). `source_url` is the Pokepaste URL
    of the bank team the spread was lifted from (None for non-bank
    sources). `detail` is a short log-only note on how strong the match
    was.
    """

    evs: dict[str, int]
    source: str
    detail: str
    source_url: str | None = None


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def _move_set(moves: list[str]) -> frozenset[str]:
    return frozenset(_norm(m) for m in moves)


@dataclass(frozen=True)
class _Candidate:
    entry: PokemonEntry
    overlap: int  # how many OTS species appear on this candidate's team
    url: str  # Pokepaste URL of the bank team this entry came from


def _score(target: PokemonEntry, cand: _Candidate) -> tuple[int, int, int, int]:
    """Lexicographic ranking key (higher is better) for one candidate.

    Nature is not part of this key — it is handled as a pool gate before
    scoring runs, so all candidates in the pool have already cleared the
    nature filter (or nature matching is being skipped as a fallback).
    """
    target_moves = _move_set(target.moves)
    return (
        int(_norm(cand.entry.ability) == _norm(target.ability)),
        int(_norm(cand.entry.item) == _norm(target.item)),
        len(target_moves & _move_set(cand.entry.moves)),
        cand.overlap,
    )


def _clamp(evs: dict[str, int], ev_model: EvModel) -> dict[str, int]:
    """Clamp each EV value to the format's per-stat cap.

    No total-budget enforcement is applied here: bank spreads come from
    real teams stored by the game, which already enforces the aggregate
    cap, so over-budget totals can't reach the database. The full 6-stat
    tuple is returned so the frequency counter in `choose_evs` can compare
    complete spreads rather than individual stats.
    """
    return {
        k: max(0, min(int(evs.get(k, 0)), ev_model.max_per_stat)) for k in STAT_KEYS
    }


def choose_evs(
    target: PokemonEntry,
    bank_teams: list[BankTeam],
    ots_species: set[str],
    ev_model: EvModel,
) -> EvChoice | None:
    """Return the best bank spread for `target`, or None if no bank match.

    `ots_species` is the normalized set of the OTS's species (used to
    compute each candidate team's composition overlap). The returned
    spread is clamped to `ev_model.max_per_stat` per stat.

    Returns None when:
    - No bank team contains a Pokemon of the same species, OR
    - All same-species bank entries have zero EVs (stored as OTS pastes,
      not useful for CTS conversion — fall through to the LLM instead).
    """
    target_species = _norm_species(target.species)
    all_candidates: list[_Candidate] = []
    for bt in bank_teams:
        overlap = len(ots_species & {_norm_species(p.species) for p in bt.team.pokemon})
        for entry in bt.team.pokemon:
            if _norm_species(entry.species) == target_species:
                all_candidates.append(
                    _Candidate(entry=entry, overlap=overlap, url=bt.url)
                )
                break  # each team has at most one of a given species; skip the rest

    if not all_candidates:
        return None

    # Exclude candidates with all-zero EVs. These are OTS pastes stored
    # in the bank — they carry no spread information and returning them as
    # a "bank match" would produce a paste that looks trained but has no
    # EVs written.
    trained_candidates = [
        c for c in all_candidates if any(v != 0 for v in c.entry.evs.values())
    ]
    if not trained_candidates:
        return None

    # Stage 1: gate by nature. Prefer same-nature candidates; fall back to
    # all candidates if none exist with the matching nature.
    same_nature = [
        c for c in trained_candidates if _norm(c.entry.nature) == _norm(target.nature)
    ]
    pool = same_nature if same_nature else trained_candidates
    nature_gated = bool(same_nature)

    best_key = max(_score(target, c) for c in pool)
    top = [c for c in pool if _score(target, c) == best_key]

    # Frequency tiebreak: count the most common *full spread* (6-tuple) among
    # the top candidates. Picks the consensus spread rather than a one-off;
    # `Counter.most_common` is stable on ties so the first-seen spread wins.
    spread_counts = Counter(
        tuple(_clamp(c.entry.evs, ev_model)[k] for k in STAT_KEYS) for c in top
    )
    winning_tuple, freq = spread_counts.most_common(1)[0]
    evs = dict(zip(STAT_KEYS, winning_tuple, strict=False))

    # Source URL: find one candidate whose clamped spread matches the winner.
    source_url = next(
        (
            c.url
            for c in top
            if tuple(_clamp(c.entry.evs, ev_model)[k] for k in STAT_KEYS)
            == winning_tuple
        ),
        None,
    )

    ability_ok, item_ok, move_overlap, overlap = best_key
    detail = (
        f"nature_gated={nature_gated} ability={'y' if ability_ok else 'n'} "
        f"item={'y' if item_ok else 'n'} moves={move_overlap} "
        f"composition={overlap} (from {len(trained_candidates)} trained "
        f"candidate(s), pool={len(pool)}, spread freq {freq}/{len(top)}, "
        f"url={source_url})"
    )
    return EvChoice(evs=evs, source="bank", detail=detail, source_url=source_url)
