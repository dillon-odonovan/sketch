"""Tests for the /replica preview UI.

The View object is testable without a real Discord gateway because its
buttons are async methods we can call directly with a minimal interaction
double. We're verifying:
  - the embed renders with one field per Pokemon plus the expected metadata;
  - the invoker-gate refuses clicks from other users;
  - Confirm sets `decision=True`, Cancel sets `decision=False`;
  - non-invoker clicks don't change the decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sketch.champions.preview_view import (
    EditTeamModal,
    ReplicaPreviewView,
    team_to_embed,
)
from sketch.pokepaste.renderer import render_showdown
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


# --- Fakes for discord.Interaction -----------------------------------------


@dataclass
class _FakeUser:
    id: int


@dataclass
class _FakeResponse:
    """Minimal Interaction.response stub.

    `defer()` and `send_message()` are exercised by the View buttons
    (Confirm / Cancel defer; the invoker gate uses send_message).
    `send_modal()` is exercised by the Edit button. `edit_message()`
    is exercised by both the Edit success path (re-render with the
    edited team) and the Edit failure path (push the disabled-Confirm
    state). We record what gets called so each test can assert the
    right code path fired.
    """

    defer_calls: int = 0
    send_message_calls: list[dict] = field(default_factory=list)
    send_modal_calls: list[Any] = field(default_factory=list)
    edit_message_calls: list[dict] = field(default_factory=list)

    async def defer(self) -> None:
        self.defer_calls += 1

    async def send_message(self, content: str, **kwargs: Any) -> None:
        self.send_message_calls.append({"content": content, **kwargs})

    async def send_modal(self, modal: Any) -> None:
        self.send_modal_calls.append(modal)

    async def edit_message(self, **kwargs: Any) -> None:
        self.edit_message_calls.append(kwargs)


@dataclass
class _FakeFollowup:
    """Minimal Interaction.followup stub.

    Used by the Edit failure path: after the modal-submit `response`
    edits the preview (to push the disabled-Confirm state), the
    parser error goes out as a `followup.send(ephemeral=True, ...)`
    so the user gets a visible notification without taking another
    interaction slot on the parent message.
    """

    send_calls: list[dict] = field(default_factory=list)

    async def send(self, content: str | None = None, **kwargs: Any) -> None:
        self.send_calls.append({"content": content, **kwargs})


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


@dataclass
class _FakeInteraction:
    user: _FakeUser
    response: _FakeResponse = field(default_factory=_FakeResponse)
    followup: _FakeFollowup = field(default_factory=_FakeFollowup)


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
        # pokepaste_renderer) while the surrounding embed code fence
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
        interaction = _FakeInteraction(user=_FakeUser(id=42))
        assert await view.interaction_check(interaction) is True
        assert interaction.response.send_message_calls == []

    async def test_other_user_blocked_with_refusal(self):
        view = _make_view(invoker_id=42)
        interaction = _FakeInteraction(user=_FakeUser(id=99))
        assert await view.interaction_check(interaction) is False
        # The refusal goes out as an ephemeral message — the user who
        # clicked sees it, but the channel doesn't get spammed.
        assert len(interaction.response.send_message_calls) == 1
        call = interaction.response.send_message_calls[0]
        assert call["ephemeral"] is True
        assert "Only the user" in call["content"]

    async def test_confirm_sets_decision_true_and_defers(self):
        view = _make_view(invoker_id=42)
        interaction = _FakeInteraction(user=_FakeUser(id=42))
        # `view.confirm.callback` is discord.py's `_ItemCallback` wrapper —
        # it implicitly binds `self=view` and takes only the interaction.
        await view.confirm.callback(interaction)
        assert view.decision is True
        assert interaction.response.defer_calls == 1

    async def test_cancel_sets_decision_false_and_defers(self):
        view = _make_view(invoker_id=42)
        interaction = _FakeInteraction(user=_FakeUser(id=42))
        await view.cancel.callback(interaction)
        assert view.decision is False
        assert interaction.response.defer_calls == 1

    async def test_edit_button_opens_modal_with_current_paste(self):
        # The Edit button feeds the view's current team into the modal's
        # prefill — re-clicking Edit after an iteration should reflect
        # the most-recently-applied team, not the original OCR.
        view = _make_view(invoker_id=42)
        interaction = _FakeInteraction(user=_FakeUser(id=42))
        await view.edit.callback(interaction)
        assert len(interaction.response.send_modal_calls) == 1
        modal = interaction.response.send_modal_calls[0]
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
        modal_interaction = _FakeInteraction(user=_FakeUser(id=42))
        await view._apply_edited_team(edited, modal_interaction)
        # Team replaced, pending cleared, Confirm re-enabled.
        assert view.team is edited
        assert view._pending_edit_text is None
        assert view.confirm.disabled is False
        # Crucially: NOT committed. Confirm still required.
        assert view.decision is None
        assert not view.is_finished()
        # Preview embed re-rendered with the updated team.
        assert len(modal_interaction.response.edit_message_calls) == 1
        call = modal_interaction.response.edit_message_calls[0]
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
        modal_interaction = _FakeInteraction(user=_FakeUser(id=42))
        await view._apply_edited_team(edited, modal_interaction)
        # User then clicks Confirm.
        confirm_interaction = _FakeInteraction(user=_FakeUser(id=42))
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
        modal_interaction = _FakeInteraction(user=_FakeUser(id=42))
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
        assert modal_interaction.response.send_modal_calls == []

    async def test_surface_edit_failure_pushes_disabled_state_and_followup_error(self):
        # The disabled-Confirm state is pushed to the message via
        # `response.edit_message(view=self)` so the user actually sees
        # the grayed-out button. The parser error then goes out as a
        # `followup.send(ephemeral=True, ...)` — a separate ephemeral
        # message the user can read and dismiss.
        view = _make_view(invoker_id=42)
        modal_interaction = _FakeInteraction(user=_FakeUser(id=42))
        await view._surface_edit_failure(
            "broken", "Expected 6 Pokemon, got 5.", modal_interaction
        )
        # Modal response was edit_message with the updated view (which
        # carries the disabled Confirm). Content / embed not touched —
        # the preview still shows the original team, which Confirm
        # can no longer commit anyway.
        assert len(modal_interaction.response.edit_message_calls) == 1
        edit_call = modal_interaction.response.edit_message_calls[0]
        assert edit_call["view"] is view
        assert "content" not in edit_call
        assert "embed" not in edit_call
        # Error delivered via followup (so it appears as a separate
        # message bubble for the user). The footgun-escape hint is
        # included so users who actually want the original have a path.
        assert len(modal_interaction.followup.send_calls) == 1
        followup = modal_interaction.followup.send_calls[0]
        assert followup["ephemeral"] is True
        assert "Couldn't parse" in followup["content"]
        assert "Expected 6 Pokemon, got 5." in followup["content"]
        assert "your text is preserved" in followup["content"]
        assert "Cancel" in followup["content"]
        assert "re-run" in followup["content"]

    async def test_edit_button_uses_pending_text_after_failure(self):
        # After a failed edit, the next Edit click pre-fills the modal
        # with the user's last submitted text — NOT the unchanged
        # render of the current team — so they don't have to retype
        # their work.
        view = _make_view(invoker_id=42)
        view._pending_edit_text = "user's in-progress edit"
        interaction = _FakeInteraction(user=_FakeUser(id=42))
        await view.edit.callback(interaction)
        assert len(interaction.response.send_modal_calls) == 1
        modal = interaction.response.send_modal_calls[0]
        assert modal.paste_input.default == "user's in-progress edit"

    async def test_cancel_clears_pending_edit_text(self):
        # Cancelling out of the preview discards any in-progress edit
        # work — cosmetic, since the view is about to be destroyed
        # anyway, but makes the state transitions predictable.
        view = _make_view(invoker_id=42)
        view._pending_edit_text = "doesn't matter"
        interaction = _FakeInteraction(user=_FakeUser(id=42))
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

        interaction = _FakeInteraction(user=_FakeUser(id=42))
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

        interaction = _FakeInteraction(user=_FakeUser(id=42))
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
        assert interaction.response.send_modal_calls == []
        assert interaction.response.edit_message_calls == []
        assert interaction.response.send_message_calls == []
        assert interaction.response.defer_calls == 0

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

        interaction = _FakeInteraction(user=_FakeUser(id=42))
        await modal.on_submit(interaction)

        assert len(failures) == 1
        _, error, _ = failures[0]
        assert "Hardy" in error or "Serious" in error
