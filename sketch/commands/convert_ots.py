"""`/convert-ots` — convert an Open Team Sheet into a Closed Team Sheet.

Accepts an OTS (Pokemon species / ability / item / moves / nature, but no
EVs) via a Pokepaste URL or as pasted Showdown text, fills in likely EV
spreads using the guild's team bank (with an LLM fallback for Pokemon
with no bank match), and returns a new Pokepaste URL for the trained team.

The converted team is NOT added to the bank — OTS teams are ephemeral
scouting aids, not persistent team records.

Two input paths:
  - `url` given → defer ephemerally, fetch the raw paste, convert, mint.
  - `url` omitted → open a modal so the user can paste multi-line
    Showdown text directly (slash-command string params are single-line).
"""

from __future__ import annotations

import logging

import anthropic
import discord
from discord import app_commands

from sketch import config
from sketch.commands._shared import (
    GUILD_ONLY_ERROR,
    UNCONFIGURED_GUILD_ERROR,
    _format_choices,
    _resolve_guild_sheets,
    _with_trace,
)
from sketch.convert.converter import ConvertResult, convert_ots_to_cts
from sketch.convert.ev_model import UnsupportedFormatError
from sketch.convert.llm_guess import EvGuessError
from sketch.logging_setup import trace_id_var
from sketch.pokepaste.fetcher import PokepasteFetchError
from sketch.pokepaste.uploader import PokepasteUploadError, post_to_pokepaste
from sketch.pokepaste.validator import ValidationError
from sketch.showdown.parser import ShowdownParseError, parse_showdown
from sketch.showdown.renderer import render_showdown
from sketch.storage.sheets_client import SheetsClient, SheetsClientRegistry
from sketch.team import STAT_DISPLAY, TeamData
from sketch.teamsource import fetch_team_from_url
from sketch.vrpaste.fetcher import VRPasteFetchError

logger = logging.getLogger(__name__)

# Discord paragraph TextInput max. A Champions Showdown export tops out
# around 1k chars, so 4000 is a comfortable margin.
_PASTE_INPUT_MAX = 4000


def _pin_suffix(pinned: tuple[str, ...]) -> str:
    """Render the ` (HP, Spe pinned)` note for stats kept from the paste."""
    if not pinned:
        return ""
    names = ", ".join(STAT_DISPLAY[k] for k in pinned)
    return f" ({names} pinned)"


def _source_summary(result: ConvertResult) -> str:
    """Build the user-facing provenance reply.

    Returns a summary line plus a per-mon attribution block so the user
    can see exactly which bank team each spread was sourced from, plus any
    stats pinned from a partial spread the caller supplied.

    Example:
        Trained 6 mons (3 from bank, 1 from usage stats, 1 estimated, 1 kept).
        • Venusaur — pokepast.es/abc123 (HP pinned)
        • Incineroar — usage stats (high)
        • Charizard — estimated
        • Garchomp — pokepast.es/def456
        ...
    """
    slots = result.slots

    from_bank = sum(1 for s in slots if s.source.label == "bank")
    from_usage = sum(1 for s in slots if s.source.label == "usage")
    estimated = sum(1 for s in slots if s.source.label == "estimated")
    kept = sum(1 for s in slots if s.source.label == "kept")

    parts: list[str] = []
    if from_bank:
        parts.append(f"{from_bank} from bank")
    if from_usage:
        parts.append(f"{from_usage} from usage stats")
    if estimated:
        parts.append(f"{estimated} estimated")
    if kept:
        parts.append(f"{kept} already trained")

    total = len(slots)
    detail = ", ".join(parts) if parts else "0 matched"
    headline = f"Trained {total} mons ({detail})."

    lines = [headline]
    for slot in slots:
        name = slot.pokemon.species
        suffix = _pin_suffix(slot.source.pinned)
        if slot.source.label == "bank" and slot.source.url:
            # Full URL so Discord renders it as a clickable hyperlink.
            lines.append(f"• {name} — {slot.source.url}{suffix}")
        elif slot.source.label == "usage":
            band = f" ({slot.source.confidence})" if slot.source.confidence else ""
            lines.append(f"• {name} — usage stats{band}{suffix}")
        elif slot.source.label == "estimated":
            lines.append(f"• {name} — estimated{suffix}")
        # "kept" mons are not listed to keep the reply concise.

    return "\n".join(lines)


async def _run_conversion(
    interaction: discord.Interaction,
    *,
    sheets: SheetsClient,
    fmt_name: str,
    sheet_name: str,
    ots: TeamData,
    anthropic_client: anthropic.AsyncAnthropic,
) -> None:
    """Core pipeline: convert → render → mint → reply.

    Accepts an already-parsed `TeamData` so URL sources (Pokepaste and
    VRPaste) and the modal text path all converge after OTS resolution.
    """
    try:
        result: ConvertResult = await convert_ots_to_cts(
            ots,
            sheets=sheets,
            sheet_name=sheet_name,
            fmt_name=fmt_name,
            anthropic_client=anthropic_client,
        )
    except UnsupportedFormatError as exc:
        await interaction.followup.send(_with_trace(str(exc)), ephemeral=True)
        return
    except EvGuessError as exc:
        await interaction.followup.send(_with_trace(str(exc)), ephemeral=True)
        return

    paste_text = render_showdown(result.team)
    try:
        cts_url = await post_to_pokepaste(paste_text, title="OTS→CTS")
    except PokepasteUploadError as exc:
        await interaction.followup.send(_with_trace(str(exc)), ephemeral=True)
        return

    summary = _source_summary(result)
    await interaction.followup.send(
        f"{summary}\n{cts_url}",
        ephemeral=True,
    )


