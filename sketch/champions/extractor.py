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

Screenshots may be in any official Pokemon game language (Japanese, Korean,
Chinese, Italian, Spanish, French, German, English, …). The model reads the
localized UI text and translates every name to its canonical English
Showdown / PokePaste form, so all downstream code (nature resolution,
PokePaste rendering, code->URL caching) only ever sees English. Pages are
identified by structure rather than the localized tab labels.

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
from typing import Any

import anthropic

from sketch import config
from sketch.team import STAT_KEYS, PokemonEntry, TeamData

logger = logging.getLogger(__name__)


class ExtractionError(Exception):
    """Raised when the vision call cannot produce a usable TeamData.

    The message is intentionally short and user-facing — slash command
    handlers forward it directly into the ephemeral followup. Don't include
    internal diagnostic detail (model name, token counts) here; log those
    separately at WARNING.
    """


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
                "Page 2 (the nature-boosted stat). The on-screen label may be "
                "localized — return the English enum value by official "
                "translation or by fixed row position. Use null if no arrow is "
                "visible (neutral nature)."
            ),
        },
        "reduced_stat": {
            **_STAT_NAME_OR_NULL,
            "description": (
                "In-game label of the stat marked with a blue down arrow on "
                "Page 2 (the nature-reduced stat). The on-screen label may be "
                "localized — return the English enum value by official "
                "translation or by fixed row position. Use null if no arrow is "
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
                "Showdown / PokePaste expect. The on-screen name may be localized "
                "(any game language); output the canonical English name."
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
                "Held item, e.g. 'Assault Vest'. May be localized; translate to "
                "the official English item name. Use null (not the string 'None') "
                "when the Pokemon is holding nothing — never invent an item."
            ),
        },
        "ability": {
            "type": "string",
            "description": (
                "e.g. 'Quark Drive'. May be localized; output the official "
                "English ability name."
            ),
        },
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
            "description": (
                "1 to 4 move names from Page 1, in display order. May be "
                "localized; output official English move names. Output only the "
                "moves actually shown — never pad to four."
            ),
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
images covering the same team.

Language: the screenshots may be in ANY official Pokemon game language — \
English, Japanese, Korean, Chinese (Simplified or Traditional), Italian, \
Spanish, French, German, and others. Every piece of on-screen text (species, \
abilities, items, moves, stat labels, and the tab labels) will be in that \
language. Read the localized text and output the canonical **English** \
Pokemon Showdown / PokePaste display form for every name, using the official \
localized-to-English Pokemon name correspondences (these are 1:1 and \
well-established per game language). Do not transliterate phonetically and do \
not anglicize loosely — map to the exact official English name. For example, \
the Japanese "こだわりスカーフ" / Korean "구애 스카프" is the official item \
"Choice Scarf", not "Gooey Scarf" or any phonetic gloss.

Translation pitfalls (illustrative failure modes seen across languages — \
the examples are NOT a closed list; apply the principle, not just the named \
cases):
  - Katakana loanwords. Many Japanese move/item names are katakana \
renderings of English words that do NOT match the official English name. \
Output the official name, never the phonetic reading. Example: the move \
"アクアブレイク" reads "Aqua Break" phonetically but its official English \
name is "Liquidation".
  - Word-order and look-alike collisions. A literal word-by-word \
translation can land on a DIFFERENT real move/ability. If your candidate \
English name is itself a valid move/ability, treat that as a red flag and \
re-derive it from the official localized name. Example: the move \
"アームハンマー" is literally "Arm Hammer" — its official name is "Hammer \
Arm" (NOT the unrelated real move "Arm Thrust", whose Japanese is the \
distinct "つっぱり").
  - Do not infer an ability or move from what the Pokemon "usually" runs — \
read the actual localized text. On the Japanese share screen, ability names \
are written in hiragana (not kanji), while moves and items are in katakana — \
so read the ability hiragana carefully, as there is no kanji to disambiguate \
and no katakana loanword to lean on. Example: Frisk ("おみとおし", literally \
"seeing through") and Insomnia ("ふみん", literally "sleeplessness") are \
distinct abilities; output the one whose hiragana is actually printed, even \
if a different ability would be the more common competitive choice for that \
species.
  - For EVERY species, ability, move, and item: resolve it by an exact \
localized-name lookup to its official English name — never settle for a \
literal gloss or phonetic reading. If the English name you land on is itself \
a valid but DIFFERENT move / ability / species, treat that as a red flag \
that you produced a look-alike instead of the official name, and re-derive \
it. The localized→English mapping is 1:1.

The two pages:

  Page 1 ("Moves & More" tab): a 2x3 grid of Pokemon cards. Each card shows \
