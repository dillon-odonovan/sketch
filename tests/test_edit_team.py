"""End-to-end test for the `/edit-team` handler helpers.

Drives the internals (`_normalize_inputs`, `_edit_and_announce`) directly
with a fake interaction and a stub SheetsClient. Mirrors
`tests/test_delete_team.py`'s pattern — avoids standing up CommandTree /
app_commands plumbing for the handler itself.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from discord import app_commands

from sketch import config
from sketch.commands import edit_team as et
from sketch.commands._shared import GENERIC_SHEET_UPDATE_ERROR
from sketch.storage.guild_config import GuildConfig, StaticGuildConfigStore
from sketch.storage.sheets_client import (
    RowShiftedError,
    SheetsClient,
    TeamNotFoundError,
    TeamRow,
)


def _make_interaction() -> MagicMock:
    interaction = MagicMock()
    interaction.user.id = 111
    interaction.user.display_name = "tester"
    interaction.user.display_avatar.url = "https://cdn.example/test.png"
    interaction.guild_id = 222
    interaction.followup.send = AsyncMock()
    return interaction


def _make_channel(channel_id: int = 555) -> AsyncMock:
    channel = AsyncMock()
    channel.id = channel_id
    return channel


def _row(
    *,
    row_number: int = 4,
    url: str = "https://pokepast.es/abc",
    description: str = "new team desc",
) -> TeamRow:
    return TeamRow(row_number=row_number, url=url, description=description, species=[])


def _inputs(
    *,
    url: str | None = None,
    replica: str | None = None,
    description: str | None = None,
    paste_type: str | None = None,
) -> et._EditTeamInputs:
    return et._EditTeamInputs(
        fmt_name="Reg M-A",
        sheet_name="Regulation M-A",
        url=url,
        replica=replica,
        description=description,
        paste_type=paste_type,
    )


def _choice(value: str = "Reg M-A") -> app_commands.Choice[str]:
    return app_commands.Choice(name=value, value=value)


def _paste_type_choice(value: str = "Recreated") -> app_commands.Choice[str]:
    return app_commands.Choice(name=value, value=value)


# --- _normalize_inputs ------------------------------------------------------


class TestNormalizeInputs:
    async def test_requires_url_or_replica(self):
        interaction = _make_interaction()
        result = await et._normalize_inputs(
            interaction,
            format_choice=_choice(),
            url=None,
            replica=None,
            description="new desc",
            paste_type=None,
        )
        assert result is None
        interaction.followup.send.assert_called_once()
        (content,), kwargs = interaction.followup.send.call_args
        assert "Pokepaste/VRPaste URL" in content
        assert kwargs["ephemeral"] is True

    async def test_requires_description_or_paste_type(self):
        interaction = _make_interaction()
        result = await et._normalize_inputs(
            interaction,
            format_choice=_choice(),
            url="https://pokepast.es/abc",
            replica=None,
            description=None,
            paste_type=None,
        )
        assert result is None
        interaction.followup.send.assert_called_once()
        (content,), kwargs = interaction.followup.send.call_args
        assert "nothing to change" in content
        assert kwargs["ephemeral"] is True

    async def test_normalizes_replica_to_upper(self):
        interaction = _make_interaction()
        result = await et._normalize_inputs(
            interaction,
            format_choice=_choice(),
            url=None,
            replica="qbxxwxl05u",
            description="new desc",
            paste_type=None,
        )
        assert result is not None
        assert result.replica == "QBXXWXL05U"

    async def test_malformed_replica_returns_error(self):
        interaction = _make_interaction()
        result = await et._normalize_inputs(
            interaction,
            format_choice=_choice(),
            url=None,
            replica="too-short",
            description="new desc",
            paste_type=None,
        )
        assert result is None
        interaction.followup.send.assert_called_once()

    async def test_omitted_format_defaults_to_current_regulation(self):
        interaction = _make_interaction()
        result = await et._normalize_inputs(
            interaction,
            format_choice=None,
            url="https://pokepast.es/abc",
            replica=None,
            description="new desc",
            paste_type=None,
        )
        assert result is not None
        assert result.fmt_name == config.DEFAULT_FORMAT
        assert result.sheet_name == config.FORMAT_SHEETS[config.DEFAULT_FORMAT]

    async def test_paste_type_choice_unwrapped_to_value(self):
        interaction = _make_interaction()
        result = await et._normalize_inputs(
            interaction,
            format_choice=_choice(),
            url="https://pokepast.es/abc",
            replica=None,
            description=None,
            paste_type=_paste_type_choice("Recreated"),
        )
        assert result is not None
        assert result.paste_type == "Recreated"


# --- _edit_and_announce -------------------------------------------------


def _store_with_broadcast(channel_id: int | None = None) -> StaticGuildConfigStore:
    return StaticGuildConfigStore(
        {222: GuildConfig(spreadsheet_id="ssid", broadcast_channel_id=channel_id)}
    )


class TestEditAndAnnounce:
    async def test_happy_path_url_with_broadcast(self):
        interaction = _make_interaction()
        broadcast_channel = _make_channel(555)
        interaction.client.get_channel.return_value = broadcast_channel
        row = _row(row_number=8, description="fixed desc")
        sheets = AsyncMock(spec=SheetsClient)
        sheets.update_by_url.return_value = row
        store = _store_with_broadcast(555)

        await et._edit_and_announce(
            interaction,
            sheets,
            store=store,
            inputs=_inputs(url="https://pokepast.es/abc", description="fixed desc"),
            target_url="https://pokepast.es/abc",
        )

        sheets.update_by_url.assert_called_once_with(
            "Regulation M-A",
            "https://pokepast.es/abc",
            description="fixed desc",
            paste_type=None,
        )
        sheets.update_by_replica.assert_not_called()
        sheets.invalidate_snapshot.assert_called_once_with("Regulation M-A")

        interaction.followup.send.assert_called_once()
        (content,), _ = interaction.followup.send.call_args
        assert "Updated row 8" in content
        assert "fixed desc" in content

        broadcast_channel.send.assert_called_once()
        embed = broadcast_channel.send.call_args.kwargs["embed"]
        assert embed.title == "Team updated in Reg M-A"
        assert embed.description == "fixed desc"

    async def test_happy_path_replica_paste_type_only(self):
        interaction = _make_interaction()
        row = _row(row_number=9)
        sheets = AsyncMock(spec=SheetsClient)
        sheets.update_by_replica.return_value = row
        store = _store_with_broadcast(None)

        await et._edit_and_announce(
            interaction,
            sheets,
            store=store,
            inputs=_inputs(replica="QBXXWXL05U", paste_type="Recreated"),
            target_url=None,
        )
        sheets.update_by_replica.assert_called_once_with(
            "Regulation M-A",
            "QBXXWXL05U",
            description=None,
            paste_type="Recreated",
        )
        sheets.update_by_url.assert_not_called()
        sheets.invalidate_snapshot.assert_called_once_with("Regulation M-A")

        interaction.followup.send.assert_called_once()
        (content,), _ = interaction.followup.send.call_args
        assert "Updated row 9" in content
        assert "paste type to Recreated" in content

    async def test_team_not_found_skips_broadcast(self):
        interaction = _make_interaction()
        broadcast_channel = _make_channel(555)
        interaction.client.get_channel.return_value = broadcast_channel
        sheets = AsyncMock(spec=SheetsClient)
        sheets.update_by_url.side_effect = TeamNotFoundError("https://pokepast.es/abc")
        store = _store_with_broadcast(555)

        await et._edit_and_announce(
            interaction,
            sheets,
            store=store,
            inputs=_inputs(url="https://pokepast.es/abc", description="fixed desc"),
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
        sheets.update_by_url.side_effect = RowShiftedError(8)
        store = _store_with_broadcast(555)

        await et._edit_and_announce(
            interaction,
            sheets,
            store=store,
            inputs=_inputs(url="https://pokepast.es/abc", description="fixed desc"),
            target_url="https://pokepast.es/abc",
        )
        sheets.invalidate_snapshot.assert_not_called()
        broadcast_channel.send.assert_not_called()
        interaction.followup.send.assert_called_once()
        (content,), kwargs = interaction.followup.send.call_args
        assert "sheet shifted" in content
        assert kwargs["ephemeral"] is True

    async def test_transport_error_sends_generic_update_error(self):
        interaction = _make_interaction()
        broadcast_channel = _make_channel(555)
        interaction.client.get_channel.return_value = broadcast_channel
        sheets = AsyncMock(spec=SheetsClient)
        sheets.update_by_url.side_effect = RuntimeError("503")
        store = _store_with_broadcast(555)

        await et._edit_and_announce(
            interaction,
            sheets,
            store=store,
            inputs=_inputs(url="https://pokepast.es/abc", description="fixed desc"),
            target_url="https://pokepast.es/abc",
        )
        sheets.invalidate_snapshot.assert_not_called()
        broadcast_channel.send.assert_not_called()
        interaction.followup.send.assert_called_once()
        (content,), kwargs = interaction.followup.send.call_args
        assert content == GENERIC_SHEET_UPDATE_ERROR
        assert kwargs["ephemeral"] is True

    async def test_no_broadcast_when_channel_unset(self):
        interaction = _make_interaction()
        broadcast_channel = _make_channel(555)
        interaction.client.get_channel.return_value = broadcast_channel
        row = _row()
        sheets = AsyncMock(spec=SheetsClient)
        sheets.update_by_url.return_value = row
        store = _store_with_broadcast(None)  # no broadcast channel configured

        await et._edit_and_announce(
            interaction,
            sheets,
            store=store,
            inputs=_inputs(url="https://pokepast.es/abc", description="fixed desc"),
            target_url="https://pokepast.es/abc",
        )
        interaction.followup.send.assert_called_once()  # success ephemeral only
        broadcast_channel.send.assert_not_called()  # broadcast channel unset


# --- handler-level routing --------------------------------------------------


class TestBothSuppliedPrefersUrl:
    """When both `url` and `replica` are supplied, only update_by_url is used."""

    async def test_url_wins_over_replica(self):
        interaction = _make_interaction()
        row = _row()
        sheets = AsyncMock(spec=SheetsClient)
        sheets.update_by_url.return_value = row
        store = _store_with_broadcast(None)

        await et._edit_and_announce(
            interaction,
            sheets,
            store=store,
            inputs=_inputs(
                url="https://pokepast.es/abc",
                replica="QBXXWXL05U",
                description="fixed desc",
            ),
            target_url="https://pokepast.es/abc",
        )
        sheets.update_by_url.assert_called()
        sheets.update_by_replica.assert_not_called()