class _OtsPasteModal(discord.ui.Modal, title="Paste OTS Showdown text"):
    """Modal for the no-URL path: accept multi-line Showdown text directly."""

    paste_input: discord.ui.TextInput = discord.ui.TextInput(
        label="OTS Showdown text",
        style=discord.TextStyle.paragraph,
        placeholder="Paste the 6-Pokemon OTS export here…",
        max_length=_PASTE_INPUT_MAX,
        required=True,
    )

    def __init__(
        self,
        *,
        sheets: SheetsClient,
        fmt_name: str,
        sheet_name: str,
        anthropic_client: anthropic.AsyncAnthropic,
    ) -> None:
        super().__init__()
        self._sheets = sheets
        self._fmt_name = fmt_name
        self._sheet_name = sheet_name
        self._anthropic_client = anthropic_client

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Acknowledge the modal submission; Discord requires a response
        # within 3 s. Deferring ephemerally keeps the "thinking…" spinner
        # visible while the conversion runs.
        await interaction.response.defer(ephemeral=True, thinking=True)
        ots_text = self.paste_input.value or ""
        try:
            ots = parse_showdown(ots_text)
        except ShowdownParseError as exc:
            await interaction.followup.send(_with_trace(str(exc)), ephemeral=True)
            return
        await _run_conversion(
            interaction,
            sheets=self._sheets,
            fmt_name=self._fmt_name,
            sheet_name=self._sheet_name,
            ots=ots,
            anthropic_client=self._anthropic_client,
        )


def register(
    tree: app_commands.CommandTree,
    registry: SheetsClientRegistry,
    *,
    anthropic_client: anthropic.AsyncAnthropic,
) -> None:
    """Register the /convert-ots slash command on `tree`."""

    @tree.command(
        name="convert-ots",
        description="Fill in EVs for an OTS team and return a trained Pokepaste URL.",
    )
    @app_commands.describe(
        format="Format/regulation (determines the EV regime)",
        url=(
            "Pokepaste or VRPaste URL of the OTS. Omit to paste Showdown text directly."
        ),
    )
    @app_commands.choices(format=_format_choices())
    async def convert_ots(
        interaction: discord.Interaction,
        format: app_commands.Choice[str],  # noqa: A002 — Discord names this 'format'
        url: str | None = None,
    ) -> None:
        trace_id_var.set(str(interaction.id))

        fmt_name = format.value
        sheet_name = config.FORMAT_SHEETS[fmt_name]

        # Resolve the guild's sheets before any further work. For the URL
        # path the interaction has already been deferred below; for the modal
        # path we haven't deferred yet — `_resolve_guild_sheets` calls
        # `followup.send`, which requires the interaction to be responded to.
        # We handle this by deferring before the URL fetch and sending the
        # modal *before* deferring for the text path.

        if url is not None:
            # URL path: defer now, validate the URL shape, fetch, convert.
            await interaction.response.defer(ephemeral=True, thinking=True)

            sheets = await _resolve_guild_sheets(interaction, registry)
            if sheets is None:
                return

            # `fetch_team_from_url` classifies the URL and returns a parsed
            # TeamData (or raises UnsupportedTeamUrlError, a ValidationError
            # subclass, for an unrecognized URL).
            try:
                ots = await fetch_team_from_url(url)
            except (
                VRPasteFetchError,
                PokepasteFetchError,
                ValidationError,
                ShowdownParseError,
            ) as exc:
                await interaction.followup.send(_with_trace(str(exc)), ephemeral=True)
                return

            logger.info(
                "convert-ots invoked by user_id=%s guild_id=%s: url=%s format=%s",
                interaction.user.id,
                interaction.guild_id,
                url,
                fmt_name,
            )

            await _run_conversion(
                interaction,
                sheets=sheets,
                fmt_name=fmt_name,
                sheet_name=sheet_name,
                ots=ots,
                anthropic_client=anthropic_client,
            )
            return

        # Modal (text-paste) path. We must respond to the interaction
        # before the 3 s window closes; send_modal IS the response here.
        # The SheetsClient is resolved inside the modal's on_submit after
        # the user submits (at which point the modal-submit interaction is
        # what we defer). We need the sheets client before opening the
        # modal so we can pass it in — but `_resolve_guild_sheets` needs a
        # deferred interaction to send a followup on failure. Work around
        # this by reading the registry directly for the guild-check (the
        # same null-check `_resolve_guild_sheets` does) and only calling
        # the full helper from `on_submit`.
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message(
                _with_trace(GUILD_ONLY_ERROR), ephemeral=True
            )
            return

        sheets = registry.get(guild_id)
        if sheets is None:
            await interaction.response.send_message(
                _with_trace(UNCONFIGURED_GUILD_ERROR), ephemeral=True
            )
            return

        logger.info(
            "convert-ots (modal) invoked by user_id=%s guild_id=%s: format=%s",
            interaction.user.id,
            guild_id,
            fmt_name,
        )

        await interaction.response.send_modal(
            _OtsPasteModal(
                sheets=sheets,
                fmt_name=fmt_name,
                sheet_name=sheet_name,
                anthropic_client=anthropic_client,
            )
        )
