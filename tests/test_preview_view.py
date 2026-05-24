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


def _entry(species: str = "Floette") -> PokemonEntry:
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
    # Species mirror the Wyatt reference team so the field-name golden test
    # matches the same data the renderer test uses.
    return TeamData(
        pokemon=[
            _entry("Floette"),
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
    def test_one_field_per_pokemon(self):
        embed = team_to_embed(
            _team(),
            code="AAAA111122",
            description="jsmith — sample team",
            fmt_name="Reg M-A",
        )
        assert len(embed.fields) == 6

    def test_field_names_carry_gender_and_item_suffix(self):
        embed = team_to_embed(
            _team(),
            code="QBXXWXL05U",
            description="...",
            fmt_name="Reg M-A",
        )
        # Matches the rendered Showdown shape: `Species (Gender) @ Item`.
        assert embed.fields[0].name == "Floette (F) @ Floettite"

    def test_field_value_includes_ability_nature_moves(self):
        embed = team_to_embed(
            _team(),
            code="QBXXWXL05U",
            description="...",
            fmt_name="Reg M-A",
        )
        value = embed.fields[0].value
        assert "Flower Veil" in value
        assert "Modest" in value
        # EV summary uses the share-screen ordering — test the exact slash
        # form so a reorder regression is caught.
        assert "32/0/0/32/0/2" in value
        assert "Dazzling Gleam, Moonblast, Light of Ruin, Protect" in value

    def test_field_value_does_not_mention_tera(self):
        # Tera isn't surfaced in the Champions share screen and isn't part
        # of the rendered output, so it shouldn't be in the preview either
        # — its absence keeps the embed honest about what we captured.
        embed = team_to_embed(
            _team(),
            code="QBXXWXL05U",
            description="...",
            fmt_name="Reg M-A",
        )
        for embed_field in embed.fields:
            assert "Tera" not in embed_field.value

    def test_title_includes_code_and_description(self):
        embed = team_to_embed(
            _team(),
            code="QBXXWXL05U",
            description="my team",
            fmt_name="Reg M-A",
        )
        assert "QBXXWXL05U" in embed.title
        assert "my team" in embed.description
        assert "Reg M-A" in embed.description

    def test_genderless_no_item_renders_clean_name(self):
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
        assert embed.fields[0].name == "Klefki"
        assert " @ " not in embed.fields[0].name
        assert "()" not in embed.fields[0].name


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
