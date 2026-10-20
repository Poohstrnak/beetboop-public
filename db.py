"""
Per-user configuration storage, backed by SQLite (via aiosqlite).

Each Discord user gets exactly one row keyed by their Discord user ID. Values
that are never provided keep sane defaults (standard 70-180 mg/dL range,
very low / very high at 55 / 250 mg/dL, a 6 hour graph window, UTC).

Glucose thresholds are always stored internally in mg/dL regardless of the
user's preferred display unit, so switching units never requires a
migration of existing threshold values.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import aiosqlite

SCHEMA_USER_CONFIG = """
CREATE TABLE IF NOT EXISTS user_config (
    user_id           INTEGER PRIMARY KEY,
    nightscout_url    TEXT,
    api_secret_hash   TEXT,
    api_token         TEXT,
    units             TEXT NOT NULL DEFAULT 'mg/dL',
    low_threshold     REAL NOT NULL DEFAULT 70,
    high_threshold    REAL NOT NULL DEFAULT 180,
    very_low_threshold  REAL NOT NULL DEFAULT 55,
    very_high_threshold REAL NOT NULL DEFAULT 250,
    default_hours     INTEGER NOT NULL DEFAULT 6,
    timezone          TEXT NOT NULL DEFAULT 'UTC',
    privacy_mode      TEXT NOT NULL DEFAULT 'private',
    y_axis_mode       TEXT NOT NULL DEFAULT 'dynamic',
    in_range_color    TEXT,
    very_low_color    TEXT,
    low_color         TEXT,
    high_color        TEXT,
    very_high_color   TEXT,
    colorblind_mode   INTEGER NOT NULL DEFAULT 0,
    bolus_min_units   REAL NOT NULL DEFAULT 1,
    show_basal        INTEGER NOT NULL DEFAULT 1,
    theme             TEXT NOT NULL DEFAULT 'dark',
    created_at        REAL,
    updated_at        REAL
);
"""

# Bot-wide settings (not tied to any one user), e.g. the presence set with
# /activity, so they survive a restart.
SCHEMA_BOT_SETTINGS = """
CREATE TABLE IF NOT EXISTS bot_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass
class UserConfig:
    user_id: int
    nightscout_url: Optional[str] = None
    api_secret_hash: Optional[str] = None
    api_token: Optional[str] = None
    units: str = "mg/dL"
    low_threshold: float = 70.0
    high_threshold: float = 180.0
    very_low_threshold: float = 55.0
    very_high_threshold: float = 250.0
    default_hours: int = 6
    timezone: str = "UTC"
    privacy_mode: str = "private"
    y_axis_mode: str = "dynamic"
    # Graph colors per range, each "#rrggbb" or None for the default.
    in_range_color: Optional[str] = None
    very_low_color: Optional[str] = None
    low_color: Optional[str] = None
    high_color: Optional[str] = None
    very_high_color: Optional[str] = None
    # Graphs use a colorblind-safe palette instead of the colors above.
    colorblind_mode: bool = False
    # /ns graph marks boluses of at least this many units.
    bolus_min_units: float = 1.0
    # /ns graph draws the basal rate hanging from the top of the chart.
    show_basal: bool = True
    # The graphs' background/text theme: a key of graphing.THEMES.
    theme: str = "dark"

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_secret_hash or self.api_token)

    @property
    def is_configured(self) -> bool:
        return bool(self.nightscout_url)


