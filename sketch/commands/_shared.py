"""Shared helpers across the slash-command handlers.

Things lifted here are used by more than one command file (e.g.,
`_resolve_guild_sheets` is called from /add-team and /search-teams) plus
the small utilities that benefit from a single source of truth (error
constants, the spreadsheet-id regex, format-choice builders).

Importing rule of thumb: command files import only what they need from
this module. The module itself does NOT import from any of the
per-command modules — that would invite a circular import.

A handful of names (`_SPREADSHEET_ID_RE`, `_filter_team_rows`,
`_spreadsheet_link`) are re-exported from `sketch.commands.__init__` to
preserve the import path that `tests/test_commands.py` already targets.
"""

from __future__ import annotations

import asyncio
import logging
import re

import discord
from discord import app_commands

from sketch import config
from sketch.logging_setup import trace_id_var
from sketch.pokepaste.validator import ValidationError, canonicalize_pokepaste_url
from sketch.storage.sheets_client import SheetsClient, SheetsClientRegistry, TeamRow
from sketch.team import norm_species

logger = logging.getLogger(__name__)


# --- Trace ID helper ------------------------------------------------------


def _with_trace(message: str) -> str:
    """Append the current trace ID to a user-facing error message."""
    tid = trace_id_var.get()
    if tid == "-":
        return message
    return f"{message}\n\nTrace ID: `{tid}`"


# --- User-facing error constants ------------------------------------------

GENERIC_SHEET_READ_ERROR = (
    "Couldn't read the sheet right now — please try again in a moment."
)
GENERIC_SHEET_WRITE_ERROR = (
    "Couldn't add the team right now — please try again in a moment."
)
GENERIC_SHEET_DELETE_ERROR = (
    "Couldn't delete the team right now — please try again in a moment."
)
GENERIC_CACHE_READ_ERROR = (
    "Couldn't check the replica-code cache right now — please try again in a moment."
)
GENERIC_CACHE_WRITE_ERROR = (
    "Couldn't save to the replica-code cache right now — please try again in a moment."
)
UNCONFIGURED_GUILD_ERROR = (
    "This server isn't configured to use Sketch. A server admin can run "
    "`/register-sheet` to set the Google Sheet this server writes to."
)
GUILD_ONLY_ERROR = "This command can only be used inside a server."


# Same validation bin/seed_guilds.py enforces. Google Sheets IDs are URL-safe-
# ish; rejecting anything outside this set blocks accidental pastes of full
# URLs and obvious typos before we waste a probe RPC on them.
_SPREADSHEET_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _spreadsheet_link(spreadsheet_id: str) -> str:
    """Render a clickable Google Sheets URL for the given ID."""
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"


def _format_choices() -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=k, value=k) for k in config.FORMAT_SHEETS]


def _resolve_format(format_choice: app_commands.Choice[str] | None) -> str:
    """Resolve an optional format choice to its name.

    None -> config.DEFAULT_FORMAT (the current regulation), so the team
    commands can leave `format` off for the common case.
    """
    return config.DEFAULT_FORMAT if format_choice is None else format_choice.value


def _paste_type_choices() -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=v, value=v) for v in config.PASTE_TYPE_CHOICES]


def _filter_team_rows(
    rows: list[TeamRow],
    *,
    resolved_groups: list[list[str]],
    description_match_indices: set[int] | None,
    url_target: str | None,
    replica_target: str | None,
) -> list[TeamRow]:
    """Apply `/search-teams` filters to `rows`.

    Replica codes and Pokepaste URLs are uniquely identifying — at most one
    row can carry either. When either is supplied, the description and mon
    filters add nothing, so the helper short-circuits:

    - If `replica_target` is set: filter only by replica. URL, description,
      and mon filters are ignored (replica wins outright when both unique
      IDs are supplied — no need to AND two already-unique IDs).
    - Else if `url_target` is set: filter only by URL.
    - Else: AND the mon and description filters as before.

    Parameters:

    - `resolved_groups`: each inner list is one user-supplied mon param after
      DEX resolution (e.g., "Charizard" → ["Charizard", "Charizard-Mega-X",
      "Charizard-Mega-Y"]). A row passes a group if ANY species in the row
      matches ANY name in the group; the row passes the mon filter overall
      only if it passes EVERY group. Empty list = no mon filter.
    - `description_match_indices`: positional indices into `rows` whose
      descriptions pass the tokenized description filter. The set comes from
      `SearchSnapshot.desc_index.match(query)` upstream — this helper stays
      ignorant of tokenization rules. ``None`` means "no description filter
      applied" (all rows pass); an empty set means "filter applied, zero
      matches" (no rows pass).
    - `url_target`: already-canonicalized Pokepaste URL. None = no URL filter.
      Stored URLs are canonicalized per-row for comparison; malformed stored
      URLs are treated as non-matching rather than raising (mirrors
      `SheetsClient.find_row_by_url`).
    - `replica_target`: already-normalized (uppercase) Champions replica
      code. None = no replica filter. Rows whose stored replica is missing
      or doesn't equal the target after uppercasing are skipped.
    """
    if replica_target is not None:
        match = next(
            (
                row
                for row in rows
                if row.replica is not None and row.replica.upper() == replica_target
            ),
            None,
        )
        return [match] if match is not None else []
    if url_target is not None:
        for row in rows:
            try:
                if canonicalize_pokepaste_url(row.url) == url_target:
                    return [row]
            except ValidationError:
                continue
        return []
    matches = []
    for idx, row in enumerate(rows):
        row_species = {norm_species(s) for s in row.species}
        mons_ok = all(
            any(norm_species(m) in row_species for m in group)
            for group in resolved_groups
        )
        desc_ok = description_match_indices is None or idx in description_match_indices
        if mons_ok and desc_ok:
            matches.append(row)
    return matches


