import asyncio
import logging

import discord
from discord import app_commands

import config
from dex import DexIndex  # re-exported for backwards-compatible callers
from guild_config import GuildConfigStore
from logging_setup import trace_id_var
from pokepaste_validator import (
    ValidationError,
    normalize_replica,
    validate_pokepaste_url,
)
from sheets_client import SheetsClient, SheetsClientRegistry

__all__ = ["DexIndex", "setup_commands"]

logger = logging.getLogger(__name__)

_GENERIC_SHEET_READ_ERROR = (
    "Couldn't read the sheet right now — please try again in a moment."
)
_GENERIC_SHEET_WRITE_ERROR = (
    "Couldn't add the team right now — please try again in a moment."
)
_UNCONFIGURED_GUILD_ERROR = (
    "This server isn't configured to use Sketch. Ask the bot owner to "
    "register a spreadsheet for this server."
)


def _format_choices() -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=k, value=k) for k in config.FORMAT_SHEETS.keys()]


def _paste_type_choices() -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=v, value=v) for v in config.PASTE_TYPE_CHOICES]


def _default_format() -> str:
    return next(iter(config.FORMAT_SHEETS))


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
        await interaction.followup.send(_UNCONFIGURED_GUILD_ERROR, ephemeral=True)
        return None
    sheets = registry.get(guild_id)
    if sheets is None:
        logger.info(
            "Refusing command from unconfigured guild_id=%s user_id=%s",
            guild_id, interaction.user.id,
        )
        await interaction.followup.send(_UNCONFIGURED_GUILD_ERROR, ephemeral=True)
        return None
    return sheets


