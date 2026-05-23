import asyncio
import logging
import re

import discord
from discord import app_commands

from sketch import config
from sketch.logging_setup import trace_id_var
from sketch.pokepaste_validator import (
    ValidationError,
    canonicalize_pokepaste_url,
    normalize_replica,
    validate_pokepaste_url,
)
from sketch.search.dex import DexIndex  # re-exported for backwards-compatible callers
from sketch.storage.guild_config import GuildConfigStore
from sketch.storage.sheets_client import SheetsClient, SheetsClientRegistry, TeamRow

__all__ = ["DexIndex", "setup_commands"]

logger = logging.getLogger(__name__)

_GENERIC_SHEET_READ_ERROR = (
    "Couldn't read the sheet right now — please try again in a moment."
)
_GENERIC_SHEET_WRITE_ERROR = (
    "Couldn't add the team right now — please try again in a moment."
)
_UNCONFIGURED_GUILD_ERROR = (
    "This server isn't configured to use Sketch. A server admin can run "
    "`/register-sheet` to set the Google Sheet this server writes to."
)
_GUILD_ONLY_ERROR = "This command can only be used inside a server."

# Same validation bin/seed_guilds.py enforces. Google Sheets IDs are URL-safe-
# ish; rejecting anything outside this set blocks accidental pastes of full
# URLs and obvious typos before we waste a probe RPC on them.
_SPREADSHEET_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _spreadsheet_link(spreadsheet_id: str) -> str:
    """Render a clickable Google Sheets URL for the given ID."""
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"


def _format_choices() -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=k, value=k) for k in config.FORMAT_SHEETS]


def _paste_type_choices() -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=v, value=v) for v in config.PASTE_TYPE_CHOICES]


def _default_format() -> str:
    return next(iter(config.FORMAT_SHEETS))


