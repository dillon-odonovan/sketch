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

The handler is broken into three async helpers below — `_normalize_inputs`,
`_resolve_canonical_url`, `_commit_team_row` — to keep each chunk small
enough to read top-to-bottom. The slash-command callback is a thin
orchestrator that wires them together.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
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
from sketch.storage.sheets_client import SheetsClient, SheetsClientRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _AddTeamInputs:
    """Normalized inputs for /add-team. Built by `_normalize_inputs`,
    consumed by `_resolve_canonical_url` and `_commit_team_row` so each
    helper has a single typed thing to read from."""

    description: str
    fmt_name: str
    sheet_name: str
    paste_type_value: str
    url: str | None
    replica: str | None  # already normalized via normalize_replica
    page1: discord.Attachment | None
    page2: discord.Attachment | None


async def _normalize_inputs(
    interaction: discord.Interaction,
    *,
    description: str,
    format_choice: app_commands.Choice[str],
    url: str | None,
    replica: str | None,
    paste_type: app_commands.Choice[str],
    page1: discord.Attachment | None,
    page2: discord.Attachment | None,
) -> _AddTeamInputs | None:
    """Validate the slash-command args and normalize them into one struct.

    Returns None and sends an ephemeral error response if any input fails
    validation (no url and no replica, malformed replica). Otherwise
    returns the `_AddTeamInputs` the rest of the handler consumes.
    """
    if url is None and replica is None:
        await interaction.followup.send(
            "Provide a **Pokepaste URL** or a **Champions Team ID** (or "
            "both). At least one is required.",
            ephemeral=True,
        )
        return None

    normalized_replica: str | None = None
    if replica is not None:
        try:
            normalized_replica = normalize_replica(replica)
        except ValidationError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return None

    fmt_name = format_choice.value
    return _AddTeamInputs(
        description=description,
        fmt_name=fmt_name,
        sheet_name=config.FORMAT_SHEETS[fmt_name],
        paste_type_value=paste_type.value,
        url=url,
        replica=normalized_replica,
        page1=page1,
        page2=page2,
    )


