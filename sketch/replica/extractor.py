"""Claude vision extractor for Pokemon Champions Replica share-screen
screenshots.

The Replica share screen has two pages, both covering the same 6 Pokemon in
the same slot order:
  - Page 1 ("Moves & More"): species, gender icon, held item, ability, 4 moves.
  - Page 2 ("Stats"): the six stats with the per-stat EV invested. Two stats
    carry arrow indicators — a red ↑ on the nature-boosted stat and a blue ↓
    on the nature-reduced stat. The nature itself is never spelled out; we
    resolve it deterministically from those two arrow positions via
    `_NATURE_MAP` below.

Pokemon Champions uses a much smaller EV pool than mainline Pokemon — totals
around 66 EVs across all six stats, max 32 per stat — so the schema caps at
32 rather than the familiar 252. Tera type, IVs, and level don't appear on
this share screen and aren't captured.

This module sends both pages to Claude in a single multimodal call with
tool-use forcing schema conformance, so the model can't return free-text
JSON that needs regex fishing. The cross-page join (build + stats per slot)
happens inside the model — we ask for one fully-merged team as the tool
input, then resolve nature from arrows in Python.

Static parts of the request (system prompt + tool schema) are tagged with
`cache_control: ephemeral` so successive calls bill the prefix at 10% rate
(5-minute TTL — savings are meaningful for burst usage, negligible when
calls are spaced further apart than that).

Prompt caching docs:
  https://docs.claude.com/en/docs/build-with-claude/prompt-caching
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


# Showdown stat keys, in canonical export order. Used as the keys of the
# `evs` dict on every PokemonEntry and as the source for renderer-side
# display labels. Internal-only — the in-game labels the model returns
# (e.g. "Sp. Atk") are mapped via `_SHOWDOWN_STAT_KEY` below.
STAT_KEYS = ("hp", "atk", "def", "spa", "spd", "spe")

# The in-game UI labels next to each stat (used in the nature-arrow lookup
# below and in the prompt). Order parallels STAT_KEYS, minus HP — nature
# can boost / reduce any non-HP stat.
_INGAME_STAT_LABELS = ("Attack", "Defense", "Sp. Atk", "Sp. Def", "Speed")

# Map from in-game stat label to Showdown evs-dict key.
_SHOWDOWN_STAT_KEY = {
    "HP": "hp",
    "Attack": "atk",
    "Defense": "def",
    "Sp. Atk": "spa",
    "Sp. Def": "spd",
    "Speed": "spe",
}

# (boosted_stat, reduced_stat) -> canonical nature name. Mirrors the table
# in the user's reference build_pokepaste.py exactly; the deterministic
# 20-entry mapping is the only safe way to translate the share-screen
# arrows since the screen never spells the nature out.
_NATURE_MAP: dict[tuple[str, str], str] = {
    ("Attack", "Defense"): "Lonely",
    ("Attack", "Sp. Atk"): "Adamant",
    ("Attack", "Sp. Def"): "Naughty",
    ("Attack", "Speed"): "Brave",
    ("Defense", "Attack"): "Bold",
    ("Defense", "Sp. Atk"): "Impish",
    ("Defense", "Sp. Def"): "Lax",
    ("Defense", "Speed"): "Relaxed",
    ("Sp. Atk", "Attack"): "Modest",
    ("Sp. Atk", "Defense"): "Mild",
    ("Sp. Atk", "Sp. Def"): "Rash",
    ("Sp. Atk", "Speed"): "Quiet",
    ("Sp. Def", "Attack"): "Calm",
    ("Sp. Def", "Defense"): "Gentle",
    ("Sp. Def", "Sp. Atk"): "Careful",
    ("Sp. Def", "Speed"): "Sassy",
    ("Speed", "Attack"): "Timid",
    ("Speed", "Defense"): "Hasty",
    ("Speed", "Sp. Atk"): "Jolly",
    ("Speed", "Sp. Def"): "Naive",
}

# "Serious" is the canonical Showdown / PokePaste neutral — by convention,
# pastes that didn't specify a nature use Serious, while Hardy implies the
# mon was actually drawn from in-game wild encounters with a Hardy nature.
# Our extractor sees "no arrows" on the share screen, which carries no
# information either way, so the canonical-neutral output is the honest one.
_NEUTRAL_NATURE = "Serious"


def _resolve_nature(boosted: str | None, reduced: str | None) -> str:
    """Translate share-screen arrow indicators into the canonical nature name.

    A neutral nature (no arrows visible, or both arrows on the same stat)
    resolves to "Serious" — the Showdown / PokePaste convention for the
    no-explicit-nature output.
    """
    if boosted is None or reduced is None or boosted == reduced:
        return _NEUTRAL_NATURE
    return _NATURE_MAP.get((boosted, reduced), _NEUTRAL_NATURE)


@dataclass(frozen=True)
class PokemonEntry:
    species: str
    gender: str | None  # "M", "F", or None for genderless / not displayed
    item: str | None
    ability: str
    nature: str  # canonical Showdown name; resolved from arrows in _parse_pokemon
    evs: dict[str, int]  # keys = STAT_KEYS, values 0-32
    moves: list[str]


@dataclass(frozen=True)
class TeamData:
    pokemon: list[PokemonEntry]
    # `team_id` is the 10-char alphanumeric code shown at the top of both
    # share-screen pages. Captured so the command handler can verify that
    # the user-submitted code matches what the screenshots actually show —
    # protects against cache poisoning by mismatched code/screenshot pairs.
    # None when the model couldn't read it (e.g. cropped-out header).
    team_id: str | None = None


# --- JSON schema for the submit_team tool ----------------------------------
#
# Anthropic's tool-use rejects model outputs whose `input` doesn't conform,
# so each constraint here doubles as a validation hop the model has to
# satisfy before we even see the response. Champions' EV pool tops out at
# 32 per stat (66ish total across all six), so we cap there — looser limits
# would let the model hallucinate mainline-Pokemon spreads.

_EVS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": list(STAT_KEYS),
    "properties": {
        k: {"type": "integer", "minimum": 0, "maximum": 32} for k in STAT_KEYS
    },
}

_STAT_NAME_OR_NULL = {
    "oneOf": [
        {"type": "string", "enum": list(_INGAME_STAT_LABELS)},
        {"type": "null"},
    ]
}

_NATURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["boosted_stat", "reduced_stat"],
    "properties": {
        "boosted_stat": {
            **_STAT_NAME_OR_NULL,
            "description": (
                "In-game label of the stat marked with a red up arrow on "
                "Page 2 (the nature-boosted stat). Use null if no arrow is "
                "visible (neutral nature)."
            ),
        },
        "reduced_stat": {
            **_STAT_NAME_OR_NULL,
            "description": (
                "In-game label of the stat marked with a blue down arrow on "
                "Page 2 (the nature-reduced stat). Use null if no arrow is "
                "visible (neutral nature)."
            ),
        },
    },
}

_POKEMON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "species",
        "gender",
        "item",
        "ability",
        "nature",
        "evs",
        "moves",
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
        "gender": {
            "oneOf": [
                {"type": "string", "enum": ["M", "F"]},
                {"type": "null"},
            ],
            "description": (
                "Gender icon next to the species name on Page 1: 'M' for the "
                "male symbol, 'F' for the female symbol, null for genderless "
                "species (Magnemite, Klefki, etc.) or when no icon is shown."
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
        "nature": _NATURE_SCHEMA,
        "evs": {
            **_EVS_SCHEMA,
            "description": (
                "Effort Values per stat as shown on Page 2. Pokemon Champions "
                "caps EVs at 32 per stat and ~66 total across all six. Use 0 "
                "for uninvested stats. Keys are Showdown short forms: "
                "hp / atk / def / spa / spd / spe."
            ),
        },
        "moves": {
            "type": "array",
            "minItems": 1,
            "maxItems": 4,
            "items": {"type": "string"},
            "description": "1 to 4 move names from Page 1, in display order.",
        },
    },
}

_TEAM_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["pokemon"],
    "properties": {
        "team_id": {
            "type": ["string", "null"],
            "description": (
                "The 10-character Team ID shown at the top of both share-screen "
                "pages (e.g. 'QBXXWXL05U'). Use null if the header is cropped "
                "or unreadable."
            ),
        },
        "pokemon": {
            "type": "array",
            "minItems": 6,
            "maxItems": 6,
            "items": _POKEMON_SCHEMA,
            "description": (
                "All 6 Pokemon on the team, in the order they appear on the "
                "share screen (top-to-bottom in each column, left column first)."
            ),
        },
    },
}

_SYSTEM_PROMPT = """\
You extract Pokemon teams from screenshots of the Pokemon Champions \
"Replicate This Battle Team?" share screen. Each call gives you one or two \
images covering the same team:

  Page 1 ("Moves & More" tab): a 2x3 grid of Pokemon cards. Each card shows \
