"""`/add-team` — the unified entry point for adding a team to the bank.

Accepts any of:
  - url only         → existing Pokepaste-URL flow.
  - replica only     → cache lookup; on hit reuse the URL, on miss prompt
                       for screenshots and run the OCR + confirm + mint +
                       cache-seed pipeline.
  - url + replica    → URL flow (cache seeding from this path is a future
                       follow-up; the team data isn't OCR'd so we don't
                       have the species list the cache entry expects).

Validation: at least one of url / replica must be present.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import anthropic
import discord
from discord import app_commands

from sketch import config
from sketch.commands._shared import (
    GENERIC_CACHE_READ_ERROR,
    GENERIC_CACHE_WRITE_ERROR,
    GENERIC_SHEET_READ_ERROR,
    GENERIC_SHEET_WRITE_ERROR,
    _await_species,
    _broadcast_team_added,
    _enrich_broadcast_with_species,
    _format_choices,
    _paste_type_choices,
    _resolve_guild_sheets,
)
from sketch.logging_setup import trace_id_var
from sketch.pokepaste_validator import (
    ValidationError,
    canonicalize_pokepaste_url,
    normalize_replica,
    validate_pokepaste_url,
)
from sketch.replica.cache import ReplicaCacheEntry, ReplicaCacheStore
from sketch.replica.extractor import (
    ExtractionError,
    extract_team_from_screenshots,
)
from sketch.replica.pokepaste_renderer import (
    PokepasteUploadError,
    post_to_pokepaste,
    render_showdown,
)
from sketch.replica.preview_view import ReplicaPreviewView, team_to_embed
from sketch.storage.guild_config import GuildConfigStore
from sketch.storage.sheets_client import SheetsClientRegistry

logger = logging.getLogger(__name__)


async def _send_status(
    interaction: discord.Interaction,
    preview_shown: bool,  # noqa: ARG001 — kept for caller-side documentation
    content: str,
) -> None:
    """Send the user-facing /add-team status message.

    Edits the original (deferred) response so the whole flow shows up as a
    single message in Discord, regardless of whether we went through the
    OCR preview branch (which already used `edit_original_response` to
    show the preview embed) or the URL branch (no preview, edit straight
    to the success / error content).

    `preview_shown` is intentionally unused — both paths edit the original
    response — but the parameter is kept at call sites so the intent is
    visible to a reader scanning the handler.
    """
    await interaction.edit_original_response(content=content, embed=None, view=None)


def register(
    tree: app_commands.CommandTree,
    store: GuildConfigStore,
    registry: SheetsClientRegistry,
    *,
    replica_cache: ReplicaCacheStore,
    anthropic_client: anthropic.AsyncAnthropic,
) -> None:
    """Register the /add-team slash command on the given tree.

    Captures `store`, `registry`, `replica_cache`, and `anthropic_client` in
    the handler's closure so each invocation can route to the right
    spreadsheet, look up the broadcast channel, hit the replica cache, and
    drive Claude vision without re-deriving the dependencies on every call.
    """

    @tree.command(
        name="add-team",
        description=(
            "Add a team to the bank — by Pokepaste URL, Champions Team ID, or both."
        ),
    )
    @app_commands.describe(
        url=(
            "Pokepaste URL (e.g., https://pokepast.es/abc123). Required unless "
            "you provide a Team ID instead."
        ),
        replica=(
            "10-character Champions Team ID (e.g. 'QBXXWXL05U'). Required unless "
            "you provide a URL instead."
        ),
        description="Short description of the team (e.g., 'Calyrex-S balance')",
        format="Format/regulation",
        paste_type="Whether this paste is exact, recreated, or unspecified",
        page1=(
            "Page 1 of the Champions Replica share screen. Only needed if you "
            "submitted a Team ID we haven't seen before (no URL). A single "
            "stitched image of both pages works — just attach it here and leave "
            "page2 empty."
        ),
        page2=(
            "Page 2 of the Champions Replica share screen. Optional even on "
            "first sighting — omit if page1 is already stitched with both pages."
        ),
    )
    @app_commands.choices(
        format=_format_choices(),
        paste_type=_paste_type_choices(),
    )
    async def add_team(
        interaction: discord.Interaction,
        description: str,
        format: app_commands.Choice[str],
        url: str | None = None,
        replica: str | None = None,
        paste_type: app_commands.Choice[str] | None = None,
        page1: discord.Attachment | None = None,
        page2: discord.Attachment | None = None,
    ) -> None:
        trace_id_var.set(str(interaction.id))
        # Discord requires interactions to be acknowledged within 3 seconds;
        # deferring buys us up to 15 minutes for the actual work (URL fetch,
        # Sheets writes, OCR call, species poll). ephemeral=True keeps the
        # reply visible only to the invoker.
        # https://discord.com/developers/docs/interactions/receiving-and-responding
        await interaction.response.defer(ephemeral=True, thinking=True)

        sheets = await _resolve_guild_sheets(interaction, registry)
        if sheets is None:
            return

        fmt_name = format.value
        sheet_name = config.FORMAT_SHEETS[fmt_name]
        paste_type_value = paste_type.value if paste_type else config.PASTE_TYPE_DEFAULT

        # At least one of `url` or `replica` must be provided — the command
        # is the unified entry point for both Pokepaste-URL teams and
        # Champions-Team-ID teams.
        if url is None and replica is None:
            await interaction.followup.send(
                "Provide a **Pokepaste URL** or a **Champions Team ID** (or "
                "both). At least one is required.",
                ephemeral=True,
            )
            return

        normalized_replica: str | None = None
        if replica is not None:
            try:
                normalized_replica = normalize_replica(replica)
            except ValidationError as e:
                await interaction.followup.send(str(e), ephemeral=True)
                return

        logger.info(
            "add-team invoked by user_id=%s guild_id=%s: url=%s replica=%s "
            "description=%r format=%s paste_type=%s has_page1=%s has_page2=%s",
            interaction.user.id,
            interaction.guild_id,
            url,
            normalized_replica,
            description,
            fmt_name,
            paste_type_value,
            page1 is not None,
            page2 is not None,
        )

        # ----- Determine the canonical Pokepaste URL ---------------------
        #
        # Three resolution paths. All converge on `canonical_url` (what we
        # write to the sheet) and `canonical_url_for_dedup` (what we compare
        # against existing rows). The latter is always the canonicalized
        # form; the former preserves the user's original spelling when they
        # supplied a URL directly so the sheet shows what they typed.

        preview_shown = False

        if url is not None:
            # URL path: user supplied a Pokepaste URL. Same as the historical
            # /add-team flow.
            try:
                canonical_url_for_dedup = canonicalize_pokepaste_url(url)
            except ValidationError as e:
                await interaction.followup.send(str(e), ephemeral=True)
                return
            canonical_url = url
        else:
            # Replica-only path. `normalized_replica` is non-None by the
            # earlier "at least one of url/replica" check.
            assert normalized_replica is not None
            try:
                cached = await asyncio.to_thread(replica_cache.get, normalized_replica)
            except Exception:
                logger.exception(
                    "Replica cache read failed for code=%s", normalized_replica
                )
                await interaction.followup.send(
                    GENERIC_CACHE_READ_ERROR, ephemeral=True
                )
                return

            if cached is not None:
                # Cache hit. Reuse the URL minted by a prior confirmed OCR
                # (possibly from a different guild — codes are global).
                logger.info(
                    "add-team cache HIT for code=%s -> %s",
                    normalized_replica,
                    cached.pokepaste_url,
                )
                canonical_url = cached.pokepaste_url
                canonical_url_for_dedup = canonical_url
            else:
                # Cold OCR path: at least page1 must be present. page2 is
                # optional — the extractor's prompt handles single-image
                # stitched submissions.
                if page1 is None:
                    await interaction.followup.send(
                        f"Code `{normalized_replica}` isn't in the cache yet. "
                        "Either provide a Pokepaste URL too, or attach "
                        "**page1** (and optionally page2) screenshots of the "
                        "Replica share screen so I can OCR the team. A "
                        "single stitched image of both pages works — just "
                        "attach as page1.",
                        ephemeral=True,
                    )
                    return

                try:
                    page1_bytes = await page1.read()
                    page2_bytes = await page2.read() if page2 is not None else None
                except (discord.HTTPException, discord.NotFound):
                    logger.warning(
                        "Failed to download replica screenshots for code=%s",
                        normalized_replica,
                        exc_info=True,
                    )
                    await interaction.followup.send(
                        "Couldn't download those attachments — please try "
                        "uploading the screenshots again.",
                        ephemeral=True,
                    )
                    return

                try:
                    team = await extract_team_from_screenshots(
                        anthropic_client, page1_bytes, page2_bytes
                    )
                except ExtractionError as exc:
                    await interaction.followup.send(str(exc), ephemeral=True)
                    return

                # Team ID mismatch check — the share screen prints the code
                # at the top of both pages, so the model usually reads it
                # back. A mismatch is almost always a wrong-attachment user
                # error; refusing keeps the cache from being seeded with the
                # wrong (code -> URL) pair that would mislead future lookups.
                if team.team_id is not None and team.team_id != normalized_replica:
                    logger.warning(
                        "Team ID mismatch: submitted=%s extracted=%s",
                        normalized_replica,
                        team.team_id,
                    )
                    await interaction.followup.send(
                        f"The screenshots show Team ID `{team.team_id}`, but "
                        f"you submitted code `{normalized_replica}`. "
                        "Double-check the attachments and re-run with the "
                        "correct screenshots.",
                        ephemeral=True,
                    )
                    return

                # Preview + Confirm/Cancel gate. The view is the v1 safety
                # net for the global cache — only confirmed extractions are
                # minted to pokepast.es and written to Firestore.
                view = ReplicaPreviewView(
                    interaction.user.id,
                    timeout=config.REPLICA_PREVIEW_TIMEOUT_SECONDS,
                )
                preview_embed = team_to_embed(
                    team,
                    code=normalized_replica,
                    description=description,
                    fmt_name=fmt_name,
                )
                await interaction.edit_original_response(
                    content=(
                        "Extracted from your screenshots. **Confirm** to "
                        "upload to pokepast.es and add to the bank; "
                        "**Cancel** to discard."
                    ),
                    embed=preview_embed,
                    view=view,
                )
                preview_shown = True

                await view.wait()

                if view.decision is not True:
                    outcome = (
                        "Cancelled." if view.decision is False else "Preview timed out."
                    )
                    await interaction.edit_original_response(
                        content=outcome, embed=None, view=None
                    )
                    logger.info(
                        "add-team preview declined for code=%s decision=%s",
                        normalized_replica,
                        view.decision,
                    )
                    return

                await interaction.edit_original_response(
                    content="Uploading to pokepast.es and adding to the bank…",
                    embed=None,
                    view=None,
                )

                # Mint the URL and seed the cache. `create` is transactional
                # fail-if-exists; a concurrent OCR for the same code will
                # land here too and one will catch AlreadyExists — the
                # store re-reads and returns the winner's URL.
                try:
                    paste_text = render_showdown(team)
                    minted_url = await post_to_pokepaste(
                        paste_text, title=f"Replica {normalized_replica}"
                    )
                except PokepasteUploadError as exc:
                    await interaction.edit_original_response(
                        content=str(exc), embed=None, view=None
                    )
                    return

                entry = ReplicaCacheEntry(
                    pokepaste_url=minted_url,
                    species=[p.species for p in team.pokemon],
                    created_at=datetime.now(timezone.utc),
                    created_by_user_id=interaction.user.id,
                    created_by_guild_id=interaction.guild_id or 0,
                )
                try:
                    canonical_entry = await asyncio.to_thread(
                        replica_cache.create, normalized_replica, entry
                    )
                except Exception:
                    logger.exception(
                        "Replica cache write failed for code=%s",
                        normalized_replica,
                    )
                    await interaction.edit_original_response(
                        content=GENERIC_CACHE_WRITE_ERROR,
                        embed=None,
                        view=None,
                    )
                    return

                canonical_url = canonical_entry.pokepaste_url
                canonical_url_for_dedup = canonical_url

        # ----- Common write path -----------------------------------------
        # From here, both paths share the dedup → validate → add_row →
        # broadcast → species poll → invalidate flow.

        try:
            existing = await sheets.find_row_by_url(sheet_name, canonical_url_for_dedup)
        except Exception:
            logger.exception("Failed to check for existing team")
            await _send_status(interaction, preview_shown, GENERIC_SHEET_READ_ERROR)
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
            await _send_status(
                interaction,
                preview_shown,
                f"This Pokepaste is already in *{fmt_name}* on row "
                f'{existing.row_number}: "{existing_desc}".',
            )
            return

        try:
            await validate_pokepaste_url(canonical_url)
        except ValidationError as e:
            await _send_status(interaction, preview_shown, str(e))
            return

        try:
            row = await sheets.add_row(
                sheet_name,
                canonical_url,
                description,
                normalized_replica,
                paste_type_value,
            )
        except Exception:
            logger.exception("Failed to add row")
            await _send_status(interaction, preview_shown, GENERIC_SHEET_WRITE_ERROR)
            return

        msg = f"Added team to row {row} in *{fmt_name}*."
        await _send_status(interaction, preview_shown, msg)

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
                url=canonical_url,
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
            await _send_status(
                interaction,
                preview_shown,
                f"{msg}\nParsed: {', '.join(species)}",
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
