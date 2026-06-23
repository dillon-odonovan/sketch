"""`/delete-team` — remove a team row by URL or Champions Replica code.

Accepts a Pokepaste URL, a VRPaste URL (resolved through the VRPaste cache
to its minted Pokepaste URL), or a Replica code. When both `url` and
`replica` are supplied, `url` wins — mirrors `/add-team`'s precedent.

No Confirm/Cancel gate: the delete fires immediately. The broadcast to the
configured channel is the public signal so anyone who disagrees can re-add.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import discord
from discord import app_commands

from sketch import config
from sketch.champions.replica_validator import normalize_replica
from sketch.commands._shared import (
    GENERIC_CACHE_READ_ERROR,
    GENERIC_SHEET_DELETE_ERROR,
    _broadcast_team_removed,
    _format_choices,
    _resolve_format,
    _resolve_guild_sheets,
    _with_trace,
)
from sketch.logging_setup import trace_id_var
from sketch.pokepaste.validator import (
    ValidationError,
    canonicalize_pokepaste_url,
)
from sketch.storage.guild_config import GuildConfigStore
from sketch.storage.sheets_client import (
    RowShiftedError,
    SheetsClient,
    SheetsClientRegistry,
    TeamNotFoundError,
)
from sketch.teamsource import (
    TeamUrlSource,
    classify_team_url,
    unsupported_team_url_message,
)
from sketch.vrpaste.cache import VRPasteCacheStore, lookup_pokepaste_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _DeleteTeamInputs:
    fmt_name: str
    sheet_name: str
    url: str | None
    replica: str | None  # already normalized via normalize_replica


async def _normalize_inputs(
    interaction: discord.Interaction,
    *,
    format_choice: app_commands.Choice[str] | None,
    url: str | None,
    replica: str | None,
) -> _DeleteTeamInputs | None:
    if url is None and replica is None:
        await interaction.followup.send(
            _with_trace(
                "Provide a **Pokepaste/VRPaste URL** or a **Champions Team ID** "
                "(or both). At least one is required."
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
    return _DeleteTeamInputs(
        fmt_name=fmt_name,
        sheet_name=config.FORMAT_SHEETS[fmt_name],
        url=url,
        replica=normalized_replica,
    )


async def _resolve_target_url(
    interaction: discord.Interaction,
    *,
    inputs: _DeleteTeamInputs,
    vrpaste_cache: VRPasteCacheStore,
) -> str | None:
    """Turn `inputs.url` into the canonical Pokepaste URL stored in the sheet.

    Explicit switch over URL shape — VRPaste / Pokepaste / unknown — so a
    third source can be added without touching the implicit "else assume
    Pokepaste" branch.

    Returns the canonical URL on success, `None` if the user already got
    an ephemeral response (cache miss, malformed URL, transport error).
    Callers that only have a replica skip this helper entirely.
    """
    assert inputs.url is not None

    kind = classify_team_url(inputs.url)
    if kind is TeamUrlSource.VRPASTE:
        try:
            resolved = await asyncio.to_thread(
                lookup_pokepaste_url, inputs.url, vrpaste_cache
            )
        except ValidationError as e:
            await interaction.followup.send(_with_trace(str(e)), ephemeral=True)
            return None
        except Exception:
            logger.exception("VRPaste cache read failed for url=%s", inputs.url)
            await interaction.followup.send(
                _with_trace(GENERIC_CACHE_READ_ERROR), ephemeral=True
            )
            return None
        if resolved is None:
            await interaction.followup.send(
                _with_trace(
                    f"We don't have a record of `{inputs.url}` — that team isn't "
                    "in the sheet. If you know the Pokepaste URL, submit that "
                    "directly."
                ),
                ephemeral=True,
            )
            return None
        return resolved

    if kind is TeamUrlSource.POKEPASTE:
        # is_pokepaste_url and canonicalize share one regex, so this
        # won't raise once the URL is classified as a Pokepaste.
        return canonicalize_pokepaste_url(inputs.url)

    await interaction.followup.send(
        _with_trace(unsupported_team_url_message(inputs.url)),
        ephemeral=True,
    )
    return None


async def _delete_and_announce(
    interaction: discord.Interaction,
    sheets: SheetsClient,
    *,
    store: GuildConfigStore,
    inputs: _DeleteTeamInputs,
    target_url: str | None,
) -> None:
    """Look up the target row, delete it, and broadcast the removal.

    Uses `SheetsClient.delete_by_url` or `delete_by_replica` so the lookup
    and the compare-and-swap delete are a single call. Returns `None` (the
    row didn't exist) or the deleted `TeamRow` (used for the broadcast embed).
    """
    try:
        if target_url is not None:
            row = await sheets.delete_by_url(inputs.sheet_name, target_url)
        else:
            assert inputs.replica is not None
            row = await sheets.delete_by_replica(inputs.sheet_name, inputs.replica)
    except TeamNotFoundError:
        key = f"`{target_url}`" if target_url is not None else f"`{inputs.replica}`"
        logger.info(
            "delete-team: no matching row for key=%s in sheet=%s guild_id=%s",
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
            "delete-team: CAS guard fired for url=%s replica=%s "
            "in sheet=%s guild_id=%s — row shifted by concurrent delete",
            target_url,
            inputs.replica,
            inputs.sheet_name,
            interaction.guild_id,
        )
        await interaction.followup.send(
            _with_trace(
                "The sheet shifted under us before we could delete that row — "
                "please run the command again."
            ),
            ephemeral=True,
        )
        return
    except Exception:
        logger.exception(
            "Failed to delete team in %s (url=%s replica=%s)",
            inputs.sheet_name,
            target_url,
            inputs.replica,
        )
        await interaction.followup.send(
            _with_trace(GENERIC_SHEET_DELETE_ERROR), ephemeral=True
        )
        return

    sheets.invalidate_snapshot(inputs.sheet_name)

    description = row.description or "(no description)"
    await interaction.followup.send(
        f'Removed row {row.row_number} from *{inputs.fmt_name}*: "{description}".',
        ephemeral=True,
    )

    guild_cfg = (
        store.get(interaction.guild_id) if interaction.guild_id is not None else None
    )
    if guild_cfg and guild_cfg.broadcast_channel_id is not None:
        await _broadcast_team_removed(
            interaction,
            guild_cfg.broadcast_channel_id,
            fmt_name=inputs.fmt_name,
            url=row.url,
            description=row.description,
            species=row.species,
        )
    else:
        logger.info(
            "Skipping delete broadcast for guild_id=%s: no broadcast channel set",
            interaction.guild_id,
        )


def register(
    tree: app_commands.CommandTree,
    store: GuildConfigStore,
    registry: SheetsClientRegistry,
    *,
    vrpaste_cache: VRPasteCacheStore,
) -> None:
    """Register the /delete-team slash command on the given tree."""

    @tree.command(
        name="delete-team",
        description=(
            "Remove a team from the bank — by Pokepaste URL, VRPaste URL, "
            "or Champions Team ID."
        ),
    )
    @app_commands.describe(
        format=f"Format/regulation. Defaults to {config.DEFAULT_FORMAT} if omitted.",
        url=(
            "Pokepaste URL (e.g., https://pokepast.es/abc123) or VRPaste "
            "URL. Required unless you provide a Team ID instead."
        ),
        replica=(
            "10-character Champions Team ID (e.g. 'QBXXWXL05U'). "
            "Required unless you provide a URL instead."
        ),
    )
    @app_commands.choices(format=_format_choices())
    async def delete_team(
        interaction: discord.Interaction,
        format: app_commands.Choice[str] | None = None,
        url: str | None = None,
        replica: str | None = None,
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
        )
        if inputs is None:
            return

        logger.info(
            "delete-team invoked by user_id=%s guild_id=%s: "
            "url=%s replica=%s format=%s",
            interaction.user.id,
            interaction.guild_id,
            inputs.url,
            inputs.replica,
            inputs.fmt_name,
        )

        target_url: str | None = None
        if inputs.url is not None:
            target_url = await _resolve_target_url(
                interaction, inputs=inputs, vrpaste_cache=vrpaste_cache
            )
            if target_url is None:
                return

        await _delete_and_announce(
            interaction,
            sheets,
            store=store,
            inputs=inputs,
            target_url=target_url,
        )
