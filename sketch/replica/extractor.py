"""Claude vision extractor for Pokemon Champions Replica share-screen
screenshots.

The Replica share screen has two pages, both covering the same 6 Pokemon in
the same slot order:
  - Page 1: builds — species, held item, ability, 4 moves, tera type.
  - Page 2: training — nature, IVs, and EV / stat-alignment distribution.

This module sends both pages to Claude in a single multimodal call with
tool-use forcing schema conformance, so the model can't return free-text
JSON that needs regex fishing. The cross-page join (build + training per
slot) happens inside the model — we ask for one fully-merged `TeamData` as
the tool input.

Static parts of the request (system prompt + tool schema) are tagged with
`cache_control: ephemeral` so successive calls bill the prefix at 10% rate.
Per-call cost is dominated by the two image tokens and the structured JSON
output; see the plan file for the per-call estimate.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from typing import Any

import anthropic

from sketch import config

logger = logging.getLogger(__name__)


class ExtractionError(Exception):
    """Raised when the vision call cannot produce a usable TeamData.

    The message is intentionally short and user-facing — slash command
    handlers forward it directly into the ephemeral followup. Don't include
    internal diagnostic detail (model name, token counts) here; log those
    separately at WARNING.
    """


# Valid Showdown stat keys. The keys match the Showdown export format exactly
# (lowercased, three-letter, no underscores), so the renderer can emit lines
# like "EVs: 252 HP / 252 Atk / 4 SpD" without an additional translation table.
STAT_KEYS = ("hp", "atk", "def", "spa", "spd", "spe")


@dataclass(frozen=True)
class PokemonEntry:
    species: str
    item: str | None
    ability: str
    tera_type: str
    nature: str
    evs: dict[str, int]
    ivs: dict[str, int] | None
    moves: list[str]
    level: int


@dataclass(frozen=True)
class TeamData:
    pokemon: list[PokemonEntry]


# JSON Schema for the `submit_team` tool. Anthropic's tool-use rejects model
# outputs whose `input` doesn't conform, so each constraint here doubles as a
# validation hop the model has to satisfy before we even see the response.
#
# The `properties` order matters for readability in the API console but is
# not load-bearing — the schema enforces by name, not position.
_STAT_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": list(STAT_KEYS),
    "properties": {
        k: {"type": "integer", "minimum": 0, "maximum": 252} for k in STAT_KEYS
    },
}

_IV_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": list(STAT_KEYS),
    "properties": {
        k: {"type": "integer", "minimum": 0, "maximum": 31} for k in STAT_KEYS
    },
}

_POKEMON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "species",
        "item",
        "ability",
        "tera_type",
        "nature",
        "evs",
        "ivs",
        "moves",
        "level",
    ],
    "properties": {
        "species": {
            "type": "string",
            "description": (
                "Pokemon species in canonical display form, e.g. 'Great Tusk', "
                "'Calyrex-Shadow', 'Urshifu-Rapid-Strike'. Use the form Pokemon "
                "Showdown / PokePaste expect."
            ),
        },
        "item": {
            "type": ["string", "null"],
            "description": (
                "Held item, e.g. 'Assault Vest'. Use null (not the string 'None') "
                "when the Pokemon is holding nothing."
            ),
        },
        "ability": {"type": "string", "description": "e.g. 'Quark Drive'"},
        "tera_type": {
            "type": "string",
            "description": "e.g. 'Water', 'Stellar'. Capitalized type name.",
        },
        "nature": {
            "type": "string",
            "description": "e.g. 'Adamant', 'Modest'. The 25-nature list.",
        },
        "evs": {
            **_STAT_OBJECT_SCHEMA,
            "description": (
                "Effort Values per stat. Total across all six should be <= 508. "
                "Use 0 for stats not invested in."
            ),
        },
        "ivs": {
            "oneOf": [_IV_OBJECT_SCHEMA, {"type": "null"}],
            "description": (
                "Individual Values per stat. Use null when all six are 31 "
                "(the standard case); only return a populated object when one "
                "or more IVs are visibly lower in the share screen."
            ),
        },
        "moves": {
            "type": "array",
            "minItems": 4,
            "maxItems": 4,
            "items": {"type": "string"},
            "description": "Exactly 4 move names, e.g. 'Fake Out', 'Drain Punch'.",
        },
        "level": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "description": "Pokemon's level. VGC defaults to 50.",
        },
    },
}

_TEAM_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["pokemon"],
    "properties": {
        "pokemon": {
            "type": "array",
            "minItems": 6,
            "maxItems": 6,
            "items": _POKEMON_SCHEMA,
            "description": (
                "All 6 Pokemon on the team, in the order they appear on the "
                "share screen (top-to-bottom, left-to-right)."
            ),
        }
    },
}

_SYSTEM_PROMPT = """\
You extract Pokemon teams from screenshots of the Pokemon Champions "Replica" \
share screen. Every call gives you two images of the same team:

  Page 1 (builds): for each of 6 Pokemon, shows species, held item, ability, \
