"""SQLite persistence for warnings, detentions, and sticky mutes."""

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS detentions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    moderator_id INTEGER,
    reason TEXT,
    detained_at TEXT NOT NULL,
    released_at TEXT,
    stripped_roles TEXT
);
CREATE TABLE IF NOT EXISTS sticky_mutes (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = path

    async def setup(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SCHEMA)
            # migrate older DBs that predate the stripped_roles column
            cur = await db.execute("PRAGMA table_info(detentions)")
            cols = {row[1] for row in await cur.fetchall()}
            if "stripped_roles" not in cols:
                await db.execute("ALTER TABLE detentions ADD COLUMN stripped_roles TEXT")
            await db.commit()

    # -- warnings ----------------------------------------------------------
    async def add_warning(self, guild_id: int, user_id: int, moderator_id: int, reason: str | None) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "INSERT INTO warnings (guild_id, user_id, moderator_id, reason, created_at) VALUES (?,?,?,?,?)",
                (guild_id, user_id, moderator_id, reason, utcnow()),
            )
            await db.commit()
            return cur.lastrowid

    async def warnings_for(self, guild_id: int, user_id: int) -> list[tuple]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT id, moderator_id, reason, created_at FROM warnings WHERE guild_id=? AND user_id=? ORDER BY id",
                (guild_id, user_id),
            )
            return await cur.fetchall()

    async def clear_warnings(self, guild_id: int, user_id: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "DELETE FROM warnings WHERE guild_id=? AND user_id=?", (guild_id, user_id)
            )
            await db.commit()
            return cur.rowcount

    # -- detentions --------------------------------------------------------
    async def open_detention(self, guild_id: int, user_id: int, moderator_id: int | None,
                             reason: str | None, stripped_roles: list[int] | None = None) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO detentions (guild_id, user_id, moderator_id, reason, detained_at, stripped_roles) "
                "VALUES (?,?,?,?,?,?)",
                (guild_id, user_id, moderator_id, reason, utcnow(),
                 ",".join(str(r) for r in (stripped_roles or [])) or None),
            )
            await db.commit()

    async def close_detention(self, guild_id: int, user_id: int) -> list[int]:
        """Close the open detention and return the roles that were stripped."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT stripped_roles FROM detentions WHERE guild_id=? AND user_id=? AND released_at IS NULL "
                "ORDER BY id DESC LIMIT 1", (guild_id, user_id))
            row = await cur.fetchone()
            await db.execute(
                "UPDATE detentions SET released_at=? WHERE guild_id=? AND user_id=? AND released_at IS NULL",
                (utcnow(), guild_id, user_id),
            )
            await db.commit()
        if row and row[0]:
            return [int(r) for r in row[0].split(",") if r]
        return []

    async def is_detained(self, guild_id: int, user_id: int) -> bool:
        """True if the member has an open (unreleased) detention — used to
        re-apply detain on rejoin so leaving/returning can't dodge it."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT 1 FROM detentions WHERE guild_id=? AND user_id=? AND released_at IS NULL LIMIT 1",
                (guild_id, user_id))
            return (await cur.fetchone()) is not None

    async def detention_history(self, guild_id: int, user_id: int) -> list[tuple]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT moderator_id, reason, detained_at, released_at FROM detentions WHERE guild_id=? AND user_id=? ORDER BY id",
                (guild_id, user_id),
            )
            return await cur.fetchall()

    # -- sticky mutes ------------------------------------------------------
    async def set_sticky_mute(self, guild_id: int, user_id: int, sticky: bool) -> None:
        async with aiosqlite.connect(self.path) as db:
            if sticky:
                await db.execute(
                    "INSERT OR IGNORE INTO sticky_mutes (guild_id, user_id) VALUES (?,?)",
                    (guild_id, user_id),
                )
            else:
                await db.execute(
                    "DELETE FROM sticky_mutes WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id),
                )
            await db.commit()

    async def is_sticky_muted(self, guild_id: int, user_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT 1 FROM sticky_mutes WHERE guild_id=? AND user_id=?",
                (guild_id, user_id),
            )
            return await cur.fetchone() is not None
