"""Admin slash commands — guild configuration self-service.

The five commands here let server admins configure Sketch without a bot
operator running `bin/seed_guilds.py`. They all share two decorators:

  @default_permissions(manage_guild=True)
    Discord's "Manage Server" gate. Server owners can further restrict via
    Server Settings → Integrations → <bot> → command permissions.

  @guild_only()
    Marks the command `dm_permission=False` in Discord's app-command
    registry, so the Discord client hides it from the picker in DMs and
    refuses delivery if a user types it there. Per-handler
    `if guild_id is None: refuse` is defense in depth for the rare case
    where a stale registration leaks past the client guard.

`/spreadsheet-link` is the lone exception — it's user-facing (no Manage
Server gate) so anyone in the server can grab the sheet URL. Still
`guild_only` because it'd be meaningless in a DM.
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands

from sketch import config
from sketch.commands._shared import (
    _SPREADSHEET_ID_RE,
    GUILD_ONLY_ERROR,
    UNCONFIGURED_GUILD_ERROR,
    _spreadsheet_link,
    _with_trace,
)
from sketch.logging_setup import trace_id_var
from sketch.storage.guild_config import GuildConfigStore
from sketch.storage.sheets_client import SheetsClientRegistry

logger = logging.getLogger(__name__)


def register(
    tree: app_commands.CommandTree,
    store: GuildConfigStore,
    registry: SheetsClientRegistry,
) -> None:
    """Register all admin slash commands on the given tree."""

    @tree.command(
        name="register-sheet",
        description=(
            "Register (or replace) the Google Sheet this server writes to. Admin only."
        ),
    )
    @app_commands.describe(
        spreadsheet_id=(
            "Google Sheets ID — the part of the URL between /d/ and /edit."
        ),
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def register_sheet(
        interaction: discord.Interaction, spreadsheet_id: str
    ) -> None:
        trace_id_var.set(str(interaction.id))
        # Probe (spreadsheets().get) can take 200-500ms — defer first so we
        # don't blow the 3s ACK budget. Ephemeral so the invoker doesn't
        # leak the sheet ID into the channel.
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.followup.send(
                _with_trace(GUILD_ONLY_ERROR), ephemeral=True
            )
            return

        spreadsheet_id = spreadsheet_id.strip()
        logger.info(
            "register-sheet invoked by user_id=%s guild_id=%s: spreadsheet_id=%s",
            interaction.user.id,
            guild_id,
            spreadsheet_id,
        )

        if not _SPREADSHEET_ID_RE.match(spreadsheet_id):
            await interaction.followup.send(
                _with_trace(
                    "That doesn't look like a Google Sheets ID. Paste just the "
                    "ID portion of the URL — the part between `/d/` and `/edit` "
                    "(letters, digits, `_`, and `-` only)."
                ),
                ephemeral=True,
            )
            return

        # Probe before writing: catch the most common first-install mistake
        # (sheet not shared with the bot's service account) and the
        # second-most-common (typo in the ID) without leaving the guild's
        # config pointing at a sheet the bot can't read. Probe client is
        # one-off — not cached on the registry, since it might be invalid.
        probe = registry.build_probe_client(spreadsheet_id)
        try:
            tabs = await probe.list_tab_names()
        except Exception:
            logger.warning(
                "register-sheet probe failed for guild_id=%s spreadsheet_id=%s",
                guild_id,
                spreadsheet_id,
                exc_info=True,
            )
            await interaction.followup.send(
                _with_trace(
                    f"Couldn't open <{_spreadsheet_link(spreadsheet_id)}>. Check "
                    "that:\n"
                    "1. The ID is correct (no extra characters from the URL).\n"
                    "2. The sheet is shared with the bot's service account as "
                    "**Editor** — ask the bot owner for the address if you don't "
                    "have it.\n"
                    "3. The sheet exists and isn't in the trash."
                ),
                ephemeral=True,
            )
            return

        # TODO: re-evaluate once the per-sheet format-set work lands. Today
        # config.FORMAT_SHEETS is a single global {format -> tab} map, so
        # every registered sheet must carry every tab. Once Sheet 1 and
        # Sheet 2 can advertise different format sets (e.g. Sheet 1: "Reg
        # F"/"Reg I"; Sheet 2: "Reg I"/"Reg M-A"), this rigid all-or-nothing
        # check will reject legitimate sheets and needs to flip to "at least
        # one known format tab exists" (or whatever the new contract is).
        missing_tabs = [t for t in config.FORMAT_SHEETS.values() if t not in tabs]
        if missing_tabs:
            await interaction.followup.send(
                _with_trace(
                    "The sheet opened, but it's missing the expected tab(s): "
                    f"{', '.join(f'`{t}`' for t in missing_tabs)}. Add the tab(s) "
                    "(or use a copy of the canonical TeamBank Parser template) "
                    "and try again."
                ),
                ephemeral=True,
            )
            return

        # Order matters: write through the store first, then drop the cached
        # client. If invalidate ran first, a racing /add-team could rebuild
        # the cache against the *old* spreadsheet_id (since the store still
        # returns it) and pin the wrong client. Doing the store write first
        # means the rebuilt client always reads the new ID.
        try:
            new_cfg = await asyncio.to_thread(
                store.set_spreadsheet_id, guild_id, spreadsheet_id
            )
        except Exception:
            logger.exception(
                "Failed to persist spreadsheet_id for guild_id=%s", guild_id
            )
            await interaction.followup.send(
                _with_trace(
                    "Couldn't save that to the bot's config right now — please "
                    "try again in a moment."
                ),
                ephemeral=True,
            )
            return
        registry.invalidate(guild_id)

        broadcast_note = (
            f" Broadcast channel <#{new_cfg.broadcast_channel_id}> is unchanged."
            if new_cfg.broadcast_channel_id is not None
            else " No broadcast channel is set — use "
            "`/set-broadcast-channel` if you want `/add-team` announcements."
        )
        await interaction.followup.send(
            f"Registered <{_spreadsheet_link(spreadsheet_id)}> for this "
            f"server.{broadcast_note}",
            ephemeral=True,
        )

    @tree.command(
        name="set-broadcast-channel",
        description=(
            "Announce every successful /add-team in this channel. Admin only."
        ),
    )
    @app_commands.describe(
        channel=(
            "Channel to post `/add-team` announcements to. The bot needs "
            "Send Messages and Embed Links here."
        ),
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def set_broadcast_channel(
        interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        trace_id_var.set(str(interaction.id))
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.followup.send(
                _with_trace(GUILD_ONLY_ERROR), ephemeral=True
            )
            return

        logger.info(
            "set-broadcast-channel invoked by user_id=%s guild_id=%s: channel_id=%s",
            interaction.user.id,
            guild_id,
            channel.id,
        )

        if store.get(guild_id) is None:
            # No spreadsheet configured → /add-team is refused → broadcast
            # would never fire. Refuse rather than silently store an
            # orphaned channel value.
            await interaction.followup.send(
                _with_trace(
                    "This server doesn't have a sheet registered yet. Run "
                    "`/register-sheet` first, then set the broadcast channel."
                ),
                ephemeral=True,
            )
            return

        try:
            await asyncio.to_thread(
                store.set_broadcast_channel_id, guild_id, channel.id
            )
        except Exception:
            logger.exception(
                "Failed to persist broadcast_channel_id for guild_id=%s", guild_id
            )
            await interaction.followup.send(
                _with_trace(
                    "Couldn't save that to the bot's config right now — please "
                    "try again in a moment."
                ),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"`/add-team` announcements will post to {channel.mention}.",
            ephemeral=True,
        )

    @tree.command(
        name="clear-broadcast-channel",
        description="Stop announcing /add-team in this server. Admin only.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def clear_broadcast_channel(interaction: discord.Interaction) -> None:
        trace_id_var.set(str(interaction.id))
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.followup.send(
                _with_trace(GUILD_ONLY_ERROR), ephemeral=True
            )
            return

        logger.info(
            "clear-broadcast-channel invoked by user_id=%s guild_id=%s",
            interaction.user.id,
            guild_id,
        )

        cfg = store.get(guild_id)
        if cfg is None:
            await interaction.followup.send(
                _with_trace(
                    "This server doesn't have a sheet registered yet. Nothing to clear."
                ),
                ephemeral=True,
            )
            return
        if cfg.broadcast_channel_id is None:
            await interaction.followup.send(
                _with_trace(
                    "No broadcast channel is currently set — nothing to clear."
                ),
                ephemeral=True,
            )
            return

        try:
            await asyncio.to_thread(store.clear_broadcast_channel_id, guild_id)
        except Exception:
            logger.exception(
                "Failed to clear broadcast_channel_id for guild_id=%s", guild_id
            )
            await interaction.followup.send(
                _with_trace(
                    "Couldn't update the bot's config right now — please try "
                    "again in a moment."
                ),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            "Cleared the broadcast channel. `/add-team` will no longer post "
            "announcements.",
            ephemeral=True,
        )

    @tree.command(
        name="show-config",
        description="Show this server's Sketch configuration. Admin only.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def show_config(interaction: discord.Interaction) -> None:
        trace_id_var.set(str(interaction.id))
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message(
                _with_trace(GUILD_ONLY_ERROR), ephemeral=True
            )
            return

        logger.info(
            "show-config invoked by user_id=%s guild_id=%s",
            interaction.user.id,
            guild_id,
        )
        cfg = store.get(guild_id)
        if cfg is None:
            await interaction.response.send_message(
                _with_trace(
                    "This server has no Sketch configuration. Run "
                    "`/register-sheet` to get started."
                ),
                ephemeral=True,
            )
            return

        broadcast_line = (
            f"**Broadcast channel:** <#{cfg.broadcast_channel_id}>"
            if cfg.broadcast_channel_id is not None
            else "**Broadcast channel:** _not set_ "
            "(use `/set-broadcast-channel` to enable)"
        )
        await interaction.response.send_message(
            f"**Spreadsheet:** <{_spreadsheet_link(cfg.spreadsheet_id)}>\n"
            f"{broadcast_line}",
            ephemeral=True,
        )

    @tree.command(
        name="spreadsheet-link",
        description="Get a link to this server's team spreadsheet.",
    )
    @app_commands.guild_only()
    async def spreadsheet_link(interaction: discord.Interaction) -> None:
        trace_id_var.set(str(interaction.id))
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message(
                _with_trace(GUILD_ONLY_ERROR), ephemeral=True
            )
            return

        logger.info(
            "spreadsheet-link invoked by user_id=%s guild_id=%s",
            interaction.user.id,
            guild_id,
        )
        cfg = store.get(guild_id)
        if cfg is None:
            await interaction.response.send_message(
                _with_trace(UNCONFIGURED_GUILD_ERROR), ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"Team spreadsheet: <{_spreadsheet_link(cfg.spreadsheet_id)}>",
            ephemeral=True,
        )