the species name with one or two icons next to it (gender, possibly other \
status), the ability, the held item, and 4 moves.
  Page 2 ("Stats" tab): the same 6 Pokemon in the same slot order. Each card \
shows the six base stats (HP, Attack, Defense, Sp. Atk, Sp. Def, Speed) with \
the EV invested in each, and on exactly two of the stats: a red up-arrow on \
the nature-boosted stat and a blue down-arrow on the nature-reduced stat.

When the user provides one image, both pages may be stitched together in \
any orientation (vertical, horizontal, or even reversed). Identify each \
page by its content rather than by position:
  - The page showing tab marker "Moves & More" — or four bulleted move \
names per Pokemon, with an ability and item listed — is **Page 1**.
  - The page showing tab marker "Stats" — or a six-row stat readout (HP, \
Attack, Defense, Sp. Atk, Sp. Def, Speed) with red ↑ / blue ↓ arrows on \
two of them — is **Page 2**.
Cross-join by slot order: slot 1 on Page 1 is the same Pokemon as slot 1 \
on Page 2, regardless of which side each page is on in the image.

Conventions for the output:
  - Species, items, abilities, moves: use the canonical Showdown / PokePaste \
display form. Examples: "Great Tusk" (not "great-tusk"), "Calyrex-Shadow" \
(hyphenated forms), "Urshifu-Rapid-Strike", "Floettite" for the mega stone.
  - Gender: read the small icon next to the species name. "M" for the male \
