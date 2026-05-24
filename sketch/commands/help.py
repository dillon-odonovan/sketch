"""`/help` — quick reference for the bot's commands.

Guild-agnostic by design: doesn't touch any sheet, so it works in DMs and
in unconfigured guilds without refusal.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from sketch import config
from sketch.logging_setup import trace_id_var

logger = logging.getLogger(__name__)


def register(tree: app_commands.CommandTree) -> None:
    """Register /help on the given tree."""

    @tree.command(
        name="help",
        description="How to use this bot.",
    )
    async def help_cmd(interaction: discord.Interaction) -> None:
        trace_id_var.set(str(interaction.id))
        logger.info("help invoked by user_id=%s", interaction.user.id)
        formats = ", ".join(config.FORMAT_SHEETS.keys())
        msg = (
            "**Sketch** — Pokepaste team bank\n\n"
            "`/add-team [url:<paste>] [replica:<code>] description:<text> "
            "format:Reg M-A [paste_type:Exact|Recreated|Unspecified] "
            "[page1:<image>] [page2:<image>]`\n"
            "  Add a team to the database. Provide a Pokepaste URL, a "
            "10-char Champions Team ID, or both. If you only have a Team ID "
            "we haven't seen before, attach screenshots of the Replica share "
            "screen (page1 and optionally page2 — or a single stitched image "
            "in page1) so I can OCR the team.\n"
            "  Examples:\n"
            "    `/add-team url:https://pokepast.es/abcd1234 "
            "description:Calyrex-S balance`\n"
            "    `/add-team replica:QBXXWXL05U description:my-team "
            "page1:<screenshot>`\n\n"
            "`/search-teams format:Reg M-A [mon1:<name>] ... [mon6:<name>] "
            "[description:<text>] [url:<paste>]`\n"
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
