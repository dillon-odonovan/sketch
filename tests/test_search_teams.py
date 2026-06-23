"""Tests for the `/search-teams` handler's format defaulting.

Captures the registered callback via a minimal CommandTree stub (the
test_convert_ots_command.py pattern) and drives it directly, so we exercise
the real defaulting without standing up Discord.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from sketch import config
from sketch.commands import search_teams


def _capture_callback(registry):
    captured: dict[str, object] = {}

    class _Tree:
        def command(self, **_kwargs):
            def deco(fn):
                captured["fn"] = fn
                return fn

            return deco

    search_teams.register(_Tree(), registry)
    return captured["fn"]


def _make_interaction() -> MagicMock:
    interaction = MagicMock()
    interaction.user.id = 111
    interaction.guild_id = 222
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


class TestSearchFormatDefaulting:
    async def test_omitted_format_searches_the_default_regulation(self):
        registry = MagicMock()
        callback = _capture_callback(registry)
        interaction = _make_interaction()

        sheets = MagicMock()
        # Unique-ID (replica) path: only needs the snapshot, no DEX. Empty
        # rows guarantee zero matches and the no-result branch.
        snapshot = MagicMock()
        snapshot.rows = []
        sheets.get_search_snapshot = AsyncMock(return_value=snapshot)

        with patch.object(
            search_teams, "_resolve_guild_sheets", AsyncMock(return_value=sheets)
        ):
            # format omitted -> defaults to the current regulation.
            await callback(interaction, replica="QBXXWXL05U")

        # The snapshot was read from the defaulted regulation's sheet.
        sheets.get_search_snapshot.assert_awaited_once_with(
            config.FORMAT_SHEETS[config.DEFAULT_FORMAT]
        )
        interaction.followup.send.assert_awaited_once()
        (content,), kwargs = interaction.followup.send.call_args
        assert "No teams found" in content
        assert config.DEFAULT_FORMAT in content
        assert kwargs["ephemeral"] is True
