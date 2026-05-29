"""Discord UI for the Confirm / Cancel / Edit gate on cold OCR.

The replica cache is global (cross-guild), so a bad OCR that lands in the
cache pollutes every future lookup of that code on every guild. The human
preview is the safety gate: the invoking user sees the extracted team in
an ephemeral embed and either confirms it, edits it, or cancels before
any pokepast.es URL is minted or any cache row is written.

Modal Submit and Confirm are intentionally distinct actions, mirroring
the mental model VGC users already have for "save then publish":

  - **Edit → modal Submit** applies the user's changes to the preview.
    The preview embed re-renders with the edited team. Decision is
    still pending; nothing has been uploaded.
  - **Confirm** uploads the team currently shown in the preview
    (whether that's the original OCR or an edited version).
  - **Cancel** discards.

On modal parse failure the View stashes the user's text and sends a
small ephemeral notification — the preview embed itself stays
unchanged (since the edit didn't apply), keeping the invariant that
"the preview embed shows what Confirm will upload." The next Edit
click pre-fills with the stashed text so the user can fix their typo
without losing work. (Discord forbids calling `send_modal` in
response to a modal-submit interaction — interaction response types
{4, 5, 6, 7, 10, 12} only — so the modal cannot "reopen itself.")

The View only ever fires for cold-OCR submissions. Cache hits skip the
preview entirely — the URL is already canonical from a prior confirmed
extraction.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import discord

from sketch.champions.showdown_parser import ShowdownParseError, parse_showdown
from sketch.pokepaste.renderer import render_showdown
from sketch.team import TeamData

# Discord's embed description maxes out at 4096 chars. A 6-Pokemon paste
# rendered to Showdown export comfortably fits (well under 1k chars), and
# a markdown code block wrapper preserves whitespace + uses a monospace
# font so columns line up the way the user expects from PokePaste.
_DESCRIPTION_LIMIT = 4096

# Discord paragraph TextInput accepts up to 4000 chars. A Champions
# Showdown export tops out around 1k, so this is a wide margin.
_TEXTINPUT_MAX_LENGTH = 4000


# Callbacks the modal invokes on submit. The modal owns no state about
# the View or the command handler — it just calls back with the parsed
# team (on success) or the user's failed text + error message (on
# parse failure), letting the caller (typically `ReplicaPreviewView`)
# decide what to do. Loose coupling keeps the modal independently
# testable and the View's policy in one place.
EditedTeamCallback = Callable[[TeamData, discord.Interaction], Awaitable[None]]
EditFailureCallback = Callable[[str, str, discord.Interaction], Awaitable[None]]


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
            "Confirm uploads to pokepast.es and adds to the bank. Edit "
            "lets you fix the parsed team first. Cancel discards."
        )
    )
    return embed


class EditTeamModal(discord.ui.Modal):
    """Modal that lets the invoker edit the extracted team as Showdown text.

    On submit:
      - Parses the text via `parse_showdown`.
      - On success, calls `on_success` with the parsed team and the
        modal-submit interaction.
      - On parse failure, calls `on_failure` with the user's submitted
        text + the error message + the modal-submit interaction.

    Both callbacks own the interaction response — the modal itself
    never calls `defer`, `edit_message`, or `send_message`, which keeps
    UI policy out of the modal and in the View.
    """

    def __init__(
        self,
        *,
        prefill: str,
        team_id: str | None,
        on_success: EditedTeamCallback,
        on_failure: EditFailureCallback,
    ) -> None:
        super().__init__(title="Edit team")

        self._team_id = team_id
        self._on_success = on_success
        self._on_failure = on_failure

        # Stored as an attribute so tests can inspect the default text
        # without going through `self.children[0]` (which is also valid
        # but couples test code to discord.py's internal ordering).
        self.paste_input: discord.ui.TextInput = discord.ui.TextInput(
            label="Showdown text",
            style=discord.TextStyle.paragraph,
            default=prefill,
            max_length=_TEXTINPUT_MAX_LENGTH,
            required=True,
        )
        self.add_item(self.paste_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        submitted = self.paste_input.value or ""
        try:
            team = parse_showdown(submitted, team_id=self._team_id)
        except ShowdownParseError as exc:
            await self._on_failure(submitted, str(exc), interaction)
            return
        await self._on_success(team, interaction)


class ReplicaPreviewView(discord.ui.View):
    """View with Confirm / Edit / Cancel buttons.

    Only the invoker (the user who ran `/add-team`) can click — other
    users get an ephemeral refusal so the gate stays meaningful on busy
    channels.

    The View keeps `self.team` synced to whatever the preview currently
    shows. On Edit + successful parse, `self.team` is replaced and the
    preview embed is re-rendered; on Edit + parse failure, `self.team`
    is untouched and the user gets an ephemeral error. Either way,
    clicking Confirm uploads `self.team` — so the embed is always a
    truthful view of what Confirm will commit.

    After construction the handler awaits `wait()` and reads `.team` /
    `.decision`:
      - decision=True  → user confirmed; commit `self.team`.
      - decision=False → user cancelled; discard the extraction.
      - decision=None  → timed out; same outcome as cancel, distinguishable
                         in logs.
    """

    def __init__(
        self,
        invoker_id: int,
        *,
        team: TeamData,
        code: str,
        description: str,
        fmt_name: str,
        timeout: float,
    ) -> None:
        super().__init__(timeout=timeout)
        self.invoker_id = invoker_id
        self.team = team
        self.decision: bool | None = None
        # Embed-build inputs kept on the View so a successful edit can
        # re-render the preview embed with the updated team. The
        # command handler also uses `render_embed()` for the initial
        # render so there's only one place team_to_embed gets called.
        self._code = code
        self._description = description
        self._fmt_name = fmt_name
        # User's most recent failed edit attempt, if any. When set, the
        # next Edit click pre-fills the modal with it instead of the
        # current team's render — preserving their in-progress work
        # across the failed-submit / click-Edit-again cycle that
        # Discord forces (since the modal can't reopen itself from a
        # modal-submit response).
        self._pending_edit_text: str | None = None

    def render_embed(self) -> discord.Embed:
        """Build the preview embed for the current team.

        Used by the command handler for the initial render and by the
        View itself after a successful edit. Single source of truth for
        the embed shape — the command handler doesn't construct embeds
        from raw TeamData anywhere else.
        """
        return team_to_embed(
            self.team,
            code=self._code,
            description=self._description,
            fmt_name=self._fmt_name,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Only the user who ran `/add-team` can confirm this preview.",
                ephemeral=True,
            )
            return False
        return True

    async def _apply_edited_team(
        self,
        new_team: TeamData,
        modal_interaction: discord.Interaction,
    ) -> None:
        """Apply a successfully-parsed edit by updating the preview.

        Replaces `self.team`, re-renders the embed, and updates the
        message in place. The view stays open so the user can confirm
        the updated team, edit again, or cancel — the modal Submit
        applies the change, but Confirm is still required to upload
        it. (This matches the mental model new users bring: Submit
        means "save changes," Confirm means "publish.")

        Re-enables Confirm in case a previous failed-edit attempt had
        disabled it; the user's edit now applies cleanly and the
        preview embed is a truthful view of what Confirm will upload.
        """
        self.team = new_team
        self._pending_edit_text = None
        self.confirm.disabled = False
        await modal_interaction.response.edit_message(
            embed=self.render_embed(),
            view=self,
        )

    async def _surface_edit_failure(
        self,
        submitted_text: str,
        error_message: str,
        modal_interaction: discord.Interaction,
    ) -> None:
        """Disable Confirm, stash the failed text, notify the user.

        The footgun this guards against: user makes an edit, the edit
        fails to parse, the preview embed stays as the unedited OCR
        team (because the edit didn't apply). If Confirm stayed live,
        the user might click it thinking it commits their edit — and
        instead it silently commits the unedited team. Disabling
        Confirm forces the user to either fix the edit or explicitly
        bail out via Cancel + re-run.

        The user's text is stashed so the next Edit click pre-fills
        with it; the preview embed is NOT changed (the edit didn't
        apply, so the embed still reflects the current team
        accurately — invariant: "preview shows what Confirm uploads"
        survives even in this state because Confirm is disabled).
        """
        self._pending_edit_text = submitted_text
        self.confirm.disabled = True
        # Push the disabled-Confirm state via the modal-submit response,
        # then deliver the error as a followup ephemeral. Discord
        # allows one interaction response per interaction; followups
        # come after.
        await modal_interaction.response.edit_message(view=self)
        await modal_interaction.followup.send(
            content=(
                f"⚠️ **Couldn't parse your edited team:** {error_message}\n"
                "Click **Edit** to retry — your text is preserved.\n"
                "_Confirm is disabled until your edit applies. To upload "
                "the original team instead, click **Cancel** and re-run "
                "`/add-team` without using Edit._"
            ),
            ephemeral=True,
        )

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

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.primary)
    async def edit(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        prefill = self._pending_edit_text or render_showdown(self.team)
        await interaction.response.send_modal(
            EditTeamModal(
                prefill=prefill,
                team_id=self.team.team_id,
                on_success=self._apply_edited_team,
                on_failure=self._surface_edit_failure,
            )
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        self.decision = False
        self._pending_edit_text = None
        await interaction.response.defer()
        self.stop()
