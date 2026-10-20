"""
Entry point for the Nightscout Discord bot.

Environment variables (see .env.example):
    DISCORD_BOT_TOKEN   - required, your bot's token
    DB_PATH             - optional, path to the SQLite file (default: nightscout_bot.db)
    GUILD_ID            - optional, a guild ID to sync slash commands to instantly
                           during development instead of waiting for the ~1h
                           global command propagation delay.
"""

from __future__ import annotations

import asyncio
import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

from db import ConfigStore

load_dotenv()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("nightscout-bot")

TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
DB_PATH = os.environ.get("DB_PATH", "nightscout_bot.db")
GUILD_ID = os.environ.get("GUILD_ID")

INTENTS = discord.Intents.default()
# No privileged intents (message content, members, presences) are needed --
# everything here runs through slash commands and modals.


class NightscoutBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned, intents=INTENTS)
        self.config_store = ConfigStore(DB_PATH)

    async def setup_hook(self) -> None:
        await self.config_store.connect()
        await self.load_extension("cogs.nightscout")
        await self.load_extension("cogs.admin")

        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d commands to guild %s", len(synced), GUILD_ID)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global commands", len(synced))

    async def close(self) -> None:
        await self.config_store.close()
        await super().close()


bot = NightscoutBot()


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "?")


def main():
    if not TOKEN:
        raise SystemExit(
            "DISCORD_BOT_TOKEN is not set. Copy .env.example to .env and fill it in."
        )
    asyncio.run(bot.start(TOKEN))


if __name__ == "__main__":
    main()
