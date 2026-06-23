"""End-to-end test for the `/delete-team` handler helpers.

Drives the internals (`_normalize_inputs`, `_resolve_target_url`,
`_delete_and_announce`) directly with a fake interaction and a stub
SheetsClient. Mirrors the test_add_team_vrpaste.py pattern — avoids
standing up CommandTree / app_commands plumbing for the handler itself.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from discord import app_commands

from sketch import config
from sketch.commands import delete_team as dt
from sketch.commands._shared import GENERIC_SHEET_DELETE_ERROR
from sketch.storage.guild_config import GuildConfig, StaticGuildConfigStore
from sketch.storage.sheets_client import (
    RowShiftedError,
    SheetsClient,
    TeamNotFoundError,
    TeamRow,
)
from sketch.vrpaste.cache import InMemoryVRPasteCacheStore


def _make_interaction() -> MagicMock:
    interaction = MagicMock()
    interaction.user.id = 111
    interaction.user.display_name = "tester"
    interaction.user.display_avatar.url = "https://cdn.example/test.png"
    interaction.guild_id = 222
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


def _make_channel(channel_id: int = 555) -> AsyncMock:
    channel = AsyncMock()
    channel.id = channel_id
    return channel


_DEFAULT_SPECIES = [
    "Charizard",
    "Venusaur",
    "Blastoise",
    "Pikachu",
    "Snorlax",
    "Gengar",
]


def _row(
    *,
    row_number: int = 4,
    url: str = "https://pokepast.es/abc",
    description: str = "team desc",
    species: list[str] | None = None,
) -> TeamRow:
    return TeamRow(
        row_number=row_number,
        url=url,
        description=description,
        species=_DEFAULT_SPECIES if species is None else species,
    )


def _inputs(
    *,
    url: str | None = None,
    replica: str | None = None,
) -> dt._DeleteTeamInputs:
    return dt._DeleteTeamInputs(
        fmt_name="Reg M-A",
        sheet_name="Regulation M-A",
        url=url,
        replica=replica,
    )


def _choice(value: str = "Reg M-A") -> app_commands.Choice[str]:
    return app_commands.Choice(name=value, value=value)


# --- _normalize_inputs ------------------------------------------------------


class TestNormalizeInputs:
    async def test_requires_url_or_replica(self):
        interaction = _make_interaction()
        result = await dt._normalize_inputs(
            interaction,
            format_choice=_choice(),
            url=None,
            replica=None,
        )
        assert result is None
        interaction.followup.send.assert_called_once()
        (content,), kwargs = interaction.followup.send.call_args
        assert "Pokepaste/VRPaste URL" in content
        assert kwargs["ephemeral"] is True

    async def test_normalizes_replica_to_upper(self):
        interaction = _make_interaction()
        result = await dt._normalize_inputs(
            interaction,
            format_choice=_choice(),
            url=None,
            replica="qbxxwxl05u",
        )
        assert result is not None
        assert result.replica == "QBXXWXL05U"

    async def test_malformed_replica_returns_error(self):
        interaction = _make_interaction()
        result = await dt._normalize_inputs(
            interaction,
            format_choice=_choice(),
            url=None,
            replica="too-short",
        )
        assert result is None
        interaction.followup.send.assert_called_once()

    async def test_omitted_format_defaults_to_current_regulation(self):
        interaction = _make_interaction()
        result = await dt._normalize_inputs(
            interaction,
            format_choice=None,
            url="https://pokepast.es/abc",
            replica=None,
        )
        assert result is not None
        assert result.fmt_name == config.DEFAULT_FORMAT
        assert result.sheet_name == config.FORMAT_SHEETS[config.DEFAULT_FORMAT]


# --- _resolve_target_url ----------------------------------------------------


class TestResolveTargetUrl:
    async def test_pokepaste_url_canonicalizes(self):
        interaction = _make_interaction()
        cache = InMemoryVRPasteCacheStore()
        result = await dt._resolve_target_url(
            interaction,
            inputs=_inputs(url="http://pokepast.es/abc/"),
            vrpaste_cache=cache,
        )
        assert result == "https://pokepast.es/abc"

    async def test_vrpaste_cache_hit_returns_cached_url(self):
        interaction = _make_interaction()
        cache = InMemoryVRPasteCacheStore()
        cache.create(
            "gxmfscC1",
            "https://pokepast.es/from-cache",
            user_id=1,
            guild_id=1,
        )
        result = await dt._resolve_target_url(
            interaction,
            inputs=_inputs(url="https://www.vrpastes.com/gxmfscC1"),
            vrpaste_cache=cache,
        )
        assert result == "https://pokepast.es/from-cache"

    async def test_vrpaste_cache_miss_refuses(self):
        interaction = _make_interaction()
        cache = InMemoryVRPasteCacheStore()
        result = await dt._resolve_target_url(
            interaction,
            inputs=_inputs(url="https://www.vrpastes.com/unseen"),
            vrpaste_cache=cache,
        )
        assert result is None
        interaction.followup.send.assert_called_once()
        (content,), _ = interaction.followup.send.call_args
        assert "don't have a record" in content
        assert "unseen" in content

    async def test_unknown_url_shape_refuses(self):
        interaction = _make_interaction()
        cache = InMemoryVRPasteCacheStore()
        result = await dt._resolve_target_url(
            interaction,
            inputs=_inputs(url="https://example.com/nope"),
            vrpaste_cache=cache,
        )
        assert result is None
        interaction.followup.send.assert_called_once()
        (content,), _ = interaction.followup.send.call_args
        assert "Pokepaste or VRPaste URL" in content


# --- _delete_and_announce --------------------------------------------------


def _store_with_broadcast(channel_id: int | None = None) -> StaticGuildConfigStore:
    return StaticGuildConfigStore(
        {222: GuildConfig(spreadsheet_id="ssid", broadcast_channel_id=channel_id)}
    )


class TestDeleteAndAnnounce:
    async def test_happy_path_url_with_broadcast(self):
        interaction = _make_interaction()
        broadcast_channel = _make_channel(555)
        interaction.client.get_channel.return_value = broadcast_channel
        row = _row(row_number=8)
        sheets = AsyncMock(spec=SheetsClient)
        sheets.delete_by_url.return_value = row
        store = _store_with_broadcast(555)

        await dt._delete_and_announce(
            interaction,
            sheets,
            store=store,
            inputs=_inputs(url="https://pokepast.es/abc"),
            target_url="https://pokepast.es/abc",
        )

        sheets.delete_by_url.assert_called_once_with(
            "Regulation M-A", "https://pokepast.es/abc"
        )
        sheets.delete_by_replica.assert_not_called()
        sheets.invalidate_snapshot.assert_called_once_with("Regulation M-A")

        interaction.followup.send.assert_called_once()
        (success_content,), _ = interaction.followup.send.call_args
        assert "Removed row 8" in success_content
        assert "team desc" in success_content

        broadcast_channel.send.assert_called_once()
        embed = broadcast_channel.send.call_args.kwargs["embed"]
        assert embed.title == "Team removed from Reg M-A"
        assert embed.description == "team desc"
        species_field = next((f for f in embed.fields if f.name == "Pokémon"), None)
        assert species_field is not None
        assert "Charizard" in species_field.value

    async def test_happy_path_replica(self):
        interaction = _make_interaction()
        row = _row(row_number=9)
        sheets = AsyncMock(spec=SheetsClient)
        sheets.delete_by_replica.return_value = row
        store = _store_with_broadcast(None)

        await dt._delete_and_announce(
            interaction,
            sheets,
            store=store,
            inputs=_inputs(replica="QBXXWXL05U"),
            target_url=None,
        )
        sheets.delete_by_replica.assert_called_once_with("Regulation M-A", "QBXXWXL05U")
        sheets.delete_by_url.assert_not_called()
        sheets.invalidate_snapshot.assert_called_once_with("Regulation M-A")

        interaction.followup.send.assert_called_once()
        (content,), _ = interaction.followup.send.call_args
        assert "Removed row 9" in content

    async def test_team_not_found_skips_broadcast(self):
        interaction = _make_interaction()
        broadcast_channel = _make_channel(555)
        interaction.client.get_channel.return_value = broadcast_channel
        sheets = AsyncMock(spec=SheetsClient)
        sheets.delete_by_url.side_effect = TeamNotFoundError("https://pokepast.es/abc")
        store = _store_with_broadcast(555)

        await dt._delete_and_announce(
            interaction,
            sheets,
            store=store,
            inputs=_inputs(url="https://pokepast.es/abc"),
            target_url="https://pokepast.es/abc",
        )
        sheets.invalidate_snapshot.assert_not_called()
        broadcast_channel.send.assert_not_called()
        interaction.followup.send.assert_called_once()
        (content,), kwargs = interaction.followup.send.call_args
        assert "No team matching" in content
        assert "`https://pokepast.es/abc`" in content
        assert "Reg M-A" in content
        assert kwargs["ephemeral"] is True

    async def test_row_shifted_skips_broadcast(self):
        interaction = _make_interaction()
        broadcast_channel = _make_channel(555)
        interaction.client.get_channel.return_value = broadcast_channel
        sheets = AsyncMock(spec=SheetsClient)
        sheets.delete_by_url.side_effect = RowShiftedError(8)
        store = _store_with_broadcast(555)

        await dt._delete_and_announce(
            interaction,
            sheets,
            store=store,
            inputs=_inputs(url="https://pokepast.es/abc"),
            target_url="https://pokepast.es/abc",
        )
        sheets.invalidate_snapshot.assert_not_called()
        broadcast_channel.send.assert_not_called()
        interaction.followup.send.assert_called_once()
        (content,), kwargs = interaction.followup.send.call_args
        assert "sheet shifted" in content
        assert kwargs["ephemeral"] is True

    async def test_transport_error_sends_generic_delete_error(self):
        interaction = _make_interaction()
        broadcast_channel = _make_channel(555)
        interaction.client.get_channel.return_value = broadcast_channel
        sheets = AsyncMock(spec=SheetsClient)
        sheets.delete_by_url.side_effect = RuntimeError("503")
        store = _store_with_broadcast(555)

        await dt._delete_and_announce(
            interaction,
            sheets,
            store=store,
            inputs=_inputs(url="https://pokepast.es/abc"),
            target_url="https://pokepast.es/abc",
        )
        sheets.invalidate_snapshot.assert_not_called()
        broadcast_channel.send.assert_not_called()
        interaction.followup.send.assert_called_once()
        (content,), kwargs = interaction.followup.send.call_args
        assert content == GENERIC_SHEET_DELETE_ERROR
        assert kwargs["ephemeral"] is True

    async def test_no_broadcast_when_channel_unset(self):
        interaction = _make_interaction()
        broadcast_channel = _make_channel(555)
        interaction.client.get_channel.return_value = broadcast_channel
        row = _row()
        sheets = AsyncMock(spec=SheetsClient)
        sheets.delete_by_url.return_value = row
        store = _store_with_broadcast(None)  # no broadcast channel configured

        await dt._delete_and_announce(
            interaction,
            sheets,
            store=store,
            inputs=_inputs(url="https://pokepast.es/abc"),
            target_url="https://pokepast.es/abc",
        )
        interaction.followup.send.assert_called_once()  # success ephemeral only
        broadcast_channel.send.assert_not_called()  # broadcast channel unset

    async def test_empty_species_omits_pokemon_field(self):
        interaction = _make_interaction()
        broadcast_channel = _make_channel(555)
        interaction.client.get_channel.return_value = broadcast_channel
        sheets = AsyncMock(spec=SheetsClient)
        sheets.delete_by_url.return_value = _row(species=[])
        store = _store_with_broadcast(555)

        await dt._delete_and_announce(
            interaction,
            sheets,
            store=store,
            inputs=_inputs(url="https://pokepast.es/abc"),
            target_url="https://pokepast.es/abc",
        )
        embed = broadcast_channel.send.call_args.kwargs["embed"]
        assert all(f.name != "Pokémon" for f in embed.fields)


# --- handler-level routing --------------------------------------------------


class TestBothSuppliedPrefersUrl:
    """When both `url` and `replica` are supplied, only delete_by_url is used."""

    async def test_url_wins_over_replica(self):
        interaction = _make_interaction()
        cache = InMemoryVRPasteCacheStore()

        # _resolve_target_url should return the URL, ignoring replica.
        target_url = await dt._resolve_target_url(
            interaction,
            inputs=_inputs(url="https://pokepast.es/abc", replica="QBXXWXL05U"),
            vrpaste_cache=cache,
        )
        assert target_url == "https://pokepast.es/abc"

        # _delete_and_announce should call delete_by_url, not delete_by_replica.
        row = _row()
        sheets = AsyncMock(spec=SheetsClient)
        sheets.delete_by_url.return_value = row
        store = _store_with_broadcast(None)

        await dt._delete_and_announce(
            interaction,
            sheets,
            store=store,
            inputs=_inputs(url="https://pokepast.es/abc", replica="QBXXWXL05U"),
            target_url=target_url,
        )
        sheets.delete_by_url.assert_called()
        sheets.delete_by_replica.assert_not_called()
