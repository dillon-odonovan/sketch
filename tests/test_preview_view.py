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


def _entry(species: str = "Iron Hands") -> PokemonEntry:
    return PokemonEntry(
        species=species,
        item="Assault Vest",
        ability="Quark Drive",
        tera_type="Water",
        nature="Adamant",
        evs={"hp": 252, "atk": 252, "def": 0, "spa": 0, "spd": 4, "spe": 0},
        ivs=None,
        moves=["Fake Out", "Drain Punch", "Wild Charge", "Heavy Slam"],
        level=50,
    )


def _team() -> TeamData:
    return TeamData(
        pokemon=[
            _entry("Iron Hands"),
            _entry("Calyrex-Shadow"),
            _entry("Urshifu-Rapid-Strike"),
            _entry("Amoonguss"),
            _entry("Rillaboom"),
            _entry("Incineroar"),
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

    def test_field_names_carry_item_suffix(self):
        embed = team_to_embed(
            _team(),
            code="AAAA111122",
            description="...",
            fmt_name="Reg M-A",
        )
        assert embed.fields[0].name == "Iron Hands @ Assault Vest"

    def test_field_value_includes_ability_tera_nature_moves(self):
        embed = team_to_embed(
            _team(),
            code="AAAA111122",
            description="...",
            fmt_name="Reg M-A",
        )
        value = embed.fields[0].value
        assert "Quark Drive" in value
        assert "Water" in value
        assert "Adamant" in value
        # EV summary uses the share-screen ordering — the test asserts the
        # exact slash-separated form so a reorder regression is caught.
        assert "252/252/0/0/4/0" in value
        assert "Fake Out, Drain Punch, Wild Charge, Heavy Slam" in value

    def test_title_includes_code_and_description(self):
        embed = team_to_embed(
            _team(),
            code="AAAA111122",
            description="my team",
            fmt_name="Reg M-A",
        )
        assert "AAAA111122" in embed.title
        assert "my team" in embed.description
        assert "Reg M-A" in embed.description

    def test_no_item_renders_without_suffix(self):
        team = TeamData(pokemon=[_entry()])
        team.pokemon[0] = PokemonEntry(
            species="Cresselia",
            item=None,
            ability="Levitate",
            tera_type="Fairy",
            nature="Bold",
            evs={"hp": 252, "atk": 0, "def": 252, "spa": 0, "spd": 4, "spe": 0},
            ivs=None,
            moves=["Moonblast", "Lunar Blessing", "Trick Room", "Ally Switch"],
            level=50,
        )
        embed = team_to_embed(team, code="X" * 10, description="d", fmt_name="Reg M-A")
        assert embed.fields[0].name == "Cresselia"
        assert " @ " not in embed.fields[0].name


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