class ConfigStore:
    """Thin async wrapper around a single SQLite file."""

    def __init__(self, path: str):
        self.path = path
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute(SCHEMA_USER_CONFIG)
        await self._db.execute(SCHEMA_BOT_SETTINGS)
        await self._migrate()
        await self._db.commit()

    async def _migrate(self) -> None:
        """
        Adds columns introduced after the initial schema, without touching
        existing rows. `CREATE TABLE IF NOT EXISTS` above is a no-op against
        a table that already exists, so a new column on an existing table
        needs an explicit `ALTER TABLE` -- otherwise anyone upgrading from an
        older DB file would hit a "no such column" error instead of just
        quietly getting the new default.
        """
        assert self._db is not None
        async with self._db.execute("PRAGMA table_info(user_config)") as cur:
            existing_columns = {row["name"] async for row in cur}
        if "privacy_mode" not in existing_columns:
            await self._db.execute(
                "ALTER TABLE user_config ADD COLUMN privacy_mode TEXT NOT NULL DEFAULT 'private'"
            )
        if "y_axis_mode" not in existing_columns:
            await self._db.execute(
                "ALTER TABLE user_config ADD COLUMN y_axis_mode TEXT NOT NULL DEFAULT 'dynamic'"
            )
        if "very_low_threshold" not in existing_columns:
            await self._db.execute(
                "ALTER TABLE user_config ADD COLUMN very_low_threshold REAL NOT NULL DEFAULT 55"
            )
        if "very_high_threshold" not in existing_columns:
            await self._db.execute(
                "ALTER TABLE user_config ADD COLUMN very_high_threshold REAL NOT NULL DEFAULT 250"
            )
        for column in ("in_range_color", "very_low_color", "low_color", "high_color", "very_high_color"):
            if column not in existing_columns:
                await self._db.execute(f"ALTER TABLE user_config ADD COLUMN {column} TEXT")
        if "colorblind_mode" not in existing_columns:
            await self._db.execute(
                "ALTER TABLE user_config ADD COLUMN colorblind_mode INTEGER NOT NULL DEFAULT 0"
            )
        if "bolus_min_units" not in existing_columns:
            await self._db.execute(
                "ALTER TABLE user_config ADD COLUMN bolus_min_units REAL NOT NULL DEFAULT 1"
            )
        if "show_basal" not in existing_columns:
            await self._db.execute(
                "ALTER TABLE user_config ADD COLUMN show_basal INTEGER NOT NULL DEFAULT 1"
            )
        if "theme" not in existing_columns:
            await self._db.execute(
                "ALTER TABLE user_config ADD COLUMN theme TEXT NOT NULL DEFAULT 'dark'"
            )

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @staticmethod
    def _row_to_config(row: aiosqlite.Row) -> UserConfig:
        return UserConfig(
            user_id=row["user_id"],
            nightscout_url=row["nightscout_url"],
            api_secret_hash=row["api_secret_hash"],
            api_token=row["api_token"],
            units=row["units"],
            low_threshold=row["low_threshold"],
            high_threshold=row["high_threshold"],
            very_low_threshold=row["very_low_threshold"],
            very_high_threshold=row["very_high_threshold"],
            default_hours=row["default_hours"],
            timezone=row["timezone"],
            privacy_mode=row["privacy_mode"],
            y_axis_mode=row["y_axis_mode"],
            in_range_color=row["in_range_color"],
            very_low_color=row["very_low_color"],
            low_color=row["low_color"],
            high_color=row["high_color"],
            very_high_color=row["very_high_color"],
            colorblind_mode=bool(row["colorblind_mode"]),
            bolus_min_units=row["bolus_min_units"],
            show_basal=bool(row["show_basal"]),
            theme=row["theme"],
        )

    async def get(self, user_id: int) -> Optional[UserConfig]:
        assert self._db is not None, "ConfigStore.connect() was never called"
        async with self._db.execute(
            "SELECT * FROM user_config WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return self._row_to_config(row) if row else None

    async def get_or_default(self, user_id: int) -> UserConfig:
        return await self.get(user_id) or UserConfig(user_id=user_id)

    async def upsert(self, user_id: int, **fields) -> UserConfig:
        """
        Merge `fields` onto the user's existing config (or defaults, if
        they have none yet) and persist the result.
        """
        assert self._db is not None, "ConfigStore.connect() was never called"

        current = await self.get_or_default(user_id)
        for key, value in fields.items():
            if not hasattr(current, key):
                raise ValueError(f"Unknown config field: {key!r}")
            setattr(current, key, value)

        now = time.time()
        await self._db.execute(
            """
            INSERT INTO user_config (
                user_id, nightscout_url, api_secret_hash, api_token,
                units, low_threshold, high_threshold, very_low_threshold,
                very_high_threshold, default_hours, timezone, privacy_mode,
                y_axis_mode, in_range_color, very_low_color, low_color,
                high_color, very_high_color, colorblind_mode, bolus_min_units,
                show_basal, theme, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                nightscout_url  = excluded.nightscout_url,
                api_secret_hash = excluded.api_secret_hash,
                api_token       = excluded.api_token,
                units           = excluded.units,
                low_threshold   = excluded.low_threshold,
                high_threshold  = excluded.high_threshold,
                very_low_threshold  = excluded.very_low_threshold,
                very_high_threshold = excluded.very_high_threshold,
                default_hours   = excluded.default_hours,
                timezone        = excluded.timezone,
                privacy_mode    = excluded.privacy_mode,
                y_axis_mode     = excluded.y_axis_mode,
                in_range_color  = excluded.in_range_color,
                very_low_color  = excluded.very_low_color,
                low_color       = excluded.low_color,
                high_color      = excluded.high_color,
                very_high_color = excluded.very_high_color,
                colorblind_mode = excluded.colorblind_mode,
                bolus_min_units = excluded.bolus_min_units,
                show_basal      = excluded.show_basal,
                theme           = excluded.theme,
                updated_at      = excluded.updated_at
            """,
            (
                current.user_id,
                current.nightscout_url,
                current.api_secret_hash,
                current.api_token,
                current.units,
                current.low_threshold,
                current.high_threshold,
                current.very_low_threshold,
                current.very_high_threshold,
                current.default_hours,
                current.timezone,
                current.privacy_mode,
                current.y_axis_mode,
                current.in_range_color,
                current.very_low_color,
                current.low_color,
                current.high_color,
                current.very_high_color,
                int(current.colorblind_mode),
                current.bolus_min_units,
                int(current.show_basal),
                current.theme,
                now,
                now,
            ),
        )
        await self._db.commit()
        return current

    async def delete(self, user_id: int) -> None:
        assert self._db is not None, "ConfigStore.connect() was never called"
        await self._db.execute("DELETE FROM user_config WHERE user_id = ?", (user_id,))
        await self._db.commit()

    async def get_setting(self, key: str) -> Optional[str]:
        assert self._db is not None, "ConfigStore.connect() was never called"
        async with self._db.execute(
            "SELECT value FROM bot_settings WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
        return row["value"] if row else None

    async def set_setting(self, key: str, value: Optional[str]) -> None:
        """Stores `value` under `key`; `None` removes the setting."""
        assert self._db is not None, "ConfigStore.connect() was never called"
        if value is None:
            await self._db.execute("DELETE FROM bot_settings WHERE key = ?", (key,))
        else:
            await self._db.execute(
                "INSERT INTO bot_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        await self._db.commit()
