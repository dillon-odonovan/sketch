"""End-to-end test for the `/delete-team` handler helpers.

Drives the internals (`_normalize_inputs`, `_resolve_target_url`,
`_delete_and_announce`) directly with a fake interaction and a stub
SheetsClient. Mirrors the test_add_team_vrpaste.py pattern — avoids
standing up CommandTree / app_commands plumbing for the handler itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from discord import app_commands

from sketch.commands import delete_team as dt
from sketch.commands._shared import GENERIC_SHEET_DELETE_ERROR
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
    """Stub SheetsClient — only the surface the handler touches.

    `delete_by_url` and `delete_by_replica` each return the configured
    `delete_result` TeamRow (found + deleted) or `None` (not found / CAS
    miss). Set `delete_raises` to simulate a transport error.
    """

    delete_by_url_result: TeamRow | None = None
    delete_by_replica_result: TeamRow | None = None
    delete_raises: Exception | None = None

    delete_by_url_calls: list[tuple[str, str]] = field(default_factory=list)
    delete_by_replica_calls: list[tuple[str, str]] = field(default_factory=list)
    invalidated: list[str] = field(default_factory=list)

    async def delete_by_url(self, sheet_name: str, url: str) -> TeamRow | None:
        self.delete_by_url_calls.append((sheet_name, url))
        if self.delete_raises is not None:
            raise self.delete_raises
        return self.delete_by_url_result

    async def delete_by_replica(self, sheet_name: str, replica: str) -> TeamRow | None:
        self.delete_by_replica_calls.append((sheet_name, replica))
        if self.delete_raises is not None:
            raise self.delete_raises
        return self.delete_by_replica_result

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


# --- _delete_and_announce --------------------------------------------------


def _store_with_broadcast(channel_id: int | None = None) -> StaticGuildConfigStore:
    return StaticGuildConfigStore(
        {222: GuildConfig(spreadsheet_id="ssid", broadcast_channel_id=channel_id)}
    )


class TestDeleteAndAnnounce:
    async def test_happy_path_url_with_broadcast(self):
        interaction = _FakeInteraction()
        broadcast_channel = _FakeChannel(id=555)
        interaction.client.channels[555] = broadcast_channel
        row = _row(row_number=8)
        sheets = _FakeSheets(delete_by_url_result=row)
        store = _store_with_broadcast(555)

        await dt._delete_and_announce(
            interaction,
            sheets,  # type: ignore[arg-type]
            store=store,
            inputs=_inputs(url="https://pokepast.es/abc"),
            target_url="https://pokepast.es/abc",
        )

        assert sheets.delete_by_url_calls == [
            ("Regulation M-A", "https://pokepast.es/abc")
        ]
        assert sheets.delete_by_replica_calls == []
        assert sheets.invalidated == ["Regulation M-A"]
        assert len(interaction.followup.sent) == 1
        success_content, _ = interaction.followup.sent[0]
        assert "Removed row 8" in success_content
        assert "team desc" in success_content

        assert len(broadcast_channel.sent) == 1
        embed = broadcast_channel.sent[0].embed
        assert embed.title == "Team removed from Reg M-A"
        assert embed.description == "team desc"
        species_field = next((f for f in embed.fields if f.name == "Pokémon"), None)
        assert species_field is not None
        assert "Charizard" in species_field.value

    async def test_happy_path_replica(self):
        interaction = _FakeInteraction()
        row = _row(row_number=9)
        sheets = _FakeSheets(delete_by_replica_result=row)
        store = _store_with_broadcast(None)

        await dt._delete_and_announce(
            interaction,
            sheets,  # type: ignore[arg-type]
            store=store,
            inputs=_inputs(replica="QBXXWXL05U"),
            target_url=None,
        )
        assert sheets.delete_by_replica_calls == [("Regulation M-A", "QBXXWXL05U")]
        assert sheets.delete_by_url_calls == []
        assert sheets.invalidated == ["Regulation M-A"]
        assert len(interaction.followup.sent) == 1
        assert "Removed row 9" in interaction.followup.sent[0][0]

    async def test_not_found_or_cas_mismatch_skips_broadcast(self):
        # delete_by_url returning None means "not found" or CAS guard fired.
        interaction = _FakeInteraction()
        broadcast_channel = _FakeChannel(id=555)
        interaction.client.channels[555] = broadcast_channel
        sheets = _FakeSheets(delete_by_url_result=None)
        store = _store_with_broadcast(555)

        await dt._delete_and_announce(
            interaction,
            sheets,  # type: ignore[arg-type]
            store=store,
            inputs=_inputs(url="https://pokepast.es/abc"),
            target_url="https://pokepast.es/abc",
        )
        assert sheets.invalidated == []
        assert broadcast_channel.sent == []
        assert len(interaction.followup.sent) == 1
        content, kwargs = interaction.followup.sent[0]
        assert "No team matching" in content
        assert kwargs["ephemeral"] is True

    async def test_transport_error_sends_generic_delete_error(self):
        interaction = _FakeInteraction()
        broadcast_channel = _FakeChannel(id=555)
        interaction.client.channels[555] = broadcast_channel
        sheets = _FakeSheets(delete_raises=RuntimeError("503"))
        store = _store_with_broadcast(555)

        await dt._delete_and_announce(
            interaction,
            sheets,  # type: ignore[arg-type]
            store=store,
            inputs=_inputs(url="https://pokepast.es/abc"),
            target_url="https://pokepast.es/abc",
        )
        assert sheets.invalidated == []
        assert broadcast_channel.sent == []
        assert len(interaction.followup.sent) == 1
        content, kwargs = interaction.followup.sent[0]
        assert content == GENERIC_SHEET_DELETE_ERROR
        assert kwargs["ephemeral"] is True

    async def test_no_broadcast_when_channel_unset(self):
        interaction = _FakeInteraction()
        broadcast_channel = _FakeChannel(id=555)
        interaction.client.channels[555] = broadcast_channel
        row = _row()
        sheets = _FakeSheets(delete_by_url_result=row)
        store = _store_with_broadcast(None)  # no broadcast channel configured

        await dt._delete_and_announce(
            interaction,
            sheets,  # type: ignore[arg-type]
            store=store,
            inputs=_inputs(url="https://pokepast.es/abc"),
            target_url="https://pokepast.es/abc",
        )
        assert len(interaction.followup.sent) == 1  # success ephemeral only
        assert broadcast_channel.sent == []  # channel is in client but not configured

    async def test_empty_species_omits_pokemon_field(self):
        interaction = _FakeInteraction()
        broadcast_channel = _FakeChannel(id=555)
        interaction.client.channels[555] = broadcast_channel
        sheets = _FakeSheets(delete_by_url_result=_row(species=[]))
        store = _store_with_broadcast(555)

        await dt._delete_and_announce(
            interaction,
            sheets,  # type: ignore[arg-type]
            store=store,
            inputs=_inputs(url="https://pokepast.es/abc"),
            target_url="https://pokepast.es/abc",
        )
        embed = broadcast_channel.sent[0].embed
        assert all(f.name != "Pokémon" for f in embed.fields)


# --- handler-level routing --------------------------------------------------


class TestBothSuppliedPrefersUrl:
    """When both `url` and `replica` are supplied, only delete_by_url is used."""

    async def test_url_wins_over_replica(self):
        interaction = _FakeInteraction()
        cache = InMemoryVRPasteCacheStore()

        # _resolve_target_url should return the URL, ignoring replica.
        target_url = await dt._resolve_target_url(
            interaction,
            inputs=_inputs(url="https://pokepast.es/abc", replica="QBXXWXL05U"),
            vrpaste_cache=cache,
        )
        assert target_url == "https://pokepast.es/abc"

        # _delete_and_announce should call delete_by_url, not delete_by_replica.
        row = _row()
        sheets = _FakeSheets(delete_by_url_result=row)
        store = _store_with_broadcast(None)

        await dt._delete_and_announce(
            interaction,
            sheets,  # type: ignore[arg-type]
            store=store,
            inputs=_inputs(url="https://pokepast.es/abc", replica="QBXXWXL05U"),
            target_url=target_url,
        )
        assert sheets.delete_by_url_calls != []
        assert sheets.delete_by_replica_calls == []
