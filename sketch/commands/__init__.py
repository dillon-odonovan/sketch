"""Slash-command package.

Each command lives in its own module under `sketch.commands.*`. The
`setup_commands` entry point composes them onto a `CommandTree` at bot
startup. Splitting per-command keeps a single command's diff scoped to
its own file rather than shifting line numbers across an 1100-line
monolith every time a handler grows or shrinks.

A handful of helpers are re-exported here so the import path used by
`tests/test_commands.py` (`from sketch.commands import ...`) keeps
working after the split.
"""

from __future__ import annotations

import anthropic
from discord import app_commands

from sketch.champions.replica_cache import ReplicaCacheStore
from sketch.commands._shared import (
    _SPREADSHEET_ID_RE,
    _filter_team_rows,
    _spreadsheet_link,
)
from sketch.commands.add_team import register as _register_add_team
from sketch.commands.admin import register as _register_admin
from sketch.commands.help import register as _register_help
from sketch.commands.search_teams import register as _register_search_teams
from sketch.search.dex import DexIndex  # re-exported for backwards-compatible callers
from sketch.storage.guild_config import GuildConfigStore
from sketch.storage.sheets_client import SheetsClientRegistry
from sketch.vrpaste.cache import VRPasteCacheStore

__all__ = [
    "DexIndex",
    "_SPREADSHEET_ID_RE",
    "_filter_team_rows",
    "_spreadsheet_link",
    "setup_commands",
]


def setup_commands(
    tree: app_commands.CommandTree,
    store: GuildConfigStore,
    registry: SheetsClientRegistry,
    *,
    replica_cache: ReplicaCacheStore,
    vrpaste_cache: VRPasteCacheStore,
    anthropic_client: anthropic.AsyncAnthropic,
) -> None:
    """Register every slash command on `tree`.

    Commands are always registered in the global scope. Dev-mode fast
    iteration is handled in bot.py via `tree.copy_global_to(guild=...)`,
    which mirrors these globals into a single dev guild without creating a
    second source of truth here. See bot.py:setup_hook.

    The registry handles spreadsheet routing; `store` is captured by each
    command's handler so they can read other per-guild settings (e.g.,
    broadcast_channel_id) that don't belong to the SheetsClient. The two
    source-specific caches and `anthropic_client` are kwarg-only because
    they're only used by `/add-team` — keyword-only makes the dependency
    explicit at the bot.py call site.
    """
    _register_add_team(
        tree,
        store,
        registry,
        replica_cache=replica_cache,
        vrpaste_cache=vrpaste_cache,
        anthropic_client=anthropic_client,
    )
    _register_search_teams(tree, registry)
    _register_help(tree)
    _register_admin(tree, store, registry)