4 moves, and tera type.
  Page 2 (training): for each of the SAME 6 Pokemon in the SAME slot order, \
shows nature, IVs, and EV distribution (often visualized as a hexagonal stat \
chart with numeric EV values).

Cross-join the two pages by slot order: slot 1 on page 1 = slot 1 on page 2. \
Produce one merged entry per Pokemon with all fields combined.

Conventions for the output:
  - Use canonical Showdown / PokePaste names for species, items, abilities, \
moves, natures, and tera types. Examples: "Great Tusk" (not "great-tusk" or \
"GreatTusk"), "Calyrex-Shadow" (hyphenated form), "Urshifu-Rapid-Strike", \
"Iron Valiant". For tera type use the capitalized type name, e.g. "Water", \
"Fairy", "Stellar".
  - Item: use null when the Pokemon is holding nothing, not the string "None".
  - IVs: return null when all six IVs are 31 (the default). Only return a \
populated object when the share screen makes it clear an IV is below 31 \
(typically a 0 Atk IV on special attackers, signaled by the Atk stat being \
visibly lower than the EV investment would otherwise produce, or by an \
explicit numeric readout).
  - EVs: read the numeric value shown for each stat. Total must be <= 508. \
Use 0 for uninvested stats.
  - Moves: exactly 4 names per Pokemon, in the order they're listed.
  - Level: typically 50 for VGC. Read from the screen if shown.

