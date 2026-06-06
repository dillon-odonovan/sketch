"""Orchestrate OTS → CTS conversion.

Coordinates `bank`, `ev_matcher`, and `llm_guess` into a finished
`TeamData` with EVs assigned.

Flow per Pokemon:
  1. Complete spread (EVs already sum to the format budget) → keep as-is
     (`"kept"`).
  2. Otherwise fill the missing stats, pinning any non-zero EVs already on
     the paste as known constraints (e.g. an HP total read off the
     broadcast, a speed tier confirmed in-game):
       a. Bank match → copy the chosen spread, biased toward the pins
          (`"bank"`).
       b. No bank match → batch with other unmatched mons for one LLM call
          that pins the known stats (`"estimated"`).

Pins are best-effort: selection/generation is biased toward honoring them,
but a chosen spread is never overwritten. `SlotSource.pinned` records which
pinned stats the final spread actually honors.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass

import anthropic

from sketch import config
from sketch.convert.bank import BankTeam, load_bank_teams
from sketch.convert.ev_matcher import EvChoice, choose_evs
from sketch.convert.ev_model import EvModel, ev_model_for_format
from sketch.convert.llm_guess import guess_ev_spreads
from sketch.convert.normalize import normalize_team
from sketch.storage.sheets_client import SheetsClient
from sketch.team import STAT_KEYS, PokemonEntry, TeamData

logger = logging.getLogger(__name__)

_ZERO_EVS = {k: 0 for k in STAT_KEYS}


@dataclass(frozen=True)
class SlotSource:
    """Where one slot's EV spread came from.

    `label` is the coarse provenance — "kept" (already a complete spread),
    "bank" (lifted from a bank team), or "estimated" (LLM fallback). `url`
    is the Pokepaste URL of the bank team the spread was lifted from, or
    None for estimated/kept slots. `pinned` lists the stat keys that were
    pinned from the paste's partial EVs and that the final spread honors
    (empty when nothing was pinned, e.g. a blank OTS mon or a kept spread).
    """

    label: str
    url: str | None = None
    pinned: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConvertedSlot:
    """One trained Pokemon paired with where its EV spread came from."""

    pokemon: PokemonEntry
    source: SlotSource


@dataclass(frozen=True)
class ConvertResult:
    """The finished CTS conversion: one `ConvertedSlot` per Pokemon."""

    slots: list[ConvertedSlot]
    # Non-Pokemon metadata carried over from the OTS team (e.g. team_id).
    team_id: str | None = None

    @property
    def team(self) -> TeamData:
        """The trained team assembled from the per-slot Pokemon."""
        return TeamData(pokemon=[s.pokemon for s in self.slots], team_id=self.team_id)


async def convert_ots_to_cts(
    ots: TeamData,
    *,
    sheets: SheetsClient,
    sheet_name: str,
    fmt_name: str,
    anthropic_client: anthropic.AsyncAnthropic,
    model: str | None = None,
) -> ConvertResult:
    """Convert an OTS `TeamData` into a CTS by filling in missing EVs.

    Parameters
    ----------
    ots:
        The parsed OTS team. Pokemon whose EVs already sum to the format
        budget are left untouched. Pokemon with no EVs get a full spread;
        Pokemon with a *partial* spread keep their non-zero stats pinned as
        known constraints and have the remaining stats filled in.
    sheets / sheet_name:
        The guild's `SheetsClient` and the sheet tab to mine for
        candidates. `load_bank_teams` is best-effort — a fetch failure
        returns an empty candidate list and all mons fall back to LLM.
    fmt_name:
        The format name (e.g. "Reg M-A"). Determines the `EvModel` (EV
        cap) and is injected into the LLM prompt so the guess is
        format-aware.
    anthropic_client / model:
        The Claude client and model for the LLM fallback. Defaults to
        `config.CONVERT_EV_MODEL`.

    Raises
    ------
    UnsupportedFormatError
        If `fmt_name` has no registered `EvModel`.
    EvGuessError
        If the LLM fallback fails for any unmatched mon and no bank
        spread was available as a safety net.
    """
    # Model name for the Anthropic API call in the LLM-guess fallback.
    llm_model = model or config.CONVERT_EV_MODEL
    ev_model: EvModel = ev_model_for_format(fmt_name)

    # Resolve pre-form species (e.g. `Charizard` + `Charizardite Y` →
    # `Charizard-Mega-Y`) so they align with the bank's resolved forms.
    # Best-effort: a DEX read failure leaves species as-parsed rather than
    # failing the conversion (mirrors `load_bank_teams`).
    try:
        dex = await sheets.get_dex()
        ots = normalize_team(ots, dex)
    except Exception:
        logger.warning("DEX load failed; skipping form normalization", exc_info=True)

    ots_species = {p.species.lower() for p in ots.pokemon}

    bank_teams: list[BankTeam] = await load_bank_teams(
        sheets, sheet_name, ots_species, ev_model
    )

    # First pass: classify each mon, match the fillable ones from the bank,
    # and collect what's left for the LLM. `pins_by_slot` maps a 1-based slot
    # to its pinned stats; it is the single source of truth for pins, read by
    # the bank matcher, the LLM call, and the final source labelling.
    choices: list[EvChoice | None] = []
    unmatched_entries: list[tuple[int, PokemonEntry]] = []
    pins_by_slot: dict[int, dict[str, int]] = {}

    for slot, mon in enumerate(ots.pokemon, start=1):
        # A spread is "complete" once its EVs reach the format budget (or
        # there's no zero stat left to fill). Such mons are real CTS input —
        # preserve them verbatim. Without a known budget (unwired format) any
        # non-zero spread is treated as complete, matching prior behavior.
        nonzero = {k: v for k, v in mon.evs.items() if v != 0}
        total = sum(nonzero.values())
        complete = (
            ev_model.max_total is None
            or total >= ev_model.max_total
            or all(mon.evs.get(k, 0) != 0 for k in STAT_KEYS)
        )
        if nonzero and complete:
            choices.append(
                EvChoice(
                    evs=dict(mon.evs), source="kept", detail="complete spread in input"
                )
            )
            continue

        # Blank mon → no pins; partial mon → pin its non-zero stats.
        pins = nonzero or None
        if pins:
            pins_by_slot[slot] = pins

        choice = choose_evs(mon, bank_teams, ots_species, ev_model, pins=pins)
        if choice is not None:
            logger.info(
                "EV match for %s (slot %d): %s", mon.species, slot, choice.detail
            )
            choices.append(choice)
        else:
            choices.append(None)
            unmatched_entries.append((slot, mon))

    # Second pass: LLM for unmatched mons (one batched call), pinning each
    # slot's known stats so the guess builds around them.
    guessed: dict[int, dict[str, int]] = {}
    if unmatched_entries:
        logger.info(
            "LLM fallback for %d unmatched mon(s): %s",
            len(unmatched_entries),
            [p.species for _, p in unmatched_entries],
        )
        guessed = await guess_ev_spreads(
            anthropic_client,
            unmatched_entries,
            fmt_name=fmt_name,
            ev_model=ev_model,
            model=llm_model,
            pins_by_slot=pins_by_slot,
        )

    # Build the trained team: each slot pairs the EV-filled Pokemon with
    # the provenance of its spread and the pinned stats it honors.
    slots: list[ConvertedSlot] = []

    for idx, (mon, choice) in enumerate(zip(ots.pokemon, choices, strict=False)):
        slot = idx + 1
        pins = pins_by_slot.get(slot)
        if choice is not None:
            evs = choice.evs
            label = choice.source
            url = choice.source_url
        else:
            evs = guessed.get(slot, _ZERO_EVS.copy())
            logger.info("EV guess for %s (slot %d): %s", mon.species, slot, evs)
            label = "estimated"
            url = None
        pinned = (
            tuple(k for k in STAT_KEYS if k in pins and evs.get(k) == pins[k])
            if pins
            else ()
        )
        source = SlotSource(label=label, url=url, pinned=pinned)
        trained_mon = dataclasses.replace(mon, evs=evs)
        slots.append(ConvertedSlot(pokemon=trained_mon, source=source))

    return ConvertResult(slots=slots, team_id=ots.team_id)