the species name, a gender icon (when applicable) and the Pokemon's 1-2 \
type icons next to it, the ability, the held item, and 4 moves with a \
type icon next to each move name.
  Page 2 ("Stats" tab): the same 6 Pokemon in the same slot order. Each card \
shows the six base stats (HP, Attack, Defense, Sp. Atk, Sp. Def, Speed) with \
the EV invested in each, and on exactly two of the stats: a red up-arrow on \
the nature-boosted stat and a blue down-arrow on the nature-reduced stat.

Identify each page by its STRUCTURE, not by tab text — the tab labels are \
localized and will not be in English:
  - **Page 1** = the page whose cards each show an ability line, a held-item \
line, and a list of up to 4 moves (each move has a type icon beside it). The \
tab label for this page is localized (English: "Moves & More").
  - **Page 2** = the page whose cards each show a six-row stat readout with a \
small EV number per row, plus a red ↑ arrow on one stat and a blue ↓ arrow on \
another. The tab label is localized (English: "Stats"). The six stat rows \
always appear in the same fixed order — HP, Attack, Defense, Sp. Atk, Sp. \
Def, Speed — even when their labels are written in another language.

When the user provides one image, both pages may be stitched together in \
any orientation (vertical, horizontal, or even reversed) — identify each by \
the structure above rather than by position. Cross-join by slot order: slot 1 \
on Page 1 is the same Pokemon as slot 1 on Page 2, regardless of which side \
each page is on in the image.

Conventions for the output:
  - Species, items, abilities, moves: read the localized on-screen text and \
output the canonical English Showdown / PokePaste display form. Examples: \
"Great Tusk" (not "great-tusk"), "Calyrex-Shadow" (hyphenated forms), \
"Urshifu-Rapid-Strike", "Floettite" for the mega stone. When the screen is \
non-English, translate via the official name mapping for that language rather \
than guessing a similar-looking English name.
  - Gender: read the small icon next to the species name. "M" for the male \
symbol (♂), "F" for the female symbol (♀), null for genderless species or \
when no icon is shown.
  - Item: null when the Pokemon is holding nothing — never the string "None".
  - Nature: the stat next to the red ↑ is the boosted stat, the stat next to \
the blue ↓ is the reduced stat. Return them as {boosted_stat, reduced_stat} \
using exactly the English enum labels "Attack", "Defense", "Sp. Atk", "Sp. \
Def", or "Speed". The on-screen stat labels are localized — map each to its \
English enum value by the official translation, or, if you cannot read it, by \
its fixed row position (row 1 HP, row 2 Attack, row 3 Defense, row 4 Sp. Atk, \
row 5 Sp. Def, row 6 Speed; HP can never carry a nature arrow). If no arrows \
are visible (neutral nature), return null for both. The host code resolves \
these into the canonical nature name — do not attempt to name the nature \
yourself. Read which row carries the red ↑ and which carries the blue ↓ \
carefully: adjacent rows (e.g. Sp. Def vs Speed) are easy to confuse.
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

  The share screen never spells out a Pokemon's form — you infer it from \
the visible signals (type icons next to the species, type icons next to \
each move, the ability text, the held item, the sprite, and the Page 2 \
stat spread). Use the canonical Showdown / PokePaste form names. Reason \
by signal type:

  - Typing change. The species type icon(s) differ from the baseline \
form's typing. Regional variants are the common case — use suffixes \
"-Alola", "-Galar", "-Hisui", or "-Paldea". Example: Typhlosion with a \
Ghost type icon → "Typhlosion-Hisui" (Frisk in the ability slot and \
Shadow Ball in the moveset reinforce, but the type icon alone is enough).

  - Ability the baseline can't have. Some forms add an ability that the \
baseline-form's ability pool does not include — when you see it, the form \
is settled. Example: Slowking with "Curious Medicine" → "Slowking-Galar" \
(baseline Slowking only rolls Oblivious / Own Tempo / Regenerator).

  - Signature move in the moveset. A move name the baseline-form cannot \
learn pins down the alternate form. Examples: Samurott with "Ceaseless \
Edge" → "Samurott-Hisui"; Slowking with "Eerie Spell" → "Slowking-Galar"; \
Slowbro with "Shell Side Arm" → "Slowbro-Galar"; Calyrex with "Astral \
Barrage" → "Calyrex-Shadow", with "Glacial Lance" → "Calyrex-Ice", with \
"As One" and neither signature move → plain "Calyrex"; Urshifu with \
"Surging Strikes" → "Urshifu-Rapid-Strike", with "Wicked Blow" → \
"Urshifu" (the unsuffixed Showdown name for Single-Strike). Mainline \
reference: Sacred Sword → "Keldeo-Resolute" — apply the same reasoning \
if the move appears, even though Champions move tables may diverge from \
mainline.

  - Held item or signature move (legendaries). Some legendaries forme-shift \
