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
from sketch.champions.showdown_parser import ShowdownParseError, parse_showdown
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
from sketch.pokepaste.fetcher import PokepasteFetchError, fetch_pokepaste_raw
from sketch.pokepaste.renderer import (
    PokepasteUploadError,
    post_to_pokepaste,
    render_showdown,
)
from sketch.pokepaste.validator import ValidationError, is_pokepaste_url
from sketch.storage.sheets_client import SheetsClient, SheetsClientRegistry

logger = logging.getLogger(__name__)

# Discord paragraph TextInput max. A Champions Showdown export tops out
# around 1k chars, so 4000 is a comfortable margin.
_PASTE_INPUT_MAX = 4000


def _source_summary(
    result: ConvertResult,
    ots_pokemon_names: list[str],
) -> str:
    """Build the user-facing provenance reply.

    Returns a summary line plus a per-mon attribution block so the user
    can see exactly which bank team each spread was sourced from.

    Example:
        Trained 6 mons (4 from bank, 1 estimated, 1 already trained).
        • Venusaur — pokepast.es/abc123
        • Charizard — estimated
        • Garchomp — pokepast.es/def456
        ...
    """
    sources = result.sources
    source_urls = result.source_urls

    from_bank = sources.count("bank")
    estimated = sources.count("estimated")
    kept = sources.count("kept")

    parts: list[str] = []
    if from_bank:
        parts.append(f"{from_bank} from bank")
    if estimated:
        parts.append(f"{estimated} estimated")
    if kept:
        parts.append(f"{kept} already trained")

    total = len(sources)
    detail = ", ".join(parts) if parts else "0 matched"
    headline = f"Trained {total} mons ({detail})."

    lines = [headline]
    for name, src, url in zip(ots_pokemon_names, sources, source_urls, strict=False):
        if src == "bank" and url:
            # Shorten pokepast.es/abc123 to just the hostname+id for brevity.
            short = url.replace("https://", "").replace("http://", "")
            lines.append(f"• {name} — {short}")
        elif src == "estimated":
            lines.append(f"• {name} — estimated")
        # "kept" mons are not listed to keep the reply concise.

    return "\n".join(lines)


async def _run_conversion(
    interaction: discord.Interaction,
    *,
    sheets: SheetsClient,
    fmt_name: str,
    sheet_name: str,
    ots_text: str,
    anthropic_client: anthropic.AsyncAnthropic,
) -> None:
    """Core pipeline: parse → convert → render → mint → reply."""
    try:
        ots = parse_showdown(ots_text)
    except ShowdownParseError as exc:
        await interaction.followup.send(_with_trace(str(exc)), ephemeral=True)
        return

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

    pokemon_names = [p.species for p in ots.pokemon]
    summary = _source_summary(result, pokemon_names)
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
        await _run_conversion(
            interaction,
            sheets=self._sheets,
            fmt_name=self._fmt_name,
            sheet_name=self._sheet_name,
            ots_text=ots_text,
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
            "Pokepaste URL of the OTS (e.g. https://pokepast.es/abc123). "
            "Omit to paste Showdown text directly."
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

            if not is_pokepaste_url(url):
                await interaction.followup.send(
                    _with_trace(
                        f"`{url}` doesn't look like a Pokepaste URL. "
                        "Expected something like `https://pokepast.es/abc123`."
                    ),
                    ephemeral=True,
                )
                return

            try:
                ots_text = await fetch_pokepaste_raw(url)
            except (PokepasteFetchError, ValidationError) as exc:
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
                ots_text=ots_text,
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
