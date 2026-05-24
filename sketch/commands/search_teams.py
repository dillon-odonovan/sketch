"""`/search-teams` — query the bank by mon, description, and/or URL.

Filters AND together. Description matching uses the tokenized
DescriptionIndex (order-independent, per-token substring); mon matching
uses the DexIndex's prefix-group rule (`Charizard` matches base + Mega-X
+ Mega-Y; `Charizard-Mega-Y` matches only that form).
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from sketch import config
from sketch.commands._shared import (
    GENERIC_SHEET_READ_ERROR,
    _filter_team_rows,
    _format_choices,
    _resolve_guild_sheets,
)
from sketch.logging_setup import trace_id_var
from sketch.pokepaste_validator import ValidationError, canonicalize_pokepaste_url
from sketch.storage.sheets_client import SheetsClientRegistry

logger = logging.getLogger(__name__)


def register(
    tree: app_commands.CommandTree,
    registry: SheetsClientRegistry,
) -> None:
    """Register /search-teams. Doesn't need the guild-config store —
    everything per-guild is reachable via the SheetsClient that the
    registry resolves for the invoking guild."""

    @tree.command(
        name="search-teams",
        description="Find teams by Pokémon, description, and/or Pokepaste URL.",
    )
    @app_commands.describe(
        format="Format/regulation",
        mon1="First Pokémon",
        mon2="Second Pokémon",
        mon3="Third Pokémon",
        mon4="Fourth Pokémon",
        mon5="Fifth Pokémon",
        mon6="Sixth Pokémon",
        description=(
            "Tokenized description search: order-independent, per-token "
            "substring match (e.g. 'caly zama' matches 'Calyrex Zamazenta'; "
            "'pex' matches 'Toxapex'). Query tokens shorter than 3 chars "
            "require an exact word match."
        ),
        url="Pokepaste URL — check whether this paste is already in the bank.",
    )
    @app_commands.choices(format=_format_choices())
    async def search_teams(
        interaction: discord.Interaction,
        format: app_commands.Choice[str],
        mon1: str | None = None,
        mon2: str | None = None,
        mon3: str | None = None,
        mon4: str | None = None,
        mon5: str | None = None,
        mon6: str | None = None,
        description: str | None = None,
        url: str | None = None,
    ) -> None:
        trace_id_var.set(str(interaction.id))
        await interaction.response.defer(thinking=True)

        sheets = await _resolve_guild_sheets(interaction, registry)
        if sheets is None:
            return

        fmt_name = format.value
        sheet_name = config.FORMAT_SHEETS[fmt_name]
        queries = [m for m in [mon1, mon2, mon3, mon4, mon5, mon6] if m]
        description_query = (description or "").strip() or None
        url_raw = (url or "").strip() or None
        url_target: str | None = None
        if url_raw is not None:
            try:
                url_target = canonicalize_pokepaste_url(url_raw)
            except ValidationError as e:
                await interaction.followup.send(str(e))
                return
        logger.info(
            "search-teams invoked by user_id=%s guild_id=%s: format=%s "
            "queries=%s description=%r url=%r",
            interaction.user.id,
            interaction.guild_id,
            fmt_name,
            queries,
            description_query,
            url_target,
        )

        if not queries and not description_query and url_target is None:
            await interaction.followup.send(
                "Provide at least one of `mon1`..`mon6`, `description`, or `url`."
            )
            return

        if queries:
            try:
                dex = await sheets.get_dex()
            except Exception:
                logger.exception("Failed to load DEX")
                await interaction.followup.send(GENERIC_SHEET_READ_ERROR)
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
            snapshot = await sheets.get_search_snapshot(sheet_name)
        except Exception:
            logger.exception("Failed to read sheet")
            await interaction.followup.send(GENERIC_SHEET_READ_ERROR)
            return

        # `desc_index.match` returns positional indices into `snapshot.rows`,
        # which `_filter_team_rows` enumerates 1:1. None = no filter applied;
        # empty set = filter ran and matched nothing (caller still ANDs it in
        # so the result is empty, which is what we want).
        description_match_indices: set[int] | None = (
            snapshot.desc_index.match(description_query) if description_query else None
        )

        matches = _filter_team_rows(
            snapshot.rows,
            resolved_groups=resolved_groups,
            description_match_indices=description_match_indices,
            url_target=url_target,
        )

        label_parts = list(queries)
        if description_query:
            label_parts.append(f'description:"{description_query}"')
        if url_target is not None:
            label_parts.append(f"url:{url_target}")
        query_label = " + ".join(label_parts)
        if not matches:
            await interaction.followup.send(
                f"No teams found in *{fmt_name}* matching *{query_label}*."
            )
            return

        embed = discord.Embed(
            title=f"{len(matches)} team(s) in {fmt_name} matching {query_label}",
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
            remaining = len(matches) - config.SEARCH_RESULT_LIMIT
            embed.set_footer(text=f"+{remaining} more — narrow your search.")
        await interaction.followup.send(embed=embed)