async def _resolve_canonical_url(
    interaction: discord.Interaction,
    *,
    inputs: _AddTeamInputs,
    replica_cache: ReplicaCacheStore,
    anthropic_client: anthropic.AsyncAnthropic,
) -> tuple[str, str, bool] | None:
    """Figure out which Pokepaste URL to write to the sheet.

    Three paths converge on `(canonical_url, canonical_url_for_dedup)`:
      - URL supplied by the user → use it directly.
      - Replica-only + cache hit → reuse the cached URL.
      - Replica-only + cache miss → OCR the screenshots, show a Confirm
        / Cancel preview, mint a pokepast.es URL on Confirm, write the
        cache entry, then use the (possibly race-loser) canonical URL.

    Returns `(canonical_url, canonical_url_for_dedup, preview_shown)` on
    success, or None when the user cancelled, the OCR failed, or any
    other error path was already responded to via `followup.send` /
    `edit_original_response`.

    `canonical_url` preserves the user's spelling when they supplied a
    URL (so the sheet shows what they typed); `canonical_url_for_dedup`
    is always the canonicalized form for `find_row_by_url` comparison.
    """
    if inputs.url is not None:
        try:
            canonical_url_for_dedup = canonicalize_pokepaste_url(inputs.url)
        except ValidationError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return None
        return inputs.url, canonical_url_for_dedup, False

    # Replica-only path. `inputs.replica` is non-None by `_normalize_inputs`.
    assert inputs.replica is not None
    try:
        cached = await asyncio.to_thread(replica_cache.get, inputs.replica)
    except Exception:
        logger.exception("Replica cache read failed for code=%s", inputs.replica)
        await interaction.followup.send(GENERIC_CACHE_READ_ERROR, ephemeral=True)
        return None

    if cached is not None:
        logger.info(
            "add-team cache HIT for code=%s -> %s",
            inputs.replica,
            cached.pokepaste_url,
        )
        return cached.pokepaste_url, cached.pokepaste_url, False

    # Cache miss → OCR path. At minimum page1 is required.
    if inputs.page1 is None:
        await interaction.followup.send(
            f"Code `{inputs.replica}` isn't in the cache yet. Either provide "
            "a Pokepaste URL too, or attach **page1** (and optionally page2) "
            "screenshots of the Replica share screen so I can OCR the team. "
            "A single stitched image of both pages works — just attach as "
            "page1.",
            ephemeral=True,
        )
        return None

    try:
        page1_bytes = await inputs.page1.read()
        page2_bytes = await inputs.page2.read() if inputs.page2 is not None else None
    except (discord.HTTPException, discord.NotFound):
        logger.warning(
            "Failed to download replica screenshots for code=%s",
            inputs.replica,
            exc_info=True,
        )
        await interaction.followup.send(
            "Couldn't download those attachments — please try uploading the "
            "screenshots again.",
            ephemeral=True,
        )
        return None

    try:
        team = await extract_team_from_screenshots(
            anthropic_client, page1_bytes, page2_bytes
        )
    except ExtractionError as exc:
        await interaction.followup.send(str(exc), ephemeral=True)
        return None

    # Team-ID mismatch check: the share screen prints the code at the top
    # of both pages, so the model usually reads it back. Mismatch is
    # almost always a wrong-attachment user error; refusing keeps the
    # cache clean from bad (code -> URL) pairs.
    if team.team_id is not None and team.team_id != inputs.replica:
        logger.warning(
            "Team ID mismatch: submitted=%s extracted=%s",
            inputs.replica,
            team.team_id,
        )
        await interaction.followup.send(
            f"The screenshots show Team ID `{team.team_id}`, but you "
            f"submitted code `{inputs.replica}`. Double-check the "
            "attachments and re-run with the correct screenshots.",
            ephemeral=True,
        )
        return None

    # Preview gate. Cache only ingests human-confirmed extractions.
    view = ReplicaPreviewView(
        interaction.user.id,
        timeout=config.REPLICA_PREVIEW_TIMEOUT_SECONDS,
    )
    preview_embed = team_to_embed(
        team,
        code=inputs.replica,
        description=inputs.description,
        fmt_name=inputs.fmt_name,
    )
    await interaction.edit_original_response(
        content=(
            "Extracted from your screenshots. **Confirm** to upload to "
            "pokepast.es and add to the bank; **Cancel** to discard."
        ),
        embed=preview_embed,
        view=view,
    )
    await view.wait()

    if view.decision is not True:
        outcome = "Cancelled." if view.decision is False else "Preview timed out."
        await interaction.edit_original_response(content=outcome, embed=None, view=None)
        logger.info(
            "add-team preview declined for code=%s decision=%s",
            inputs.replica,
            view.decision,
        )
        return None

    await interaction.edit_original_response(
        content="Uploading to pokepast.es and adding to the bank…",
        embed=None,
        view=None,
    )

    try:
        paste_text = render_showdown(team)
        minted_url = await post_to_pokepaste(
            paste_text, title=f"Replica {inputs.replica}"
        )
    except PokepasteUploadError as exc:
        await interaction.edit_original_response(
            content=str(exc), embed=None, view=None
        )
        return None

    # `create` is transactional fail-if-exists; a concurrent OCR for the
    # same code will land here too and one of us will catch AlreadyExists.
    # The store re-reads on that path and returns the winner's URL.
    entry = ReplicaCacheEntry(
        pokepaste_url=minted_url,
        species=[p.species for p in team.pokemon],
        created_at=datetime.now(timezone.utc),
        created_by_user_id=interaction.user.id,
        # In a guild interaction `guild_id` is the source of truth;
        # fall back to the user's id for the rare DM-context case so the
        # audit column always has something traceable.
        created_by_guild_id=(
            interaction.guild_id
            if interaction.guild_id is not None
            else interaction.user.id
        ),
    )
    try:
        canonical_entry = await asyncio.to_thread(
            replica_cache.create, inputs.replica, entry
        )
    except Exception:
        logger.exception("Replica cache write failed for code=%s", inputs.replica)
        await interaction.edit_original_response(
            content=GENERIC_CACHE_WRITE_ERROR, embed=None, view=None
        )
        return None

    canonical_url = canonical_entry.pokepaste_url
    return canonical_url, canonical_url, True


