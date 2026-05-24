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

from sketch.replica.extractor import STAT_KEYS, TeamData

# Stat order for the inline EV summary line. Matches the canonical order
# used in the renderer and the share-screen UI itself.
_EV_SUMMARY_KEYS = STAT_KEYS


def team_to_embed(
    team: TeamData,
    *,
    code: str,
    description: str,
    fmt_name: str,
) -> discord.Embed:
    """Build the preview embed shown to the invoker before commit.

    Field-per-Pokemon layout (6 fields, well under the 25-field limit) keeps
    each entry scannable. EVs render as a 6-slash summary (`HP/Atk/Def/SpA/
    SpD/Spe`) which is the in-game share-screen ordering — quick to
    eyeball-check against the screenshot the user just uploaded.
    """
    embed = discord.Embed(
        title=f"Replica {code} — confirm to add",
        description=f"**{description}** — *{fmt_name}*",
        color=discord.Color.gold(),
    )
    for p in team.pokemon:
        ev_summary = "/".join(str(p.evs.get(k, 0)) for k in _EV_SUMMARY_KEYS)
        item_suffix = f" @ {p.item}" if p.item else ""
        value = (
            f"**Ability:** {p.ability}  ·  **Tera:** {p.tera_type}\n"
            f"**Nature:** {p.nature}  ·  **EVs (HP/Atk/Def/SpA/SpD/Spe):** "
            f"{ev_summary}\n"
            f"**Moves:** {', '.join(p.moves)}"
        )
        embed.add_field(name=f"{p.species}{item_suffix}", value=value, inline=False)
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
