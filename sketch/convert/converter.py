"""Orchestrate OTS → CTS conversion.

Coordinates `bank`, `ev_matcher`, and `llm_guess` into a finished
`TeamData` with EVs assigned.

Flow per Pokemon:
  1. Already trained (any non-zero EV) → keep as-is (`"kept"`).
  2. Bank match → copy the chosen spread (`"bank"`).
  3. No bank match → batch with other unmatched mons for one LLM call
     (`"estimated"`).

# TODO(issue #52): accept per-mon known-stat hints (e.g. confirmed HP
# from broadcast/spectator mode, speed tier from in-game interactions)
# to pin or bias the chosen spread rather than treating every untracked
# stat as fully unknown.
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
from sketch.storage.sheets_client import SheetsClient
from sketch.team import STAT_KEYS, PokemonEntry, TeamData

logger = logging.getLogger(__name__)

_ZERO_EVS = {k: 0 for k in STAT_KEYS}


@dataclass(frozen=True)
class SlotSource:
    """Where one slot's EV spread came from.

    `label` is the coarse provenance — "kept" (already trained), "bank"
    (lifted from a bank team), or "estimated" (LLM fallback). `url` is the
    Pokepaste URL of the bank team the spread was lifted from, or None for
    estimated/kept slots.
    """

    label: str
    url: str | None = None


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
        The parsed OTS team. Pokemon with all-zero EVs are treated as
        needing spreads; Pokemon already carrying non-zero EVs (i.e. the
        caller passed a CTS) are left untouched.
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

    ots_species = {p.species.lower() for p in ots.pokemon}

    bank_teams: list[BankTeam] = await load_bank_teams(
        sheets, sheet_name, ots_species, ev_model
    )

    # First pass: match from the bank; collect unmatched slots for LLM.
    choices: list[EvChoice | None] = []
    unmatched_entries: list[tuple[int, object]] = []  # (1-based slot, PokemonEntry)

    for slot, mon in enumerate(ots.pokemon, start=1):
        # Already trained: preserve the existing spread.
        if any(v != 0 for v in mon.evs.values()):
            choices.append(
                EvChoice(
                    evs=dict(mon.evs), source="kept", detail="non-zero EVs in input"
                )
            )
            continue

        choice = choose_evs(mon, bank_teams, ots_species, ev_model)
        if choice is not None:
            logger.info(
                "EV match for %s (slot %d): %s", mon.species, slot, choice.detail
            )
            choices.append(choice)
        else:
            choices.append(None)
            unmatched_entries.append((slot, mon))

    # Second pass: LLM for unmatched mons (one batched call).
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
        )

    # Build the trained team: each slot pairs the EV-filled Pokemon with
    # the provenance of its spread.
    slots: list[ConvertedSlot] = []

    for slot, (mon, choice) in enumerate(
        zip(ots.pokemon, choices, strict=False), start=1
    ):
        if choice is not None:
            source = SlotSource(label=choice.source, url=choice.source_url)
            trained_mon = dataclasses.replace(mon, evs=choice.evs)
        else:
            evs = guessed.get(slot, _ZERO_EVS.copy())
            logger.info("EV guess for %s (slot %d): %s", mon.species, slot, evs)
            source = SlotSource(label="estimated")
            trained_mon = dataclasses.replace(mon, evs=evs)
        slots.append(ConvertedSlot(pokemon=trained_mon, source=source))

    return ConvertResult(slots=slots, team_id=ots.team_id)
