import logging

import discord
from discord import app_commands
from google.cloud import firestore

import config
import logging_setup
from commands import setup_commands
from guild_config import FirestoreGuildConfigStore
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

        # Firestore.Client() auto-detects the project via ADC (the metadata
        # server on GCE, or `gcloud auth application-default login` locally).
        # No explicit project arg — passing `None` keeps the same auto-detect
        # path the Sheets and Secret Manager clients already use.
        self._store = FirestoreGuildConfigStore(firestore.Client())
        self._registry = SheetsClientRegistry(self._store)

        self._dev_guild = (
            discord.Object(id=int(config.DEV_GUILD_ID)) if config.DEV_GUILD_ID else None
        )
        if self._dev_guild is not None:
            logger.warning(
                "DEV_GUILD_ID is set — slash commands will be mirrored to guild %s "
                "for fast iteration (dev mode). Unset DEV_GUILD_ID in production.",
                self._dev_guild.id,
            )

        # on_ready can fire multiple times per process (reconnect/resume).
        # This flag ensures the guild-scope cleanup pass runs only once
        # per process, not on every resume.
        self._cleaned_guild_commands = False

    async def setup_hook(self) -> None:
        # discord.py runs setup_hook exactly once before the gateway connects.
        # That's the right place for one-time async init: register commands
        # and sync them. DEX is no longer preloaded here — each per-guild
        # SheetsClient lazy-loads its own DEX on first /search-teams.
        # https://discordpy.readthedocs.io/en/stable/api.html#discord.Client.setup_hook
        setup_commands(self.tree, self._store, self._registry)
        if self._dev_guild:
            # copy_global_to mirrors the global command list into the dev
            # guild's slot on the tree (client-side, no network). The
            # subsequent sync(guild=...) pushes that slot to Discord, which
            # makes the commands visible in the dev guild within ~5s — no
            # waiting on global propagation. The actual global scope on
            # Discord's side is untouched in this branch.
            self.tree.copy_global_to(guild=self._dev_guild)
            await self.tree.sync(guild=self._dev_guild)
            logger.info("Mirrored global commands to dev guild %s", self._dev_guild.id)
        else:
            await self.tree.sync()
            logger.info("Synced commands globally (~1h to propagate)")

    async def on_ready(self) -> None:
        user = self.user
        logger.info("Logged in as %s (id=%s)", user, user.id if user else "?")

        # Self-healing cleanup of stale guild-scope commands. Discord stores
        # global and guild-scope commands as separate registrations; a
        # command name present in both scopes shows up twice in the picker.
        # This pass clears the guild-scope of every guild we're in (skipping
        # the current dev guild, whose mirror we just installed in
        # setup_hook). It leaves the global scope untouched, so users still
        # see global commands via Discord's normal propagation.
        if self._cleaned_guild_commands:
            return
        self._cleaned_guild_commands = True

        for guild in self.guilds:
            if self._dev_guild is not None and guild.id == self._dev_guild.id:
                continue
            self.tree.clear_commands(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("Cleared guild-scope commands in guild %s", guild.id)


def main() -> None:
    SketchBot().run(config.DISCORD_TOKEN)


if __name__ == "__main__":
    main()
