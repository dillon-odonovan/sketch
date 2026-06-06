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
        # Concentration guidance, no concrete spread numbers: naming a "two
        # stats at max" example anchors the singles 252/252 (here 32/32)
        # shape, while telling the model to "fill every stat" makes it
        # sprinkle wasted single-digit EVs. The budget is small, so stress
        # concentrating on the few stats that matter.
        budget_note = (
            f"The six values must sum to exactly {max_t} — a hard, fixed "
            f"budget, not an approximation; spend all of it. {max_t} points "
            f"is a small budget (per-stat cap {max_s}), so concentrate it on "
            f"the two or three stats that matter for the set; a few points "
            f"dropped into a stat are wasted. "
        )
    else:
        budget_note = (
            "Concentrate EVs on the few stats the set actually uses rather "
            "than spreading them thin across stats that don't need them. "
        )
    return (
        "You are a competitive Pokemon team builder assigning EV spreads for "
        f"the VGC / doubles (bring-6, pick-4) format {fmt_name}. You are given "
        "each Pokemon's species, ability, held item, nature, and moves, but "
        "its EVs are unknown. Return a plausible, competitively sensible "
        "spread for each, tuned to its role and moves.\n\n"
        "Spread shape:\n"
        "- Invest purposefully in the FEW stats the set actually uses — "
        "usually two or three — in chunks big enough to matter. Leaving the "
        "other stats at 0 is normal and correct.\n"
        "- Do NOT sprinkle small leftover EVs across stats that don't need "
        "them: a handful of points (single digits) rarely moves a stat enough "
        "to matter, so fold them into a stat that does.\n"
        "- Equally, do NOT mechanically max two stats and ignore the rest; let "
        "the role and moves decide which stats earn investment.\n\n"
        "Which stats:\n"
        "- Always give real investment to the stat the nature boosts. A "
        "speed-boosting nature (Jolly, Timid, Hasty, Naive) wants substantial "
        "Speed — not leftovers — even when the moves point at an attacking "
        "stat (a Timid special attacker still wants Speed, not just Special "
        "Attack). An attack-boosting nature (Adamant, Modest, Brave, Quiet) "
        "wants its attacking stat.\n"
        "- A defensive or support Pokemon — few or no attacking moves, or a "
        "supportive item/ability/movepool (Tailwind, Protect, screens, "
        "redirection) — wants bulk (HP + Def/SpD) over offense, even if its "
        "nature boosts an attacking stat.\n\n"
        f"This format uses '{ev_model.label}'. Each stat takes an integer from "
        f"0 to {max_s} inclusive. "
        f"{budget_note}"
        "If a Pokemon lists 'Known EVs (fixed)', those stats are already "
        "confirmed — keep them at exactly those values and spend the rest of "
        "the budget on the stats that matter most for the set, prioritizing "
        "the nature-boosted stat and the role. "
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
        # Pins are confirmed ground truth — overlay them onto the model's
        # spread so a disobedient value is corrected, not merely shielded from
        # the trim. Overlaying before the trim lets `_trim_to_budget` account
        # for the pinned totals while protecting the pins themselves.
        pins = pins_by_slot.get(slot, {})
        for k, v in pins.items():
            evs[k] = max(0, min(int(v), ev_model.max_per_stat))
        if ev_model.max_total is not None:
            evs = _trim_to_budget(evs, ev_model.max_total, protected=set(pins))
        out[slot] = evs
    return out
