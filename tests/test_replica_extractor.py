"""Tests for the Claude vision extractor.

The Anthropic SDK is mocked end-to-end (no network) — we substitute a fake
`AsyncAnthropic` whose `messages.create` returns canned tool-use payloads.
What we're actually verifying:
  - the per-Pokemon parser turns a well-formed tool input into a typed
    `TeamData` with all six entries populated;
  - schema-violating outputs surface as `ExtractionError` rather than
    leaking dict-shape exceptions into the slash command;
  - the image-format sniffer accepts the formats Discord users actually
    upload and rejects everything else.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import anthropic
import pytest

from sketch.replica.extractor import (
    ExtractionError,
    PokemonEntry,
    TeamData,
    _sniff_media_type,
    extract_team_from_screenshots,
)

# --- Fakes for the Anthropic SDK -------------------------------------------


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


class _FakeAsyncMessages:
    def __init__(self, response: _FakeMessage | Exception) -> None:
        self._response = response
        self.calls: list[dict] = []

    async def create(self, **kwargs: Any) -> _FakeMessage:
        self.calls.append(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _FakeAnthropic:
    """Just enough of `anthropic.AsyncAnthropic` to drive the extractor.

    The extractor only touches `client.messages.create`, so we don't need
    to stub the rest of the SDK surface.
    """

    def __init__(self, response: _FakeMessage | Exception) -> None:
        self.messages = _FakeAsyncMessages(response)


# --- Sample tool-use payloads ----------------------------------------------


def _full_team_input() -> dict:
    """One well-formed `submit_team` tool input with all six Pokemon."""
    return {
        "pokemon": [
            {
                "species": "Iron Hands",
                "item": "Assault Vest",
                "ability": "Quark Drive",
                "tera_type": "Water",
                "nature": "Adamant",
                "evs": {
                    "hp": 252,
                    "atk": 252,
                    "def": 0,
                    "spa": 0,
                    "spd": 4,
                    "spe": 0,
                },
                "ivs": None,
                "moves": ["Fake Out", "Drain Punch", "Wild Charge", "Heavy Slam"],
                "level": 50,
            },
            {
                "species": "Calyrex-Shadow",
                "item": "Life Orb",
                "ability": "As One (Spectrier)",
                "tera_type": "Normal",
                "nature": "Timid",
                "evs": {
                    "hp": 4,
                    "atk": 0,
                    "def": 0,
                    "spa": 252,
                    "spd": 0,
                    "spe": 252,
                },
                "ivs": {"hp": 31, "atk": 0, "def": 31, "spa": 31, "spd": 31, "spe": 31},
                "moves": ["Astral Barrage", "Nasty Plot", "Psyshock", "Protect"],
                "level": 50,
            },
            {
                "species": "Urshifu-Rapid-Strike",
                "item": "Mystic Water",
                "ability": "Unseen Fist",
                "tera_type": "Water",
                "nature": "Jolly",
                "evs": {
                    "hp": 4,
                    "atk": 252,
                    "def": 0,
                    "spa": 0,
                    "spd": 0,
                    "spe": 252,
                },
                "ivs": None,
                "moves": ["Surging Strikes", "Close Combat", "Aqua Jet", "Detect"],
                "level": 50,
            },
            {
                "species": "Amoonguss",
                "item": "Sitrus Berry",
                "ability": "Regenerator",
                "tera_type": "Water",
                "nature": "Calm",
                "evs": {
                    "hp": 244,
                    "atk": 0,
                    "def": 4,
                    "spa": 0,
                    "spd": 252,
                    "spe": 4,
                },
                "ivs": {"hp": 31, "atk": 0, "def": 31, "spa": 31, "spd": 31, "spe": 31},
                "moves": ["Spore", "Rage Powder", "Pollen Puff", "Protect"],
                "level": 50,
            },
            {
                "species": "Rillaboom",
                "item": "Miracle Seed",
                "ability": "Grassy Surge",
                "tera_type": "Fire",
                "nature": "Adamant",
                "evs": {
                    "hp": 252,
                    "atk": 252,
                    "def": 0,
                    "spa": 0,
                    "spd": 4,
                    "spe": 0,
                },
                "ivs": None,
                "moves": ["Wood Hammer", "Grassy Glide", "Fake Out", "U-turn"],
                "level": 50,
            },
            {
                "species": "Incineroar",
                "item": "Safety Goggles",
                "ability": "Intimidate",
                "tera_type": "Ghost",
                "nature": "Careful",
                "evs": {
                    "hp": 244,
                    "atk": 4,
                    "def": 4,
                    "spa": 0,
                    "spd": 252,
                    "spe": 4,
                },
                "ivs": None,
                "moves": ["Fake Out", "Flare Blitz", "Knock Off", "Parting Shot"],
                "level": 50,
            },
        ]
    }


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


class TestExtractTeamFromScreenshots:
    async def test_returns_team_data_on_well_formed_tool_use(self):
        client = _FakeAnthropic(_team_message(_full_team_input()))
        team = await extract_team_from_screenshots(client, _TINY_PNG, _TINY_PNG)
        assert isinstance(team, TeamData)
        assert len(team.pokemon) == 6
        assert team.pokemon[0].species == "Iron Hands"
        assert team.pokemon[0].item == "Assault Vest"
        assert team.pokemon[0].ivs is None
        # Cross-page join: this entry should carry both page-1 build data
        # (ability, item, moves, tera) and page-2 training data (nature,
        # EVs, IVs). All present here proves the parser doesn't lose either.
        assert team.pokemon[1].ivs == {
            "hp": 31,
            "atk": 0,
            "def": 31,
            "spa": 31,
            "spd": 31,
            "spe": 31,
        }
        assert team.pokemon[1].evs["spa"] == 252

    async def test_sends_both_images_in_single_call(self):
        client = _FakeAnthropic(_team_message(_full_team_input()))
        await extract_team_from_screenshots(client, _TINY_PNG, _TINY_PNG)
        assert len(client.messages.calls) == 1
        call = client.messages.calls[0]
        user_content = call["messages"][0]["content"]
        image_blocks = [b for b in user_content if b.get("type") == "image"]
        assert len(image_blocks) == 2
        # Sniffed media types match the bytes we passed in.
        assert all(b["source"]["media_type"] == "image/png" for b in image_blocks)

    async def test_uses_configured_model_by_default(self):
        client = _FakeAnthropic(_team_message(_full_team_input()))
        await extract_team_from_screenshots(client, _TINY_PNG, _TINY_PNG)
        from sketch import config

        assert client.messages.calls[0]["model"] == config.REPLICA_OCR_MODEL

    async def test_model_override_wins(self):
        client = _FakeAnthropic(_team_message(_full_team_input()))
        await extract_team_from_screenshots(
            client, _TINY_PNG, _TINY_PNG, model="claude-opus-4-7"
        )
        assert client.messages.calls[0]["model"] == "claude-opus-4-7"

    async def test_tool_choice_forces_submit_team(self):
        client = _FakeAnthropic(_team_message(_full_team_input()))
        await extract_team_from_screenshots(client, _TINY_PNG, _TINY_PNG)
        call = client.messages.calls[0]
        assert call["tool_choice"] == {"type": "tool", "name": "submit_team"}
        # Tools list contains exactly the submit_team definition.
        assert [t["name"] for t in call["tools"]] == ["submit_team"]

    async def test_system_prompt_is_cacheable(self):
        client = _FakeAnthropic(_team_message(_full_team_input()))
        await extract_team_from_screenshots(client, _TINY_PNG, _TINY_PNG)
        call = client.messages.calls[0]
        system = call["system"]
        # System is a list with one block carrying cache_control.
        assert isinstance(system, list) and len(system) == 1
        assert system[0]["cache_control"] == {"type": "ephemeral"}

    async def test_missing_tool_use_raises_extraction_error(self):
        # Model returned a text-only response (which would be a bug given
        # tool_choice=submit_team, but we treat it as failure mode rather
        # than crashing).
        client = _FakeAnthropic(
            _FakeMessage(
                content=[_FakeTextBlock(type="text", text="I'm not sure.")],
                stop_reason="end_turn",
            )
        )
        with pytest.raises(ExtractionError, match="Couldn't read"):
            await extract_team_from_screenshots(client, _TINY_PNG, _TINY_PNG)

    async def test_wrong_pokemon_count_raises_extraction_error(self):
        # If for any reason the tool input slips through with the wrong
        # length, surface a user-facing error rather than a downstream
        # IndexError or len-mismatch deep in the renderer.
        bad = _full_team_input()
        bad["pokemon"] = bad["pokemon"][:5]
        client = _FakeAnthropic(_team_message(bad))
        with pytest.raises(ExtractionError, match="Expected 6 Pokemon"):
            await extract_team_from_screenshots(client, _TINY_PNG, _TINY_PNG)

    async def test_anthropic_api_error_raises_extraction_error(self):
        # Network / 5xx failure. Caller sees a friendly retry message;
        # the underlying SDK exception is logged but not surfaced.
        client = _FakeAnthropic(
            anthropic.APIError(
                "boom",
                request=None,  # type: ignore[arg-type]
                body=None,
            )
        )
        with pytest.raises(ExtractionError, match="please try again"):
            await extract_team_from_screenshots(client, _TINY_PNG, _TINY_PNG)

    async def test_non_image_bytes_raises_extraction_error(self):
        client = _FakeAnthropic(_team_message(_full_team_input()))
        with pytest.raises(ExtractionError, match="Unrecognized image"):
            await extract_team_from_screenshots(client, b"plain text", _TINY_PNG)

    async def test_item_null_passes_through_as_none(self):
        # The schema marks item as nullable; the parser should produce
        # PokemonEntry.item == None rather than the literal string "None".
        payload = _full_team_input()
        payload["pokemon"][0]["item"] = None
        client = _FakeAnthropic(_team_message(payload))
        team = await extract_team_from_screenshots(client, _TINY_PNG, _TINY_PNG)
        assert team.pokemon[0].item is None

    async def test_isinstance_pokemon_entry(self):
        client = _FakeAnthropic(_team_message(_full_team_input()))
        team = await extract_team_from_screenshots(client, _TINY_PNG, _TINY_PNG)
        assert all(isinstance(p, PokemonEntry) for p in team.pokemon)
