"""
Bot-owner commands.

/activity sets the bot's presence (the "Playing ..." / "Watching ..." line
under its name, plus online / idle / do-not-disturb). Only the bot's owner
(or a member of the owning Developer Portal team) can use it, since the
presence is shared across every server the bot is in. The setting is saved
to the database and re-applied whenever the bot connects, so it survives
restarts.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from db import ConfigStore

log = logging.getLogger(__name__)

PRESENCE_SETTING_KEY = "presence"

ACTIVITY_TYPE_CHOICES = [
    app_commands.Choice(name="Playing", value="playing"),
    app_commands.Choice(name="Watching", value="watching"),
    app_commands.Choice(name="Listening to", value="listening"),
    app_commands.Choice(name="Competing in", value="competing"),
    app_commands.Choice(name="Custom status (text only)", value="custom"),
    app_commands.Choice(name="None - clear the activity", value="clear"),
]

STATUS_CHOICES = [
    app_commands.Choice(name="Online", value="online"),
    app_commands.Choice(name="Idle", value="idle"),
    app_commands.Choice(name="Do Not Disturb", value="dnd"),
    app_commands.Choice(name="Invisible", value="invisible"),
]

ACTIVITY_TYPES = {
    "playing": discord.ActivityType.playing,
    "watching": discord.ActivityType.watching,
    "listening": discord.ActivityType.listening,
    "competing": discord.ActivityType.competing,
}

ACTIVITY_LABELS = {
    "playing": "Playing",
    "watching": "Watching",
    "listening": "Listening to",
    "competing": "Competing in",
}


def _build_activity(kind: Optional[str], text: Optional[str]) -> Optional[discord.BaseActivity]:
    if not kind or not text:
        return None
    if kind == "custom":
        return discord.CustomActivity(name=text)
    return discord.Activity(type=ACTIVITY_TYPES[kind], name=text)


def _describe(kind: Optional[str], text: Optional[str], status: str) -> str:
    if not kind or not text:
        activity = "no activity"
    elif kind == "custom":
        activity = f"custom status **{text}**"
    else:
        activity = f"**{ACTIVITY_LABELS[kind]} {text}**"
    return f"{activity}, status **{status}**"


async def _is_owner(interaction: discord.Interaction) -> bool:
    return await interaction.client.is_owner(interaction.user)


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot, store: ConfigStore):
        self.bot = bot
        self.store = store

    async def _load(self) -> dict:
        raw = await self.store.get_setting(PRESENCE_SETTING_KEY)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            log.warning("Ignoring unreadable saved presence: %r", raw)
            return {}

    async def _apply(self, saved: dict) -> None:
        await self.bot.change_presence(
            activity=_build_activity(saved.get("type"), saved.get("text")),
            status=discord.Status(saved.get("status", "online")),
        )

    @commands.Cog.listener()
    async def on_ready(self):
        # on_ready fires again after a full reconnect, so this also restores
        # the presence after Discord drops the session.
        saved = await self._load()
        if saved:
            await self._apply(saved)

    @app_commands.command(name="activity", description="Set the bot's activity status (bot owner only)")
    @app_commands.describe(
        type="What kind of activity to show",
        text="The activity text, e.g. 'your glucose' for 'Watching your glucose'",
        status="Online indicator (default: leave as is)",
    )
    @app_commands.choices(type=ACTIVITY_TYPE_CHOICES, status=STATUS_CHOICES)
    # Owner-only, so it's offered in servers only, never through a personal install.
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    @app_commands.check(_is_owner)
    async def set_activity(
        self,
        interaction: discord.Interaction,
        type: app_commands.Choice[str],
        text: Optional[app_commands.Range[str, 1, 128]] = None,
        status: Optional[app_commands.Choice[str]] = None,
    ):
        saved = await self._load()

        if type.value == "clear":
            kind, text = None, None
        else:
            text = text.strip() if text else None
            if not text:
                await interaction.response.send_message(
                    f"Give some `text` to show for **{type.name}**.", ephemeral=True
                )
                return
            kind = type.value

        new = {
            "type": kind,
            "text": text,
            "status": status.value if status else saved.get("status", "online"),
        }
        await self._apply(new)
        await self.store.set_setting(PRESENCE_SETTING_KEY, json.dumps(new))
        await interaction.response.send_message(
            f"Bot presence set to {_describe(kind, text, new['status'])}.", ephemeral=True
        )

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            msg = "Only the bot's owner can use this command."
        else:
            log.error("Unhandled error in /%s", interaction.command.name if interaction.command else "?",
                      exc_info=error)
            msg = "Something went wrong running that command. Please try again later."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot):
    store: ConfigStore = bot.config_store  # attached in bot.py
    await bot.add_cog(Admin(bot, store))
