"""Discord UI for the Confirm / Cancel / Edit gate on cold OCR.

The replica cache is global (cross-guild), so a bad OCR that lands in the
cache pollutes every future lookup of that code on every guild. The human
preview is the safety gate: the invoking user sees the extracted team in
an ephemeral embed and either confirms it, edits it, or cancels before
any pokepast.es URL is minted or any cache row is written.

Edit opens a Discord modal pre-populated with the rendered Showdown /
PokePaste text. On Submit the bot parses the text back into a `TeamData`
and — on parser success — **auto-commits** (treats the edit submission
as if Confirm had been clicked). The user inspects the canonical text in
the modal before submitting, so re-rendering the preview embed for a
second confirm click would only add friction. If the user wants to bail
mid-edit, the modal's native X button closes without firing on_submit.

On parse failure the modal-submit response edits the preview message
content with the error and stashes the user's failed text on the View;
the next Edit click pre-fills the modal with the stashed text so the
user can fix their typo without losing work. (Discord's API forbids
calling `send_modal` in response to a modal-submit interaction —
[interaction response types 4 / 5 / 6 / 7 / 10 / 12 only], so the
modal cannot "reopen itself" on failure. `edit_message` is permitted
on modal-submit when the modal was launched from a component, and is
the right vehicle here.)

The View only ever fires for cold-OCR submissions. Cache hits skip the
preview entirely — the URL is already canonical from a prior confirmed
extraction.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import discord

from sketch.replica.extractor import TeamData
from sketch.replica.pokepaste_renderer import render_showdown
from sketch.replica.showdown_parser import ShowdownParseError, parse_showdown

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
        modal-submit interaction. The View applies the team and
        auto-commits.
      - On parse failure, calls `on_failure` with the user's submitted
        text + the error message + the modal-submit interaction. The
        View stashes the text so the next Edit click pre-fills with
        it, and edits the preview message to surface the error.

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

    After construction the handler awaits `wait()` and reads `.team` and
    `.decision`:
      - decision=True  → user confirmed (or successfully edited, which
                         auto-commits); commit `self.team`.
      - decision=False → user cancelled; discard the extraction.
      - decision=None  → timed out; same outcome as cancel, distinguishable
                         in logs.

    Edit replaces `self.team` with the user-edited version before the
    auto-commit, so the command handler always reads the team the user
    last approved (whether that was the original OCR output or an edit).
    """

    def __init__(
        self,
        invoker_id: int,
        *,
        team: TeamData,
        preview_content: str,
        preview_embed: discord.Embed,
        timeout: float,
    ) -> None:
        super().__init__(timeout=timeout)
        self.invoker_id = invoker_id
        self.team = team
        self.decision: bool | None = None
        # The View needs to remember the original preview content + embed
        # so a parse-failure path that edits the message can later
        # restore the unprefixed version on a subsequent successful
        # edit. Without this the error notice would stick around even
        # after the user fixes their typo.
        self._original_content = preview_content
        self._preview_embed = preview_embed
        # User's most recent failed edit attempt, if any. When set, the
        # next Edit click pre-fills the modal with it instead of the
        # current team's render — preserving their in-progress work
        # across the failed-submit / click-Edit-again cycle that
        # Discord forces (since the modal can't reopen itself from a
        # modal-submit response).
        self._pending_edit_text: str | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Only the user who ran `/replica` can confirm this preview.",
                ephemeral=True,
            )
            return False
        return True

    async def _apply_edited_team(
        self,
        new_team: TeamData,
        modal_interaction: discord.Interaction,
    ) -> None:
        """Commit a successfully-parsed edited team.

        Treats the edit submission as if Confirm had been clicked:
        replaces `self.team`, marks the decision as True, and stops the
        view so the command handler proceeds to mint + cache. The modal
        interaction is deferred to acknowledge the click within Discord's
        3-second window without leaving an extra ephemeral message
        behind — the command handler will edit the original preview
        message to "Uploading…" as the next user-visible state.
        """
        self.team = new_team
        self.decision = True
        self._pending_edit_text = None
        await modal_interaction.response.defer()
        self.stop()

    async def _surface_edit_failure(
        self,
        submitted_text: str,
        error_message: str,
        modal_interaction: discord.Interaction,
    ) -> None:
        """Report a parse failure prominently and disambiguate Confirm.

        Stashes the user's failed text so the next Edit click pre-fills
        with it (Discord won't let us reopen the modal directly from a
        modal-submit interaction — only command / component / autocomplete
        responses can `send_modal`).

        Two UX moves that turn out to matter, both based on real-world
        user testing:

        1. The error needs to be IMPOSSIBLE to miss. Footer text is too
           small; content prefixes scroll off-screen when the embed is
           tall. The error goes into the embed *title* (largest text)
           plus a *field* at the bottom of the description (right above
           the buttons), and the embed color flips to red.
        2. The Confirm button's label changes to "Use original". The
           original button name is a footgun in the failed-edit state:
           it does NOT commit the user's edit (the edit doesn't parse),
           it commits the unedited OCR team. Renaming makes the action
           literal so the user can't be surprised by the result.

        Both changes are durable for the rest of the failed-edit state.
        If the user later edits successfully or cancels, the view stops
        before any of this matters.
        """
        self._pending_edit_text = submitted_text

        error_embed = self._preview_embed.copy()
        error_embed.color = discord.Color.red()
        # Title caps at 256 chars. Parser errors are typically <100, but
        # a misformatted species line can quote the offending input and
        # blow past that — truncate defensively.
        title_text = f"⚠️ Couldn't parse — {error_message}"
        if len(title_text) > 256:
            title_text = title_text[:253] + "…"
        error_embed.title = title_text
        error_embed.add_field(
            name="What now?",
            value=(
                "• **Edit** — retry (your text is preserved)\n"
                "• **Use original** — upload the team above as-is "
                "(your edit attempt is discarded)\n"
                "• **Cancel** — discard everything"
            ),
            inline=False,
        )
        # Original footer ("Confirm uploads to pokepast.es...") is
        # misleading once Confirm is relabeled. The new field carries
        # the accurate button explanations.
        error_embed.remove_footer()

        self.confirm.label = "Use original"

        await modal_interaction.response.edit_message(
            embed=error_embed,
            view=self,
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
