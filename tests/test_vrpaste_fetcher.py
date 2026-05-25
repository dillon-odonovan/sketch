"""Tests for `sketch.vrpaste.fetcher.fetch_vrpaste`.

All network is mocked via `aioresponses`. The sample payload mirrors
the real VRPaste backend shape captured during issue-30 investigation
(no EVs, gender absent on some entries, mixed item presence).
"""

import pytest
from aioresponses import aioresponses

from sketch.team import PokemonEntry, TeamData
from sketch.vrpaste.fetcher import VRPasteFetchError, fetch_vrpaste

# Real-shape sample (trimmed): the production backend returns these
# fields and we ignore the translation companions + UI metadata.
_SAMPLE_PAYLOAD = {
    "id": "gxmfscC1",
    "is_public": True,
    "is_encrypted": False,
    "title": "Untitled Team",
    "teams": [
        {
            "species": "Dragonite",
            "item": "Dragonium Z",
            "ability": "Multiscale",
            "moves": ["Dragon Pulse", "Thunderbolt", "Heat Wave", "Protect"],
            "nature": "Timid",
            "level": 50,
        },
        {
            "species": "Whimsicott",
            "item": "Focus Sash",
            "ability": "Prankster",
            "moves": ["Protect", "Moonblast", "Encore", "Tailwind"],
            "nature": "Modest",
            "level": 50,
        },
        {
            "species": "Basculegion",
            "item": "Sitrus Berry",
            "ability": "Adaptability",
            "moves": ["Protect", "Wave Crash", "Aqua Jet", "Last Respects"],
            "nature": "Adamant",
            "gender": "M",
            "level": 50,
        },
    ],
    "hasPassword": False,
    "createdAt": 1779542876,
}

_BACKEND_URL = "https://vrpaste-backend.vercel.app/api/paste/gxmfscC1?lang=english"
_SAMPLE_URL = "https://www.vrpastes.com/gxmfscC1"


class TestFetchVRPasteHappyPath:
    async def test_returns_team_data(self):
        with aioresponses() as mock:
            mock.get(_BACKEND_URL, payload=_SAMPLE_PAYLOAD)
            team = await fetch_vrpaste(_SAMPLE_URL)
        assert isinstance(team, TeamData)
        assert len(team.pokemon) == 3
        assert all(isinstance(p, PokemonEntry) for p in team.pokemon)

    async def test_maps_required_fields_directly(self):
        with aioresponses() as mock:
            mock.get(_BACKEND_URL, payload=_SAMPLE_PAYLOAD)
            team = await fetch_vrpaste(_SAMPLE_URL)
        dragonite = team.pokemon[0]
        assert dragonite.species == "Dragonite"
        assert dragonite.item == "Dragonium Z"
        assert dragonite.ability == "Multiscale"
        assert dragonite.nature == "Timid"
        assert dragonite.moves == [
            "Dragon Pulse",
            "Thunderbolt",
            "Heat Wave",
            "Protect",
        ]

    async def test_gender_pass_through_when_present(self):
        with aioresponses() as mock:
            mock.get(_BACKEND_URL, payload=_SAMPLE_PAYLOAD)
            team = await fetch_vrpaste(_SAMPLE_URL)
        basculegion = team.pokemon[2]
        assert basculegion.gender == "M"

    async def test_gender_none_when_absent(self):
        with aioresponses() as mock:
            mock.get(_BACKEND_URL, payload=_SAMPLE_PAYLOAD)
            team = await fetch_vrpaste(_SAMPLE_URL)
        dragonite = team.pokemon[0]
        assert dragonite.gender is None

    async def test_evs_default_to_zero_when_absent(self):
        # Sample payload has no EVs (this is the most common shape from
        # the production API as of issue-30 investigation). Renderer
        # filters EVs > 0, so an all-zero dict produces no EV line —
        # the correct behavior for "no EVs declared".
        with aioresponses() as mock:
            mock.get(_BACKEND_URL, payload=_SAMPLE_PAYLOAD)
            team = await fetch_vrpaste(_SAMPLE_URL)
        for p in team.pokemon:
            assert p.evs == {
                "hp": 0,
                "atk": 0,
                "def": 0,
                "spa": 0,
                "spd": 0,
                "spe": 0,
            }

    async def test_evs_pass_through_when_present(self):
        # If/when VRPaste starts returning EVs in this field, we want to
        # propagate them.
        payload = {
            **_SAMPLE_PAYLOAD,
            "teams": [
                {
                    **_SAMPLE_PAYLOAD["teams"][0],
                    "evs": {
                        "hp": 4,
                        "atk": 0,
                        "def": 0,
                        "spa": 252,
                        "spd": 0,
                        "spe": 252,
                    },
                }
            ],
        }
        with aioresponses() as mock:
            mock.get(_BACKEND_URL, payload=payload)
            team = await fetch_vrpaste(_SAMPLE_URL)
        assert team.pokemon[0].evs == {
            "hp": 4,
            "atk": 0,
            "def": 0,
            "spa": 252,
            "spd": 0,
            "spe": 252,
        }

    async def test_item_null_passes_through_as_none(self):
        payload = {
            **_SAMPLE_PAYLOAD,
            "teams": [{**_SAMPLE_PAYLOAD["teams"][0], "item": ""}],
        }
        with aioresponses() as mock:
            mock.get(_BACKEND_URL, payload=payload)
            team = await fetch_vrpaste(_SAMPLE_URL)
        assert team.pokemon[0].item is None

    async def test_truncates_moves_to_four(self):
        # Defensive: VRPaste's UI caps moves at 4 but if the backend ever
        # returned more (or fewer) we shouldn't smuggle a malformed paste
        # to Showdown.
        payload = {
            **_SAMPLE_PAYLOAD,
            "teams": [
                {
                    **_SAMPLE_PAYLOAD["teams"][0],
                    "moves": ["A", "B", "C", "D", "E"],
                }
            ],
        }
        with aioresponses() as mock:
            mock.get(_BACKEND_URL, payload=payload)
            team = await fetch_vrpaste(_SAMPLE_URL)
        assert team.pokemon[0].moves == ["A", "B", "C", "D"]


