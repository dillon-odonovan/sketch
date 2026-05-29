"""End-to-end test for the `/delete-team` handler helpers.

Drives the internals (`_normalize_inputs`, `_resolve_target_url`,
`_locate_row`, `_delete_and_announce`) directly with a fake interaction
and a stub SheetsClient. Mirrors the test_add_team_vrpaste.py pattern —
avoids standing up CommandTree / app_commands plumbing for the handler
itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from discord import app_commands

from sketch.commands import delete_team as dt
from sketch.storage.guild_config import GuildConfig, StaticGuildConfigStore
from sketch.storage.sheets_client import TeamRow
from sketch.vrpaste.cache import InMemoryVRPasteCacheStore

# --- fakes ------------------------------------------------------------------


@dataclass
class _FakeUser:
    id: int = 111
    display_name: str = "tester"

    @property
    def display_avatar(self):
        class _A:
            url = "https://cdn.example/test.png"

        return _A()


@dataclass
class _FakeFollowup:
    sent: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def send(self, content: str, **kwargs: Any) -> None:
        self.sent.append((content, kwargs))


@dataclass
class _FakeChannel:
    id: int
    sent: list[Any] = field(default_factory=list)

    async def send(self, *, embed):
        msg = _FakeMessage(embed=embed)
        self.sent.append(msg)
        return msg


@dataclass
class _FakeMessage:
    embed: Any = None


@dataclass
class _FakeClient:
    channels: dict[int, _FakeChannel] = field(default_factory=dict)

    def get_channel(self, channel_id: int):
        return self.channels.get(channel_id)


@dataclass
class _FakeInteraction:
    user: _FakeUser = field(default_factory=_FakeUser)
    guild_id: int | None = 222
    edits: list[dict[str, Any]] = field(default_factory=list)
    followup: _FakeFollowup = field(default_factory=_FakeFollowup)
    client: _FakeClient = field(default_factory=_FakeClient)

    async def edit_original_response(self, **kwargs: Any) -> None:
        self.edits.append(kwargs)


@dataclass
class _FakeSheets:
    """Stub SheetsClient — only the surface the handler touches."""

    find_url_result: TeamRow | None = None
    find_replica_result: TeamRow | None = None
    delete_result: bool = True
    delete_raises: Exception | None = None
    find_raises: Exception | None = None

    find_url_calls: list[tuple[str, str]] = field(default_factory=list)
    find_replica_calls: list[tuple[str, str]] = field(default_factory=list)
    delete_calls: list[dict[str, Any]] = field(default_factory=list)
    invalidated: list[str] = field(default_factory=list)

    async def find_row_by_url(self, sheet_name: str, url: str):
        self.find_url_calls.append((sheet_name, url))
        if self.find_raises is not None:
            raise self.find_raises
        return self.find_url_result

    async def find_row_by_replica(self, sheet_name: str, replica: str):
        self.find_replica_calls.append((sheet_name, replica))
        if self.find_raises is not None:
            raise self.find_raises
        return self.find_replica_result

    async def delete_row(self, sheet_name: str, row_number: int, **kwargs):
        self.delete_calls.append(
            {"sheet_name": sheet_name, "row_number": row_number, **kwargs}
        )
        if self.delete_raises is not None:
            raise self.delete_raises
        return self.delete_result

    def invalidate_snapshot(self, sheet_name: str) -> None:
        self.invalidated.append(sheet_name)


_DEFAULT_SPECIES = [
    "Charizard",
    "Venusaur",
    "Blastoise",
    "Pikachu",
    "Snorlax",
    "Gengar",
]


def _row(
    *,
    row_number: int = 4,
    url: str = "https://pokepast.es/abc",
    description: str = "team desc",
    species: list[str] | None = None,
) -> TeamRow:
    return TeamRow(
        row_number=row_number,
        url=url,
        description=description,
        species=_DEFAULT_SPECIES if species is None else species,
    )


def _inputs(
    *,
    url: str | None = None,
    replica: str | None = None,
) -> dt._DeleteTeamInputs:
    return dt._DeleteTeamInputs(
        fmt_name="Reg M-A",
        sheet_name="Regulation M-A",
        url=url,
        replica=replica,
    )


def _choice(value: str = "Reg M-A") -> app_commands.Choice[str]:
    return app_commands.Choice(name=value, value=value)


# --- _normalize_inputs ------------------------------------------------------


class TestNormalizeInputs:
    async def test_requires_url_or_replica(self):
        interaction = _FakeInteraction()
        result = await dt._normalize_inputs(
            interaction,
            format_choice=_choice(),
            url=None,
            replica=None,
        )
        assert result is None
        assert len(interaction.followup.sent) == 1
        content, kwargs = interaction.followup.sent[0]
        assert "Pokepaste/VRPaste URL" in content
        assert kwargs["ephemeral"] is True

    async def test_normalizes_replica_to_upper(self):
        interaction = _FakeInteraction()
        result = await dt._normalize_inputs(
            interaction,
            format_choice=_choice(),
            url=None,
            replica="qbxxwxl05u",
        )
        assert result is not None
        assert result.replica == "QBXXWXL05U"

    async def test_malformed_replica_returns_error(self):
        interaction = _FakeInteraction()
        result = await dt._normalize_inputs(
            interaction,
            format_choice=_choice(),
            url=None,
            replica="too-short",
        )
        assert result is None
        assert len(interaction.followup.sent) == 1


# --- _resolve_target_url ----------------------------------------------------


class TestResolveTargetUrl:
    async def test_pokepaste_url_canonicalizes(self):
        interaction = _FakeInteraction()
        cache = InMemoryVRPasteCacheStore()
        result = await dt._resolve_target_url(
            interaction,
            inputs=_inputs(url="http://pokepast.es/abc/"),
            vrpaste_cache=cache,
        )
        assert result == "https://pokepast.es/abc"

    async def test_vrpaste_cache_hit_returns_cached_url(self):
        interaction = _FakeInteraction()
        cache = InMemoryVRPasteCacheStore()
        cache.create(
            "gxmfscC1",
            "https://pokepast.es/from-cache",
            user_id=1,
            guild_id=1,
        )
        result = await dt._resolve_target_url(
            interaction,
            inputs=_inputs(url="https://www.vrpastes.com/gxmfscC1"),
            vrpaste_cache=cache,
        )
        assert result == "https://pokepast.es/from-cache"

    async def test_vrpaste_cache_miss_refuses(self):
        interaction = _FakeInteraction()
        cache = InMemoryVRPasteCacheStore()
        result = await dt._resolve_target_url(
            interaction,
            inputs=_inputs(url="https://www.vrpastes.com/unseen"),
            vrpaste_cache=cache,
        )
        assert result is None
        assert len(interaction.followup.sent) == 1
        content, _ = interaction.followup.sent[0]
        assert "don't have a record" in content
        assert "unseen" in content

    async def test_unknown_url_shape_refuses(self):
        interaction = _FakeInteraction()
        cache = InMemoryVRPasteCacheStore()
        result = await dt._resolve_target_url(
            interaction,
            inputs=_inputs(url="https://example.com/nope"),
            vrpaste_cache=cache,
        )
        assert result is None
        assert len(interaction.followup.sent) == 1
        content, _ = interaction.followup.sent[0]
        assert "Pokepaste or VRPaste URL" in content


# --- _locate_row -----------------------------------------------------------


class TestLocateRow:
    async def test_uses_url_lookup_when_target_url_set(self):
        interaction = _FakeInteraction()
        sheets = _FakeSheets(find_url_result=_row(row_number=10))
        row = await dt._locate_row(
            interaction,
            sheets,  # type: ignore[arg-type]
            inputs=_inputs(url="https://pokepast.es/abc"),
            target_url="https://pokepast.es/abc",
        )
        assert row is not None
        assert row.row_number == 10
        assert sheets.find_url_calls == [("Regulation M-A", "https://pokepast.es/abc")]
        assert sheets.find_replica_calls == []

    async def test_uses_replica_lookup_when_no_target_url(self):
        interaction = _FakeInteraction()
        sheets = _FakeSheets(find_replica_result=_row(row_number=11))
        row = await dt._locate_row(
            interaction,
            sheets,  # type: ignore[arg-type]
            inputs=_inputs(replica="QBXXWXL05U"),
            target_url=None,
        )
        assert row is not None
        assert row.row_number == 11
        assert sheets.find_url_calls == []
        assert sheets.find_replica_calls == [("Regulation M-A", "QBXXWXL05U")]

    async def test_no_match_sends_ephemeral(self):
        interaction = _FakeInteraction()
        sheets = _FakeSheets(find_url_result=None)
        row = await dt._locate_row(
            interaction,
            sheets,  # type: ignore[arg-type]
            inputs=_inputs(url="https://pokepast.es/abc"),
            target_url="https://pokepast.es/abc",
        )
        assert row is None
        assert len(interaction.followup.sent) == 1
        content, kwargs = interaction.followup.sent[0]
        assert "No team matching" in content
        assert kwargs["ephemeral"] is True

    async def test_transport_error_surfaces_generic_message(self):
        interaction = _FakeInteraction()
        sheets = _FakeSheets(find_raises=RuntimeError("503"))
        row = await dt._locate_row(
            interaction,
            sheets,  # type: ignore[arg-type]
            inputs=_inputs(url="https://pokepast.es/abc"),
            target_url="https://pokepast.es/abc",
        )
        assert row is None
        assert len(interaction.followup.sent) == 1


# --- _delete_and_announce --------------------------------------------------


def _store_with_broadcast(channel_id: int | None = None) -> StaticGuildConfigStore:
    return StaticGuildConfigStore(
        {222: GuildConfig(spreadsheet_id="ssid", broadcast_channel_id=channel_id)}
    )


class TestDeleteAndAnnounce:
    async def test_happy_path_with_broadcast(self):
        interaction = _FakeInteraction()
        broadcast_channel = _FakeChannel(id=555)
        interaction.client.channels[555] = broadcast_channel
        sheets = _FakeSheets()
        store = _store_with_broadcast(555)
        row = _row(row_number=8)

        await dt._delete_and_announce(
            interaction,
            sheets,  # type: ignore[arg-type]
            store=store,
            inputs=_inputs(url="https://pokepast.es/abc"),
            row=row,
            target_url="https://pokepast.es/abc",
        )

        assert sheets.delete_calls == [
            {
                "sheet_name": "Regulation M-A",
                "row_number": 8,
                "expected_url": "https://pokepast.es/abc",
                "expected_replica": None,
            }
        ]
        assert sheets.invalidated == ["Regulation M-A"]
        assert len(interaction.followup.sent) == 1
        success_content, _ = interaction.followup.sent[0]
        assert "Removed row 8" in success_content
        assert "team desc" in success_content

        assert len(broadcast_channel.sent) == 1
        embed = broadcast_channel.sent[0].embed
        assert embed.title == "Team removed from Reg M-A"
        assert embed.description == "team desc"
        # Species rendered as a field on the embed.
        species_field = next((f for f in embed.fields if f.name == "Pokémon"), None)
        assert species_field is not None
        assert "Charizard" in species_field.value

    async def test_replica_path_passes_expected_replica(self):
        interaction = _FakeInteraction()
        sheets = _FakeSheets()
        store = _store_with_broadcast(None)
        row = _row(row_number=9)

        await dt._delete_and_announce(
            interaction,
            sheets,  # type: ignore[arg-type]
            store=store,
            inputs=_inputs(replica="QBXXWXL05U"),
            row=row,
            target_url=None,
        )
        assert sheets.delete_calls[0]["expected_replica"] == "QBXXWXL05U"
        assert sheets.delete_calls[0]["expected_url"] is None

    async def test_compare_and_swap_mismatch_skips_broadcast(self):
        interaction = _FakeInteraction()
        broadcast_channel = _FakeChannel(id=555)
        interaction.client.channels[555] = broadcast_channel
        sheets = _FakeSheets(delete_result=False)
        store = _store_with_broadcast(555)

        await dt._delete_and_announce(
            interaction,
            sheets,  # type: ignore[arg-type]
            store=store,
            inputs=_inputs(url="https://pokepast.es/abc"),
            row=_row(),
            target_url="https://pokepast.es/abc",
        )
        assert sheets.invalidated == []
        assert broadcast_channel.sent == []
        assert len(interaction.followup.sent) == 1
        content, _ = interaction.followup.sent[0]
        assert "sheet shifted" in content

    async def test_delete_error_surfaces_generic_message(self):
        interaction = _FakeInteraction()
        sheets = _FakeSheets(delete_raises=RuntimeError("503"))
        store = _store_with_broadcast(555)

        await dt._delete_and_announce(
            interaction,
            sheets,  # type: ignore[arg-type]
            store=store,
            inputs=_inputs(url="https://pokepast.es/abc"),
            row=_row(),
            target_url="https://pokepast.es/abc",
        )
        assert sheets.invalidated == []
        assert len(interaction.followup.sent) == 1

    async def test_no_broadcast_when_channel_unset(self):
        interaction = _FakeInteraction()
        sheets = _FakeSheets()
        store = _store_with_broadcast(None)

        await dt._delete_and_announce(
            interaction,
            sheets,  # type: ignore[arg-type]
            store=store,
            inputs=_inputs(url="https://pokepast.es/abc"),
            row=_row(),
            target_url="https://pokepast.es/abc",
        )
        # Success message went through; no broadcast attempts.
        assert len(interaction.followup.sent) == 1

    async def test_empty_species_omits_pokemon_field(self):
        interaction = _FakeInteraction()
        broadcast_channel = _FakeChannel(id=555)
        interaction.client.channels[555] = broadcast_channel
        sheets = _FakeSheets()
        store = _store_with_broadcast(555)
        row = _row(species=[])

        await dt._delete_and_announce(
            interaction,
            sheets,  # type: ignore[arg-type]
            store=store,
            inputs=_inputs(url="https://pokepast.es/abc"),
            row=row,
            target_url="https://pokepast.es/abc",
        )
        embed = broadcast_channel.sent[0].embed
        assert all(f.name != "Pokémon" for f in embed.fields)


# --- handler-level routing --------------------------------------------------


class TestBothSuppliedPrefersUrl:
    """End-to-end check on _resolve_target_url + _locate_row that when both
    `url` and `replica` are supplied, only the URL lookup is exercised."""

    async def test_url_wins_over_replica(self):
        interaction = _FakeInteraction()
        cache = InMemoryVRPasteCacheStore()
        sheets = _FakeSheets(find_url_result=_row())

        target_url = await dt._resolve_target_url(
            interaction,
            inputs=_inputs(url="https://pokepast.es/abc", replica="QBXXWXL05U"),
            vrpaste_cache=cache,
        )
        assert target_url == "https://pokepast.es/abc"

        row = await dt._locate_row(
            interaction,
            sheets,  # type: ignore[arg-type]
            inputs=_inputs(url="https://pokepast.es/abc", replica="QBXXWXL05U"),
            target_url=target_url,
        )
        assert row is not None
        assert sheets.find_url_calls != []
        assert sheets.find_replica_calls == []