def setup_commands(
    tree: app_commands.CommandTree,
    store: GuildConfigStore,
    registry: SheetsClientRegistry,
    dev_guild: discord.Object | None = None,
) -> None:
    """Register slash commands on `tree`.

    `store` is currently unused inside handlers (the registry consults it),
    but is accepted here so a future runtime-config path (e.g. an admin
    `/set-spreadsheet`) has the seam already wired.
    """
    del store  # currently only consulted via the registry
    cmd_kwargs = {"guild": dev_guild} if dev_guild else {}

    @tree.command(
        name="add-team",
        description="Add a Pokepaste team to the database.",
        **cmd_kwargs,
    )
    @app_commands.describe(
        url="Pokepaste URL (e.g., https://pokepast.es/abc123)",
        description="Short description of the team (e.g., 'Calyrex-S balance')",
        format="Format/regulation",
        replica="Optional 10-character hex replica code",
        paste_type="Whether this paste is exact, recreated, or unspecified",
    )
    @app_commands.choices(
        format=_format_choices(),
        paste_type=_paste_type_choices(),
    )
    async def add_team(
        interaction: discord.Interaction,
        url: str,
        description: str,
        format: app_commands.Choice[str] | None = None,
        replica: str | None = None,
        paste_type: app_commands.Choice[str] | None = None,
    ) -> None:
        trace_id_var.set(str(interaction.id))
        # Discord requires interactions to be acknowledged within 3 seconds;
        # deferring buys us up to 15 minutes for the actual work (URL fetch,
        # Sheets writes, species poll). ephemeral=True keeps the reply
        # visible only to the invoker.
        # https://discord.com/developers/docs/interactions/receiving-and-responding
        await interaction.response.defer(ephemeral=True, thinking=True)

        sheets = await _resolve_guild_sheets(interaction, registry)
        if sheets is None:
            return

        fmt_name = format.value if format else _default_format()
        sheet_name = config.FORMAT_SHEETS[fmt_name]
        paste_type_value = paste_type.value if paste_type else config.PASTE_TYPE_DEFAULT
        logger.info(
            "add-team invoked by user_id=%s guild_id=%s: url=%s description=%r format=%s replica=%s paste_type=%s",
            interaction.user.id, interaction.guild_id, url, description, fmt_name, replica, paste_type_value,
        )

        try:
            await validate_pokepaste_url(url)
            normalized_replica = normalize_replica(
                replica) if replica else None
        except ValidationError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return

        try:
            row = await sheets.add_row(
                sheet_name, url, description, normalized_replica, paste_type_value
            )
        except Exception:
            logger.exception("Failed to add row")
            await interaction.followup.send(
                _GENERIC_SHEET_WRITE_ERROR, ephemeral=True
            )
            return

        msg = f"Added team to row {row} in *{fmt_name}*."
        await interaction.followup.send(msg, ephemeral=True)

        species = await _await_species(sheets, sheet_name, row)
        if species:
            await interaction.edit_original_response(
                content=f"{msg}\nParsed: {', '.join(species)}"
            )

    @tree.command(
        name="search-teams",
        description="Find teams containing the given Pokémon.",
        **cmd_kwargs,
    )
    @app_commands.describe(
        format="Format/regulation",
        mon1="First Pokémon (required)",
        mon2="Second Pokémon",
        mon3="Third Pokémon",
        mon4="Fourth Pokémon",
        mon5="Fifth Pokémon",
        mon6="Sixth Pokémon",
    )
    @app_commands.choices(format=_format_choices())
    async def search_teams(
        interaction: discord.Interaction,
        mon1: str,
        format: app_commands.Choice[str] | None = None,
        mon2: str | None = None,
        mon3: str | None = None,
        mon4: str | None = None,
        mon5: str | None = None,
        mon6: str | None = None,
    ) -> None:
        trace_id_var.set(str(interaction.id))
        await interaction.response.defer(thinking=True)

        sheets = await _resolve_guild_sheets(interaction, registry)
        if sheets is None:
            return

        fmt_name = format.value if format else _default_format()
        sheet_name = config.FORMAT_SHEETS[fmt_name]
        queries = [m for m in [mon1, mon2, mon3, mon4, mon5, mon6] if m]
        logger.info(
            "search-teams invoked by user_id=%s guild_id=%s: format=%s queries=%s",
            interaction.user.id, interaction.guild_id, fmt_name, queries,
        )

        try:
            dex = await sheets.get_dex()
        except Exception:
            logger.exception("Failed to load DEX")
            await interaction.followup.send(_GENERIC_SHEET_READ_ERROR)
            return

        resolved_groups: list[list[str]] = []
        for q in queries:
            r = dex.resolve(q)
            if not r.canonical_matches:
                hint = (
                    f" Did you mean: {', '.join(r.suggestions)}?"
                    if r.suggestions
                    else ""
                )
                await interaction.followup.send(
                    f"Couldn't find Pokémon `{q}` in the DEX.{hint}"
                )
                return
            resolved_groups.append(r.canonical_matches)

        try:
            rows = await sheets.search_rows(sheet_name)
        except Exception:
            logger.exception("Failed to read sheet")
            await interaction.followup.send(_GENERIC_SHEET_READ_ERROR)
            return

        matches = []
        for row in rows:
            species_lower = {s.lower() for s in row.species}
            if all(
                any(m.lower() in species_lower for m in group)
                for group in resolved_groups
            ):
                matches.append(row)

        query_label = ", ".join(queries)
        if not matches:
            await interaction.followup.send(
                f"No teams found in *{fmt_name}* containing *{query_label}*."
            )
            return

        embed = discord.Embed(
            title=f"{len(matches)} team(s) in {fmt_name} with {query_label}",
            color=discord.Color.blue(),
        )
        for row in matches[: config.SEARCH_RESULT_LIMIT]:
            title = (row.description or "(no description)")[:80]
            species_line = ", ".join(row.species)
            embed.add_field(
                name=title,
                value=f"{row.url}\n*{species_line}*",
                inline=False,
            )
        if len(matches) > config.SEARCH_RESULT_LIMIT:
            embed.set_footer(
                text=f"+{len(matches) - config.SEARCH_RESULT_LIMIT} more — narrow your search."
            )
        await interaction.followup.send(embed=embed)

    @tree.command(
        name="help",
        description="How to use this bot.",
        **cmd_kwargs,
    )
    async def help_cmd(interaction: discord.Interaction) -> None:
        # /help is intentionally guild-agnostic — it doesn't touch any sheet,
        # so it works in DMs and in unconfigured guilds without refusal.
        trace_id_var.set(str(interaction.id))
        logger.info("help invoked by user_id=%s", interaction.user.id)
        formats = ", ".join(config.FORMAT_SHEETS.keys())
        msg = (
            "**Sketch** — Pokepaste team bank\n\n"
            "`/add-team url:<paste> description:<text> [format:Reg M-A] "
            "[replica:<hex>] [paste_type:Exact|Recreated|Unspecified]`\n"
            "  Add a team to the database.\n"
            "  Example: `/add-team url:https://pokepast.es/abcd1234 description:Calyrex-S balance`\n\n"
            "`/search-teams mon1:<name> [mon2:<name>] ... [mon6:<name>] [format:Reg M-A]`\n"
            "  Find teams containing all the listed Pokémon (AND across params).\n"
            "  Examples:\n"
            "    `/search-teams mon1:Calyrex-Shadow mon2:Urshifu`\n"
            "    `/search-teams mon1:Charizard`         (matches base or Mega-X/Y)\n"
            "    `/search-teams mon1:Charizard-Mega-Y`  (Mega-Y only)\n\n"
            f"Available formats: {formats}"
        )
        await interaction.response.send_message(msg, ephemeral=True)


async def _await_species(
    sheets: SheetsClient, sheet_name: str, row: int
) -> list[str] | None:
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
