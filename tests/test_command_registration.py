"""Guards against Discord API constraints that `discord.py` only validates
server-side, at `tree.sync()` — never when the command tree is merely built
locally.

Concretely: `/edit-team` shipped with a 109-character command description.
Building the `CommandTree` (what our other tests, and even a manual local
run of the bot without a real token, exercise) raised nothing — discord.py
doesn't enforce Discord's 100-character command-description cap client-side.
It only surfaces once `tree.sync()` round-trips to the real API, which needs
a live token, so this shipped straight through CI and crash-looped the
production bot on every restart until a human read the container logs.

This test builds the real `CommandTree` (same `@tree.command`/`@describe`/
`@choices` decorators the bot applies in `setup_hook`) and checks the one
constraint we know Discord enforces without needing network access: each
top-level command's `description` is 100 characters or fewer.
"""

from unittest.mock import MagicMock

from discord import Client, Intents, app_commands

from sketch.commands import setup_commands

_COMMAND_DESCRIPTION_MAX_LENGTH = 100


def _build_tree() -> app_commands.CommandTree:
    client = Client(intents=Intents.none())
    tree = app_commands.CommandTree(client)
    setup_commands(
        tree,
        MagicMock(),
        MagicMock(),
        replica_cache=MagicMock(),
        vrpaste_cache=MagicMock(),
        anthropic_client=MagicMock(),
    )
    return tree


def test_every_command_description_fits_discords_limit():
    tree = _build_tree()
    commands = tree.get_commands()
    assert commands, "setup_commands registered nothing — test exercises nothing"

    over_limit = [
        f"/{cmd.name} ({len(cmd.description)} chars)"
        for cmd in commands
        if len(cmd.description) > _COMMAND_DESCRIPTION_MAX_LENGTH
    ]
    assert not over_limit, (
        f"Command description(s) exceed Discord's "
        f"{_COMMAND_DESCRIPTION_MAX_LENGTH}-char limit: {over_limit}"
    )