If any field is ambiguous or unreadable, prefer the most common VGC \
convention (e.g. 31/31/31/31/31/31 IVs, level 50) rather than guessing. The \
output must conform to the `submit_team` tool's schema — fields that don't \
fit the schema will be rejected.\
"""

_TOOL_NAME = "submit_team"

_TOOLS: list[dict[str, Any]] = [
    {
        "name": _TOOL_NAME,
        "description": (
            "Submit the extracted 6-Pokemon team. Call this exactly once "
            "per response after reading both share-screen pages."
        ),
        "input_schema": _TEAM_TOOL_SCHEMA,
    }
]

# Image format sniffing. The slash command receives `discord.Attachment`
# objects whose `content_type` MIGHT be present, but Discord doesn't always
# populate it for image uploads. Sniffing from the bytes' magic header is
# more reliable and doesn't add a dependency.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_WEBP_RIFF = b"RIFF"
_WEBP_FORMAT = b"WEBP"
_GIF_MAGIC = b"GIF8"


def _sniff_media_type(data: bytes) -> str:
    if data.startswith(_PNG_MAGIC):
        return "image/png"
    if data.startswith(_JPEG_MAGIC):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == _WEBP_RIFF and data[8:12] == _WEBP_FORMAT:
        return "image/webp"
    if data.startswith(_GIF_MAGIC):
        return "image/gif"
    raise ExtractionError(
        "Unrecognized image format. Upload PNG, JPEG, WebP, or GIF screenshots."
    )


def _image_block(data: bytes) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": _sniff_media_type(data),
            "data": base64.standard_b64encode(data).decode("ascii"),
        },
    }


def _parse_pokemon(raw: dict) -> PokemonEntry:
    """Convert the tool-use input dict for one Pokemon into a PokemonEntry.

    The tool schema already enforces shape; this is a thin marshaller so the
    rest of the codebase deals in our dataclass rather than untyped dicts.
    """
    return PokemonEntry(
        species=str(raw["species"]),
        item=str(raw["item"]) if raw.get("item") else None,
        ability=str(raw["ability"]),
        tera_type=str(raw["tera_type"]),
        nature=str(raw["nature"]),
        evs={k: int(raw["evs"][k]) for k in STAT_KEYS},
        ivs=(
            {k: int(raw["ivs"][k]) for k in STAT_KEYS}
            if raw.get("ivs") is not None
            else None
        ),
        moves=[str(m) for m in raw["moves"]],
        level=int(raw["level"]),
    )


def _parse_tool_input(tool_input: dict) -> TeamData:
    pokemon = [_parse_pokemon(p) for p in tool_input["pokemon"]]
    if len(pokemon) != 6:
        raise ExtractionError(
            f"Expected 6 Pokemon in the extracted team, got {len(pokemon)}."
        )
    return TeamData(pokemon=pokemon)


def _extract_tool_use(message: anthropic.types.Message) -> dict | None:
    """Pull the `submit_team` tool input off the model's response.

    Returns None when the model declined to call the tool (which would be a
    bug given `tool_choice={"type": "tool", ...}`, but we treat it as a
    failure mode rather than crashing).
    """
    for block in message.content:
        if block.type == "tool_use" and block.name == _TOOL_NAME:
            input_val = block.input
            if isinstance(input_val, dict):
                return input_val
            # The SDK should always return dict; if a future shape returns a
            # JSON string, decode defensively.
            if isinstance(input_val, str):
                try:
                    return json.loads(input_val)
                except json.JSONDecodeError:
                    return None
    return None


async def extract_team_from_screenshots(
    client: anthropic.AsyncAnthropic,
    page1: bytes,
    page2: bytes,
    *,
    model: str | None = None,
) -> TeamData:
    """OCR the two Replica share-screen pages into a structured TeamData.

    Raises `ExtractionError` with a user-facing message on any failure path
    (bad image bytes, model refused tool, malformed tool input, transport
    error). The slash-command handler forwards the message verbatim into
    its ephemeral followup.
    """
    effective_model = model or config.REPLICA_OCR_MODEL

    # Both images live in a single user message so the model can correlate
    # build (page 1) and training (page 2) per slot without us scaffolding
    # a multi-turn dialog. The text block at the end is the imperative
    # — the system prompt covers the conventions.
    user_content: list[dict[str, Any]] = [
        _image_block(page1),
        _image_block(page2),
        {
            "type": "text",
            "text": (
                "Extract the team from these two pages of a Pokemon Champions "
                "Replica share screen. Page 1 has builds, Page 2 has training. "
                "Cross-join by slot order and call submit_team with the result."
            ),
        },
    ]

    try:
        message = await client.messages.create(
            model=effective_model,
            max_tokens=4096,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    # The system prompt is identical across calls — caching
                    # it drops the per-call input cost on this prefix by ~90%
                    # once the cache is warm. The tool schema travels in the
                    # `tools` field, which the SDK caches under the same key
                    # when the system block is marked cacheable.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=_TOOLS,
            tool_choice={"type": "tool", "name": _TOOL_NAME},
            messages=[{"role": "user", "content": user_content}],
        )
    except anthropic.APIError as exc:
        logger.warning("Anthropic API call failed during /replica extraction: %s", exc)
        raise ExtractionError(
            "Couldn't read these screenshots right now — please try again in a moment."
        ) from exc

    tool_input = _extract_tool_use(message)
    if tool_input is None:
        logger.warning(
            "Anthropic response had no submit_team tool call: model=%s stop_reason=%s",
            effective_model,
            getattr(message, "stop_reason", "?"),
        )
        raise ExtractionError(
            "Couldn't read these screenshots — please retake them with the full "
            "Replica share screen visible and try again."
        )

    try:
        return _parse_tool_input(tool_input)
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "submit_team tool input failed local validation: %s; payload=%s",
            exc,
            tool_input,
        )
        raise ExtractionError(
            "Couldn't read these screenshots — please retake them with the full "
            "Replica share screen visible and try again."
        ) from exc