via a specific item and also gain a form-exclusive move; either is \
sufficient. Groudon with "Red Orb" or "Precipice Blades" → \
"Groudon-Primal"; Kyogre with "Blue Orb" or "Origin Pulse" → \
"Kyogre-Primal"; Giratina with "Griseous Orb" or "Griseous Core" → \
"Giratina-Origin". Mega Stones follow the same item-triggered pattern: \
holding "Charizardite Y" → "Charizard-Mega-Y", and analogously for any \
other Mega Stone.

  - Sprite or stat-spread only. When no ability, move, item, or type icon \
disambiguates, lean on the sprite (Page 1) and the Page 2 stat readout. \
Deoxys forms (Normal / Attack / Defense / Speed) share the species name \
and Psychic typing but have visibly different sprites and drastically \
different stat distributions; Gourgeist sizes (Small / Average / Large / \
Super) have minor sprite differences and HP / Speed totals that shift \
across sizes.

  These are illustrative, not a closed list — don't refuse to output a \
form just because it isn't in the examples above, and don't force a form \
just because one signal matches when other context (moves, types, item) \
disagrees. When no visible signal disambiguates a form (e.g. battle-only \
forms like Aegislash Blade or Cherrim Sunshine), output the baseline \
species rather than guessing.

Transcription discipline (critical):
  - Transcribe ONLY what is visibly present on the cards. Never invent, \
infer, or "complete" an item, ability, move, or Pokemon from what a \
competitive set "usually" runs.
  - If a Pokemon's item slot is empty, output null — do not add a Berry or \
any other item that is not shown.
  - Output only the moves actually printed on the card, in order. Never add a \
move to reach four.
  - Species: the printed name is the authoritative signal. Read and \
translate it, and trust it over the sprite when they seem to conflict. Do \
NOT substitute a visually similar or better-known look-alike, and do not \
collapse a newer species onto an older one that merely shares its typing or \
silhouette — many competitively-used species are from recent generations \
and their names must be read, not guessed from the sprite.
  - Derive every field independently from what is printed for that Pokemon — \
never back-fill an ability, move, or item from the species you think you \
identified. If a printed ability or move reads as the signature of a \
DIFFERENT species than the one you chose, that is a strong sign you misread \
the species (more likely a newer species you should re-read than a familiar \
one running an off-kit set): re-examine the name before committing.
  - A Pokemon never lists the same move twice. A duplicate in your output \
means you misread one slot — re-read that card.
  - Do not default to the most common item. Defensive items and the \
type-resist Berries share similar icons and localized names; read the \
specific item printed rather than the popular guess.
  - When you cannot read a name confidently, transcribe the localized text \
faithfully and map it to its official English name — a faithful translation \
always beats a confident guess at a different English name.
  - Treat each screenshot independently; do not pattern-match it to a \
well-known sample team.

Champions roster note: the only Floette in Champions is the mega-capable \
form, output as "Floette-Eternal" (never plain "Floette"). All other \
species use the form name implied by the share-screen context.

Before you call submit_team, re-verify each Pokemon against its card: the \
species matches the printed name (not a look-alike); every ability and move \
is the official English name (not a literal gloss or a look-alike); no move \
is duplicated; and the item is the specific one shown. Correct any mismatch \
first.

If a field is genuinely unreadable, prefer null over a guess for nullable \
fields, and zero for EVs that don't show an investment number — never \
substitute a guessed value. The output must conform to the `submit_team` \
tool's schema.\
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
        " These screenshots may be in any language — read the localized text "
        "and output canonical English Showdown names."
    )
    user_content.append({"type": "text", "text": instruction})

    try:
        message = await client.messages.create(
            model=effective_model,
            max_tokens=4096,
            # OCR is a faithful-transcription task, not a creative one. Pin
            # temperature to 0 for the most deterministic, highest-probability
            # reading — reduces run-to-run drift and the model's tendency to
            # "creatively" substitute a familiar look-alike for text it finds
            # hard to read (e.g. newer-species names in non-Latin scripts).
            temperature=0,
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
        logger.warning(
            "Anthropic API call failed during Replica OCR extraction: %s", exc
        )
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
            "Replica share screen visible and try again. Screenshots in any "
            "in-game language are supported."
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
            "Replica share screen visible and try again. Screenshots in any "
            "in-game language are supported."
        ) from exc