class TestFetchVRPasteRefusals:
    async def test_password_protected_paste_raises(self):
        payload = {**_SAMPLE_PAYLOAD, "hasPassword": True}
        with aioresponses() as mock:
            mock.get(_BACKEND_URL, payload=payload)
            with pytest.raises(VRPasteFetchError, match="password-protected"):
                await fetch_vrpaste(_SAMPLE_URL)

    async def test_encrypted_paste_raises(self):
        payload = {**_SAMPLE_PAYLOAD, "is_encrypted": True}
        with aioresponses() as mock:
            mock.get(_BACKEND_URL, payload=payload)
            with pytest.raises(VRPasteFetchError, match="password-protected"):
                await fetch_vrpaste(_SAMPLE_URL)


class TestFetchVRPasteErrors:
    async def test_http_404_raises(self):
        with aioresponses() as mock:
            mock.get(_BACKEND_URL, status=404)
            with pytest.raises(VRPasteFetchError):
                await fetch_vrpaste(_SAMPLE_URL)

    async def test_http_500_raises(self):
        with aioresponses() as mock:
            mock.get(_BACKEND_URL, status=500)
            with pytest.raises(VRPasteFetchError):
                await fetch_vrpaste(_SAMPLE_URL)

    async def test_non_object_payload_raises(self):
        with aioresponses() as mock:
            mock.get(_BACKEND_URL, payload=["not", "an", "object"])
            with pytest.raises(VRPasteFetchError):
                await fetch_vrpaste(_SAMPLE_URL)

    async def test_empty_teams_raises(self):
        payload = {**_SAMPLE_PAYLOAD, "teams": []}
        with aioresponses() as mock:
            mock.get(_BACKEND_URL, payload=payload)
            with pytest.raises(VRPasteFetchError, match="no pokemon"):
                await fetch_vrpaste(_SAMPLE_URL)

    async def test_missing_teams_field_raises(self):
        payload = {k: v for k, v in _SAMPLE_PAYLOAD.items() if k != "teams"}
        with aioresponses() as mock:
            mock.get(_BACKEND_URL, payload=payload)
            with pytest.raises(VRPasteFetchError, match="no pokemon"):
                await fetch_vrpaste(_SAMPLE_URL)

    async def test_pokemon_missing_species_raises(self):
        payload = {
            **_SAMPLE_PAYLOAD,
            "teams": [
                {k: v for k, v in _SAMPLE_PAYLOAD["teams"][0].items() if k != "species"}
            ],
        }
        with aioresponses() as mock:
            mock.get(_BACKEND_URL, payload=payload)
            with pytest.raises(VRPasteFetchError, match="unexpected pokemon shape"):
                await fetch_vrpaste(_SAMPLE_URL)

    async def test_invalid_url_raises_at_id_extraction(self):
        # extract_vrpaste_id (called at the top of fetch_vrpaste) raises
        # ValidationError for malformed URLs before any HTTP call.
        from sketch.pokepaste.validator import ValidationError

        with pytest.raises(ValidationError):
            await fetch_vrpaste("https://example.com/nope")