symbol (♂), "F" for the female symbol (♀), null for genderless species or \
when no icon is shown.
  - Item: null when the Pokemon is holding nothing — never the string "None".
  - Nature: return the in-game stat label next to each arrow as \
{boosted_stat, reduced_stat}. Use exactly the labels "Attack", "Defense", \
"Sp. Atk", "Sp. Def", or "Speed". If no arrows are visible (neutral nature), \
return null for both. The host code resolves these into the canonical nature \
name — do not attempt to name the nature yourself.
  - EVs: read the small numeric value shown next to each stat on Page 2. \
Pokemon Champions caps EVs at 32 per stat with ~66 total — much smaller than \
mainline Pokemon's 252/508 system. Use 0 for stats with no investment. The \
output keys are Showdown short forms (hp, atk, def, spa, spd, spe).
  - Moves: 1 to 4 per Pokemon, in the order shown on Page 1. Most \
Pokemon have 4 moves, but rare sets run fewer (e.g. Kangaskhan with just \
Fake Out + Last Resort). Output only the moves actually shown on the \
card; do not pad to 4.
  - team_id: the alphanumeric code shown next to "Team ID:" at the top of \
both pages, e.g. "QBXXWXL05U". Null if cropped out.

Form resolution:

  Use your general knowledge of Pokemon and the canonical Showdown / \
PokePaste form names. The full visible context — ability, moves, held \
item, Pokemon types if shown — usually disambiguates which form is on the \
field. Some examples (not exhaustive — apply the same kind of reasoning to \
any species with multiple competitive formes):

  - Calyrex with "As One" + "Astral Barrage" or Psychic/Ghost typing → \
