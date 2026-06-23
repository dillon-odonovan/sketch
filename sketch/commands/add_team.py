"""`/add-team` — the unified entry point for adding a team to the bank.

Accepts any of:
  - url only         → existing Pokepaste-URL flow.
  - replica only     → cache lookup; on hit reuse the URL, on miss prompt
                       for screenshots and run the OCR + confirm + mint +
                       cache-seed pipeline.
  - url + replica    → URL flow, plus the paste contents are fetched and
                       seeded into the global Replica Cache keyed on the
                       code (so a later replica-only add skips OCR). No
                       OCR is run — we read the species list from the
                       fetched paste itself.

Validation: at least one of url / replica must be present. When a replica
code is supplied, a row for that code already in this guild's sheet blocks
the add up front (a Team ID is one physical team — a second row for it,
even under a different URL, is a duplicate).

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
from sketch.champions.extractor import (
    ExtractionError,
    extract_team_from_screenshots,
)
from sketch.champions.preview_view import ReplicaPreviewView
from sketch.champions.replica_cache import ReplicaCacheEntry, ReplicaCacheStore
from sketch.champions.replica_validator import normalize_replica
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
    _resolve_format,
    _resolve_guild_sheets,
    _with_trace,
)
from sketch.logging_setup import trace_id_var
from sketch.pokepaste.fetcher import PokepasteFetchError, fetch_pokepaste_raw
from sketch.pokepaste.uploader import PokepasteUploadError, post_to_pokepaste
from sketch.pokepaste.validator import (
    ValidationError,
    canonicalize_pokepaste_url,
    validate_pokepaste_url,
)
from sketch.showdown.parser import extract_species
from sketch.showdown.renderer import render_showdown
from sketch.storage.guild_config import GuildConfigStore
from sketch.storage.sheets_client import SheetsClient, SheetsClientRegistry
from sketch.team import TeamData
from sketch.teamsource import (
    TeamUrlSource,
    classify_team_url,
    unsupported_team_url_message,
)
from sketch.vrpaste.cache import VRPasteCacheStore
from sketch.vrpaste.fetcher import VRPasteFetchError, fetch_vrpaste
from sketch.vrpaste.validator import (
    canonicalize_vrpaste_url,
    extract_vrpaste_id,
)

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
    format_choice: app_commands.Choice[str] | None,
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
            _with_trace(
                "Provide a **Pokepaste URL** or a **Champions Team ID** (or "
                "both). At least one is required."
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


async def _check_replica_already_in_sheet(
    interaction: discord.Interaction,
    sheets: SheetsClient,
    inputs: _AddTeamInputs,
) -> bool:
    """Return True and respond to the user when this Team ID already has a row.

    A Champions Team ID identifies one physical team, so a second row for
    the same code in this guild's sheet is a duplicate even when the
    supplied Pokepaste URL differs (an exact paste vs a recreation).

    Returns True when it has already responded to the user (duplicate
    found or read error) and the caller should stop; False to proceed.
    """
    if inputs.replica is None:
        return False
    try:
        existing = await sheets.find_row_by_replica(inputs.sheet_name, inputs.replica)
    except Exception:
        logger.exception(
            "Failed to check for existing replica row for code=%s", inputs.replica
        )
        await _edit_status(interaction, GENERIC_SHEET_READ_ERROR)
        return True
    if existing is None:
        return False
    logger.info(
        "add-team replica dedup hit: code=%s guild_id=%s format=%s row=%d",
        inputs.replica,
        interaction.guild_id,
        inputs.fmt_name,
        existing.row_number,
    )
    existing_desc = existing.description or "(no description)"
    await _edit_status(
        interaction,
        f"Team ID `{inputs.replica}` is already in *{inputs.fmt_name}* on row "
        f'{existing.row_number}: "{existing_desc}".',
    )
    return True


async def _resolve_canonical_url(
    interaction: discord.Interaction,
    *,
    inputs: _AddTeamInputs,
    replica_cache: ReplicaCacheStore,
    vrpaste_cache: VRPasteCacheStore,
    anthropic_client: anthropic.AsyncAnthropic,
) -> tuple[str, str, bool] | None:
    """Figure out which Pokepaste URL to write to the sheet.

    Five paths converge on `(canonical_url, canonical_url_for_dedup)`:
      - Pokepaste URL supplied → use it directly.
      - VRPaste URL supplied → fetch its team, mint a Pokepaste, store
        the Pokepaste URL; results in a Pokepaste URL on the sheet.
      - Replica-only + full cache hit (URL set) → reuse the cached URL.
      - Replica-only + partial cache hit (paste cached but URL is None
        from a prior failed upload) → re-mint using the cached paste,
        no OCR needed.
      - Replica-only + cache miss → OCR the screenshots, show a
        Confirm / Cancel preview, write the parsed paste to the cache,
        mint a pokepast.es URL, upgrade the cache entry with the URL.

    Returns `(canonical_url, canonical_url_for_dedup, preview_shown)`
    on success, or None when the user cancelled, the OCR failed, or
    any other error path was already responded to via `followup.send`
    / `edit_original_response`.

    `canonical_url` preserves the user's spelling when they supplied a
    Pokepaste URL (so the sheet shows what they typed); the VRPaste
    branch sets it to the minted Pokepaste URL since that's what gets
    written. `canonical_url_for_dedup` is always the canonicalized
    Pokepaste form for `find_row_by_url` comparison.
    """
    if inputs.url is not None:
        kind = classify_team_url(inputs.url)
        if kind is TeamUrlSource.VRPASTE:
            return await _resolve_via_vrpaste(
                interaction,
                inputs=inputs,
                vrpaste_cache=vrpaste_cache,
            )
        if kind is TeamUrlSource.POKEPASTE:
            # is_pokepaste_url and canonicalize share one regex, so this
            # won't raise once the URL is classified as a Pokepaste.
            return inputs.url, canonicalize_pokepaste_url(inputs.url), False
        await interaction.followup.send(
            _with_trace(unsupported_team_url_message(inputs.url)),
            ephemeral=True,
        )
        return None

    # Replica-only path. `inputs.replica` is non-None by `_normalize_inputs`.
    assert inputs.replica is not None
    try:
        cached = await asyncio.to_thread(replica_cache.get, inputs.replica)
    except Exception:
        logger.exception("Replica cache read failed for code=%s", inputs.replica)
        await interaction.followup.send(
            _with_trace(GENERIC_CACHE_READ_ERROR), ephemeral=True
        )
        return None

    if cached is not None and cached.pokepaste_url is not None:
        logger.info(
            "add-team cache HIT for code=%s -> %s",
            inputs.replica,
            cached.pokepaste_url,
        )
        return cached.pokepaste_url, cached.pokepaste_url, False

    if cached is not None:
        # Partial cache hit: the OCR was confirmed and the paste was
        # written, but the pokepast.es upload either hasn't happened
        # yet or failed last time. Retry the upload using the cached
        # paste — no need to re-OCR, no need for screenshots, no need
        # for the preview gate (user already confirmed once).
        logger.info(
            "add-team cache PARTIAL HIT for code=%s; retrying mint",
            inputs.replica,
        )
        url = await _retry_mint_from_cached_paste(
            interaction, replica_cache, inputs, cached
        )
        if url is None:
            return None
        return url, url, False

    # Cache miss: hand off to the OCR sub-flow so this function stays a
    # thin URL/cache-hit/miss decision tree.
    return await _resolve_via_ocr(
        interaction,
        inputs=inputs,
        replica_cache=replica_cache,
        anthropic_client=anthropic_client,
    )


async def _resolve_via_vrpaste(
    interaction: discord.Interaction,
    *,
    inputs: _AddTeamInputs,
    vrpaste_cache: VRPasteCacheStore,
) -> tuple[str, str, bool] | None:
    """Fetch a VRPaste, mint a Pokepaste from it, return the Pokepaste URL.

    Cache-first: re-submissions of the same VRPaste reuse the previously
    minted Pokepaste URL, which restores sheet-level dedup (without the
    cache, two submissions of the same VRPaste would mint two distinct
    pokepast.es URLs and land on two distinct sheet rows).

    No preview gate, unlike the Champions OCR flow: the VRPaste backend
    returns structured JSON we trust the same way we'd trust a user
    typing a pokepast.es URL directly.

    Returns `(pokepaste_url, pokepaste_url, preview_shown=False)` — both
    fields are the minted URL because that's what gets written to the
    sheet and that's what dedup keys on.
    """
    assert inputs.url is not None
    try:
        vrpaste_id = extract_vrpaste_id(inputs.url)
        canonical_vrpaste_url = canonicalize_vrpaste_url(inputs.url)
    except ValidationError as e:
        await interaction.followup.send(_with_trace(str(e)), ephemeral=True)
        return None

    try:
        cached = await asyncio.to_thread(vrpaste_cache.get, vrpaste_id)
    except Exception:
        logger.exception("VRPaste cache read failed for id=%s", vrpaste_id)
        await interaction.followup.send(
            _with_trace(GENERIC_CACHE_READ_ERROR), ephemeral=True
        )
        return None

    if cached is not None:
        logger.info(
            "add-team VRPaste cache HIT for id=%s -> %s",
            vrpaste_id,
            cached.pokepaste_url,
        )
        return cached.pokepaste_url, cached.pokepaste_url, False

    await interaction.edit_original_response(
        content="Fetching team from VRPaste and uploading to pokepast.es…",
        embed=None,
        view=None,
    )
    try:
        team = await fetch_vrpaste(canonical_vrpaste_url)
    except VRPasteFetchError as exc:
        await _edit_status(interaction, _with_trace(str(exc)))
        return None

    paste_text = render_showdown(team)
    try:
        minted_url = await post_to_pokepaste(paste_text, title=f"VRPaste {vrpaste_id}")
    except PokepasteUploadError as exc:
        await _edit_status(interaction, _with_trace(str(exc)))
        return None

    # Race-safe cache write: `create` is fail-if-exists. If two
    # submissions of the same VRPaste both make it past the cache.get
    # check above (they minted in parallel), only the first create
    # wins. The loser gets back the winner's entry and uses the
    # winner's URL — so both sheet writes converge on a single
    # Pokepaste URL and dedup catches the second row. The loser's
    # minted paste becomes a harmless orphan on pokepast.es.
    guild_id_for_audit = (
        interaction.guild_id
        if interaction.guild_id is not None
        else interaction.user.id
    )
    try:
        entry = await asyncio.to_thread(
            vrpaste_cache.create,
            vrpaste_id,
            minted_url,
            user_id=interaction.user.id,
            guild_id=guild_id_for_audit,
        )
    except Exception:
        # Cache write failed entirely (transport error, etc.). Fall
        # back to our minted URL — we'd rather write a (potentially
        # duplicate) row than fail the whole command after a successful
        # mint. The next submission for this id re-mints; persistent
        # outages surface in the warning log.
        logger.warning(
            "VRPaste cache create failed for id=%s -> %s; using minted URL "
            "without dedup convergence",
            vrpaste_id,
            minted_url,
            exc_info=True,
        )
        canonical_url = minted_url
    else:
        canonical_url = entry.pokepaste_url
        if canonical_url != minted_url:
            logger.info(
                "add-team VRPaste id=%s lost race; using winner's URL %s "
                "(local mint %s orphaned on pokepast.es)",
                vrpaste_id,
                canonical_url,
                minted_url,
            )
        else:
            logger.info(
                "add-team VRPaste id=%s minted -> %s",
                vrpaste_id,
                minted_url,
            )

    return canonical_url, canonical_url, False


async def _resolve_via_ocr(
    interaction: discord.Interaction,
    *,
    inputs: _AddTeamInputs,
    replica_cache: ReplicaCacheStore,
    anthropic_client: anthropic.AsyncAnthropic,
) -> tuple[str, str, bool] | None:
    """OCR-path orchestrator for a cache-missed replica submission.

    Steps: download attachments → extract team → confirm with the user
    → render paste text → seed the cache (paste_text, url=None) → mint
    pokepast.es URL → upgrade the cache entry with the URL. Any step
    returning None (validation failure, user cancel, transport error)
    short-circuits; the sub-step has already responded to the user.

    Seeding the cache BEFORE the mint is what makes a failed mint
    recoverable — the next /add-team for the same code hits the
    partial-cache-hit branch and retries the upload without re-OCR.
    """
    if inputs.page1 is None:
        await interaction.followup.send(
            _with_trace(
                f"Code `{inputs.replica}` isn't in the cache yet. Either provide "
                "a Pokepaste URL too, or attach **page1** (and optionally page2) "
                "screenshots of the Replica share screen so I can OCR the team. "
                "A single stitched image of both pages works — just attach as "
                "page1."
            ),
            ephemeral=True,
        )
        return None

    screenshots = await _download_screenshots(interaction, inputs)
    if screenshots is None:
        return None

    team = await _extract_and_validate_team(
        interaction, anthropic_client, inputs, *screenshots
    )
    if team is None:
        return None

    confirmed_team = await _confirm_preview(interaction, inputs, team)
    if confirmed_team is None:
        return None

    # Render once, here. The text is what gets cached AND what we POST
    # to pokepast.es — keeping the two in lockstep avoids any drift
    # between cache content and minted paste. `confirmed_team` may
    # differ from the original OCR output if the user used the Edit
    # button before confirming.
    paste_text = render_showdown(confirmed_team)

    cache_entry = await _seed_cache_with_paste(
        interaction, replica_cache, inputs, confirmed_team, paste_text
    )
    if cache_entry is None:
        return None

    # If we lost a race, the existing entry might already have a URL.
    # Skip the mint and use it directly.
    if cache_entry.pokepaste_url is not None:
        logger.info(
            "add-team cache race for code=%s; using winner's URL %s",
            inputs.replica,
            cache_entry.pokepaste_url,
        )
        return cache_entry.pokepaste_url, cache_entry.pokepaste_url, True

    minted_url = await _mint_pokepaste(interaction, inputs, cache_entry.paste_text)
    if minted_url is None:
        return None

    await _attach_url_to_cache(replica_cache, inputs, minted_url)
    return minted_url, minted_url, True


async def _retry_mint_from_cached_paste(
    interaction: discord.Interaction,
    replica_cache: ReplicaCacheStore,
    inputs: _AddTeamInputs,
    cache_entry: ReplicaCacheEntry,
) -> str | None:
    """Re-attempt the pokepast.es upload for a partial cache entry.

    Used when a previous /add-team for the same code confirmed the OCR
    and wrote the parsed paste to Firestore, but the upload itself
    failed. The paste text is the same as the confirmed version — no
    OCR, no preview, just upload + cache upgrade.
    """
    assert cache_entry.pokepaste_url is None
    await interaction.edit_original_response(
        content="Retrying pokepast.es upload from the cached paste…",
        embed=None,
        view=None,
    )
    minted_url = await _mint_pokepaste(interaction, inputs, cache_entry.paste_text)
    if minted_url is None:
        return None
    await _attach_url_to_cache(replica_cache, inputs, minted_url)
    return minted_url


async def _seed_cache_with_paste(
    interaction: discord.Interaction,
    replica_cache: ReplicaCacheStore,
    inputs: _AddTeamInputs,
    team: TeamData,
    paste_text: str,
) -> ReplicaCacheEntry | None:
    """Write the confirmed OCR'd team to the cache with `pokepaste_url=None`.

    The transactional `create` collapses concurrent OCR for the same
    code: the loser gets back the winner's entry (which may or may not
    have a URL set yet — caller decides what to do next).
    """
    entry = ReplicaCacheEntry(
        paste_text=paste_text,
        pokepaste_url=None,
        species=[p.species for p in team.pokemon],
        created_at=datetime.now(timezone.utc),
        created_by_user_id=interaction.user.id,
        # In a guild interaction `guild_id` is the source of truth; fall
        # back to the user's id for the rare DM-context case so the
        # audit column always has a traceable snowflake.
        created_by_guild_id=(
            interaction.guild_id
            if interaction.guild_id is not None
            else interaction.user.id
        ),
    )
    assert inputs.replica is not None
    try:
        return await asyncio.to_thread(replica_cache.create, inputs.replica, entry)
    except Exception:
        logger.exception("Replica cache create failed for code=%s", inputs.replica)
        await interaction.edit_original_response(
            content=_with_trace(GENERIC_CACHE_WRITE_ERROR), embed=None, view=None
        )
        return None


async def _attach_url_to_cache(
    replica_cache: ReplicaCacheStore,
    inputs: _AddTeamInputs,
    url: str,
) -> None:
    """Upgrade the cached entry with the freshly-minted URL.

    Best-effort: if the cache write fails after a successful mint, the
    URL is still valid and the row write proceeds. The next /add-team
    for this code would re-mint a fresh URL (harmless — both URLs
    point at valid pastes of the same team — but worth logging at
    WARN so we notice if it becomes frequent).
    """
    assert inputs.replica is not None
    try:
        await asyncio.to_thread(replica_cache.set_url, inputs.replica, url)
    except Exception:
        logger.warning(
            "Failed to set pokepaste_url=%s for code=%s; cache will retry on "
            "next /add-team for this code",
            url,
            inputs.replica,
            exc_info=True,
        )


async def _seed_replica_cache_from_pokepaste_url(
    interaction: discord.Interaction,
    replica_cache: ReplicaCacheStore,
    inputs: _AddTeamInputs,
    *,
    pokepaste_url: str,
) -> None:
    """Seed the global Replica Cache from an already-resolved Pokepaste URL.

    Used when /add-team was given a `url` + `replica` (no screenshots):
    the paste already exists, so instead of OCR we fetch its raw Showdown
    text and cache it keyed on the code. The entry is complete from the
    start — both `paste_text` and `pokepaste_url` set — so a later
    replica-only add hits the full-cache branch and reuses the URL.

    Checks the cache first and only writes when the code isn't already
    present (skips the /raw fetch and the write for known codes).

    Best-effort throughout (mirrors `_attach_url_to_cache`): any fetch,
    parse, or cache failure is logged and swallowed — seeding must never
    fail the command, whose primary job (the sheet row) already
    succeeded.
    """
    assert inputs.replica is not None
    try:
        existing = await asyncio.to_thread(replica_cache.get, inputs.replica)
        if existing is not None:
            logger.info(
                "Replica cache already has code=%s; skipping url seed",
                inputs.replica,
            )
            return

        paste_text = await fetch_pokepaste_raw(pokepaste_url)
        entry = ReplicaCacheEntry(
            paste_text=paste_text,
            pokepaste_url=canonicalize_pokepaste_url(pokepaste_url),
            species=extract_species(paste_text),
            created_at=datetime.now(timezone.utc),
            created_by_user_id=interaction.user.id,
            created_by_guild_id=(
                interaction.guild_id
                if interaction.guild_id is not None
                else interaction.user.id
            ),
        )
        await asyncio.to_thread(replica_cache.create, inputs.replica, entry)
        logger.info(
            "Seeded replica cache from url for code=%s -> %s",
            inputs.replica,
            entry.pokepaste_url,
        )
    except PokepasteFetchError as exc:
        logger.warning(
            "Skipping replica cache seed for code=%s: %s", inputs.replica, exc
        )
    except Exception:
        logger.warning(
            "Failed to seed replica cache from url for code=%s",
            inputs.replica,
            exc_info=True,
        )


async def _download_screenshots(
    interaction: discord.Interaction,
    inputs: _AddTeamInputs,
) -> tuple[bytes, bytes | None] | None:
    """Read the user-attached page1 (and optional page2) into bytes.

    Returns the byte pair on success, None on Discord download failure
    (after sending an ephemeral retry message).
    """
    assert inputs.page1 is not None
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
            _with_trace(
                "Couldn't download those attachments — please try uploading the "
                "screenshots again."
            ),
            ephemeral=True,
        )
        return None
    return page1_bytes, page2_bytes


async def _extract_and_validate_team(
    interaction: discord.Interaction,
    anthropic_client: anthropic.AsyncAnthropic,
    inputs: _AddTeamInputs,
    page1_bytes: bytes,
    page2_bytes: bytes | None,
) -> TeamData | None:
    """Run Claude vision OCR then cross-check the extracted Team ID
    against the user's submitted replica code.

    A Team-ID mismatch is almost always a wrong-attachment user error;
    refusing keeps the global cache from being seeded with the wrong
    (code → URL) pair, which would mislead every future lookup.
    """
    try:
        team = await extract_team_from_screenshots(
            anthropic_client, page1_bytes, page2_bytes
        )
    except ExtractionError as exc:
        await interaction.followup.send(_with_trace(str(exc)), ephemeral=True)
        return None

    if team.team_id is not None and team.team_id != inputs.replica:
        logger.warning(
            "Team ID mismatch: submitted=%s extracted=%s",
            inputs.replica,
            team.team_id,
        )
        await interaction.followup.send(
            _with_trace(
                f"The screenshots show Team ID `{team.team_id}`, but you "
                f"submitted code `{inputs.replica}`. Double-check the "
                "attachments and re-run with the correct screenshots."
            ),
            ephemeral=True,
        )
        return None

    return team


async def _confirm_preview(
    interaction: discord.Interaction,
    inputs: _AddTeamInputs,
    team: TeamData,
) -> TeamData | None:
    """Show the Confirm / Edit / Cancel preview and wait for the click.

    Returns the team the user approved (the original OCR output, or an
    edited version if they used the Edit button before confirming) on
    success, or None if they cancelled or the preview timed out (after
    editing the response to reflect the outcome). The cache only ever
    ingests human-confirmed extractions.
    """
    view = ReplicaPreviewView(
        interaction.user.id,
        team=team,
        code=inputs.replica,
        description=inputs.description,
        fmt_name=inputs.fmt_name,
        timeout=config.REPLICA_PREVIEW_TIMEOUT_SECONDS,
    )
    await interaction.edit_original_response(
        content=(
            "Extracted from your screenshots. **Confirm** to upload to "
            "pokepast.es and add to the bank, **Edit** to fix the parsed "
            "team first, or **Cancel** to discard."
        ),
        embed=view.render_embed(),
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
    return view.team


async def _mint_pokepaste(
    interaction: discord.Interaction,
    inputs: _AddTeamInputs,
    paste_text: str,
) -> str | None:
    """POST the already-rendered Showdown text to pokepast.es.

    Returns the new paste's canonical URL on success, None on upload
    failure (after editing the response to show the error message).
    Takes pre-rendered `paste_text` rather than a `TeamData` so the
    same helper handles both fresh-OCR mints and partial-cache-hit
    retries (the latter has only the cached paste_text, not the team).
    """
    await interaction.edit_original_response(
        content="Uploading to pokepast.es and adding to the bank…",
        embed=None,
        view=None,
    )
    try:
        return await post_to_pokepaste(paste_text, title=f"Replica {inputs.replica}")
    except PokepasteUploadError as exc:
        await interaction.edit_original_response(
            content=_with_trace(str(exc)), embed=None, view=None
        )
        return None


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

    Dedup checks both Team ID (replica column) and Pokepaste URL so that
    a second row for the same physical team is rejected regardless of
    which identifier was supplied.
    """
    if await _check_replica_already_in_sheet(interaction, sheets, inputs):
        return

    try:
        existing = await sheets.find_row_by_url(
            inputs.sheet_name, canonical_url_for_dedup
        )
    except Exception:
        logger.exception("Failed to check for existing team")
        await _edit_status(interaction, _with_trace(GENERIC_SHEET_READ_ERROR))
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
            _with_trace(
                f"This Pokepaste is already in *{inputs.fmt_name}* on row "
                f'{existing.row_number}: "{existing_desc}".'
            ),
        )
        return

    try:
        await validate_pokepaste_url(canonical_url)
    except ValidationError as e:
        await _edit_status(interaction, _with_trace(str(e)))
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
        await _edit_status(interaction, _with_trace(GENERIC_SHEET_WRITE_ERROR))
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
            "Skipping broadcast for guild_id=%s: no broadcast_channel_id configured",
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
    vrpaste_cache: VRPasteCacheStore,
    anthropic_client: anthropic.AsyncAnthropic,
) -> None:
    """Register the /add-team slash command on the given tree.

    Captures `store`, `registry`, the two source-specific caches, and
    `anthropic_client` in the handler's closure so each invocation can
    route to the right spreadsheet, look up the broadcast channel, hit
    the appropriate paste-source cache, and drive Claude vision without
    re-deriving the dependencies on every call.
    """

    @tree.command(
        name="add-team",
        description=(
            "Add a team to the bank — by Pokepaste URL, VRPaste URL, "
            "Champions Team ID, or a URL + Team ID."
        ),
    )
    @app_commands.describe(
        url=(
            "Pokepaste URL (e.g., https://pokepast.es/abc123) or VRPaste "
            "URL (e.g., https://www.vrpastes.com/abc123). Required unless "
            "you provide a Team ID instead."
        ),
        replica=(
            "10-character Champions Team ID (e.g. 'QBXXWXL05U'). Required "
            "unless you provide a URL instead."
        ),
        description="Short description of the team (e.g., 'Calyrex-S balance')",
        format=f"Format/regulation. Defaults to {config.DEFAULT_FORMAT} if omitted.",
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
        paste_type: app_commands.Choice[str],
        format: app_commands.Choice[str] | None = None,
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
            vrpaste_cache=vrpaste_cache,
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

        # url + replica: seed the global cache from the paste so a later
        # replica-only add (any guild) skips OCR. Best-effort, post-commit
        # so it never delays or fails the sheet write. The OCR path seeds
        # the cache itself, so this is gated on a user-supplied url.
        if inputs.url is not None and inputs.replica is not None:
            await _seed_replica_cache_from_pokepaste_url(
                interaction,
                replica_cache,
                inputs,
                pokepaste_url=canonical_url_for_dedup,
            )
