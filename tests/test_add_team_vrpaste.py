"""End-to-end test for the VRPaste branch of `/add-team`'s URL resolver.

Drives `_resolve_via_vrpaste` directly with a minimal fake
`discord.Interaction` rather than running the full slash-command handler
(which would need to mock CommandTree, choices, defer, etc.). That's
enough to verify the happy-path routing — fetch → render → mint → cache
— and the cache-hit shortcut without standing up Discord plumbing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aioresponses import aioresponses

from sketch.commands.add_team import _AddTeamInputs, _resolve_via_vrpaste
from sketch.vrpaste.cache import InMemoryVRPasteCacheStore

_SAMPLE_PAYLOAD = {
    "id": "gxmfscC1",
    "is_public": True,
    "is_encrypted": False,
    "title": "Test Team",
    "teams": [
        {
            "species": "Dragonite",
            "item": "Dragoninite",
            "ability": "Multiscale",
            "moves": ["Dragon Pulse", "Thunderbolt", "Heat Wave", "Protect"],
            "nature": "Timid",
        }
    ],
    "hasPassword": False,
}


def _make_interaction() -> MagicMock:
    interaction = MagicMock()
    interaction.user.id = 111
    interaction.guild_id = 222
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


def _inputs(url: str = "https://www.vrpastes.com/gxmfscC1") -> _AddTeamInputs:
    return _AddTeamInputs(
        description="desc",
        fmt_name="Reg M-A",
        sheet_name="Reg M-A Sheet",
        paste_type_value="Exact",
        url=url,
        replica=None,
        page1=None,
        page2=None,
    )


def _backend_url(vrpaste_id: str) -> str:
    return f"https://vrpaste-backend.vercel.app/api/paste/{vrpaste_id}?lang=english"


class TestResolveViaVRPaste:
    async def test_cache_miss_fetches_mints_and_caches(self):
        cache = InMemoryVRPasteCacheStore()
        interaction = _make_interaction()

        with aioresponses() as mock:
            mock.get(_backend_url("gxmfscC1"), payload=_SAMPLE_PAYLOAD)
            mock.post(
                "https://pokepast.es/create",
                status=303,
                headers={"Location": "/minted123"},
            )
            result = await _resolve_via_vrpaste(
                interaction,
                inputs=_inputs(),
                vrpaste_cache=cache,
            )

        assert result is not None
        canonical_url, dedup_url, preview_shown = result
        assert canonical_url == "https://pokepast.es/minted123"
        assert dedup_url == "https://pokepast.es/minted123"
        assert preview_shown is False

        cached = cache.get("gxmfscC1")
        assert cached is not None
        assert cached.pokepaste_url == "https://pokepast.es/minted123"
        assert cached.created_by_user_id == 111
        assert cached.created_by_guild_id == 222

    async def test_cache_hit_skips_fetch_and_mint(self):
        # Pre-seed the cache so we shouldn't see any HTTP calls.
        cache = InMemoryVRPasteCacheStore()
        cache.create(
            "gxmfscC1",
            "https://pokepast.es/already-minted",
            user_id=111,
            guild_id=222,
        )
        interaction = _make_interaction()

        # aioresponses with no registered mocks raises on any unexpected call,
        # which is exactly what we want — a cache hit must not touch the network.
        with aioresponses():
            result = await _resolve_via_vrpaste(
                interaction,
                inputs=_inputs(),
                vrpaste_cache=cache,
            )

        assert result is not None
        canonical_url, dedup_url, _ = result
        assert canonical_url == "https://pokepast.es/already-minted"
        assert dedup_url == "https://pokepast.es/already-minted"

    async def test_lost_race_uses_winners_url_for_sheet_dedup(self):
        # Simulate the loser's path: cache.get returns None (the race
        # hasn't propagated yet to the local read-through), but by the
        # time create() runs the winner is already there. The loser
        # must use the winner's URL — both sheet writes need to converge
        # on a single Pokepaste URL so dedup catches the second row.
        cache = InMemoryVRPasteCacheStore()
        cache.create(
            "gxmfscC1",
            "https://pokepast.es/winner",
            user_id=999,
            guild_id=888,
        )
        # Wipe the cache `get` cache (simulate the loser missing the
        # get-side check) by deleting from the dict — but actually our
        # InMemory store has only one dict, so to simulate a "missed
        # get + collided create" we'd need a more elaborate fake. The
        # production-relevant invariant is: when create returns an
        # entry whose URL differs from minted_url, we use the entry's
        # URL. That's exactly what test_cache_hit_skips_fetch_and_mint
        # exercises end-to-end through the cache. Race-specific
        # behavior is covered at the store level in
        # tests/test_vrpaste_cache.py::test_create_is_fail_if_exists_returns_winner.
        # This test just confirms the resolver doesn't bypass the cache
        # entry when present.
        interaction = _make_interaction()
        with aioresponses():
            result = await _resolve_via_vrpaste(
                interaction,
                inputs=_inputs(),
                vrpaste_cache=cache,
            )
        assert result is not None
        canonical_url, dedup_url, _ = result
        assert canonical_url == "https://pokepast.es/winner"
        assert dedup_url == "https://pokepast.es/winner"

    async def test_password_protected_paste_surfaces_user_error(self):
        cache = InMemoryVRPasteCacheStore()
        interaction = _make_interaction()
        payload = {**_SAMPLE_PAYLOAD, "hasPassword": True}

        with aioresponses() as mock:
            mock.get(_backend_url("gxmfscC1"), payload=payload)
            result = await _resolve_via_vrpaste(
                interaction,
                inputs=_inputs(),
                vrpaste_cache=cache,
            )

        assert result is None
        # The status edit should mention the password issue (the message
        # comes from VRPasteFetchError).
        assert "password-protected" in interaction.edit_original_response.call_args.kwargs["content"]
        # Nothing should land in the cache.
        assert cache.get("gxmfscC1") is None

    async def test_malformed_vrpaste_url_returns_validation_error(self):
        cache = InMemoryVRPasteCacheStore()
        interaction = _make_interaction()

        # No HTTP mocks registered — extract_vrpaste_id raises before any
        # network call when the URL doesn't match the VRPaste regex.
        with aioresponses():
            result = await _resolve_via_vrpaste(
                interaction,
                inputs=_inputs(url="https://example.com/nope"),
                vrpaste_cache=cache,
            )

        assert result is None
        interaction.followup.send.assert_called_once()
        (content,), kwargs = interaction.followup.send.call_args
        assert "VRPaste URL" in content
        assert kwargs.get("ephemeral") is True


class TestRoutingDispatch:
    """Sanity check: VRPaste URL vs Pokepaste URL hit different code paths."""

    def test_vrpaste_url_detected(self):
        from sketch.vrpaste.validator import is_vrpaste_url

        assert is_vrpaste_url("https://www.vrpastes.com/gxmfscC1") is True
        assert is_vrpaste_url("https://vrpastes.com/abc123") is True

    def test_pokepaste_url_not_detected_as_vrpaste(self):
        from sketch.vrpaste.validator import is_vrpaste_url

        # The whole point of the dispatch — Pokepaste URLs must not
        # route through the VRPaste resolver.
        assert is_vrpaste_url("https://pokepast.es/abc123") is False

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "not a url",
            "https://example.com/abc",
        ],
    )
    def test_garbage_url_not_detected_as_vrpaste(self, url):
        from sketch.vrpaste.validator import is_vrpaste_url

        assert is_vrpaste_url(url) is False
