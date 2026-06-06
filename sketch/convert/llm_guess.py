"""LLM fallback: guess EV spreads for Pokemon with no bank match.

When the team bank has no set for an OTS Pokemon, we ask Claude to assign
a plausible competitive spread. Mirrors the tool-use pattern in
`sketch.champions.extractor`: a cached system prompt, a single forced
tool call (`submit_spreads`) so the model can't drift into free-text
JSON, and defensive local validation + clamping of whatever comes back.

All of a conversion's unmatched mons go in one call (keyed by slot) to
keep it to a single round-trip.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic

from sketch.convert.ev_model import EvModel
from sketch.team import STAT_DISPLAY, STAT_KEYS, PokemonEntry

logger = logging.getLogger(__name__)

_TOOL_NAME = "submit_spreads"


class EvGuessError(Exception):
    """Raised when the LLM fallback couldn't produce spreads.

    Message is user-facing; the command handler forwards it verbatim.
    """


def _system_prompt(fmt_name: str, ev_model: EvModel) -> str:
    max_s = ev_model.max_per_stat
    max_t = ev_model.max_total
    if max_t is not None:
        # Remainder after maxing two stats — the third stat in the
        # canonical default spread (e.g. 2/32/32 = 66 for Champions). The
        # per-stat cap plus the exact-total budget already force a sparse
        # spread, so we don't tell the model how many stats to invest in.
        third = max_t - 2 * max_s
        budget_note = (
            f"The six values must sum to exactly {max_t} — a hard, fixed "
            f"budget, not an approximation; spend all of it. Distribute it the "
            f"way a strong VGC player would for this Pokemon's role and moves; "
            f"when nothing more specific is implied, two stats at {max_s} plus "
            f"the remaining {third} on a third (e.g. {third} HP / {max_s} Atk / "
            f"{max_s} Spe = {max_t}) is a sensible default. "
        )
    else:
        budget_note = (
            "Distribute EVs the way a strong VGC player would for this "
            "Pokemon's role and moves. "
        )
    return (
        "You are a competitive Pokemon team builder assigning EV spreads for "
        f"the VGC / doubles (bring-6, pick-4) format {fmt_name}. You are given "
        "each Pokemon's species, ability, held item, nature, and moves, but "
        "its EVs are unknown. Return a plausible, competitively sensible EV "
        "spread for each Pokemon, consistent with its nature (invest in the "
        "stats the nature and moves imply — e.g. a physical attacker with a "
        f"+Spe nature wants Attack and Speed).\n\n"
        f"This format uses '{ev_model.label}'. Each stat takes an integer from "
        f"0 to {max_s} inclusive. "
        f"{budget_note}"
        "If a Pokemon lists 'Known EVs (fixed)', those stats are already "
        "confirmed — keep them at exactly those values and spend the rest of "
        "the budget on the remaining stats. "
        "Call submit_spreads with one entry per Pokemon slot."
    )


def _make_tools(ev_model: EvModel) -> list[dict[str, Any]]:
    """Build the submit_spreads tool schema with explicit per-stat bounds.

    Encoding minimum=0/maximum=max_per_stat directly in the JSON Schema
    anchors the model's value range so it doesn't guess that the bound is
    exclusive (e.g. treat 32 as off-limits when the cap is 32).
    """
    stat_schema = {
        k: {
            "type": "integer",
            "minimum": 0,
            "maximum": ev_model.max_per_stat,
        }
        for k in STAT_KEYS
    }
    return [
        {
            "name": _TOOL_NAME,
            "description": "Submit the assigned EV spreads, one per Pokemon slot.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "spreads": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "slot": {"type": "integer"},
                                "evs": {
                                    "type": "object",
                                    "properties": stat_schema,
                                    "required": list(STAT_KEYS),
                                },
                            },
                            "required": ["slot", "evs"],
                        },
                    }
                },
                "required": ["spreads"],
            },
        }
    ]


def _describe(slot: int, p: PokemonEntry, pins: dict[str, int] | None = None) -> str:
    item = p.item or "(no item)"
    moves = ", ".join(p.moves) if p.moves else "(no moves)"
    line = (
        f"Slot {slot}: {p.species} @ {item} | Ability: {p.ability} | "
        f"Nature: {p.nature} | Moves: {moves}"
    )
    if pins:
        known = ", ".join(
            f"{STAT_DISPLAY[k]}={pins[k]}" for k in STAT_KEYS if k in pins
        )
        line += f" | Known EVs (fixed, keep exactly): {known}"
    return line


def _extract_tool_input(message: anthropic.types.Message) -> dict[str, Any] | None:
    for block in message.content:
        if block.type == "tool_use" and block.name == _TOOL_NAME:
            if isinstance(block.input, dict):
                return block.input
            if isinstance(block.input, str):
                try:
                    return json.loads(block.input)
                except json.JSONDecodeError:
                    return None
    return None


async def guess_ev_spreads(
    client: anthropic.AsyncAnthropic,
    entries: list[tuple[int, PokemonEntry]],
    *,
    fmt_name: str,
    ev_model: EvModel,
    model: str,
    pins_by_slot: dict[int, dict[str, int]] | None = None,
) -> dict[int, dict[str, int]]:
    """Guess EV spreads for `entries` (a list of `(slot, PokemonEntry)`).

    Returns a `{slot: evs}` map. Slots present in `entries` but missing
    from the model's response are omitted (the caller decides the
    fallback — currently an all-zero spread). Raises `EvGuessError` on
    API failure or a missing/malformed tool call.

    `pins_by_slot` maps a slot to its known (fixed) stats — the non-zero
    EVs already on the OTS paste. Those values are pinned in the prompt so
    the guess builds around them, and shielded from the over-budget trim.
    """
    if not entries:
        return {}

    pins_by_slot = pins_by_slot or {}
    listing = "\n".join(
        _describe(slot, p, pins_by_slot.get(slot)) for slot, p in entries
    )
    instruction = (
        "Assign an EV spread for each of the following Pokemon and call "
        f"submit_spreads with one entry per slot:\n\n{listing}"
    )

    tools = _make_tools(ev_model)

    try:
        message = await client.messages.create(
            model=model,
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": _system_prompt(fmt_name, ev_model),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=tools,
            tool_choice={"type": "tool", "name": _TOOL_NAME},
            messages=[{"role": "user", "content": instruction}],
        )
    except anthropic.APIError as exc:
        logger.warning("Anthropic API call failed during EV guess: %s", exc)
        raise EvGuessError(
            "Couldn't estimate EV spreads right now — please try again in a moment."
        ) from exc

    tool_input = _extract_tool_input(message)
    if tool_input is None:
        logger.warning(
            "EV-guess response had no %s tool call: model=%s stop_reason=%s",
            _TOOL_NAME,
            model,
            getattr(message, "stop_reason", "?"),
        )
        raise EvGuessError(
            "Couldn't estimate EV spreads right now — please try again in a moment."
        )

    return _parse_spreads(tool_input, ev_model, pins_by_slot)


def _trim_to_budget(
    evs: dict[str, int], max_total: int, protected: set[str] | None = None
) -> dict[str, int]:
    """Reduce EVs until total <= max_total by trimming smaller stats first.

    Trimming the smallest investments first preserves the larger ones,
    which are more likely to be intentional (e.g. a 32 Spe investment
    matters more than a 4 HP investment). This avoids the rounding error
    of proportional scaling, which can produce totals below the budget
    (e.g. int(32 * 66/67) = 31, leaving 1 point on the table).

    `protected` stats are never trimmed: they are the slot's known (fixed)
    EVs, so cutting them would silently violate a confirmed value. The
    excess is taken from the unprotected stats instead.
    """
    protected = protected or set()
    result = dict(evs)
    excess = sum(result.values()) - max_total
    if excess <= 0:
        return result
    # Sort ascending so we trim the smallest stats first; skip protected ones.
    for key in sorted(result, key=lambda k: result[k]):
        if excess <= 0:
            break
        if key in protected:
            continue
        cut = min(result[key], excess)
        result[key] -= cut
        excess -= cut
    return result


def _parse_spreads(
    tool_input: dict[str, Any],
    ev_model: EvModel,
    pins_by_slot: dict[int, dict[str, int]] | None = None,
) -> dict[int, dict[str, int]]:
    """Pull ``{slot: evs}`` out of the tool input, clamping defensively.

    Unlike bank spreads (which come from real teams the game already
    constrains), LLM output can exceed the format's total budget — e.g.
    the model might invest every stat at 32. Per-stat clamping runs first;
    if the result still exceeds ``ev_model.max_total``, the excess is
    trimmed from the smallest stats first (see `_trim_to_budget`), leaving
    the slot's pinned stats untouched.
    """
    pins_by_slot = pins_by_slot or {}
    out: dict[int, dict[str, int]] = {}
    for item in tool_input.get("spreads", []) or []:
        if not isinstance(item, dict):
            continue
        try:
            slot = int(item["slot"])
        except (KeyError, TypeError, ValueError):
            continue
        raw = item.get("evs") or {}
        evs: dict[str, int] = {
            k: max(0, min(int(raw.get(k, 0) or 0), ev_model.max_per_stat))
            if isinstance(raw, dict)
            else 0
            for k in STAT_KEYS
        }
        if ev_model.max_total is not None:
            evs = _trim_to_budget(
                evs, ev_model.max_total, protected=set(pins_by_slot.get(slot, {}))
            )
        out[slot] = evs
    return out
