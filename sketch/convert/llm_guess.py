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
from sketch.team import STAT_KEYS, PokemonEntry

logger = logging.getLogger(__name__)

_TOOL_NAME = "submit_spreads"


class EvGuessError(Exception):
    """Raised when the LLM fallback couldn't produce spreads.

    Message is user-facing; the command handler forwards it verbatim.
    """


def _system_prompt(fmt_name: str, ev_model: EvModel) -> str:
    return (
        "You are a competitive Pokemon team builder assigning EV spreads for "
        f"the VGC / doubles (bring-6, pick-4) format {fmt_name}. You are given "
        "each Pokemon's species, ability, held item, nature, and moves, but "
        "its EVs are unknown. Return a plausible, competitively sensible EV "
        "spread for each Pokemon, consistent with its nature (invest in the "
        "stats the nature and moves imply — e.g. a physical attacker with a "
        "+Spe nature wants Attack and Speed).\n\n"
        f"This format uses '{ev_model.label}': EVs are capped at "
        f"{ev_model.max_per_stat} per stat and spreads are sparse (only a few "
        "stats invested, not every stat maxed). Call submit_spreads with one "
        "entry per Pokemon, keyed by the slot number you were given."
    )


_TOOLS: list[dict[str, Any]] = [
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
                                "properties": {
                                    k: {"type": "integer"} for k in STAT_KEYS
                                },
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


def _describe(slot: int, p: PokemonEntry) -> str:
    item = p.item or "(no item)"
    moves = ", ".join(p.moves) if p.moves else "(no moves)"
    return (
        f"Slot {slot}: {p.species} @ {item} | Ability: {p.ability} | "
        f"Nature: {p.nature} | Moves: {moves}"
    )


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
) -> dict[int, dict[str, int]]:
    """Guess EV spreads for `entries` (a list of `(slot, PokemonEntry)`).

    Returns a `{slot: evs}` map. Slots present in `entries` but missing
    from the model's response are omitted (the caller decides the
    fallback — currently an all-zero spread). Raises `EvGuessError` on
    API failure or a missing/malformed tool call.
    """
    if not entries:
        return {}

    listing = "\n".join(_describe(slot, p) for slot, p in entries)
    instruction = (
        "Assign an EV spread for each of the following Pokemon and call "
        f"submit_spreads with one entry per slot:\n\n{listing}"
    )

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
            tools=_TOOLS,
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

    return _parse_spreads(tool_input, ev_model)


def _parse_spreads(
    tool_input: dict[str, Any], ev_model: EvModel
) -> dict[int, dict[str, int]]:
    """Pull ``{slot: evs}`` out of the tool input, clamping defensively.

    Unlike bank spreads (which come from real teams the game already
    constrains), LLM output can exceed the format's total budget — e.g.
    the model might invest every stat at 32. Per-stat clamping runs first;
    if the result still exceeds ``ev_model.max_total``, values are scaled
    down proportionally so the returned spread is always a legal Champions
    (or legacy) spread.
    """
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
            total = sum(evs.values())
            if total > ev_model.max_total:
                ratio = ev_model.max_total / total
                evs = {k: int(v * ratio) for k, v in evs.items()}
        out[slot] = evs
    return out
