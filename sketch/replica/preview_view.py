"""Discord UI for the Confirm / Cancel gate on cold OCR.

The replica cache is global (cross-guild), so a bad OCR that lands in the
cache pollutes every future lookup of that code on every guild. The human
preview is the v1 safety gate: the invoking user sees the extracted team
in an ephemeral embed and clicks Confirm before any pokepast.es URL is
minted or any cache row is written.

The View only ever fires for cold-OCR submissions. Cache hits skip the
preview entirely — the URL is already canonical from a prior confirmed
extraction. See the v2 section of the plan for the Showdown-validator
replacement that retires this manual gate.
"""

from __future__ import annotations

import discord

from sketch.replica.extractor import TeamData
from sketch.replica.pokepaste_renderer import render_showdown

# Discord's embed description maxes out at 4096 chars. A 6-Pokemon paste
# rendered to Showdown export comfortably fits (well under 1k chars), and
# a markdown code block wrapper preserves whitespace + uses a monospace
# font so columns line up the way the user expects from PokePaste.
_DESCRIPTION_LIMIT = 4096


def team_to_embed(
    team: TeamData,
    *,
    code: str,
    description: str,
    fmt_name: str,
) -> discord.Embed:
    """Build the preview embed shown to the invoker before commit.

    The body is the actual `render_showdown(team)` text wrapped in a fenced
    code block — the same text we'll POST to pokepast.es on Confirm. The
    user sees exactly what they're about to publish, in the lingua franca
    of VGC team sharing (Showdown / PokePaste format), rather than a
    bespoke Discord field layout that they'd then have to re-verify
    against the rendered paste.
    """
    paste = render_showdown(team)
    # Truncate defensively. A 6-mon Showdown paste is normally ~500–800
    # chars, well within Discord's 4096-char embed description limit, but
    # an unusually long set of move names + items could in theory push
    # past it. Truncating with an ellipsis keeps the preview functional
    # — the actual paste posted to pokepast.es is the un-truncated text.
    if len(paste) > _DESCRIPTION_LIMIT - 50:
        paste = paste[: _DESCRIPTION_LIMIT - 50] + "\n… (truncated for preview)"

    embed = discord.Embed(
        title=f"Replica {code} — confirm to add",
        description=f"**{description}** — *{fmt_name}*\n```\n{paste}\n```",
        color=discord.Color.gold(),
    )
    embed.set_footer(
        text=(
            "Click Confirm to upload to pokepast.es and add this team to the "
            "bank. Cancel discards the extraction."
        )
    )
    return embed


class ReplicaPreviewView(discord.ui.View):
    """View with Confirm and Cancel buttons.

    Only the invoker (the user who ran `/replica`) can click — other users
    get an ephemeral refusal so the gate stays meaningful on busy channels.

    After construction the handler awaits `wait()` and reads `.decision`:
      - True  → user confirmed; commit the team.
      - False → user cancelled; discard the extraction.
      - None  → timed out; same outcome as cancel, distinguishable in logs.
    """

    def __init__(self, invoker_id: int, *, timeout: float) -> None:
        super().__init__(timeout=timeout)
        self.invoker_id = invoker_id
        self.decision: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Only the user who ran `/replica` can confirm this preview.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        self.decision = True
        # Defer (without sending a message) so the click is acknowledged
        # within Discord's 3s window. The command handler will edit the
        # original preview message after the commit settles.
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        self.decision = False
        await interaction.response.defer()
        self.stop()
