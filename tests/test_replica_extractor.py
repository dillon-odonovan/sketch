"""Tests for the Claude vision extractor.

The Anthropic SDK is mocked end-to-end (no network) — we substitute a fake
`AsyncAnthropic` whose `messages.create` returns canned tool-use payloads.
What we're actually verifying:
  - the per-Pokemon parser turns a well-formed tool input into a typed
    `TeamData` with all six entries populated;
  - the nature-arrow lookup turns (boosted, reduced) labels into the
    canonical Showdown nature name;
  - schema-violating outputs surface as `ExtractionError` rather than
    leaking dict-shape exceptions into the slash command;
  - the image-format sniffer accepts the formats Discord users actually
    upload and rejects everything else;
  - single-image (stitched) and two-image submissions both work.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anthropic
import pytest

from sketch.champions.extractor import (
    _SYSTEM_PROMPT,
    ExtractionError,
    _resolve_nature,
    _sniff_media_type,
    extract_team_from_screenshots,
)
from sketch.team import PokemonEntry, TeamData

# --- Test data fixtures for Anthropic SDK response shapes -------------------


@dataclass
class _FakeToolUseBlock:
    type: str
    name: str
    input: dict | str


@dataclass
class _FakeTextBlock:
    type: str
    text: str


@dataclass
class _FakeMessage:
    content: list[Any]
    stop_reason: str = "tool_use"


def _make_client(response: _FakeMessage | Exception) -> MagicMock:
    client = MagicMock()
    if isinstance(response, Exception):
        client.messages.create = AsyncMock(side_effect=response)
    else:
        client.messages.create = AsyncMock(return_value=response)
    return client


# --- Sample tool-use payloads ----------------------------------------------


def _pair(raw: str, en: str | None = None) -> dict[str, str]:
    """A {raw, en} name pair; English screenshots have raw == en."""
    return {"raw": raw, "en": raw if en is None else en}


def _wrap_names(team: dict) -> dict:
    """Convert a plain-English team literal into the {raw, en} tool shape.

    Lets the fixture below stay readable as plain names while matching the
    schema the model now returns (verbatim `raw` + English `en`). raw == en
    here models an English screenshot, so the lookup is a no-op and existing
    value assertions hold.
    """
    for mon in team["pokemon"]:
        mon["species"] = _pair(mon["species"])
        mon["ability"] = _pair(mon["ability"])
        if mon.get("item") is not None:
            mon["item"] = _pair(mon["item"])
        mon["moves"] = [_pair(m) for m in mon["moves"]]
    return team


def _full_team_input(team_id: str | None = "QBXXWXL05U") -> dict:
    """A well-formed `submit_team` tool input with all six Pokemon.

    Values mirror the reference team — small EV pool (~66 total),
    arrow-based nature, gender icons present. Anything the model would
    realistically extract from the sample-replica-code.png share screen.
    """
    plain = {
        "team_id": team_id,
        "pokemon": [
            {
                "species": "Floette-Eternal",
                "gender": "F",
                "item": "Floettite",
                "ability": "Flower Veil",
                "nature": {"boosted_stat": "Sp. Atk", "reduced_stat": "Attack"},
                "evs": {
                    "hp": 32,
                    "atk": 0,
                    "def": 0,
                    "spa": 32,
                    "spd": 0,
                    "spe": 2,
                },
                "moves": ["Dazzling Gleam", "Moonblast", "Light of Ruin", "Protect"],
            },
            {
                "species": "Aerodactyl",
                "gender": "M",
                "item": "Lum Berry",
                "ability": "Unnerve",
                "nature": {"boosted_stat": "Speed", "reduced_stat": "Sp. Atk"},
                "evs": {
                    "hp": 7,
                    "atk": 32,
                    "def": 5,
                    "spa": 0,
                    "spd": 5,
                    "spe": 17,
                },
                "moves": ["Rock Slide", "Dual Wingbeat", "Tailwind", "Protect"],
            },
            {
                "species": "Incineroar",
                "gender": "M",
                "item": "Sitrus Berry",
                "ability": "Intimidate",
                "nature": {"boosted_stat": "Attack", "reduced_stat": "Sp. Atk"},
                "evs": {
                    "hp": 32,
                    "atk": 2,
                    "def": 8,
                    "spa": 0,
                    "spd": 8,
                    "spe": 16,
                },
                "moves": ["Flare Blitz", "Throat Chop", "Parting Shot", "Fake Out"],
            },
            {
                "species": "Garchomp",
                "gender": "F",
                "item": "Choice Scarf",
                "ability": "Rough Skin",
                "nature": {"boosted_stat": "Attack", "reduced_stat": "Sp. Atk"},
                "evs": {
                    "hp": 24,
                    "atk": 20,
                    "def": 0,
                    "spa": 0,
                    "spd": 0,
                    "spe": 22,
                },
                "moves": [
                    "Dragon Claw",
                    "Earthquake",
                    "Stomping Tantrum",
                    "Rock Slide",
                ],
            },
            {
                "species": "Charizard",
                "gender": "M",
                "item": "Charizardite Y",
                "ability": "Solar Power",
                "nature": {"boosted_stat": "Sp. Atk", "reduced_stat": "Attack"},
                "evs": {
                    "hp": 6,
                    "atk": 0,
                    "def": 16,
                    "spa": 31,
                    "spd": 0,
                    "spe": 13,
                },
                "moves": ["Heat Wave", "Weather Ball", "Air Slash", "Protect"],
            },
            {
                "species": "Venusaur",
                "gender": "M",
                "item": "Focus Sash",
                "ability": "Chlorophyll",
                "nature": {"boosted_stat": "Sp. Atk", "reduced_stat": "Attack"},
                "evs": {
                    "hp": 2,
                    "atk": 0,
                    "def": 0,
                    "spa": 32,
                    "spd": 0,
                    "spe": 32,
                },
                "moves": ["Leaf Storm", "Sludge Bomb", "Earth Power", "Sleep Powder"],
            },
        ],
    }
    return _wrap_names(plain)


def _team_message(input_payload: dict) -> _FakeMessage:
    return _FakeMessage(
        content=[
            _FakeToolUseBlock(type="tool_use", name="submit_team", input=input_payload)
        ]
    )


# Smallest valid PNG (1x1 transparent pixel) so we can pass `read()`-able
# bytes through the extractor without standing up real screenshots. The
# extractor doesn't introspect the pixels — it just forwards them to
# Anthropic — so a minimal valid PNG suffices.
_TINY_PNG = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


# --- Tests -----------------------------------------------------------------


class TestSniffMediaType:
    def test_png(self):
        assert _sniff_media_type(_TINY_PNG) == "image/png"

    def test_jpeg(self):
        assert _sniff_media_type(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00") == "image/jpeg"

    def test_webp(self):
        webp_header = b"RIFF\x00\x00\x00\x00WEBPVP8 "
        assert _sniff_media_type(webp_header) == "image/webp"

    def test_gif(self):
        assert _sniff_media_type(b"GIF89a\x01\x00") == "image/gif"

    def test_unknown_raises(self):
        with pytest.raises(ExtractionError, match="Unrecognized image"):
            _sniff_media_type(b"not an image at all")

    def test_too_short_for_webp_check_doesnt_crash(self):
        # The webp check indexes [8:12] — make sure short non-webp bytes
        # don't trip an IndexError before the fall-through error.
        with pytest.raises(ExtractionError):
            _sniff_media_type(b"abc")


class TestResolveNature:
    """The deterministic 20-entry table is the only safe way to translate
    Page 2's red ↑ / blue ↓ arrows. The table mirrors the canonical
    Pokémon nature spec (see e.g.
    <https://bulbapedia.bulbagarden.net/wiki/Nature>)."""

    def test_adamant_atk_up_spa_down(self):
        assert _resolve_nature("Attack", "Sp. Atk") == "Adamant"

    def test_modest_spa_up_atk_down(self):
        assert _resolve_nature("Sp. Atk", "Attack") == "Modest"

    def test_jolly_speed_up_spa_down(self):
        # Jolly = +Speed, -Sp. Atk (physical attackers).
        assert _resolve_nature("Speed", "Sp. Atk") == "Jolly"

    def test_timid_speed_up_atk_down(self):
        # Timid = +Speed, -Attack (special attackers). Paired with the
        # Jolly test above so neither direction can silently flip back.
        assert _resolve_nature("Speed", "Attack") == "Timid"

    def test_neutral_when_arrows_absent(self):
        assert _resolve_nature(None, None) == "Serious"

    def test_neutral_when_same_stat_for_both(self):
        # A nature can't boost AND reduce the same stat. Defensive: treat
        # as neutral rather than emitting a fake name.
        assert _resolve_nature("Attack", "Attack") == "Serious"

    def test_unknown_combo_falls_back_to_hardy(self):
        # Off-table inputs (HP, garbage strings) shouldn't crash — they
        # degrade to the neutral default.
        assert _resolve_nature("HP", "Attack") == "Serious"


class TestExtractTeamFromScreenshots:
    async def test_returns_team_data_on_well_formed_tool_use(self):
        client = _make_client(_team_message(_full_team_input()))
        team = await extract_team_from_screenshots(client, _TINY_PNG, _TINY_PNG)
        assert isinstance(team, TeamData)
        assert len(team.pokemon) == 6
        floette = team.pokemon[0]
        assert floette.species == "Floette-Eternal"
        assert floette.gender == "F"
        assert floette.item == "Floettite"
        # Nature resolved from arrows in code, not by the model.
        assert floette.nature == "Modest"
        assert floette.evs == {
            "hp": 32,
            "atk": 0,
            "def": 0,
            "spa": 32,
            "spd": 0,
            "spe": 2,
        }

    async def test_captures_team_id(self):
        client = _make_client(_team_message(_full_team_input("QBXXWXL05U")))
        team = await extract_team_from_screenshots(client, _TINY_PNG, _TINY_PNG)
        assert team.team_id == "QBXXWXL05U"

    async def test_team_id_uppercased(self):
        # Model might return lowercase; we canonicalize so the equality
        # check against the user's submitted code is case-insensitive.
        client = _make_client(_team_message(_full_team_input("qbxxwxl05u")))
        team = await extract_team_from_screenshots(client, _TINY_PNG, _TINY_PNG)
        assert team.team_id == "QBXXWXL05U"

    async def test_team_id_none_when_model_returns_null(self):
        client = _make_client(_team_message(_full_team_input(team_id=None)))
        team = await extract_team_from_screenshots(client, _TINY_PNG, _TINY_PNG)
        assert team.team_id is None

    async def test_two_images_sent_when_page2_provided(self):
        client = _make_client(_team_message(_full_team_input()))
        await extract_team_from_screenshots(client, _TINY_PNG, _TINY_PNG)
        client.messages.create.assert_called_once()
        user_content = client.messages.create.call_args.kwargs["messages"][0]["content"]
        image_blocks = [b for b in user_content if b.get("type") == "image"]
        assert len(image_blocks) == 2

    async def test_single_image_sent_when_page2_omitted(self):
        # Stitched-image submissions: user uploads one image containing
        # both pages. Extractor should send just that one to the model
        # and the prompt's instruction text adjusts to match.
        client = _make_client(_team_message(_full_team_input()))
        await extract_team_from_screenshots(client, _TINY_PNG, None)
        call = client.messages.create.call_args.kwargs
        user_content = call["messages"][0]["content"]
        image_blocks = [b for b in user_content if b.get("type") == "image"]
        assert len(image_blocks) == 1
        text_block = next(b for b in user_content if b.get("type") == "text")
        assert "stitched" in text_block["text"]

    async def test_uses_configured_model_by_default(self):
        client = _make_client(_team_message(_full_team_input()))
        await extract_team_from_screenshots(client, _TINY_PNG, _TINY_PNG)
        from sketch import config

        actual_model = client.messages.create.call_args.kwargs["model"]
        assert actual_model == config.REPLICA_OCR_MODEL

    async def test_model_override_wins(self):
        client = _make_client(_team_message(_full_team_input()))
        await extract_team_from_screenshots(
            client, _TINY_PNG, _TINY_PNG, model="claude-opus-4-7"
        )
        assert client.messages.create.call_args.kwargs["model"] == "claude-opus-4-7"

    async def test_tool_choice_forces_submit_team(self):
        client = _make_client(_team_message(_full_team_input()))
        await extract_team_from_screenshots(client, _TINY_PNG, _TINY_PNG)
        call = client.messages.create.call_args.kwargs
        assert call["tool_choice"] == {"type": "tool", "name": "submit_team"}
        assert [t["name"] for t in call["tools"]] == ["submit_team"]

    async def test_system_prompt_is_cacheable(self):
        client = _make_client(_team_message(_full_team_input()))
        await extract_team_from_screenshots(client, _TINY_PNG, _TINY_PNG)
        system = client.messages.create.call_args.kwargs["system"]
        assert isinstance(system, list) and len(system) == 1
        assert system[0]["cache_control"] == {"type": "ephemeral"}

    async def test_missing_tool_use_raises_extraction_error(self):
        # Model returned a text-only response (which would be a bug given
        # tool_choice=submit_team, but we treat it as failure mode rather
        # than crashing).
        client = _make_client(
            _FakeMessage(
                content=[_FakeTextBlock(type="text", text="I'm not sure.")],
                stop_reason="end_turn",
            )
        )
        with pytest.raises(ExtractionError, match="Couldn't read"):
            await extract_team_from_screenshots(client, _TINY_PNG, _TINY_PNG)

    async def test_wrong_pokemon_count_raises_extraction_error(self):
        bad = _full_team_input()
        bad["pokemon"] = bad["pokemon"][:5]
        client = _make_client(_team_message(bad))
        with pytest.raises(ExtractionError, match="Expected 6 Pokemon"):
            await extract_team_from_screenshots(client, _TINY_PNG, _TINY_PNG)

    async def test_anthropic_api_error_raises_extraction_error(self):
        client = _make_client(
            anthropic.APIError(
                "boom",
                request=None,  # type: ignore[arg-type]
                body=None,
            )
        )
        with pytest.raises(ExtractionError, match="please try again"):
            await extract_team_from_screenshots(client, _TINY_PNG, _TINY_PNG)

    async def test_non_image_bytes_raises_extraction_error(self):
        client = _make_client(_team_message(_full_team_input()))
        with pytest.raises(ExtractionError, match="Unrecognized image"):
            await extract_team_from_screenshots(client, b"plain text", _TINY_PNG)

    async def test_item_null_passes_through_as_none(self):
        payload = _full_team_input()
        payload["pokemon"][0]["item"] = None
        client = _make_client(_team_message(payload))
        team = await extract_team_from_screenshots(client, _TINY_PNG, _TINY_PNG)
        assert team.pokemon[0].item is None

    async def test_gender_null_passes_through_as_none(self):
        payload = _full_team_input()
        payload["pokemon"][0]["gender"] = None
        client = _make_client(_team_message(payload))
        team = await extract_team_from_screenshots(client, _TINY_PNG, _TINY_PNG)
        assert team.pokemon[0].gender is None

    async def test_isinstance_pokemon_entry(self):
        client = _make_client(_team_message(_full_team_input()))
        team = await extract_team_from_screenshots(client, _TINY_PNG, _TINY_PNG)
        assert all(isinstance(p, PokemonEntry) for p in team.pokemon)

    async def test_neutral_nature_when_arrows_absent(self):
        # Defensive: a Pokemon with no arrows on Page 2 (genuinely neutral
        # nature) should resolve to "Serious" rather than the lookup table's
        # default failing into something nonsensical.
        payload = _full_team_input()
        payload["pokemon"][0]["nature"] = {
            "boosted_stat": None,
            "reduced_stat": None,
        }
        client = _make_client(_team_message(payload))
        team = await extract_team_from_screenshots(client, _TINY_PNG, _TINY_PNG)
        assert team.pokemon[0].nature == "Serious"

    async def test_instruction_text_mentions_language(self):
        # The per-call user instruction reinforces, right next to the images,
        # that screenshots may be non-English and output must be English.
        client = _make_client(_team_message(_full_team_input()))
        await extract_team_from_screenshots(client, _TINY_PNG, _TINY_PNG)
        user_content = client.messages.create.call_args.kwargs["messages"][0]["content"]
        text_block = next(b for b in user_content if b.get("type") == "text")
        assert "language" in text_block["text"].lower()


class TestLocalizedLookupPostProcess:
    """The {raw, en} echo plus the PokeAPI table corrects translation-class
    misses in Python after the vision call (issue #46). These feed localized
    `raw` glyphs with a deliberately wrong `en` and assert the canonical name
    wins — and that table misses / model-resolved forms fall back to `en`.
    """

    async def _extract_first(self, payload: dict) -> PokemonEntry:
        client = _make_client(_team_message(payload))
        team = await extract_team_from_screenshots(client, _TINY_PNG, _TINY_PNG)
        return team.pokemon[0]

    async def test_localized_move_overrides_wrong_english(self):
        payload = _full_team_input()
        # Korean Calm Mind; model mislabeled it the look-alike "Meditate".
        payload["pokemon"][0]["moves"][0] = {"raw": "명상", "en": "Meditate"}
        mon = await self._extract_first(payload)
        assert mon.moves[0] == "Calm Mind"

    async def test_localized_item_overrides_wrong_english(self):
        payload = _full_team_input()
        # Korean Choice Scarf with the screen's internal space.
        payload["pokemon"][0]["item"] = {"raw": "구애 스카프", "en": "Gooey Scarf"}
        mon = await self._extract_first(payload)
        assert mon.item == "Choice Scarf"

    async def test_localized_species_overrides_lookalike(self):
        payload = _full_team_input()
        # Korean Delphox; model collapsed it onto the look-alike Blaziken.
        payload["pokemon"][0]["species"] = {"raw": "마폭시", "en": "Blaziken"}
        mon = await self._extract_first(payload)
        assert mon.species == "Delphox"

    async def test_species_form_suffix_is_preserved(self):
        payload = _full_team_input()
        # Model resolved a regional form from type/ability signals; the base
        # name in `raw` must not strip the suffix.
        payload["pokemon"][0]["species"] = {
            "raw": "바쿠퐁",
            "en": "Typhlosion-Hisui",
        }
        mon = await self._extract_first(payload)
        assert mon.species == "Typhlosion-Hisui"

    async def test_custom_content_falls_back_to_model_english(self):
        payload = _full_team_input()
        # Champions-custom item absent from the table — keep the model's guess.
        payload["pokemon"][0]["item"] = {"raw": "플로엣타이트", "en": "Floettite"}
        mon = await self._extract_first(payload)
        assert mon.item == "Floettite"


class TestSystemPromptMultilingualGuidance:
    """Real foreign-language OCR accuracy can't be tested here (the SDK is
    mocked, no images, no network). What we can guard is that the system
    prompt keeps the guidance that makes foreign-language screenshots work —
    so a future edit can't silently drop it and regress issue #35.
    """

    def test_declares_multilingual_input(self):
        lowered = _SYSTEM_PROMPT.lower()
        assert "language" in lowered
        # Names a few non-English languages so the model expects them.
        assert "japanese" in lowered
        assert "korean" in lowered

    def test_requires_translation_to_canonical_english(self):
        lowered = _SYSTEM_PROMPT.lower()
        assert "canonical english" in lowered or "official english" in lowered
        assert "translate" in lowered

    def test_requires_raw_and_english_name_pair(self):
        # The post-process lookup (issue #46) depends on the model echoing the
        # on-screen glyphs in `raw` alongside its English `en`.
        lowered = _SYSTEM_PROMPT.lower()
        assert "`raw`" in lowered
        assert "`en`" in lowered
        assert "verbatim" in lowered

    def test_identifies_pages_by_structure_not_tab_text(self):
        # Tab labels are localized, so page ID must lean on structure.
        assert "STRUCTURE" in _SYSTEM_PROMPT
        assert "localized" in _SYSTEM_PROMPT.lower()

    def test_has_anti_hallucination_guardrails(self):
        lowered = _SYSTEM_PROMPT.lower()
        assert "never invent" in lowered
        assert "only what is visibly present" in lowered

    def test_has_translation_pitfall_examples(self):
        # Worked examples for the three observed failure modes: katakana
        # loanword (Liquidation), word-order/look-alike collision (Hammer
        # Arm), and read-the-kana vs guess-the-common-ability (Frisk).
        assert "Liquidation" in _SYSTEM_PROMPT
        assert "Hammer Arm" in _SYSTEM_PROMPT
        assert "Frisk" in _SYSTEM_PROMPT

    def test_examples_framed_as_non_exhaustive(self):
        # Guard the anti-overfitting framing: the named examples must be
        # presented as illustrative, not a closed list.
        assert "not a closed list" in _SYSTEM_PROMPT.lower()

    def test_guards_against_species_lookalike_substitution(self):
        # The species name is the signal; guard against collapsing a newer
        # species onto an older look-alike (the non-Latin failure mode).
        lowered = _SYSTEM_PROMPT.lower()
        assert "look-alike" in lowered
        assert "newer species" in lowered

    def test_has_duplicate_move_guard(self):
        assert "same move twice" in _SYSTEM_PROMPT.lower()

    def test_has_pre_submit_self_check(self):
        lowered = _SYSTEM_PROMPT.lower()
        assert "before you call submit_team" in lowered
