"""`/edit-team` — fix a team row's description and/or paste type in place.

Accepts a Pokepaste URL, a VRPaste URL (resolved through the VRPaste cache
to its minted Pokepaste URL), or a Replica code to identify the row — same
lookup as `/delete-team`, `url` wins when both are supplied. At least one of
`description` / `paste_type` must be given to actually change something.

URL and Replica code themselves are NOT editable here — changing either
would require re-running URL dedup, re-polling species, and reconciling the
global Replica/VRPaste caches. Use `/delete-team` + `/add-team` for that.

No Confirm/Cancel gate, matching `/delete-team`'s precedent: the broadcast
to the configured channel is the public signal so anyone who disagrees can
raise it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import discord
from discord import app_commands

from sketch import config
from sketch.champions.replica_validator import normalize_replica
from sketch.commands._shared import (
    GENERIC_SHEET_UPDATE_ERROR,
    _broadcast_team_updated,
    _format_choices,
    _paste_type_choices,
    _resolve_format,
    _resolve_guild_sheets,
    _resolve_target_url,
    _with_trace,
)
from sketch.logging_setup import trace_id_var
from sketch.pokepaste.validator import ValidationError
from sketch.storage.guild_config import GuildConfigStore
from sketch.storage.sheets_client import (
    RowShiftedError,
    SheetsClient,
    SheetsClientRegistry,
    TeamNotFoundError,
)
from sketch.vrpaste.cache import VRPasteCacheStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _EditTeamInputs:
    fmt_name: str
    sheet_name: str
    url: str | None
    replica: str | None  # already normalized via normalize_replica
    description: str | None
    paste_type: str | None


async def _normalize_inputs(
    interaction: discord.Interaction,
    *,
    format_choice: app_commands.Choice[str] | None,
    url: str | None,
    replica: str | None,
    description: str | None,
    paste_type: app_commands.Choice[str] | None,
) -> _EditTeamInputs | None:
    if url is None and replica is None:
        await interaction.followup.send(
            _with_trace(
                "Provide a **Pokepaste/VRPaste URL** or a **Champions Team ID** "
                "to identify the team to edit (or both). At least one is required."
            ),
            ephemeral=True,
        )
        return None

    if description is None and paste_type is None:
        await interaction.followup.send(
            _with_trace(
                "Provide a new **description** and/or **paste type** — "
                "there's nothing to change otherwise."
            ),
            ephemeral=True,
        )
        return None

    normalized_replica: str | None = None
    if replica is not None:
        try:
            normalized_replica = normalize_replica(replica)
        except ValidationError as e:
            await interaction.followup.send(_with_trace(str(e)), ephemeral=True)
            return None

    fmt_name = _resolve_format(format_choice)
    return _EditTeamInputs(
        fmt_name=fmt_name,
        sheet_name=config.FORMAT_SHEETS[fmt_name],
        url=url,
        replica=normalized_replica,
        description=description,
        paste_type=paste_type.value if paste_type is not None else None,
    )


async def _edit_and_announce(
    interaction: discord.Interaction,
    sheets: SheetsClient,
    *,
    store: GuildConfigStore,
    inputs: _EditTeamInputs,
    target_url: str | None,
) -> None:
    """Look up the target row, update it, and broadcast the change.

    Uses `SheetsClient.update_by_url` or `update_by_replica` so the lookup
    and the compare-and-swap update are a single call. Mirrors
    `_delete_and_announce` in `delete_team.py`.
    """
    try:
        if target_url is not None:
            row = await sheets.update_by_url(
                inputs.sheet_name,
                target_url,
                description=inputs.description,
                paste_type=inputs.paste_type,
            )
        else:
            assert inputs.replica is not None
            row = await sheets.update_by_replica(
                inputs.sheet_name,
                inputs.replica,
                description=inputs.description,
                paste_type=inputs.paste_type,
            )
    except TeamNotFoundError:
        key = f"`{target_url}`" if target_url is not None else f"`{inputs.replica}`"
        logger.info(
            "edit-team: no matching row for key=%s in sheet=%s guild_id=%s",
            key,
            inputs.sheet_name,
            interaction.guild_id,
        )
        await interaction.followup.send(
            _with_trace(f"No team matching {key} found in *{inputs.fmt_name}*."),
            ephemeral=True,
        )
        return
    except RowShiftedError:
        logger.warning(
            "edit-team: CAS guard fired for url=%s replica=%s "
            "in sheet=%s guild_id=%s — row shifted by concurrent delete/edit",
            target_url,
            inputs.replica,
            inputs.sheet_name,
            interaction.guild_id,
        )
        await interaction.followup.send(
            _with_trace(
                "The sheet shifted under us before we could update that row — "
                "please run the command again."
            ),
            ephemeral=True,
        )
        return
    except Exception:
        logger.exception(
            "Failed to update team in %s (url=%s replica=%s)",
            inputs.sheet_name,
            target_url,
            inputs.replica,
        )
        await interaction.followup.send(
            _with_trace(GENERIC_SHEET_UPDATE_ERROR), ephemeral=True
        )
        return

    # The description feeds the search index directly, and even a
    # paste-type-only edit should drop the stale cached row so a rebuild
    # picks up any other change made concurrently.
    sheets.invalidate_snapshot(inputs.sheet_name)

    changed = []
    if inputs.description is not None:
        changed.append(f'description to "{inputs.description}"')
    if inputs.paste_type is not None:
        changed.append(f"paste type to {inputs.paste_type}")
    await interaction.followup.send(
        f"Updated row {row.row_number} in *{inputs.fmt_name}*: "
        f"set {' and '.join(changed)}.",
        ephemeral=True,
    )

    guild_cfg = (
        store.get(interaction.guild_id) if interaction.guild_id is not None else None
    )
    if guild_cfg and guild_cfg.broadcast_channel_id is not None:
        await _broadcast_team_updated(
            interaction,
            guild_cfg.broadcast_channel_id,
            fmt_name=inputs.fmt_name,
            url=row.url,
            description=row.description,
        )
    else:
        logger.info(
            "Skipping edit broadcast for guild_id=%s: no broadcast channel set",
            interaction.guild_id,
        )


def register(
    tree: app_commands.CommandTree,
    store: GuildConfigStore,
    registry: SheetsClientRegistry,
    *,
    vrpaste_cache: VRPasteCacheStore,
) -> None:
    """Register the /edit-team slash command on the given tree."""

    @tree.command(
        name="edit-team",
        description=(
            "Fix a team's description and/or paste type — identify it by URL "
            "or Team ID."
        ),
    )
    @app_commands.describe(
        format=f"Format/regulation. Defaults to {config.DEFAULT_FORMAT} if omitted.",
        url=(
            "Pokepaste URL (e.g., https://pokepast.es/abc123) or VRPaste "
            "URL of the team to edit. Required unless you provide a Team "
            "ID instead."
        ),
        replica=(
            "10-character Champions Team ID (e.g. 'QBXXWXL05U') of the team "
            "to edit. Required unless you provide a URL instead."
        ),
        description="New description for the team, if you want to change it.",
        paste_type="New paste-type tag for the team, if you want to change it.",
    )
    @app_commands.choices(
        format=_format_choices(),
        paste_type=_paste_type_choices(),
    )
    async def edit_team(
        interaction: discord.Interaction,
        format: app_commands.Choice[str] | None = None,
        url: str | None = None,
        replica: str | None = None,
        description: str | None = None,
        paste_type: app_commands.Choice[str] | None = None,
    ) -> None:
        trace_id_var.set(str(interaction.id))
        await interaction.response.defer(ephemeral=True, thinking=True)

        sheets = await _resolve_guild_sheets(interaction, registry)
        if sheets is None:
            return

        inputs = await _normalize_inputs(
            interaction,
            format_choice=format,
            url=url,
            replica=replica,
            description=description,
            paste_type=paste_type,
        )
        if inputs is None:
            return

        logger.info(
            "edit-team invoked by user_id=%s guild_id=%s: "
            "url=%s replica=%s format=%s description=%s paste_type=%s",
            interaction.user.id,
            interaction.guild_id,
            inputs.url,
            inputs.replica,
            inputs.fmt_name,
            inputs.description,
            inputs.paste_type,
        )

        target_url: str | None = None
        if inputs.url is not None:
            target_url = await _resolve_target_url(
                interaction, url=inputs.url, vrpaste_cache=vrpaste_cache
            )
            if target_url is None:
                return

        await _edit_and_announce(
            interaction,
            sheets,
            store=store,
            inputs=inputs,
            target_url=target_url,
        )