"Calyrex-Shadow"; with "As One" + "Glacial Lance" or Psychic/Ice typing → \
"Calyrex-Ice"; with "Unnerve" and neither signature move → plain "Calyrex".
  - Urshifu with "Surging Strikes" or Water typing → "Urshifu-Rapid-Strike"; \
with "Wicked Blow" or Dark typing → "Urshifu" (the unsuffixed Showdown name \
for Single-Strike).
  - Groudon holding "Red Orb" → "Groudon-Primal"; Kyogre holding "Blue Orb" \
→ "Kyogre-Primal"; Giratina holding "Griseous Orb" or "Griseous Core" → \
"Giratina-Origin".

  These are illustrative, not a closed list — don't refuse to output a form \
just because it isn't in the examples above, and don't force a form just \
because the ability matches when other context (moves, types, item) \
disagrees.

Champions roster note: the only Floette in Champions is the mega-capable \
form, output as "Floette-Eternal" (never plain "Floette"). All other \
species use the form name implied by the share-screen context.

If a field is genuinely unreadable, prefer null over a guess for nullable \
fields, and zero for EVs that don't show an investment number. The output \
must conform to the `submit_team` tool's schema.\
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

    The tool schema enforces shape; this is a thin marshaller plus the
    nature-arrow → canonical-name lookup, which is deterministic and lives
    in Python rather than the model because the model can't see the
    mapping table at inference time.
    """
    nature_in = raw.get("nature") or {}
    nature_name = _resolve_nature(
        nature_in.get("boosted_stat"),
        nature_in.get("reduced_stat"),
    )
    gender_in = raw.get("gender")
    gender = gender_in if gender_in in ("M", "F") else None
    return PokemonEntry(
        species=str(raw["species"]),
        gender=gender,
        item=str(raw["item"]) if raw.get("item") else None,
        ability=str(raw["ability"]),
        nature=nature_name,
        evs={k: int(raw["evs"][k]) for k in STAT_KEYS},
        moves=[str(m) for m in raw["moves"]],
    )


def _parse_tool_input(tool_input: dict) -> TeamData:
    pokemon = [_parse_pokemon(p) for p in tool_input["pokemon"]]
    if len(pokemon) != 6:
        raise ExtractionError(
            f"Expected 6 Pokemon in the extracted team, got {len(pokemon)}."
        )
    team_id_raw = tool_input.get("team_id")
    team_id = str(team_id_raw).upper() if isinstance(team_id_raw, str) else None
    return TeamData(pokemon=pokemon, team_id=team_id)


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
    page2: bytes | None = None,
    *,
    model: str | None = None,
) -> TeamData:
    """OCR the Replica share-screen page(s) into a structured TeamData.

    Accepts either two separate page screenshots (Discord users typically
    attach Page 1 and Page 2 individually) or a single stitched image
    containing both pages (some users screenshot both in one frame). When
    `page2` is None, only `page1` is sent and the prompt expects a
    stitched image.

    Raises `ExtractionError` with a user-facing message on any failure path
    (bad image bytes, model refused tool, malformed tool input, transport
    error). The slash-command handler forwards the message verbatim into
    its ephemeral followup.
    """
    effective_model = model or config.REPLICA_OCR_MODEL

    user_content: list[dict[str, Any]] = [_image_block(page1)]
    if page2 is not None:
        user_content.append(_image_block(page2))

    instruction = (
        "Extract the team from this Pokemon Champions Replica share screen. "
        + (
            "Both pages are included as two images — Page 1 is builds, Page 2 is stats."
            if page2 is not None
            else "Both pages are stitched into this one image — top is Page 1 "
            "(builds), bottom is Page 2 (stats)."
        )
        + " Cross-join by slot order, resolve the nature arrows into "
        "boosted_stat / reduced_stat, and call submit_team with the result."
    )
    user_content.append({"type": "text", "text": instruction})

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