async def _commit_team_row(
    interaction: discord.Interaction,
    sheets: SheetsClient,
    *,
    store: GuildConfigStore,
    inputs: _AddTeamInputs,
    canonical_url: str,
    canonical_url_for_dedup: str,
) -> None:
    """Common write path: dedup, validate the URL, write the row,
    broadcast, poll species, invalidate the search snapshot.

    Shared by both the URL path and the post-OCR path — once
    `_resolve_canonical_url` settles on a URL, every other step is
    identical to the historical /add-team flow.
    """
    try:
        existing = await sheets.find_row_by_url(
            inputs.sheet_name, canonical_url_for_dedup
        )
    except Exception:
        logger.exception("Failed to check for existing team")
        await _edit_status(interaction, GENERIC_SHEET_READ_ERROR)
        return

    if existing is not None:
        logger.info(
            "add-team dedup hit: user_id=%s guild_id=%s format=%s row=%d",
            interaction.user.id,
            interaction.guild_id,
            inputs.fmt_name,
            existing.row_number,
        )
        existing_desc = existing.description or "(no description)"
        await _edit_status(
            interaction,
            f"This Pokepaste is already in *{inputs.fmt_name}* on row "
            f'{existing.row_number}: "{existing_desc}".',
        )
        return

    try:
        await validate_pokepaste_url(canonical_url)
    except ValidationError as e:
        await _edit_status(interaction, str(e))
        return

    try:
        row = await sheets.add_row(
            inputs.sheet_name,
            canonical_url,
            inputs.description,
            inputs.replica,
            inputs.paste_type_value,
        )
    except Exception:
        logger.exception("Failed to add row")
        await _edit_status(interaction, GENERIC_SHEET_WRITE_ERROR)
        return

    msg = f"Added team to row {row} in *{inputs.fmt_name}*."
    await _edit_status(interaction, msg)

    broadcast_message: discord.Message | None = None
    guild_cfg = (
        store.get(interaction.guild_id) if interaction.guild_id is not None else None
    )
    if guild_cfg and guild_cfg.broadcast_channel_id is not None:
        broadcast_message = await _broadcast_team_added(
            interaction,
            guild_cfg.broadcast_channel_id,
            fmt_name=inputs.fmt_name,
            url=canonical_url,
            description=inputs.description,
        )
    else:
        logger.info(
            "Skipping broadcast for guild_id=%s: no broadcast_channel_id " "configured",
            interaction.guild_id,
        )

    species = await _await_species(sheets, inputs.sheet_name, row)
    if species:
        await _edit_status(
            interaction,
            f"{msg}\nParsed: {', '.join(species)}",
        )
        if broadcast_message is not None:
            await _enrich_broadcast_with_species(broadcast_message, species)
        # Drop the cached search snapshot so the next /search-teams
        # rebuilds and includes this row. We deliberately wait until
        # species columns settle: invalidating earlier would just cause
        # `search_rows` to skip this row on the rebuild (it filters rows
        # whose species cells read "Loading..." / "#N/A"). On timeout the
        # snapshot stays stale, but the 5-minute TTL backstop in
        # SheetsClient eventually catches it.
        sheets.invalidate_snapshot(inputs.sheet_name)
    else:
        logger.info(
            "Species poll timed out for row %d in %s; skipping snapshot "
            "invalidation (TTL backstop will catch it)",
            row,
            inputs.sheet_name,
        )


async def _edit_status(interaction: discord.Interaction, content: str) -> None:
    """Replace the original (deferred) response's content with `content`,
    clearing any preview embed + view. Used for the common write path so
    both URL and OCR branches converge on a single ephemeral message.
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

    Captures `store`, `registry`, `replica_cache`, and `anthropic_client`
    in the handler's closure so each invocation can route to the right
    spreadsheet, look up the broadcast channel, hit the replica cache,
    and drive Claude vision without re-deriving the dependencies on
    every call.
    """

    @tree.command(
        name="add-team",
        description=(
            "Add a team to the bank — by Pokepaste URL, Champions Team ID, " "or both."
        ),
    )
    @app_commands.describe(
        url=(
            "Pokepaste URL (e.g., https://pokepast.es/abc123). Required "
            "unless you provide a Team ID instead."
        ),
        replica=(
            "10-character Champions Team ID (e.g. 'QBXXWXL05U'). Required "
            "unless you provide a URL instead."
        ),
        description="Short description of the team (e.g., 'Calyrex-S balance')",
        format="Format/regulation",
        paste_type="Whether this paste is exact, recreated, or unspecified",
        page1=(
            "Page 1 of the Champions Replica share screen. Only needed if "
            "you submitted a Team ID we haven't seen before (no URL). A "
            "single stitched image of both pages works — just attach it "
            "here and leave page2 empty."
        ),
        page2=(
            "Page 2 of the Champions Replica share screen. Optional even "
            "on first sighting — omit if page1 is already stitched with "
            "both pages."
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
        paste_type: app_commands.Choice[str],
        url: str | None = None,
        replica: str | None = None,
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

        inputs = await _normalize_inputs(
            interaction,
            description=description,
            format_choice=format,
            url=url,
            replica=replica,
            paste_type=paste_type,
            page1=page1,
            page2=page2,
        )
        if inputs is None:
            return

        logger.info(
            "add-team invoked by user_id=%s guild_id=%s: url=%s replica=%s "
            "description=%r format=%s paste_type=%s has_page1=%s has_page2=%s",
            interaction.user.id,
            interaction.guild_id,
            inputs.url,
            inputs.replica,
            inputs.description,
            inputs.fmt_name,
            inputs.paste_type_value,
            inputs.page1 is not None,
            inputs.page2 is not None,
        )

        resolved = await _resolve_canonical_url(
            interaction,
            inputs=inputs,
            replica_cache=replica_cache,
            anthropic_client=anthropic_client,
        )
        if resolved is None:
            return
        canonical_url, canonical_url_for_dedup, _preview_shown = resolved

        await _commit_team_row(
            interaction,
            sheets,
            store=store,
            inputs=inputs,
            canonical_url=canonical_url,
            canonical_url_for_dedup=canonical_url_for_dedup,
        )
