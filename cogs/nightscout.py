"""
Slash commands for the Nightscout bot.

Everything is grouped under `/ns ...`. Configuration is entirely
per-Discord-user (keyed by user ID), so the same bot instance in a shared
server lets each member point at their own Nightscout site.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

import discord
from discord import app_commands
from discord.ext import commands

from db import ConfigStore, UserConfig
from graphing import (
    GRAPH_FIXED_Y_AXIS_MAX_MGDL,
    GRAPH_FIXED_Y_AXIS_MIN_MGDL,
    MGDL_PER_MMOL,
    ROLLING_AVERAGE_WINDOWS_DAYS,
    NoDataError,
    compute_gmi,
    compute_time_in_range_stats,
    format_delta,
    format_time_span,
    format_units,
    format_value,
    render_agp_graph,
    render_glucose_graph,
    render_rolling_averages_graph,
    render_tdd_image,
    render_time_in_range_image,
    COLORBLIND_COLORS,
    COLOR_PRESETS,
    DEFAULT_COLORS,
    MIN_COLOR_LUMINANCE,
    RangeColors,
    Theme,
    THEME_NAMES,
    THEMES,
    color_luminance,
    parse_color,
)
from nightscout_client import (
    NightscoutError,
    detect_server_type,
    direction_to_arrow,
    devicestatus_time_ms,
    extract_iob,
    extract_last_loop_ms,
    extract_pump_battery,
    extract_pump_suspended,
    extract_reservoir,
    extract_uploader_battery,
    fetch_latest_devicestatus,
    fetch_latest_treatment,
    fetch_profile,
    fetch_recent_devicestatus,
    fetch_sgv_entries,
    fetch_status,
    fetch_treatments,
    treatment_time_ms,
)
from insulin import basal_schedule_from_profile, basal_timeline, bolus_units, compute_daily_doses

log = logging.getLogger(__name__)


def _discord_time(ms: int, style: str = "R") -> str:
    """A Discord timestamp tag -- shown in each viewer's own timezone, and
    for style "R" kept live ("3 minutes ago")."""
    return f"<t:{ms // 1000}:{style}>"


def _format_age(ms: int) -> str:
    minutes = max(0, ms) // 60000
    days, rem = divmod(minutes, 24 * 60)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {mins}m" if hours else f"{mins}m"


# /ns ages: label -> the treatment eventTypes that reset that clock.
DEVICE_AGE_EVENTS = [
    ("Sensor", ("Sensor Start", "Sensor Change")),
    # Tandem pumps (and some uploaders) log a cannula fill with every site
    # change and a cartridge / reservoir change with every insulin change.
    ("Site / cannula", ("Site Change", "Cannula Change")),
    ("Insulin", ("Insulin Change", "Reservoir Change")),
    ("Pump battery", ("Pump Battery Change",)),
]
# Left off /ns ages entirely when nothing is recorded, rather than shown as
# "no record": rechargeable pumps (e.g. Tandem t:slim X2 / Mobi) never log a
# battery change, so for them it could never fill in.
HIDE_AGE_WHEN_MISSING = {"Pump battery"}

# /ns status: minutes without a new reading before the data counts as stale.
STALE_READING_MINUTES = 15


def _format_thresholds(values: dict, units: str) -> str:
    """e.g. "very low 55, low 70, high 180, very high 250 mg/dL", from mg/dL values."""
    return (
        f"very low {format_value(values['very_low_threshold'], units)}, "
        f"low {format_value(values['low_threshold'], units)}, "
        f"high {format_value(values['high_threshold'], units)}, "
        f"very high {format_value(values['very_high_threshold'], units)} {units}"
    )


def _safe_error(exc: Exception, config: UserConfig) -> str:
    """
    An error's message with the site URL, hostname, and credentials from
    `config` scrubbed out. Error replies can be public, or go to someone
    other than the site's owner (/ns check-on), so this is applied to every
    error shown in Discord -- a second line of defence behind
    `NightscoutError` messages never including them in the first place.
    """
    message = str(exc)
    sensitive = [config.nightscout_url, config.api_token, config.api_secret_hash]
    if config.nightscout_url:
        sensitive.append(urlsplit(config.nightscout_url).netloc)
        sensitive.append(urlsplit(config.nightscout_url).hostname)
    for value in sorted(filter(None, set(sensitive)), key=len, reverse=True):
        message = re.sub(re.escape(value), "[hidden]", message, flags=re.IGNORECASE)
    return message


async def _send_error(interaction: discord.Interaction, message: str) -> None:
    """
    Shows `message` only to the person who ran the command.

    Most data commands defer with a public "thinking..." reply, and Discord
    turns the first followup into that same public message, ignoring
    `ephemeral=True`. So once a response exists it's deleted first, which
    lets the followup go out as a fresh ephemeral message instead.
    """
    if not interaction.response.is_done():
        await interaction.response.send_message(message, ephemeral=True)
        return
    try:
        await interaction.delete_original_response()
    except discord.HTTPException:
        pass
    await interaction.followup.send(message, ephemeral=True)


UNIT_CHOICES = [
    app_commands.Choice(name="mg/dL", value="mg/dL"),
    app_commands.Choice(name="mmol/L", value="mmol/L"),
]

PRIVACY_MODE_CHOICES = [
    app_commands.Choice(name="Private (default) - nobody can check on you", value="private"),
    app_commands.Choice(name="Public - anyone who shares a server with you", value="public"),
]

Y_AXIS_MODE_CHOICES = [
    app_commands.Choice(name="Dynamic (default) - scales to fit your readings", value="dynamic"),
    app_commands.Choice(
        name=f"Fixed - always {GRAPH_FIXED_Y_AXIS_MIN_MGDL:.0f}-{GRAPH_FIXED_Y_AXIS_MAX_MGDL:.0f} mg/dL",
        value="fixed",
    ),
]

# A pure safety ceiling for `fetch_sgv_entries(count=...)` calls that also
# pass `start_ms` -- Nightscout's own `find[date]` filter does the real work
# of bounding the request to the desired window, so this only matters as a
# fallback against a very old Nightscout version that doesn't understand
# `find`. Sized for the widest window any command can ask for (/ns averages'
# 90 days + 30 days of lead-in for its 30-day line) at an aggressive
# 1-reading/minute pace, with room to spare.
MAX_ENTRIES_SAFETY_CAP = 200_000


class SetupModal(discord.ui.Modal, title="Nightscout / Nocturne setup"):
    """
    Collects the site URL and credentials in one pop-up form. Credentials go
    through a form rather than slash-command parameters, since command
    parameters are visible to everyone in the channel when the command is
    invoked -- a secret typed as a visible parameter would leak to anyone
    watching.

    A blank credential field keeps the stored one, unless the URL changed:
    then blank fields clear it, so one site's credentials are never sent to
    another.
    """

    def __init__(self, store: ConfigStore, config: UserConfig):
        super().__init__()
        self.store = store
        self.config = config
        keep_hint = " - leave blank to keep the current one"
        self.url = discord.ui.TextInput(
            label="Site URL",
            placeholder="e.g. https://my-site.up.railway.app",
            default=config.nightscout_url,
            max_length=300,
        )
        self.api_secret = discord.ui.TextInput(
            label="Nightscout API secret (optional)",
            placeholder="Your API_SECRET, hashed before it's stored"
            + (keep_hint if config.api_secret_hash else ""),
            required=False,
            max_length=200,
        )
        self.api_token = discord.ui.TextInput(
            label="Access token or Nocturne key (optional)",
            placeholder="e.g. my-subject-a1b2c3d4 or noc_..." + (keep_hint if config.api_token else ""),
            required=False,
            max_length=200,
        )
        for item in (self.url, self.api_secret, self.api_token):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        url = self.url.value.strip().rstrip("/")
        if not (url.startswith("http://") or url.startswith("https://")):
            await interaction.response.send_message(
                "That doesn't look like a URL - it should start with `http://` or `https://`. "
                "Run `/ns setup` again to fix it.",
                ephemeral=True,
            )
            return

        secret_value = self.api_secret.value.strip()
        token_value = self.api_token.value.strip()
        same_site = url == (self.config.nightscout_url or "")
        config = await self.store.upsert(
            interaction.user.id,
            nightscout_url=url,
            api_secret_hash=(
                hashlib.sha1(secret_value.encode()).hexdigest() if secret_value
                else self.config.api_secret_hash if same_site else None
            ),
            api_token=token_value or (self.config.api_token if same_site else None),
        )

        # Saved -- now check it actually works, which can take a few seconds.
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(await _connection_report(config), ephemeral=True)


class Nightscout(commands.Cog):
    def __init__(self, bot: commands.Bot, store: ConfigStore):
        self.bot = bot
        self.store = store

    # Usable from a server install and from a personal ("My Apps") install, in
    # servers, DMs with the bot, and group DMs.
    group = app_commands.Group(
        name="ns",
        description="Configure and view your Nightscout data",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
        allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True),
    )

    # ---- configuration commands ------------------------------------------------------

    @group.command(name="setup", description="Set your Nightscout or Nocturne site URL and credentials")
    async def setup_site(self, interaction: discord.Interaction):
        config = await self.store.get_or_default(interaction.user.id)
        await interaction.response.send_modal(SetupModal(self.store, config))

    @group.command(name="units", description="Choose mg/dL or mmol/L for display")
    @app_commands.choices(units=UNIT_CHOICES)
    async def set_units(self, interaction: discord.Interaction, units: app_commands.Choice[str]):
        await self.store.upsert(interaction.user.id, units=units.value)
        await interaction.response.send_message(f"Display units set to {units.value}.", ephemeral=True)

    @group.command(name="thresholds", description="Set your very low / low / high / very high glucose thresholds")
    @app_commands.describe(
        low="Low threshold, in your configured display units",
        high="High threshold, in your configured display units",
        very_low="Very low threshold (default 55 mg/dL / 3.1 mmol/L)",
        very_high="Very high threshold (default 250 mg/dL / 13.9 mmol/L)",
    )
    async def set_thresholds(
        self,
        interaction: discord.Interaction,
        low: Optional[float] = None,
        high: Optional[float] = None,
        very_low: Optional[float] = None,
        very_high: Optional[float] = None,
    ):
        if low is None and high is None and very_low is None and very_high is None:
            await interaction.response.send_message(
                "Give at least one of `low`, `high`, `very_low` or `very_high`.", ephemeral=True
            )
            return

        config = await self.store.get_or_default(interaction.user.id)
        factor = MGDL_PER_MMOL if config.units == "mmol/L" else 1.0

        # Unset options keep their current value, so the ordering check below
        # runs against the full set as it will be saved.
        new = {
            "very_low_threshold": very_low * factor if very_low is not None else config.very_low_threshold,
            "low_threshold": low * factor if low is not None else config.low_threshold,
            "high_threshold": high * factor if high is not None else config.high_threshold,
            "very_high_threshold": very_high * factor if very_high is not None else config.very_high_threshold,
        }
        ordered = [new["very_low_threshold"], new["low_threshold"], new["high_threshold"], new["very_high_threshold"]]
        if ordered[0] <= 0 or any(a >= b for a, b in zip(ordered, ordered[1:])):
            await interaction.response.send_message(
                "Thresholds must be positive and in order: very low < low < high < very high.\n"
                f"That would give {_format_thresholds(new, config.units)}.",
                ephemeral=True,
            )
            return

        await self.store.upsert(interaction.user.id, **new)
        await interaction.response.send_message(
            f"Thresholds set: {_format_thresholds(new, config.units)}.", ephemeral=True
        )

    @group.command(name="default-hours", description="Set the default graph window when /ns graph is run without a value")
    @app_commands.describe(hours="Number of hours, 1-168")
    async def set_default_hours(self, interaction: discord.Interaction, hours: app_commands.Range[int, 1, 168]):
        await self.store.upsert(interaction.user.id, default_hours=hours)
        await interaction.response.send_message(f"Default graph window set to {hours}h.", ephemeral=True)

    @group.command(name="config", description="Show your current Nightscout bot configuration")
    async def show_config(self, interaction: discord.Interaction):
        config = await self.store.get_or_default(interaction.user.id)

        # Checking the server type is a network call, so defer first.
        await interaction.response.defer(ephemeral=True, thinking=True)
        if config.nightscout_url:
            server_type = await detect_server_type(config.nightscout_url)
            server = server_type or "unknown (couldn't reach it as a Nightscout or Nocturne site)"
        else:
            server = "*not set*"

        embed = discord.Embed(title="Your Nightscout Configuration", color=discord.Color.blurple())
        embed.add_field(name="Site URL", value=config.nightscout_url or "*not set*", inline=False)
        embed.add_field(name="Server", value=server, inline=False)
        embed.add_field(
            name="Credentials",
            value="set (hidden)" if config.has_credentials else "*not set*",
            inline=False,
        )
        embed.add_field(name="Units", value=config.units, inline=True)
        embed.add_field(
            name="Thresholds",
            value=_format_thresholds(vars(config), config.units).replace(", ", "\n"),
            inline=True,
        )
        embed.add_field(name="Default window", value=f"{config.default_hours}h", inline=True)
        embed.add_field(name="Timezone", value=config.timezone, inline=True)
        embed.add_field(name="Privacy", value=config.privacy_mode, inline=True)
        embed.add_field(name="Graph Y-axis", value=config.y_axis_mode, inline=True)
        embed.add_field(name="Theme", value=THEME_NAMES[config.theme], inline=True)
        embed.add_field(name="Colorblind mode", value="on" if config.colorblind_mode else "off", inline=True)
        embed.add_field(name="Boluses marked", value=_describe_min_bolus(config), inline=True)
        embed.add_field(name="Basal on graph", value="shown" if config.show_basal else "hidden", inline=True)
        embed.add_field(name="Graph colors", value=_describe_colors(config), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @group.command(name="clear", description="Delete all of your stored Nightscout configuration")
    async def clear_config(self, interaction: discord.Interaction):
        await self.store.delete(interaction.user.id)
        await interaction.response.send_message("Your Nightscout configuration has been deleted.", ephemeral=True)

    # ---- privacy & display -------------------------------------------------------------

    @group.command(name="privacy", description="Control whether others can check on your glucose with /ns check-on")
    @app_commands.choices(mode=PRIVACY_MODE_CHOICES)
    async def set_privacy(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        await self.store.upsert(interaction.user.id, privacy_mode=mode.value)
        if mode.value == "public":
            msg = "Privacy set to **public** - anyone who shares a server with you can check on you with `/ns check-on`."
        else:
            msg = "Privacy set to **private** - nobody can check on you with `/ns check-on`."
        await interaction.response.send_message(msg, ephemeral=True)

    @group.command(
        name="y-axis-scaling", description="Choose how the /ns graph chart scales its y-axis"
    )
    @app_commands.choices(mode=Y_AXIS_MODE_CHOICES)
    async def set_y_axis_mode(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        await self.store.upsert(interaction.user.id, y_axis_mode=mode.value)
        if mode.value == "fixed":
            msg = (
                f"Y-axis scaling set to **fixed** - always "
                f"{GRAPH_FIXED_Y_AXIS_MIN_MGDL:.0f}-{GRAPH_FIXED_Y_AXIS_MAX_MGDL:.0f} mg/dL."
            )
        else:
            msg = "Y-axis scaling set to **dynamic** - scales to fit whatever your readings span."
        await interaction.response.send_message(msg, ephemeral=True)

    @group.command(name="colors", description="Choose the colors your graphs use for each glucose range")
    @app_commands.describe(
        very_low="Very low readings: a preset (e.g. Dark red), a hex code like #a91d1d, or Default",
        low="Low readings: a preset (e.g. Red), a hex code, or Default",
        in_range="In-range readings: a preset (e.g. Green, Sky blue), a hex code, or Default",
        high="High readings: a preset (e.g. Yellow), a hex code, or Default",
        very_high="Very high readings: a preset (e.g. Orange), a hex code, or Default",
    )
    async def set_colors(
        self,
        interaction: discord.Interaction,
        very_low: Optional[str] = None,
        low: Optional[str] = None,
        in_range: Optional[str] = None,
        high: Optional[str] = None,
        very_high: Optional[str] = None,
    ):
        given = {
            name: value
            for name, value in (("very_low", very_low), ("low", low), ("in_range", in_range),
                                ("high", high), ("very_high", very_high))
            if value is not None
        }
        config = await self.store.get_or_default(interaction.user.id)
        if not given:
            await interaction.response.send_message(
                "Give a color for at least one range. Your current colors:\n" + _describe_colors(config),
                ephemeral=True,
            )
            return

        updates = {}
        for name, text in given.items():
            label = RANGE_LABELS[name]
            if text.strip().lower() == "default":
                # The default is stored as "no choice", so it follows any future change to it.
                updates[f"{name}_color"] = None
                continue
            hex_code = parse_color(text)
            if hex_code is None:
                await interaction.response.send_message(
                    f"I don't recognize `{discord.utils.escape_markdown(text)}` as a color for {label}. "
                    f"Pick one of {', '.join(COLOR_PRESETS)}, use a hex code like `#87ceeb`, or `Default`.",
                    ephemeral=True,
                )
                return
            if config.theme != "light" and color_luminance(hex_code) < MIN_COLOR_LUMINANCE:
                await interaction.response.send_message(
                    f"`{hex_code}` ({label}) is too dark to see against your graphs' dark background "
                    "- try a lighter color.",
                    ephemeral=True,
                )
                return
            updates[f"{name}_color"] = None if hex_code == getattr(DEFAULT_COLORS, name) else hex_code

        config = await self.store.upsert(interaction.user.id, **updates)
        await interaction.response.send_message(
            "Graph colors updated:\n" + _describe_colors(config), ephemeral=True
        )

    @group.command(name="colorblind", description="Use a colorblind-safe color palette on your graphs")
    @app_commands.describe(
        enabled="True to use the colorblind-safe palette, False to go back to your own colors (default)"
    )
    async def set_colorblind(self, interaction: discord.Interaction, enabled: bool):
        config = await self.store.upsert(interaction.user.id, colorblind_mode=enabled)
        if enabled:
            msg = (
                "Colorblind mode is **on**. Your graphs now use a colorblind-safe palette: lows in reds, "
                "highs in blues, in range in near-white.\n" + _describe_palette(COLORBLIND_COLORS)
                + "\nYour own `/ns colors` choices are kept and come back if you turn it off."
            )
        else:
            msg = "Colorblind mode is **off**. Your graphs use your own colors again:\n" + _describe_colors(config)
        await interaction.response.send_message(msg, ephemeral=True)

    @set_colors.autocomplete("very_low")
    @set_colors.autocomplete("low")
    @set_colors.autocomplete("in_range")
    @set_colors.autocomplete("high")
    @set_colors.autocomplete("very_high")
    async def color_autocomplete(self, interaction: discord.Interaction, current: str):
        query = current.strip().lower()
        choices = [app_commands.Choice(name="Default", value="Default")]
        choices += [
            app_commands.Choice(name=f"{name} ({hex_code})", value=name)
            for name, hex_code in COLOR_PRESETS.items()
        ]
        return [c for c in choices if query in c.name.lower()][:25]

    @group.command(name="theme", description="Choose the background and text theme your graphs use")
    @app_commands.describe(theme="The theme for your graphs (default Dark)")
    @app_commands.choices(theme=[
        app_commands.Choice(name=name, value=key) for key, name in THEME_NAMES.items()
    ])
    async def set_theme(self, interaction: discord.Interaction, theme: app_commands.Choice[str]):
        await self.store.upsert(interaction.user.id, theme=theme.value)
        await interaction.response.send_message(
            f"Your graphs now use the **{theme.name}** theme. Your range colors stay as they are.",
            ephemeral=True,
        )

    @group.command(name="min-bolus", description="Set the smallest bolus /ns graph marks, in units")
    @app_commands.describe(units="Only mark boluses of at least this many units (default 1; 0 marks every bolus)")
    async def set_min_bolus(self, interaction: discord.Interaction, units: app_commands.Range[float, 0.0, 25.0]):
        config = await self.store.upsert(interaction.user.id, bolus_min_units=round(units, 2))
        await interaction.response.send_message(
            f"`/ns graph` now marks {_describe_min_bolus(config)}.", ephemeral=True
        )

    @group.command(name="basal", description="Show or hide the basal rate on /ns graph")
    @app_commands.describe(show="True to draw your basal rate along the top of /ns graph (default), False to hide it")
    async def set_basal(self, interaction: discord.Interaction, show: bool):
        await self.store.upsert(interaction.user.id, show_basal=show)
        msg = ("`/ns graph` now draws your basal rate along the top of the chart." if show
               else "`/ns graph` no longer draws your basal rate.")
        await interaction.response.send_message(msg, ephemeral=True)

    # ---- data commands ---------------------------------------------------------------

    @group.command(name="graph", description="Generate a glucose trend graph")
    @app_commands.describe(
        hours="Hours of history to show, 1-24 (defaults to your configured default)",
        days="Days of history to show, 2-7 (use instead of hours)",
    )
    async def graph(
        self,
        interaction: discord.Interaction,
        hours: Optional[app_commands.Range[int, 1, 24]] = None,
        days: Optional[app_commands.Range[int, 2, 7]] = None,
    ):
        if hours is not None and days is not None:
            await interaction.response.send_message(
                "Pass either `hours` or `days`, not both.", ephemeral=True
            )
            return

        config = await self.store.get_or_default(interaction.user.id)
        if not config.is_configured:
            await interaction.response.send_message(
                "You haven't set a Nightscout URL yet. Run `/ns setup` first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        window_hours = days * 24 if days is not None else (hours or config.default_hours)

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        start_ms = now_ms - window_hours * 3600 * 1000

        try:
            entries, insulin = await asyncio.gather(
                fetch_sgv_entries(
                    config.nightscout_url,
                    count=MAX_ENTRIES_SAFETY_CAP,
                    start_ms=start_ms,
                    api_secret_hash=config.api_secret_hash,
                    api_token=config.api_token,
                ),
                _fetch_graph_insulin(config, start_ms, now_ms),
            )
            buf, stats = await asyncio.to_thread(
                _render_in_thread, entries, config, window_hours,
                days_mode=days is not None, title_name=interaction.user.display_name,
                insulin=insulin,
            )
        except NightscoutError as exc:
            await _send_error(interaction, f"Couldn't fetch your Nightscout data: {_safe_error(exc, config)}")
            return
        except NoDataError:
            await _send_error(interaction,
                f"No readings found in the last {window_hours}h. Is your Nightscout site reporting data?"
            )
            return

        file = discord.File(buf, filename="glucose.png")
        await interaction.followup.send(file=file)

    @group.command(name="agp", description="Ambulatory Glucose Profile: your typical day, by time of day")
    @app_commands.describe(days="Days of history to build the profile from, 1-30 (default 14)")
    async def agp(self, interaction: discord.Interaction, days: app_commands.Range[int, 1, 30] = 14):
        config = await self.store.get_or_default(interaction.user.id)
        if not config.is_configured:
            await interaction.response.send_message(
                "You haven't set a Nightscout URL yet. Run `/ns setup` first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        start_ms = now_ms - days * 24 * 3600 * 1000

        try:
            entries = await fetch_sgv_entries(
                config.nightscout_url,
                count=MAX_ENTRIES_SAFETY_CAP,
                start_ms=start_ms,
                api_secret_hash=config.api_secret_hash,
                api_token=config.api_token,
                timeout=30,
            )
            buf = await asyncio.to_thread(
                render_agp_graph,
                entries,
                units=config.units,
                low_mgdl=config.low_threshold,
                high_mgdl=config.high_threshold,
                days=days,
                tz_name=config.timezone,
                very_low_mgdl=config.very_low_threshold,
                very_high_mgdl=config.very_high_threshold,
                colors=_range_colors(config),
                theme=_theme(config),
            )
        except NightscoutError as exc:
            await _send_error(interaction, f"Couldn't fetch your Nightscout data: {_safe_error(exc, config)}")
            return
        except NoDataError:
            await _send_error(interaction,
                f"Not enough readings in the last {days}d to build a profile. "
                "Is your Nightscout site reporting data?"
            )
            return

        file = discord.File(buf, filename="agp.png")
        await interaction.followup.send(file=file)

    @group.command(name="status", description="Check your Nightscout data: last reading, uploader, pump and loop")
    async def status(self, interaction: discord.Interaction):
        config = await self.store.get_or_default(interaction.user.id)
        if not config.is_configured:
            await interaction.response.send_message(
                "You haven't set a Nightscout URL yet. Run `/ns setup` first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        auth = dict(api_secret_hash=config.api_secret_hash, api_token=config.api_token)
        entries_result, devicestatus_result = await asyncio.gather(
            fetch_sgv_entries(config.nightscout_url, count=1, **auth),
            fetch_recent_devicestatus(config.nightscout_url, **auth),
            return_exceptions=True,
        )
        if isinstance(entries_result, NightscoutError):
            await _send_error(interaction, f"Couldn't reach your Nightscout site: {_safe_error(entries_result, config)}")
            return
        if isinstance(entries_result, BaseException):
            raise entries_result
        if isinstance(devicestatus_result, BaseException):
            log.warning("Couldn't fetch devicestatus for user %s: %r", interaction.user.id, devicestatus_result)
            devicestatus_result = []

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        embed = discord.Embed(title=f"{interaction.user.display_name}'s Nightscout status")

        if entries_result:
            latest = entries_result[0]
            age_min = (now_ms - latest["date"]) / 60000
            embed.add_field(
                name="Last reading",
                value=f"**{format_value(float(latest['sgv']), config.units)} {config.units}** "
                f"{direction_to_arrow(latest.get('direction'))}\n{_discord_time(latest['date'])}",
                inline=True,
            )
            if age_min > STALE_READING_MINUTES:
                embed.description = f"⚠️ No new readings for {_format_age(now_ms - latest['date'])}."
            embed.color = (
                discord.Color.green() if age_min <= 10
                else discord.Color.orange() if age_min <= 20
                else discord.Color.red()
            )
        else:
            embed.description = "⚠️ No glucose readings found on your Nightscout site."
            embed.color = discord.Color.red()

        # Uploaders split data across documents, so each field comes from the
        # newest document that actually has it.
        def newest(extract):
            for doc in devicestatus_result:
                value = extract(doc)
                if value is not None:
                    return value, devicestatus_time_ms(doc)
            return None, None

        uploader_battery, _ = newest(extract_uploader_battery)
        pump_battery, _ = newest(extract_pump_battery)
        reservoir, _ = newest(extract_reservoir)
        suspended, _ = newest(extract_pump_suspended)
        last_loop_ms, _ = newest(extract_last_loop_ms)
        iob, _ = newest(extract_iob)

        if uploader_battery is not None:
            embed.add_field(name="Phone / uploader", value=f"{uploader_battery:.0f}% battery", inline=True)
        if pump_battery is not None:
            embed.add_field(name="Pump battery", value=pump_battery, inline=True)
        if reservoir is not None:
            embed.add_field(name="Reservoir", value=f"{reservoir:.0f} U", inline=True)
        if suspended:
            embed.add_field(name="Pump", value="⏸️ Suspended", inline=True)
        if last_loop_ms is not None:
            embed.add_field(name="Last loop", value=_discord_time(last_loop_ms), inline=True)
        if iob is not None:
            embed.add_field(name="IOB", value=f"{iob:.1f} U", inline=True)
        if len(embed.fields) <= 1:
            embed.set_footer(text="No uploader, pump or loop details are being reported to Nightscout.")

        await interaction.followup.send(embed=embed)

    @group.command(name="ages", description="Time since your last sensor, site, insulin and pump battery change")
    async def ages(self, interaction: discord.Interaction):
        config = await self.store.get_or_default(interaction.user.id)
        if not config.is_configured:
            await interaction.response.send_message(
                "You haven't set a Nightscout URL yet. Run `/ns setup` first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        auth = dict(api_secret_hash=config.api_secret_hash, api_token=config.api_token)
        event_types = [et for _, types in DEVICE_AGE_EVENTS for et in types]
        results = await asyncio.gather(
            *(fetch_latest_treatment(config.nightscout_url, et, **auth) for et in event_types),
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, BaseException)]
        if len(errors) == len(results):
            if isinstance(errors[0], NightscoutError):
                await _send_error(interaction, f"Couldn't fetch your Nightscout treatments: {_safe_error(errors[0], config)}")
                return
            raise errors[0]
        latest_ms = {
            et: treatment_time_ms(r)
            for et, r in zip(event_types, results)
            if isinstance(r, dict) and r.get("eventType") == et
        }

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        embed = discord.Embed(
            title=f"{interaction.user.display_name}'s device ages", color=discord.Color.blurple()
        )
        found = False
        for label, types in DEVICE_AGE_EVENTS:
            times = [latest_ms[et] for et in types if latest_ms.get(et)]
            if times:
                found = True
                changed = max(times)
                value = f"**{_format_age(now_ms - changed)}**\n{_discord_time(changed, 'f')}"
            elif label in HIDE_AGE_WHEN_MISSING:
                continue
            else:
                value = "*no record*"
            embed.add_field(name=label, value=value, inline=True)

        if not found:
            await _send_error(
                interaction,
                "No sensor, site, insulin or pump battery changes are recorded in your Nightscout. "
                "They're logged from Nightscout's Careportal or by uploaders that support them.",
            )
            return
        await interaction.followup.send(embed=embed)

    @group.command(name="tdd", description="Total daily insulin dose, split into basal and bolus")
    @app_commands.describe(days="Days to average over, 1-30 (default 1 = the last 24 hours)")
    async def tdd(self, interaction: discord.Interaction, days: app_commands.Range[int, 1, 30] = 1):
        config = await self.store.get_or_default(interaction.user.id)
        if not config.is_configured:
            await interaction.response.send_message(
                "You haven't set a Nightscout URL yet. Run `/ns setup` first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        start_ms = end_ms - days * 24 * 3600 * 1000
        auth = dict(api_secret_hash=config.api_secret_hash, api_token=config.api_token)

        # Treatments reach back an extra 12h so a temp basal that started
        # before the window still counts for the part inside it. The profile
        # (for the scheduled basal) is optional -- some uploaders send every
        # basal change as a temp basal and never need it.
        treatments_result, profile_result = await asyncio.gather(
            fetch_treatments(
                config.nightscout_url, start_ms=start_ms - 12 * 3600 * 1000, timeout=30, **auth
            ),
            fetch_profile(config.nightscout_url, **auth),
            return_exceptions=True,
        )
        if isinstance(treatments_result, NightscoutError):
            await _send_error(
                interaction, f"Couldn't fetch your Nightscout treatments: {_safe_error(treatments_result, config)}"
            )
            return
        if isinstance(treatments_result, BaseException):
            raise treatments_result
        if isinstance(profile_result, BaseException):
            log.warning("Couldn't fetch profile for user %s: %r", interaction.user.id, profile_result)
            profile_result = None

        # Without any treatments in the window, the scheduled basal alone
        # would "fill" the whole period and report a confident TDD for a site
        # that isn't uploading pump data at all.
        if not any(start_ms <= (treatment_time_ms(t) or 0) < end_ms for t in treatments_result):
            await _send_error(
                interaction,
                f"No insulin treatments found in the last {days}d. Is your pump or loop "
                "uploading treatments to Nightscout?",
            )
            return

        try:
            tz = ZoneInfo(config.timezone)
        except Exception:  # noqa: BLE001 - fall back rather than hard error
            tz = ZoneInfo("UTC")
        schedule = basal_schedule_from_profile(profile_result, tz)
        overall, per_day = compute_daily_doses(
            treatments_result, schedule=schedule, end_ms=end_ms, days=days
        )

        if overall.total_units <= 0:
            await _send_error(
                interaction,
                f"No insulin found in the last {days}d. Is your pump or loop uploading "
                "treatments to Nightscout?",
            )
            return

        daily = [
            (datetime.fromtimestamp(window_end / 1000, tz=tz), dose.basal_units, dose.bolus_units)
            for window_end, dose in per_day
        ]
        buf = await asyncio.to_thread(
            render_tdd_image, daily, basal_incomplete=overall.basal_incomplete,
            theme=_theme(config),
        )
        await interaction.followup.send(file=discord.File(buf, filename="total_daily_dose.png"))

    @group.command(name="gmi", description="Calculate your Glucose Management Indicator (estimated A1C)")
    @app_commands.describe(days="Days of history to calculate over, 1-90 (default 14)")
    async def gmi(self, interaction: discord.Interaction, days: app_commands.Range[int, 1, 90] = 14):
        config = await self.store.get_or_default(interaction.user.id)
        if not config.is_configured:
            await interaction.response.send_message(
                "You haven't set a Nightscout URL yet. Run `/ns setup` first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        start_ms = now_ms - days * 24 * 3600 * 1000

        try:
            entries = await fetch_sgv_entries(
                config.nightscout_url,
                count=MAX_ENTRIES_SAFETY_CAP,
                start_ms=start_ms,
                api_secret_hash=config.api_secret_hash,
                api_token=config.api_token,
                timeout=30,
            )
        except NightscoutError as exc:
            await _send_error(interaction, f"Couldn't fetch your Nightscout data: {_safe_error(exc, config)}")
            return

        entries = [e for e in entries if e["date"] >= start_ms]
        if not entries:
            await _send_error(interaction,
                f"No readings found in the last {days}d. Is your Nightscout site reporting data?"
            )
            return

        values = [float(e["sgv"]) for e in entries]
        average_mgdl = sum(values) / len(values)
        gmi_pct = compute_gmi(average_mgdl)
        # IFCC units, the A1C convention used outside the US.
        gmi_mmol_mol = 10.929 * (gmi_pct - 2.15)

        dates = [e["date"] for e in entries]
        span_days = (max(dates) - min(dates)) / (24 * 3600 * 1000)

        embed = discord.Embed(
            title=f"{interaction.user.display_name}'s GMI",
            description=f"Last {days} day{'s' if days != 1 else ''}",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="GMI", value=f"**{gmi_pct:.1f}%** ({gmi_mmol_mol:.0f} mmol/mol)", inline=True)
        embed.add_field(
            name="Average glucose",
            value=f"{format_value(average_mgdl, config.units)} {config.units}",
            inline=True,
        )
        embed.add_field(name="Readings", value=f"{len(values):,}", inline=True)
        # GMI is validated against lab A1C using 14+ days of CGM data;
        # shorter windows are still shown, just flagged.
        if span_days < 14:
            embed.set_footer(text=f"Based on {span_days:.1f} days of data - GMI is most reliable over 14+ days.")
        await interaction.followup.send(embed=embed)

    @group.command(name="averages", description="Graph your 7, 14 and 30-day rolling average glucose")
    @app_commands.describe(days="Days of history to show, 7-90 (default 90)")
    async def averages(self, interaction: discord.Interaction, days: app_commands.Range[int, 7, 90] = 90):
        config = await self.store.get_or_default(interaction.user.id)
        if not config.is_configured:
            await interaction.response.send_message(
                "You haven't set a Nightscout URL yet. Run `/ns setup` first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        # Reach back an extra longest-window's worth of days so every line is
        # a full average right from the left edge of the chart.
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        start_ms = now_ms - (days + max(ROLLING_AVERAGE_WINDOWS_DAYS)) * 24 * 3600 * 1000

        try:
            entries = await fetch_sgv_entries(
                config.nightscout_url,
                count=MAX_ENTRIES_SAFETY_CAP,
                start_ms=start_ms,
                api_secret_hash=config.api_secret_hash,
                api_token=config.api_token,
                timeout=60,
            )
            buf = await asyncio.to_thread(
                render_rolling_averages_graph,
                entries,
                units=config.units,
                low_mgdl=config.low_threshold,
                high_mgdl=config.high_threshold,
                days=days,
                tz_name=config.timezone,
                title_name=interaction.user.display_name,
                very_low_mgdl=config.very_low_threshold,
                very_high_mgdl=config.very_high_threshold,
                colors=_range_colors(config),
                theme=_theme(config),
            )
        except NightscoutError as exc:
            await _send_error(interaction, f"Couldn't fetch your Nightscout data: {_safe_error(exc, config)}")
            return
        except NoDataError:
            await _send_error(interaction,
                f"No readings found in the last {days}d. Is your Nightscout site reporting data?"
            )
            return

        file = discord.File(buf, filename="rolling_averages.png")
        await interaction.followup.send(file=file)

    @group.command(name="now", description="Show your most recent glucose reading")
    async def now(self, interaction: discord.Interaction):
        config = await self.store.get_or_default(interaction.user.id)
        if not config.is_configured:
            await interaction.response.send_message(
                "You haven't set a Nightscout URL yet. Run `/ns setup` first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        await self._send_glucose_embed(
            interaction, config, fallback_title=f"{interaction.user.display_name}'s BG", ephemeral=False
        )

    @group.command(
        name="check-on", description="Check another person's most recent glucose reading, if they've allowed it"
    )
    @app_commands.describe(user="The person you want to check on")
    async def check_on(self, interaction: discord.Interaction, user: discord.User):
        if user.id == interaction.user.id:
            await interaction.response.send_message("That's you! Just run `/ns now`.", ephemeral=True)
            return
        if user.bot:
            await interaction.response.send_message("That's a bot, not a person.", ephemeral=True)
            return

        target_config = await self.store.get_or_default(user.id)

        # Checked before the "are they even configured" check on purpose: an
        # unauthorized requester should see the exact same denial whether
        # `user` has never touched the bot, isn't configured, or is
        # configured but private -- otherwise the response itself would leak
        # which of those is true.
        if target_config.privacy_mode != "public":
            await interaction.response.send_message(
                f"{user.display_name} hasn't made their glucose checkable. "
                "They can allow it with `/ns privacy` (set to Public).",
                ephemeral=True,
            )
            return

        if not target_config.is_configured:
            await interaction.response.send_message(
                f"{user.display_name} hasn't set up a Nightscout URL yet.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        await self._send_glucose_embed(
            interaction, target_config, fallback_title=f"{user.display_name}'s BG", ephemeral=True
        )

    async def _send_glucose_embed(
        self, interaction: discord.Interaction, config: UserConfig, *, fallback_title: str, ephemeral: bool
    ) -> None:
        """
        Fetches the latest reading (+ optional site title / IOB) and posts
        the resulting embed as a followup. Shared by `/ns now` and `/ns check-on`
        so the two commands can't drift out of sync with each other.
        """
        # Entries are required; site status (for the custom title) and
        # devicestatus (for IOB) are nice-to-haves fetched in parallel, and
        # each degrades gracefully on its own if the site doesn't have them
        # or the request fails.
        entries_result, status_result, devicestatus_result = await asyncio.gather(
            fetch_sgv_entries(
                config.nightscout_url, count=2,
                api_secret_hash=config.api_secret_hash, api_token=config.api_token,
            ),
            fetch_status(
                config.nightscout_url,
                api_secret_hash=config.api_secret_hash, api_token=config.api_token,
            ),
            fetch_latest_devicestatus(
                config.nightscout_url,
                api_secret_hash=config.api_secret_hash, api_token=config.api_token,
            ),
            return_exceptions=True,
        )

        if isinstance(entries_result, BaseException):
            if isinstance(entries_result, NightscoutError):
                await _send_error(interaction,
                    f"Couldn't fetch that Nightscout data: {_safe_error(entries_result, config)}"
                )
                return
            raise entries_result

        entries = sorted(entries_result, key=lambda e: e["date"], reverse=True)
        if not entries:
            await _send_error(interaction, "No recent readings found.")
            return

        latest = entries[0]
        previous = entries[1] if len(entries) > 1 else None
        sgv_mgdl = float(latest["sgv"])
        delta_mgdl = sgv_mgdl - float(previous["sgv"]) if previous else 0.0

        custom_title = None
        if not isinstance(status_result, BaseException):
            custom_title = (status_result.get("settings") or {}).get("customTitle")
            # "Nightscout" is the out-of-the-box title on both Nightscout and
            # Nocturne sites, so it's not a real custom title -- use the
            # person's own "<name>'s BG" title instead.
            if isinstance(custom_title, str) and custom_title.strip().lower() == "nightscout":
                custom_title = None

        iob = None
        if not isinstance(devicestatus_result, BaseException):
            iob = extract_iob(devicestatus_result)

        if sgv_mgdl < config.low_threshold or sgv_mgdl > config.high_threshold:
            color = discord.Color.red() if sgv_mgdl > config.high_threshold else discord.Color.orange()
        else:
            color = discord.Color.green()

        embed = discord.Embed(
            title=custom_title or fallback_title,
            color=color,
            timestamp=datetime.fromtimestamp(latest["date"] / 1000, tz=timezone.utc),
        )
        embed.add_field(
            name="mmol/L",
            value=f"{format_value(sgv_mgdl, 'mmol/L')} ({format_delta(delta_mgdl, 'mmol/L')})",
            inline=True,
        )
        embed.add_field(
            name="mg/dL",
            value=f"{format_value(sgv_mgdl, 'mg/dL')} ({format_delta(delta_mgdl, 'mg/dL')})",
            inline=True,
        )
        embed.add_field(name="trend", value=direction_to_arrow(latest.get("direction")), inline=True)
        if iob is not None:
            embed.add_field(name="iob", value=f"{iob:.1f}", inline=True)
        # No footer text set -- Discord still renders the embed's timestamp
        # (set above) in the footer area on its own, just without a leading
        # "measured •" label in front of it.
        await interaction.followup.send(embed=embed, ephemeral=ephemeral)

    @group.command(name="tir", description="Calculate time in range over a number of days")
    @app_commands.describe(days="Number of days of history to calculate over (1-90, default 1)")
    async def time_in_range(self, interaction: discord.Interaction, days: app_commands.Range[int, 1, 90] = 1):
        config = await self.store.get_or_default(interaction.user.id)
        if not config.is_configured:
            await interaction.response.send_message(
                "You haven't set a Nightscout URL yet. Run `/ns setup` first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        window_hours = days * 24

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        start_ms = now_ms - window_hours * 3600 * 1000

        try:
            entries = await fetch_sgv_entries(
                config.nightscout_url,
                count=MAX_ENTRIES_SAFETY_CAP,
                start_ms=start_ms,
                api_secret_hash=config.api_secret_hash,
                api_token=config.api_token,
                timeout=30,
            )
        except NightscoutError as exc:
            await _send_error(interaction, f"Couldn't fetch your Nightscout data: {_safe_error(exc, config)}")
            return

        values = [float(e["sgv"]) for e in entries if e["date"] >= start_ms]
        if not values:
            await _send_error(interaction,
                f"No readings found in the last {days}d. Is your Nightscout site reporting data?"
            )
            return

        stats = compute_time_in_range_stats(
            values,
            config.low_threshold,
            config.high_threshold,
            very_low_mgdl=config.very_low_threshold,
            very_high_mgdl=config.very_high_threshold,
        )

        buf = await asyncio.to_thread(
            render_time_in_range_image, stats, colors=_range_colors(config),
            subtitle=format_time_span(days * 24), theme=_theme(config),
        )

        file = discord.File(buf, filename="time_in_range.png")
        await interaction.followup.send(file=file)

    # /bg: every /ns command again under a second name, built from /ns so the
    # two can never drift apart. Copies are unbound here and get bound to the
    # cog along with the group; `_attr` is cleared so binding them doesn't
    # overwrite the cog attribute that points at the /ns original.
    bg = app_commands.Group(
        name="bg",
        description="Configure and view your Nightscout data (same commands as /ns)",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
        allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True),
    )
    for _ns_command in group.commands:
        _bg_command = _ns_command._copy_with(parent=bg, binding=None, set_on_binding=False)
        _bg_command._attr = None
        bg.add_command(_bg_command)
    del _ns_command, _bg_command

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        # The raw error can carry anything -- including a Nightscout URL or
        # token that belongs to someone other than the person who'll see this
        # (e.g. /ns check-on) -- so users get a fixed message and the details
        # go to the bot's log for whoever runs it.
        log.error(
            "Unhandled error in /ns %s for user %s",
            interaction.command.qualified_name if interaction.command else "?",
            interaction.user.id,
            exc_info=error,
        )
        await _send_error(interaction, "Something went wrong running that command. Please try again later.")


# Temp basals are fetched from this far before the graph starts, so one
# that began earlier but was still running is drawn from the left edge.
BASAL_LOOKBACK_MS = 6 * 3600 * 1000


@dataclass
class GraphInsulin:
    """Insulin to draw on /ns graph (see `render_glucose_graph`)."""
    boluses: list[tuple[int, float]]
    basal: list[tuple[int, int, Optional[float], bool]]
    scheduled_basal: list[tuple[int, int, float]]


async def _fetch_graph_insulin(config: UserConfig, start_ms: int, end_ms: int) -> GraphInsulin:
    """
    Boluses (those of at least the user's minimum size) and, unless they've
    turned it off, the basal rate for /ns graph, from the
    site's treatments and profile. Either fetch failing (e.g. a key without
    treatment or profile access) just leaves that part off -- the graph
    itself still renders.
    """
    auth = dict(api_secret_hash=config.api_secret_hash, api_token=config.api_token)

    async def no_profile() -> None:
        return None

    # The profile only matters for basal, so it's skipped when basal is off.
    treatments, profile = await asyncio.gather(
        fetch_treatments(
            config.nightscout_url,
            start_ms=start_ms - (BASAL_LOOKBACK_MS if config.show_basal else 0),
            **auth,
        ),
        fetch_profile(config.nightscout_url, **auth) if config.show_basal else no_profile(),
        return_exceptions=True,
    )
    for result in (treatments, profile):
        if isinstance(result, BaseException) and not isinstance(result, NightscoutError):
            raise result
    if isinstance(treatments, NightscoutError):
        log.info("No treatments for /ns graph (user %s): %s", config.user_id, treatments)
        treatments = []
    if isinstance(profile, NightscoutError):
        log.info("No profile for /ns graph (user %s): %s", config.user_id, profile)
        profile = None

    boluses = []
    for treatment in treatments:
        units = bolus_units(treatment)
        ms = treatment_time_ms(treatment)
        if units > 0 and units >= config.bolus_min_units and ms is not None and ms >= start_ms:
            boluses.append((ms, units))

    if not config.show_basal:
        return GraphInsulin(boluses=boluses, basal=[], scheduled_basal=[])

    try:
        tz = ZoneInfo(config.timezone)
    except Exception:  # noqa: BLE001 - fall back rather than hard error
        tz = ZoneInfo("UTC")
    schedule = basal_schedule_from_profile(profile, tz)
    basal = basal_timeline(treatments, schedule=schedule, start_ms=start_ms, end_ms=end_ms)
    scheduled = schedule.rate_segments(start_ms, end_ms) if schedule is not None else []
    return GraphInsulin(boluses=boluses, basal=basal, scheduled_basal=scheduled)


def _render_in_thread(entries, config, window_hours, *, days_mode=False, title_name=None,
                      insulin: Optional[GraphInsulin] = None):
    y_axis_max_mgdl = GRAPH_FIXED_Y_AXIS_MAX_MGDL if config.y_axis_mode == "fixed" else None
    return render_glucose_graph(
        entries,
        units=config.units,
        low_mgdl=config.low_threshold,
        high_mgdl=config.high_threshold,
        hours=window_hours,
        tz_name=config.timezone,
        y_axis_max_mgdl=y_axis_max_mgdl,
        days_mode=days_mode,
        title_name=title_name,
        very_low_mgdl=config.very_low_threshold,
        very_high_mgdl=config.very_high_threshold,
        colors=_range_colors(config),
        theme=_theme(config),
        boluses=insulin.boluses if insulin else None,
        basal=insulin.basal if insulin else None,
        scheduled_basal=insulin.scheduled_basal if insulin else None,
    )


RANGE_LABELS = {
    "very_low": "very low",
    "low": "low",
    "in_range": "in range",
    "high": "high",
    "very_high": "very high",
}


async def _connection_report(config: UserConfig) -> str:
    """What /ns setup tells the user after saving: whether the site answers
    and accepts their credentials, and how old the latest reading is."""
    server = await detect_server_type(config.nightscout_url) or "Nightscout"
    saved = f"Saved <{config.nightscout_url}>"
    try:
        entries = await fetch_sgv_entries(
            config.nightscout_url, count=1,
            api_secret_hash=config.api_secret_hash, api_token=config.api_token,
        )
    except NightscoutError as exc:
        return f"{saved}, but couldn't read from it: {_safe_error(exc, config)}"
    if not entries:
        return f"{saved}. Connected to your {server} site, but it has no glucose readings yet."
    age_min = max(0, round((datetime.now(timezone.utc).timestamp() * 1000 - entries[0]["date"]) / 60000))
    age = "just now" if age_min < 1 else f"{age_min} min ago" if age_min < 120 else f"{age_min // 60}h ago"
    return f"{saved}. Connected to your {server} site - latest reading {age}. You're all set."


def _describe_min_bolus(config: UserConfig) -> str:
    """e.g. "boluses of 1U and up" or "every bolus"."""
    if config.bolus_min_units <= 0:
        return "every bolus"
    return f"boluses of {format_units(config.bolus_min_units)} and up"


def _range_colors(config: UserConfig) -> RangeColors:
    """The colors the user's graphs are drawn with: the colorblind-safe
    palette when /ns colorblind is on, otherwise their own /ns colors."""
    return COLORBLIND_COLORS if config.colorblind_mode else _custom_colors(config)


def _theme(config: UserConfig) -> Theme:
    """The user's /ns theme (dark if it's somehow one the bot no longer has)."""
    return THEMES.get(config.theme, THEMES["dark"])


def _custom_colors(config: UserConfig) -> RangeColors:
    """The user's /ns colors, with the default for any range they haven't set."""
    return RangeColors(**{
        name: getattr(config, f"{name}_color") or getattr(DEFAULT_COLORS, name)
        for name in RANGE_LABELS
    })


def _describe_color(hex_code: str) -> str:
    """e.g. "Sky blue (#87ceeb)", or just the hex code for a custom color."""
    for name, preset in COLOR_PRESETS.items():
        if preset == hex_code:
            return f"{name} ({hex_code})"
    return hex_code


def _describe_colors(config: UserConfig) -> str:
    """The user's /ns colors, one line per range, high to low, e.g.
    "Very high: Orange (#e6791e)", marking the ones still on their default
    -- headed by a note when colorblind mode is overriding them."""
    colors = _custom_colors(config)
    lines = []
    if config.colorblind_mode:
        lines.append("Colorblind mode is on, so your graphs use the colorblind-safe palette for now. "
                     "These are your own colors, used when it's off:")
    for name in ("very_high", "high", "in_range", "low", "very_low"):
        is_default = getattr(config, f"{name}_color") is None
        lines.append(
            f"{RANGE_LABELS[name].capitalize()}: {_describe_color(getattr(colors, name))}"
            + (" - default" if is_default else "")
        )
    return "\n".join(lines)


def _describe_palette(colors: RangeColors) -> str:
    """One line per range, high to low, e.g. "Very high: #d9483b"."""
    return "\n".join(
        f"{RANGE_LABELS[name].capitalize()}: {getattr(colors, name)}"
        for name in ("very_high", "high", "in_range", "low", "very_low")
    )


async def setup(bot: commands.Bot):
    store: ConfigStore = bot.config_store  # attached in bot.py
    await bot.add_cog(Nightscout(bot, store))
