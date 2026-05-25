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

from sketch.replica.extractor import PokemonEntry, TeamData
from sketch.replica.pokepaste_renderer import render_showdown
from sketch.replica.preview_view import (
    EditTeamModal,
    ReplicaPreviewView,
    team_to_embed,
)


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

    `defer()` and `send_message()` are exercised by the View buttons;
    `send_modal()` is exercised by the Edit button and the modal's
    re-open path on parse failure. We record what gets called so each
    test can assert the right code path fired (e.g. refusal path goes
    through `send_message` with ephemeral=True; modal re-open goes
    through `send_modal` with a fresh modal carrying the user's text).
    """

    defer_calls: int = 0
    send_message_calls: list[dict] = field(default_factory=list)
    send_modal_calls: list[Any] = field(default_factory=list)

    async def defer(self) -> None:
        self.defer_calls += 1

    async def send_message(self, content: str, **kwargs: Any) -> None:
        self.send_message_calls.append({"content": content, **kwargs})

    async def send_modal(self, modal: Any) -> None:
        self.send_modal_calls.append(modal)


@dataclass
class _FakeInteraction:
    user: _FakeUser
    response: _FakeResponse = field(default_factory=_FakeResponse)


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
        view = ReplicaPreviewView(invoker_id=1, team=_team(), timeout=300)
        assert view.decision is None
        # The view holds the original team unchanged until Edit / Confirm.
        assert view.team is not None

    async def test_invoker_can_pass_check(self):
        view = ReplicaPreviewView(invoker_id=42, team=_team(), timeout=300)
        interaction = _FakeInteraction(user=_FakeUser(id=42))
        assert await view.interaction_check(interaction) is True
        assert interaction.response.send_message_calls == []

    async def test_other_user_blocked_with_refusal(self):
        view = ReplicaPreviewView(invoker_id=42, team=_team(), timeout=300)
        interaction = _FakeInteraction(user=_FakeUser(id=99))
        assert await view.interaction_check(interaction) is False
        # The refusal goes out as an ephemeral message — the user who
        # clicked sees it, but the channel doesn't get spammed.
        assert len(interaction.response.send_message_calls) == 1
        call = interaction.response.send_message_calls[0]
        assert call["ephemeral"] is True
        assert "Only the user" in call["content"]

    async def test_confirm_sets_decision_true_and_defers(self):
        view = ReplicaPreviewView(invoker_id=42, team=_team(), timeout=300)
        interaction = _FakeInteraction(user=_FakeUser(id=42))
        # `view.confirm.callback` is discord.py's `_ItemCallback` wrapper —
        # it implicitly binds `self=view` and takes only the interaction.
        await view.confirm.callback(interaction)
        assert view.decision is True
        assert interaction.response.defer_calls == 1

    async def test_cancel_sets_decision_false_and_defers(self):
        view = ReplicaPreviewView(invoker_id=42, team=_team(), timeout=300)
        interaction = _FakeInteraction(user=_FakeUser(id=42))
        await view.cancel.callback(interaction)
        assert view.decision is False
        assert interaction.response.defer_calls == 1

    async def test_edit_button_opens_modal_with_current_paste(self):
        # The Edit button feeds the view's current team into the modal's
        # prefill — re-clicking Edit after an iteration should reflect
        # the most-recently-applied team, not the original OCR.
        view = ReplicaPreviewView(invoker_id=42, team=_team(), timeout=300)
        interaction = _FakeInteraction(user=_FakeUser(id=42))
        await view.edit.callback(interaction)
        assert len(interaction.response.send_modal_calls) == 1
        modal = interaction.response.send_modal_calls[0]
        assert isinstance(modal, EditTeamModal)
        # Prefill is the canonical Showdown render of the current team.
        assert modal.paste_input.default == render_showdown(view.team)
        # Decision shouldn't flip just from opening the modal.
        assert view.decision is None

    async def test_apply_edited_team_sets_decision_and_stops(self):
        # `_apply_edited_team` is the modal -> view callback. A successful
        # edit auto-commits: view.team is updated, decision=True, the
        # modal-submit interaction is deferred (no extra ephemeral
        # message), and the view stops so the command handler proceeds.
        original = _team()
        view = ReplicaPreviewView(invoker_id=42, team=original, timeout=300)
        edited = TeamData(
            pokemon=[_entry("Urshifu-Rapid-Strike")] + original.pokemon[1:],
            team_id=original.team_id,
        )
        modal_interaction = _FakeInteraction(user=_FakeUser(id=42))
        await view._apply_edited_team(edited, modal_interaction)
        assert view.team is edited
        assert view.decision is True
        assert modal_interaction.response.defer_calls == 1
        assert view.is_finished()


class TestEditTeamModal:
    async def test_submit_parses_and_calls_callback(self):
        # Happy path: feed the modal a valid Showdown paste; on submit
        # the parser succeeds and the callback fires with the parsed
        # team + the modal-submit interaction.
        original = _team()
        captured: list[tuple] = []

        async def callback(team, interaction):
            captured.append((team, interaction))

        modal = EditTeamModal(
            prefill=render_showdown(original),
            team_id="QBXXWXL05U",
            on_submit_callback=callback,
        )
        # `TextInput.value` is a property backed by `_value`; in production
        # discord.py populates it from the interaction payload. Tests
        # write it directly so we can exercise on_submit without a
        # gateway round-trip. Simulates the user not changing anything
        # (or re-submitting the same canonical text).
        modal.paste_input._value = render_showdown(original)  # type: ignore[attr-defined]

        interaction = _FakeInteraction(user=_FakeUser(id=42))
        await modal.on_submit(interaction)

        assert len(captured) == 1
        parsed_team, parsed_interaction = captured[0]
        assert parsed_interaction is interaction
        # The parser preserves the passed-in team_id.
        assert parsed_team.team_id == "QBXXWXL05U"
        # Six Pokemon, first one still Floette-Eternal (canonical fixture).
        assert len(parsed_team.pokemon) == 6
        assert parsed_team.pokemon[0].species == "Floette-Eternal"
        # No re-open modal was triggered.
        assert interaction.response.send_modal_calls == []

    async def test_submit_with_parse_error_reopens_modal(self):
        # Sad path: feed the modal a paste that parses-fails. The modal
        # responds by sending a fresh modal back to the user with the
        # broken text in its prefill and the error fragment in the title
        # / input label.
        async def callback(team, interaction):
            raise AssertionError("callback should not fire on parse error")

        # Only 5 Pokemon blocks → "Expected 6 Pokemon, got 5."
        broken_paste = "\r\n\r\n".join(
            [
                "Mimikyu\nAbility: Disguise\nAdamant Nature\n- Play Rough",
            ]
            * 5
        )

        modal = EditTeamModal(
            prefill=broken_paste,
            team_id="QBXXWXL05U",
            on_submit_callback=callback,
        )
        modal.paste_input._value = broken_paste  # type: ignore[attr-defined]

        interaction = _FakeInteraction(user=_FakeUser(id=42))
        await modal.on_submit(interaction)

        # A new modal was opened with the user's broken text preserved.
        assert len(interaction.response.send_modal_calls) == 1
        reopened = interaction.response.send_modal_calls[0]
        assert isinstance(reopened, EditTeamModal)
        assert reopened.paste_input.default == broken_paste
        # The error fragment is reflected in the modal title.
        assert "Expected 6" in reopened.title

    async def test_submit_with_nature_error_mentions_serious(self):
        # The plan calls out that the nature error must explicitly tell
        # the user to use "Serious" for neutral natures — Hardy is a
        # Showdown-accepted neutral but we don't support it.
        async def callback(team, interaction):
            raise AssertionError("callback should not fire on parse error")

        # Take the canonical paste and swap one nature to Hardy.
        paste = render_showdown(_team()).replace("Modest Nature", "Hardy Nature", 1)

        modal = EditTeamModal(
            prefill=paste,
            team_id="QBXXWXL05U",
            on_submit_callback=callback,
        )
        modal.paste_input._value = paste  # type: ignore[attr-defined]

        interaction = _FakeInteraction(user=_FakeUser(id=42))
        await modal.on_submit(interaction)

        assert len(interaction.response.send_modal_calls) == 1
        reopened = interaction.response.send_modal_calls[0]
        # Error must surface in the modal title (capped at 45 chars by
        # Discord and by `_truncate_for_discord`), naming either the
        # offending nature or the accepted neutral.
        assert "Hardy" in reopened.title or "Serious" in reopened.title
