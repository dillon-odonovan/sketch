import logging

import discord
from discord import app_commands

import config
from commands import DexIndex, setup_commands
from sheets_client import SheetsClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class SketchBot(discord.Client):
    def __init__(self) -> None:
        # Slash commands deliver their args directly via the interaction
        # payload, so no privileged intents (Message Content, Members, Presence)
        # are required. Keeping intents minimal avoids gating issues when the
        # bot grows past 100 servers.
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self._sheets = SheetsClient()
        self._guild = (
            discord.Object(id=int(config.DISCORD_GUILD_ID))
            if config.DISCORD_GUILD_ID
            else None
        )

    async def setup_hook(self) -> None:
        # discord.py runs setup_hook exactly once before the gateway connects.
        # That's the right place for one-time async init: load DEX, register
        # commands, sync them. https://discordpy.readthedocs.io/en/stable/api.html#discord.Client.setup_hook
        names = await self._sheets.load_dex_names()
        dex = DexIndex(names)
        setup_commands(self.tree, self._sheets, dex, guild=self._guild)
        if self._guild:
            await self.tree.sync(guild=self._guild)
            logger.info("Synced commands to guild %s", self._guild.id)
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
