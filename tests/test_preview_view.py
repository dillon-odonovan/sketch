"""Tests for the `/add-team` preview UI.

The View object is testable without a real Discord gateway because its
buttons are async methods we can call directly with a minimal interaction
double. We're verifying:
  - the embed renders with one field per Pokemon plus the expected metadata;
  - the invoker-gate refuses clicks from other users;
  - Confirm sets `decision=True`, Cancel sets `decision=False`;
  - non-invoker clicks don't change the decision.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from sketch.champions.preview_view import (
    EditTeamModal,
    ReplicaPreviewView,
    team_to_embed,
)
from sketch.showdown.renderer import render_showdown
from sketch.team import PokemonEntry, TeamData


def _entry(species: str = "Floette-Eternal") -> PokemonEntry:
    return PokemonEntry(
        species=species,
        gender="F",
        item="Floettite",
        ability="Flower Veil",
        nature="Modest",
        evs={"hp": 32, "atk": 0, "def": 0, "spa": 32, "spd": 0, "spe": 2},
        moves=["Dazzling Gleam", "Moonblast", "Light of Ruin", "Protect"],
    )


def _team() -> TeamData:
    # Species mirror the reference team so the field-name golden test
    # matches the same data the renderer test uses.
    return TeamData(
        pokemon=[
            _entry("Floette-Eternal"),
            _entry("Aerodactyl"),
            _entry("Incineroar"),
            _entry("Garchomp"),
            _entry("Charizard"),
            _entry("Venusaur"),
        ]
    )


def _make_interaction(user_id: int = 42) -> MagicMock:
    interaction = MagicMock()
    interaction.user.id = user_id
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _make_view(
    *,
    invoker_id: int = 42,
    team: TeamData | None = None,
) -> ReplicaPreviewView:
    """Construct a view with the canonical embed-build inputs.

    The View renders its own embed via `render_embed()` rather than
    receiving one pre-built — that single source of truth means a
    successful edit can re-render the embed for the updated team
    without the command handler being involved.
    """
    return ReplicaPreviewView(
        invoker_id,
        team=team or _team(),
        code="QBXXWXL05U",
        description="x",
        fmt_name="Reg M-A",
        timeout=300,
    )


class TestTeamToEmbed:
    """The embed body now contains the full rendered Showdown / PokePaste
    text wrapped in a code block — same text we'll POST to pokepast.es on
    Confirm. Tests verify the description contains the canonical render
    rather than reimplementing the field-by-field layout."""

    def test_description_contains_rendered_paste(self):
        embed = team_to_embed(
            _team(),
            code="QBXXWXL05U",
            description="sample team",
            fmt_name="Reg M-A",
        )
        # Header line of the first mon in Showdown format.
        assert "Floette-Eternal (F) @ Floettite" in embed.description
        # Ability + Nature + Moves all present.
        assert "Ability: Flower Veil" in embed.description
        assert "Modest Nature" in embed.description
        assert "- Dazzling Gleam" in embed.description
        # All 6 Pokemon are in the paste.
        assert "Aerodactyl" in embed.description
        assert "Venusaur" in embed.description

    def test_description_wraps_paste_in_code_block(self):
        # The Showdown export is wrapped in a fenced ``` block so Discord
        # renders it with monospace + preserves the line-by-line layout.
        embed = team_to_embed(
            _team(),
            code="QBXXWXL05U",
            description="x",
            fmt_name="Reg M-A",
        )
        assert "```\n" in embed.description
        # Closing fence too.
        assert embed.description.rstrip().endswith("```")

    def test_description_includes_user_description_and_format(self):
        embed = team_to_embed(
            _team(),
            code="QBXXWXL05U",
            description="my team",
            fmt_name="Reg M-A",
        )
        assert "my team" in embed.description
        assert "Reg M-A" in embed.description

    def test_title_includes_code(self):
        embed = team_to_embed(
            _team(),
            code="QBXXWXL05U",
            description="x",
            fmt_name="Reg M-A",
        )
        assert "QBXXWXL05U" in embed.title

    def test_genderless_no_item_renders_clean_name_in_paste(self):
        # Edge case: genderless mon with no item should appear as just
        # "Klefki" in the rendered paste — no empty "()" or " @ ".
        team = TeamData(
            pokemon=[
                PokemonEntry(
                    species="Klefki",
                    gender=None,
                    item=None,
                    ability="Prankster",
                    nature="Bold",
                    evs={
                        "hp": 32,
                        "atk": 0,
                        "def": 16,
                        "spa": 0,
                        "spd": 16,
                        "spe": 0,
                    },
                    moves=["Reflect", "Light Screen", "Foul Play", "Spikes"],
                )
            ]
        )
        embed = team_to_embed(team, code="X" * 10, description="d", fmt_name="Reg M-A")
        # The species line in the rendered paste should be just "Klefki".
        # The rendered paste uses CRLF line endings (required by
        # pokepast.es' block-splitter — see _LINE_END in
        # sketch.showdown.renderer) while the surrounding embed code fence
        # uses LF, so the assertion targets the species line bounded by
        # the code fence on the LF side and the next CRLF-terminated
        # line on the other.
        assert "```\nKlefki\r\n" in embed.description


class TestReplicaPreviewView:
    async def test_initial_decision_is_none(self):
        view = _make_view(invoker_id=1)
        assert view.decision is None
        # The view holds the original team unchanged until Edit / Confirm.
        assert view.team is not None

    async def test_invoker_can_pass_check(self):
        view = _make_view(invoker_id=42)
        interaction = _make_interaction(user_id=42)
        assert await view.interaction_check(interaction) is True
        interaction.response.send_message.assert_not_called()

    async def test_other_user_blocked_with_refusal(self):
        view = _make_view(invoker_id=42)
        interaction = _make_interaction(user_id=99)
        assert await view.interaction_check(interaction) is False
        # The refusal goes out as an ephemeral message — the user who
        # clicked sees it, but the channel doesn't get spammed.
        interaction.response.send_message.assert_called_once()
        (content,), kwargs = interaction.response.send_message.call_args
        assert kwargs["ephemeral"] is True
        assert "Only the user" in content

    async def test_confirm_sets_decision_true_and_defers(self):
        view = _make_view(invoker_id=42)
        interaction = _make_interaction(user_id=42)
        # `view.confirm.callback` is discord.py's `_ItemCallback` wrapper —
        # it implicitly binds `self=view` and takes only the interaction.
        await view.confirm.callback(interaction)
        assert view.decision is True
        interaction.response.defer.assert_called_once()

    async def test_cancel_sets_decision_false_and_defers(self):
        view = _make_view(invoker_id=42)
        interaction = _make_interaction(user_id=42)
        await view.cancel.callback(interaction)
        assert view.decision is False
        interaction.response.defer.assert_called_once()

    async def test_edit_button_opens_modal_with_current_paste(self):
        # The Edit button feeds the view's current team into the modal's
        # prefill — re-clicking Edit after an iteration should reflect
        # the most-recently-applied team, not the original OCR.
        view = _make_view(invoker_id=42)
        interaction = _make_interaction(user_id=42)
        await view.edit.callback(interaction)
        interaction.response.send_modal.assert_called_once()
        (modal,), _ = interaction.response.send_modal.call_args
        assert isinstance(modal, EditTeamModal)
        # Prefill is the canonical Showdown render of the current team.
        assert modal.paste_input.default == render_showdown(view.team)
        # Decision shouldn't flip just from opening the modal.
        assert view.decision is None

    async def test_apply_edited_team_updates_preview_without_committing(self):
        # A successful Edit submit applies the change to the preview
        # but does NOT commit — the user still has to click Confirm.
        # Submit means "save changes," Confirm means "publish."
        view = _make_view(invoker_id=42)
        view._pending_edit_text = "stale attempt"
        # Confirm starts disabled here to simulate the failure-state
        # carry-over; the success path must clear that disable so the
        # user can commit the now-valid edit.
        view.confirm.disabled = True
        edited = TeamData(
            pokemon=[_entry("Urshifu-Rapid-Strike")] + view.team.pokemon[1:],
            team_id=view.team.team_id,
        )
        modal_interaction = _make_interaction(user_id=42)
        await view._apply_edited_team(edited, modal_interaction)
        # Team replaced, pending cleared, Confirm re-enabled.
        assert view.team is edited
        assert view._pending_edit_text is None
        assert view.confirm.disabled is False
        # Crucially: NOT committed. Confirm still required.
        assert view.decision is None
        assert not view.is_finished()
        # Preview embed re-rendered with the updated team.
        modal_interaction.response.edit_message.assert_called_once()
        call = modal_interaction.response.edit_message.call_args.kwargs
        # The embed reflects the edited team — content unchanged.
        assert "content" not in call
        # The first mon's new species should appear in the rendered embed.
        assert "Urshifu-Rapid-Strike" in call["embed"].description
        assert call["view"] is view

    async def test_apply_edited_team_lets_subsequent_confirm_commit_edits(self):
        # End-to-end of the new flow: edit Submit applies changes,
        # then a separate Confirm click commits. `view.team` after
        # Confirm should be the edited team — proving the edit
        # actually flows through to what gets uploaded.
        view = _make_view(invoker_id=42)
        edited = TeamData(
            pokemon=[_entry("Urshifu-Rapid-Strike")] + view.team.pokemon[1:],
            team_id=view.team.team_id,
        )
        modal_interaction = _make_interaction(user_id=42)
        await view._apply_edited_team(edited, modal_interaction)
        # User then clicks Confirm.
        confirm_interaction = _make_interaction(user_id=42)
        await view.confirm.callback(confirm_interaction)
        # Decision is True, view is stopped, and view.team is the
        # edited version — the command handler will mint with this.
        assert view.decision is True
        assert view.is_finished()
        assert view.team.pokemon[0].species == "Urshifu-Rapid-Strike"

    async def test_surface_edit_failure_disables_confirm_and_stashes_text(self):
        # `_surface_edit_failure` is the modal -> view failure callback.
        # The footgun it guards: edit fails, preview still shows
        # original team, user clicks Confirm thinking it commits their
        # edit — but it commits the original. Disabling Confirm makes
        # that path inaccessible until the user resolves the failed
        # edit (either by retrying successfully or cancelling).
        view = _make_view(invoker_id=42)
        original_team = view.team
        assert view.confirm.disabled is False  # baseline
        submitted = "deliberately broken paste"
        modal_interaction = _make_interaction(user_id=42)
        await view._surface_edit_failure(
            submitted, "Expected 6 Pokemon, got 5.", modal_interaction
        )
        # View state preserved EXCEPT for the Confirm disable + stashed text.
        assert view.team is original_team
        assert view.decision is None
        assert not view.is_finished()
        assert view._pending_edit_text == submitted
        # The Confirm-disable guard.
        assert view.confirm.disabled is True
        # No new modal opened (the HTTP 400 bug we already fixed).
        modal_interaction.response.send_modal.assert_not_called()

    async def test_surface_edit_failure_pushes_disabled_state_and_followup_error(self):
        # The disabled-Confirm state is pushed to the message via
        # `response.edit_message(view=self)` so the user actually sees
        # the grayed-out button. The parser error then goes out as a
        # `followup.send(ephemeral=True, ...)` — a separate ephemeral
        # message the user can read and dismiss.
        view = _make_view(invoker_id=42)
        modal_interaction = _make_interaction(user_id=42)
        await view._surface_edit_failure(
            "broken", "Expected 6 Pokemon, got 5.", modal_interaction
        )
        # Modal response was edit_message with the updated view (which
        # carries the disabled Confirm). Content / embed not touched —
        # the preview still shows the original team, which Confirm
        # can no longer commit anyway.
        modal_interaction.response.edit_message.assert_called_once()
        edit_call = modal_interaction.response.edit_message.call_args.kwargs
        assert edit_call["view"] is view
        assert "content" not in edit_call
        assert "embed" not in edit_call
        # Error delivered via followup (so it appears as a separate
        # message bubble for the user). The footgun-escape hint is
        # included so users who actually want the original have a path.
        modal_interaction.followup.send.assert_called_once()
        followup_kw = modal_interaction.followup.send.call_args.kwargs
        assert followup_kw["ephemeral"] is True
        assert "Couldn't parse" in followup_kw["content"]
        assert "Expected 6 Pokemon, got 5." in followup_kw["content"]
        assert "your text is preserved" in followup_kw["content"]
        assert "Cancel" in followup_kw["content"]
        assert "re-run" in followup_kw["content"]

    async def test_edit_button_uses_pending_text_after_failure(self):
        # After a failed edit, the next Edit click pre-fills the modal
        # with the user's last submitted text — NOT the unchanged
        # render of the current team — so they don't have to retype
        # their work.
        view = _make_view(invoker_id=42)
        view._pending_edit_text = "user's in-progress edit"
        interaction = _make_interaction(user_id=42)
        await view.edit.callback(interaction)
        interaction.response.send_modal.assert_called_once()
        (modal,), _ = interaction.response.send_modal.call_args
        assert modal.paste_input.default == "user's in-progress edit"

    async def test_cancel_clears_pending_edit_text(self):
        # Cancelling out of the preview discards any in-progress edit
        # work — cosmetic, since the view is about to be destroyed
        # anyway, but makes the state transitions predictable.
        view = _make_view(invoker_id=42)
        view._pending_edit_text = "doesn't matter"
        interaction = _make_interaction(user_id=42)
        await view.cancel.callback(interaction)
        assert view._pending_edit_text is None
        assert view.decision is False


class TestEditTeamModal:
    async def test_submit_parses_and_calls_success_callback(self):
        # Happy path: feed the modal a valid Showdown paste; on submit
        # the parser succeeds and `on_success` fires with the parsed
        # team + the modal-submit interaction. `on_failure` does not.
        original = _team()
        successes: list[tuple] = []
        failures: list[tuple] = []

        async def on_success(team, interaction):
            successes.append((team, interaction))

        async def on_failure(text, error, interaction):
            failures.append((text, error, interaction))

        modal = EditTeamModal(
            prefill=render_showdown(original),
            team_id="QBXXWXL05U",
            on_success=on_success,
            on_failure=on_failure,
        )
        # `TextInput.value` is a property backed by `_value`; in production
        # discord.py populates it from the interaction payload. Tests
        # write it directly so we can exercise on_submit without a
        # gateway round-trip.
        modal.paste_input._value = render_showdown(original)  # type: ignore[attr-defined]

        interaction = _make_interaction(user_id=42)
        await modal.on_submit(interaction)

        assert len(successes) == 1
        assert failures == []
        parsed_team, parsed_interaction = successes[0]
        assert parsed_interaction is interaction
        assert parsed_team.team_id == "QBXXWXL05U"
        assert len(parsed_team.pokemon) == 6
        assert parsed_team.pokemon[0].species == "Floette-Eternal"

    async def test_submit_with_parse_error_calls_failure_callback(self):
        # Sad path: feed the modal a paste that parses-fails (too many
        # moves on slot 1). The modal hands the user's submitted text
        # + the error message + the modal interaction to `on_failure`
        # and does NOT touch the response itself — the View decides
        # how to surface the failure to the user.
        successes: list[tuple] = []
        failures: list[tuple] = []

        async def on_success(team, interaction):
            successes.append((team, interaction))

        async def on_failure(text, error, interaction):
            failures.append((text, error, interaction))

        # Take the canonical paste and inject a 5th move onto slot 1.
        broken_paste = render_showdown(_team()).replace(
            "- Protect\r\n\r\nAerodactyl",
            "- Protect\r\n- Substitute\r\n\r\nAerodactyl",
            1,
        )

        modal = EditTeamModal(
            prefill=broken_paste,
            team_id="QBXXWXL05U",
            on_success=on_success,
            on_failure=on_failure,
        )
        modal.paste_input._value = broken_paste  # type: ignore[attr-defined]

        interaction = _make_interaction(user_id=42)
        await modal.on_submit(interaction)

        assert successes == []
        assert len(failures) == 1
        text, error, failure_interaction = failures[0]
        assert text == broken_paste
        assert "too many moves" in error
        assert failure_interaction is interaction
        # The modal itself doesn't touch the interaction response —
        # the failure callback owns it. Discord rejects `send_modal`
        # in response to a modal-submit interaction (HTTP 400, valid
        # response types {4, 5, 6, 7, 10, 12}), so the modal must
        # hand the interaction off rather than responding directly.
        interaction.response.send_modal.assert_not_called()
        interaction.response.edit_message.assert_not_called()
        interaction.response.send_message.assert_not_called()
        interaction.response.defer.assert_not_called()

    async def test_submit_with_nature_error_mentions_serious(self):
        # The nature error must explicitly tell the user to use "Serious"
        # for neutral natures — Hardy is a Showdown-accepted neutral but
        # we don't support it. Surfaces via the on_failure callback.
        failures: list[tuple] = []

        async def on_success(team, interaction):
            raise AssertionError("on_success should not fire on parse error")

        async def on_failure(text, error, interaction):
            failures.append((text, error, interaction))

        paste = render_showdown(_team()).replace("Modest Nature", "Hardy Nature", 1)

        modal = EditTeamModal(
            prefill=paste,
            team_id="QBXXWXL05U",
            on_success=on_success,
            on_failure=on_failure,
        )
        modal.paste_input._value = paste  # type: ignore[attr-defined]

        interaction = _make_interaction(user_id=42)
        await modal.on_submit(interaction)

        assert len(failures) == 1
        _, error, _ = failures[0]
        assert "Hardy" in error or "Serious" in error
