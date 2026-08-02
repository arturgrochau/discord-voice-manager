"""P&P Sentinel — voice management & moderation bot for the Politics & Philosophy server.

Modernized rewrite of discord_voice_manager.py using discord.py 2.x:
slash commands, cogs, SQLite persistence, and structured logging.
"""

import json
import logging
import logging.handlers
import os
import sys
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"

load_dotenv(BASE_DIR / ".env")
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

log = logging.getLogger("sentinel")


def setup_logging() -> None:
    logs_dir = BASE_DIR / "logs"
    logs_dir.mkdir(exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        logs_dir / "sentinel.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    stream = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    handler.setFormatter(fmt)
    stream.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(stream)
    logging.getLogger("discord").setLevel(logging.WARNING)


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        log.critical("config.json not found. Copy config_template.json and fill it in.")
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


class Sentinel(commands.Bot):
    def __init__(self, config: dict):
        intents = discord.Intents.default()
        intents.members = True
        intents.voice_states = True
        intents.message_content = True  # required for "." prefix commands
        super().__init__(
            command_prefix=commands.when_mentioned_or("."),
            intents=intents,
            case_insensitive=True,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False),
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="over the server"
            ),
        )
        self.config = config
        self.guild_id: int = int(config.get("GUILD_ID", 0))

    async def setup_hook(self) -> None:
        from db import Database

        self.db = Database(BASE_DIR / "sentinel.db")
        await self.db.setup()

        for ext in ("cogs.voice", "cogs.moderation", "cogs.info"):
            await self.load_extension(ext)
            log.info("Loaded extension %s", ext)

        # Sync slash commands to the configured guild for instant availability.
        if self.guild_id:
            guild = discord.Object(id=self.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d app commands to guild %s", len(synced), self.guild_id)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id)

    def channel_id(self, key: str) -> int:
        try:
            return int(self.config.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0


def main() -> None:
    setup_logging()
    if not TOKEN:
        log.critical("DISCORD_BOT_TOKEN missing from .env")
        sys.exit(1)
    config = load_config()
    bot = Sentinel(config)
    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
