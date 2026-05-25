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
from sketch.replica.preview_view import ReplicaPreviewView, team_to_embed


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

    Only `defer()` and `send_message()` are exercised by the View — both
    are awaited inside the callbacks. We record what gets called so the
    invoker-gate test can assert the refusal path went through
    `send_message` (ephemeral=True) rather than silently failing.
    """

    defer_calls: int = 0
    send_message_calls: list[dict] = field(default_factory=list)

    async def defer(self) -> None:
        self.defer_calls += 1

    async def send_message(self, content: str, **kwargs: Any) -> None:
        self.send_message_calls.append({"content": content, **kwargs})


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
        view = ReplicaPreviewView(invoker_id=1, timeout=300)
        assert view.decision is None

    async def test_invoker_can_pass_check(self):
        view = ReplicaPreviewView(invoker_id=42, timeout=300)
        interaction = _FakeInteraction(user=_FakeUser(id=42))
        assert await view.interaction_check(interaction) is True
        assert interaction.response.send_message_calls == []

    async def test_other_user_blocked_with_refusal(self):
        view = ReplicaPreviewView(invoker_id=42, timeout=300)
        interaction = _FakeInteraction(user=_FakeUser(id=99))
        assert await view.interaction_check(interaction) is False
        # The refusal goes out as an ephemeral message — the user who
        # clicked sees it, but the channel doesn't get spammed.
        assert len(interaction.response.send_message_calls) == 1
        call = interaction.response.send_message_calls[0]
        assert call["ephemeral"] is True
        assert "Only the user" in call["content"]

    async def test_confirm_sets_decision_true_and_defers(self):
        view = ReplicaPreviewView(invoker_id=42, timeout=300)
        interaction = _FakeInteraction(user=_FakeUser(id=42))
        # `view.confirm.callback` is discord.py's `_ItemCallback` wrapper —
        # it implicitly binds `self=view` and takes only the interaction.
        await view.confirm.callback(interaction)
        assert view.decision is True
        assert interaction.response.defer_calls == 1

    async def test_cancel_sets_decision_false_and_defers(self):
        view = ReplicaPreviewView(invoker_id=42, timeout=300)
        interaction = _FakeInteraction(user=_FakeUser(id=42))
        await view.cancel.callback(interaction)
        assert view.decision is False
        assert interaction.response.defer_calls == 1
