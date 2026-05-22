import logging

import discord
from discord import app_commands

import config
import logging_setup
from commands import setup_commands
from guild_config import StaticGuildConfigStore, parse_guild_config_json
from sheets_client import SheetsClientRegistry

logging_setup.configure()
logger = logging.getLogger(__name__)


class SketchBot(discord.Client):
    def __init__(self) -> None:
        # Slash commands deliver their args directly via the interaction
        # payload, so no privileged intents (Message Content, Members, Presence)
        # are required. Keeping intents minimal avoids gating issues when the
        # bot grows past 100 servers.
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

        # Fail fast on malformed GUILD_CONFIG_JSON — better a startup crash
        # with a clear message than a runtime KeyError on the first command.
        guild_map = parse_guild_config_json(config.GUILD_CONFIG_JSON)
        self._store = StaticGuildConfigStore(guild_map)
        self._registry = SheetsClientRegistry(self._store)
        logger.info(
            "Loaded guild config for %d guild(s): %s",
            len(guild_map),
            self._store.configured_guild_ids(),
        )

        self._dev_guild = (
            discord.Object(id=int(config.DEV_GUILD_ID)) if config.DEV_GUILD_ID else None
        )
        if self._dev_guild is not None:
            logger.warning(
                "DEV_GUILD_ID is set — slash commands will sync ONLY to guild %s "
                "(dev mode). Unset DEV_GUILD_ID in production for global sync.",
                self._dev_guild.id,
            )

    async def setup_hook(self) -> None:
        # discord.py runs setup_hook exactly once before the gateway connects.
        # That's the right place for one-time async init: register commands
        # and sync them. DEX is no longer preloaded here — each per-guild
        # SheetsClient lazy-loads its own DEX on first /search-teams.
        # https://discordpy.readthedocs.io/en/stable/api.html#discord.Client.setup_hook
        setup_commands(
            self.tree, self._store, self._registry, dev_guild=self._dev_guild
        )
        if self._dev_guild:
            await self.tree.sync(guild=self._dev_guild)
            logger.info("Synced commands to dev guild %s", self._dev_guild.id)
        else:
            await self.tree.sync()
            logger.info("Synced commands globally (~1h to propagate)")

    async def on_ready(self) -> None:
        user = self.user
        logger.info("Logged in as %s (id=%s)", user, user.id if user else "?")


def main() -> None:
    SketchBot().run(config.DISCORD_TOKEN)


if __name__ == "__main__":
    main()