async def _broadcast_team_event(
    interaction: discord.Interaction,
    channel_id: int,
    *,
    title: str,
    color: discord.Color,
    url: str,
    description: str,
    species: list[str] | None = None,
) -> discord.Message | None:
    """Send a public team-event embed to `channel_id`.

    Shared plumbing behind `_broadcast_team_added` and
    `_broadcast_team_removed`. Returns the sent Message so callers can
    edit it later (e.g., `/add-team` enriches with species once the
    AppsScript formula settles), or None if the broadcast couldn't be
    sent. Never raises — broadcast failures must not fail the user's
    command.

    `species`, if provided and non-empty, is rendered as a "Pokémon"
    field on the embed at send time — used by removed-team broadcasts,
    which already have species in hand from the lookup read.
    """
    logger.info(
        "Broadcasting team event %r to channel_id=%s for guild_id=%s",
        title,
        channel_id,
        interaction.guild_id,
    )
    channel = interaction.client.get_channel(channel_id)
    if channel is None:
        logger.warning(
            "Broadcast channel %s not in cache (deleted? bot missing access?); "
            "skipping broadcast for guild_id=%s",
            channel_id,
            interaction.guild_id,
        )
        return None

    user = interaction.user
    embed = discord.Embed(
        title=title,
        url=url,
        description=description,
        color=color,
    )
    embed.set_author(
        name=user.display_name,
        icon_url=user.display_avatar.url,
    )
    if species:
        embed.add_field(name="Pokémon", value=", ".join(species), inline=False)
    try:
        return await channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        logger.warning(
            "Failed to send broadcast to channel %s in guild_id=%s",
            channel_id,
            interaction.guild_id,
            exc_info=True,
        )
        return None


async def _broadcast_team_added(
    interaction: discord.Interaction,
    channel_id: int,
    *,
    fmt_name: str,
    url: str,
    description: str,
) -> discord.Message | None:
    """Post the public 'new team' embed to the configured channel.

    Returns the sent Message so it can be enriched with parsed species once
    they're available (see `_enrich_broadcast_with_species`), or None if
    the broadcast couldn't be sent.
    """
    return await _broadcast_team_event(
        interaction,
        channel_id,
        title=f"New team added to {fmt_name}",
        color=discord.Color.green(),
        url=url,
        description=description,
    )


async def _broadcast_team_removed(
    interaction: discord.Interaction,
    channel_id: int,
    *,
    fmt_name: str,
    url: str,
    description: str,
    species: list[str],
) -> discord.Message | None:
    """Post the public 'team removed' embed to the configured channel.

    Species are included up front (unlike `/add-team`, which patches them
    in once the AppsScript formula resolves) because the lookup read on
    the delete path already returns them. Pass an empty list when the
    row was deleted before its species cells settled.
    """
    return await _broadcast_team_event(
        interaction,
        channel_id,
        title=f"Team removed from {fmt_name}",
        color=discord.Color.red(),
        url=url,
        description=description,
        species=species,
    )


async def _enrich_broadcast_with_species(
    message: discord.Message,
    species: list[str],
) -> None:
    """Edit the broadcast embed to add a Pokémon field. Best-effort."""
    if not message.embeds:
        return
    embed = message.embeds[0]
    embed.add_field(name="Pokémon", value=", ".join(species), inline=False)
    try:
        await message.edit(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        logger.warning(
            "Failed to enrich broadcast message %s with species",
            message.id,
            exc_info=True,
        )


async def _resolve_guild_sheets(
    interaction: discord.Interaction,
    registry: SheetsClientRegistry,
) -> SheetsClient | None:
    """Look up the per-guild SheetsClient, or refuse via followup.

    Assumes the interaction has already been deferred — sends the refusal as
    an ephemeral followup and returns None when the guild isn't configured
    (or the interaction came from a DM with no guild_id). Callers should
    bail out on None.

    Returns just the SheetsClient (not DEX); commands that need DEX call
    `await sheets.get_dex()` themselves so /add-team doesn't pay the DEX
    load cost on cold start.
    """
    guild_id = interaction.guild_id
    if guild_id is None:
        await interaction.followup.send(
            _with_trace(UNCONFIGURED_GUILD_ERROR), ephemeral=True
        )
        return None
    sheets = registry.get(guild_id)
    if sheets is None:
        logger.info(
            "Refusing command from unconfigured guild_id=%s user_id=%s",
            guild_id,
            interaction.user.id,
        )
        await interaction.followup.send(
            _with_trace(UNCONFIGURED_GUILD_ERROR), ephemeral=True
        )
        return None
    return sheets


async def _await_species(
    sheets: SheetsClient, sheet_name: str, row: int
) -> list[str] | None:
    """Poll the sheet until species cells populate or the timeout expires.

    Returns the 6-species list on success, None on timeout. Used by
    /add-team after a successful add_row to enrich the user-facing message
    once the AppsScript-driven `TEAMDATAFROMPASTE` formula has resolved.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + config.POLL_TIMEOUT_SECONDS
    while loop.time() < deadline:
        try:
            species = await sheets.poll_species(sheet_name, row)
        except Exception:
            logger.exception("Species poll failed")
            return None
        if species:
            return species
        await asyncio.sleep(config.POLL_INTERVAL_SECONDS)
    return None
