# Nightscout Discord Bot

A Discord bot that pulls glucose data from [Nightscout](https://ns.github.io/)
and generates a trend graph + summary stats image on demand, via slash
commands. Configuration (site URL, credentials, units, thresholds,
default graph window) is stored **per Discord user**, so one bot can serve
many people, each pointed at their own Nightscout site.

## Features

Every `/ns` command is also available as `/bg` (e.g. `/bg graph`, `/bg now`); the two are identical.

- `/ns setup` - set your Nightscout or Nocturne site URL and credentials (API secret, access token, or Nocturne `noc_` key) in a pop-up form, never as a visible command parameter; it checks the connection when you save
- `/ns units` - mg/dL or mmol/L
- `/ns thresholds [low] [high] [very_low] [very_high]` - your glucose thresholds (defaults 55 / 70 / 180 / 250 mg/dL); set any subset
- `/ns default-hours` - default graph window
- `/ns config` - view your current settings (private reply)
- `/ns clear` - delete your stored configuration
- `/ns graph [hours 1-24 | days 2-7]` - post a glucose trend graph with a stats panel (average, std dev, min/max, time-in-range, estimated A1C)
- `/ns agp [days]` - Ambulatory Glucose Profile: median and 25-75% / 5-95% bands by time of day (1-30 days, default 14)
- `/ns status` - last reading and how old it is (flags stale data), plus phone/uploader battery, pump battery, reservoir, last loop and IOB where your uploader reports them
- `/ns ages` - time since your last sensor, site, insulin and pump battery change
- `/ns tdd [days]` - total daily insulin dose graphic with basal / bolus split; last 24h by default, or a daily average over 1-30 days with a per-day breakdown
- `/ns gmi [days]` - Glucose Management Indicator (estimated A1C) as % and mmol/mol, with average glucose (1-90 days, default 14)
- `/ns averages [days]` - graph of your 7, 14 and 30-day rolling average glucose (7-90 days, default 90)
- `/ns now` - post your latest reading
- `/ns tir [days]` - time-in-range bar across the five ranges (very low to very high) with percentages (1-90 days, default 1)
- `/ns check-on <user>` - view another member's latest reading (only if they've set `/ns privacy` to Public)
- `/ns privacy` - allow or block others from using `/ns check-on` on you
- `/ns y-axis-scaling` - dynamic or fixed graph y-axis
- `/ns basal <show>` - True (the default) draws your basal rate along the top of `/ns graph`, Nightscout-style, with your scheduled basal as a dashed line; False hides it
- `/ns min-bolus <units>` - the smallest bolus `/ns graph` marks (default 1 U; 0 marks every bolus, including small automatic ones). Boluses are always marked, with their size
- `/ns colors [very_low] [low] [in_range] [high] [very_high]` - the colors your graphs use for each glucose range: a preset (Green, Sky blue, Blue, Teal, Mint, Lime, Yellow, Orange, Red, Dark red, Purple, Pink, White), any hex code like `#87ceeb`, or `Default`; set any subset
- `/ns colorblind <enabled>` - True switches your graphs to a colorblind-safe palette (reds for lows, blues for highs, near-white in range) that stays distinct with red-green and blue-yellow color blindness; False (the default) goes back to your `/ns colors`
- `/ns theme <theme>` - the background and text theme for your graphs: Dark (the default), Light, OLED black, Midnight blue or Discord. Your range colors stay the same in every theme
- `/activity <type> [text] [status]` - (bot owner only) set the bot's activity status (Playing / Watching / Listening to / Competing in / custom text, or clear it) and online / idle / DND / invisible; saved across restarts

## Setup

### 1. Create the Discord application

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and create a new application.
2. Under **Bot**, add a bot and copy its token.
3. No privileged intents are required (the bot only uses slash commands and modals).
4. Under **OAuth2 -> URL Generator**, select the `bot` and `applications.commands` scopes, and at least the `Send Messages`, `Embed Links`, and `Attach Files` bot permissions. Use the generated URL to invite the bot to your server.

### 2. Install and configure

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and set DISCORD_BOT_TOKEN (and optionally GUILD_ID for instant
# command sync while developing)
```

### 3. Run it

```bash
python bot.py
```

On first run it creates a local SQLite file (`nightscout_bot.db` by default)
to hold per-user configuration.

## Usage

Each user configures their own Nightscout site independently:

```
/ns setup          -> opens a form for your site URL and API secret / access token
/ns units mg/dL
/ns thresholds low: 70 high: 180
/ns thresholds very_low: 54 very_high: 250
/ns graph hours: 12
/ns graph days: 3
```

## Notes on credentials

- Nightscout's legacy `API_SECRET` is SHA1-hashed before it's stored or sent, matching what Nightscout itself expects in the `api-secret` header - the plaintext secret is never persisted.
- Newer role-based **access tokens** are sent as-is (that's how Nightscout's token auth works), so they are stored in the SQLite database in plaintext. If your Nightscout instance supports role-based tokens, create one scoped to `readable` only (not `admin`) for use with this bot, and restrict filesystem permissions on the database file.
- Credentials are only ever collected through the `/ns setup` pop-up form, not as a typed command parameter, since Discord shows other members in the channel what parameters a slash command was invoked with.

## Project layout

```
bot.py                 - entry point, bot setup, command sync
db.py                  - per-user config storage (SQLite via aiosqlite)
nightscout_client.py   - Nightscout REST API client
graphing.py            - matplotlib chart + stats rendering
cogs/ns.py     - the /ns slash command group
cogs/admin.py          - owner-only commands (/activity)
```

## Extending

- To add more chart types (e.g. a daily overlay / "modal day" view), add a new render function in `graphing.py` and a new subcommand in `cogs/ns.py`.
- To support multiple saved profiles per user (e.g. tracking a child's Nightscout separately from their own), key `user_config` on `(user_id, profile_name)` instead of `user_id` alone.