def _filter_team_rows(
    rows: list[TeamRow],
    *,
    resolved_groups: list[list[str]],
    description_match_indices: set[int] | None,
    url_target: str | None,
) -> list[TeamRow]:
    """Apply `/search-teams` filters to `rows`. Filters AND together.

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
    """
    matches: list[TeamRow] = []
    for idx, row in enumerate(rows):
        species_lower = {s.lower() for s in row.species}
        mons_ok = all(
            any(m.lower() in species_lower for m in group) for group in resolved_groups
        )
        desc_ok = description_match_indices is None or idx in description_match_indices
        if url_target is None:
            url_ok = True
        else:
            try:
                url_ok = canonicalize_pokepaste_url(row.url) == url_target
            except ValidationError:
                url_ok = False
        if mons_ok and desc_ok and url_ok:
            matches.append(row)
    return matches


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
    they're available, or None if the broadcast couldn't be sent. Never raises
    into the caller — broadcast failures must not fail the user's command.
    """
    logger.info(
        "Broadcasting team to channel_id=%s for guild_id=%s",
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
        title=f"New team added to {fmt_name}",
        url=url,
        description=description,
        color=discord.Color.green(),
    )
    embed.set_author(
        name=user.display_name,
        icon_url=user.display_avatar.url,
    )
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
        await interaction.followup.send(_UNCONFIGURED_GUILD_ERROR, ephemeral=True)
        return None
    sheets = registry.get(guild_id)
    if sheets is None:
        logger.info(
            "Refusing command from unconfigured guild_id=%s user_id=%s",
            guild_id,
            interaction.user.id,
        )
        await interaction.followup.send(_UNCONFIGURED_GUILD_ERROR, ephemeral=True)
        return None
    return sheets


def setup_commands(
    tree: app_commands.CommandTree,
    store: GuildConfigStore,
    registry: SheetsClientRegistry,
) -> None:
    """Register slash commands on `tree`.

    Commands are always registered in the global scope. Dev-mode fast
    iteration is handled in bot.py via `tree.copy_global_to(guild=...)`,
    which mirrors these globals into a single dev guild without creating
    a second source of truth here. See bot.py:setup_hook.

    The registry handles spreadsheet routing; `store` is captured here so
    handlers can read other per-guild settings (e.g., broadcast_channel_id)
    that don't belong to the SheetsClient.
    """

    @tree.command(
        name="add-team",
        description="Add a Pokepaste team to the database.",
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
            "add-team invoked by user_id=%s guild_id=%s: url=%s description=%r "
            "format=%s replica=%s paste_type=%s",
            interaction.user.id,
            interaction.guild_id,
            url,
            description,
            fmt_name,
            replica,
            paste_type_value,
        )

        # Cheap shape check first so we can dedup against the sheet before
        # paying for the live HTTP fetch in validate_pokepaste_url. The
        # canonical form is comparison-only — the row we eventually write
        # stores the user-provided string.
        try:
            canonical_url = canonicalize_pokepaste_url(url)
        except ValidationError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return

        try:
            existing = await sheets.find_row_by_url(sheet_name, canonical_url)
        except Exception:
            logger.exception("Failed to check for existing team")
            await interaction.followup.send(_GENERIC_SHEET_READ_ERROR, ephemeral=True)
            return

        if existing is not None:
            logger.info(
                "add-team dedup hit: user_id=%s guild_id=%s format=%s row=%d",
                interaction.user.id,
                interaction.guild_id,
                fmt_name,
                existing.row_number,
            )
            existing_desc = existing.description or "(no description)"
            await interaction.followup.send(
                f"This Pokepaste is already in *{fmt_name}* on row "
                f'{existing.row_number}: "{existing_desc}".',
                ephemeral=True,
            )
            return

        try:
            await validate_pokepaste_url(url)
            normalized_replica = normalize_replica(replica) if replica else None
        except ValidationError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return

        try:
            row = await sheets.add_row(
                sheet_name, url, description, normalized_replica, paste_type_value
            )
        except Exception:
            logger.exception("Failed to add row")
            await interaction.followup.send(_GENERIC_SHEET_WRITE_ERROR, ephemeral=True)
            return

        msg = f"Added team to row {row} in *{fmt_name}*."
        await interaction.followup.send(msg, ephemeral=True)

        broadcast_message: discord.Message | None = None
        guild_cfg = (
            store.get(interaction.guild_id)
            if interaction.guild_id is not None
            else None
        )
        if guild_cfg and guild_cfg.broadcast_channel_id is not None:
            broadcast_message = await _broadcast_team_added(
                interaction,
                guild_cfg.broadcast_channel_id,
                fmt_name=fmt_name,
                url=url,
                description=description,
            )
        else:
            logger.info(
                "Skipping broadcast for guild_id=%s: no broadcast_channel_id "
                "configured",
                interaction.guild_id,
            )

        species = await _await_species(sheets, sheet_name, row)
        if species:
            await interaction.edit_original_response(
                content=f"{msg}\nParsed: {', '.join(species)}"
            )
            if broadcast_message is not None:
                await _enrich_broadcast_with_species(broadcast_message, species)
            # Drop the cached search snapshot so the next /search-teams
            # rebuilds and includes this row. We deliberately wait until
            # species columns settle: invalidating earlier would just cause
            # `search_rows` to skip this row on the rebuild (it filters
            # rows whose species cells read "Loading..." / "#N/A"). On
            # timeout the snapshot stays stale, but the 5-minute TTL
            # backstop in SheetsClient eventually catches it.
            sheets.invalidate_snapshot(sheet_name)
        else:
            logger.info(
                "Species poll timed out for row %d in %s; skipping snapshot "
                "invalidation (TTL backstop will catch it)",
                row,
                sheet_name,
            )

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
        format: app_commands.Choice[str] | None = None,
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

        fmt_name = format.value if format else _default_format()
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
            snapshot = await sheets.get_search_snapshot(sheet_name)
        except Exception:
            logger.exception("Failed to read sheet")
            await interaction.followup.send(_GENERIC_SHEET_READ_ERROR)
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

    @tree.command(
        name="help",
        description="How to use this bot.",
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
            "  Example: `/add-team url:https://pokepast.es/abcd1234 "
            "description:Calyrex-S balance`\n\n"
            "`/search-teams [mon1:<name>] ... [mon6:<name>] "
            "[description:<text>] [url:<paste>] [format:Reg M-A]`\n"
            "  Find teams. Filter by Pokémon (AND across mon params), "
            "by tokenized\n"
            "  description (order-independent, per-token substring), by "
            "Pokepaste URL,\n"
            "  or any combination. At least one filter is required.\n"
            "  Examples:\n"
            "    `/search-teams mon1:Calyrex-Shadow mon2:Urshifu`\n"
            "    `/search-teams mon1:Charizard`     (matches base or Mega-X/Y)\n"
            "    `/search-teams mon1:Charizard-Mega-Y`     (Mega-Y only)\n"
            "    `/search-teams description:jsmithvgc`     (by player / gamertag)\n"
            "    `/search-teams description:caly zama`     "
            "(matches 'Calyrex Zamazenta')\n"
            "    `/search-teams description:pex`     "
            "(matches descriptions containing 'Toxapex')\n"
            "    `/search-teams description:jsmithvgc mon1:Charizard`  (AND)\n"
            "    `/search-teams url:https://pokepast.es/abcd1234`  "
            "(is this paste already banked?)\n\n"
            f"Available formats: {formats}\n\n"
            "**Admin commands** (require Manage Server):\n"
            "`/register-sheet spreadsheet_id:<id>` — register or replace the "
            "Google Sheet this server writes to. Required after the first "
            "install. The bot's service account must be shared on the sheet "
            "as Editor before you run this.\n"
            "`/set-broadcast-channel channel:<#channel>` — announce every "
            "`/add-team` in `<#channel>`. The bot needs Send Messages + "
            "Embed Links there.\n"
            "`/clear-broadcast-channel` — stop broadcasting `/add-team` "
            "announcements.\n"
            "`/show-config` — display this server's current configuration."
        )
        await interaction.response.send_message(msg, ephemeral=True)

    # --- Admin commands ---------------------------------------------------
    #
    # Self-service configuration so server admins don't need a bot operator to
    # run `bin/seed_guilds.py`. All four are gated with two decorators:
    #
    #   @default_permissions(manage_guild=True)
    #     Discord's "Manage Server" gate. Server owners can further restrict
    #     via Server Settings → Integrations → <bot> → command permissions.
    #
    #   @guild_only()
    #     Marks the command `dm_permission=False` in Discord's app-command
    #     registry, so the Discord client hides it from the picker in DMs
    #     and refuses delivery if a user types it there. Per-handler
    #     `if guild_id is None: refuse` is defense in depth for the rare
    #     case where a stale registration leaks past the client guard.
    #
    # The commands all defer ephemerally so error messages and config
    # readouts stay private to the invoker.

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
            await interaction.followup.send(_GUILD_ONLY_ERROR, ephemeral=True)
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
                "That doesn't look like a Google Sheets ID. Paste just the "
                "ID portion of the URL — the part between `/d/` and `/edit` "
                "(letters, digits, `_`, and `-` only).",
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
                f"Couldn't open <{_spreadsheet_link(spreadsheet_id)}>. Check "
                "that:\n"
                "1. The ID is correct (no extra characters from the URL).\n"
                "2. The sheet is shared with the bot's service account as "
                "**Editor** — ask the bot owner for the address if you don't "
                "have it.\n"
                "3. The sheet exists and isn't in the trash.",
                ephemeral=True,
            )
            return

        missing_tabs = [t for t in config.FORMAT_SHEETS.values() if t not in tabs]
        if missing_tabs:
            await interaction.followup.send(
                "The sheet opened, but it's missing the expected tab(s): "
                f"{', '.join(f'`{t}`' for t in missing_tabs)}. Add the tab(s) "
                "(or use a copy of the canonical TeamBank Parser template) "
                "and try again.",
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
                "Couldn't save that to the bot's config right now — please "
                "try again in a moment.",
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
            await interaction.followup.send(_GUILD_ONLY_ERROR, ephemeral=True)
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
                "This server doesn't have a sheet registered yet. Run "
                "`/register-sheet` first, then set the broadcast channel.",
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
                "Couldn't save that to the bot's config right now — please "
                "try again in a moment.",
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
            await interaction.followup.send(_GUILD_ONLY_ERROR, ephemeral=True)
            return

        logger.info(
            "clear-broadcast-channel invoked by user_id=%s guild_id=%s",
            interaction.user.id,
            guild_id,
        )

        cfg = store.get(guild_id)
        if cfg is None:
            # Same shape as set-broadcast-channel — no config means there's
            # nothing to clear and no sheet either; the friendlier prompt is
            # to point at /register-sheet.
            await interaction.followup.send(
                "This server doesn't have a sheet registered yet. Nothing to clear.",
                ephemeral=True,
            )
            return
        if cfg.broadcast_channel_id is None:
            await interaction.followup.send(
                "No broadcast channel is currently set — nothing to clear.",
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
                "Couldn't update the bot's config right now — please try "
                "again in a moment.",
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
            await interaction.response.send_message(_GUILD_ONLY_ERROR, ephemeral=True)
            return

        logger.info(
            "show-config invoked by user_id=%s guild_id=%s",
            interaction.user.id,
            guild_id,
        )
        cfg = store.get(guild_id)
        if cfg is None:
            await interaction.response.send_message(
                "This server has no Sketch configuration. Run "
                "`/register-sheet` to get started.",
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
