"""One-shot server audit: dump channels, categories, roles, and key IDs as JSON.

Usage: .venv/bin/python scripts/audit_server.py > audit.json
Reads DISCORD_BOT_TOKEN from .env and GUILD_ID from config.json (or env).
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv

BASE_DIR = Path(os.environ.get("SENTINEL_HOME", Path(__file__).resolve().parent.parent))
load_dotenv(BASE_DIR / ".env")
TOKEN = os.environ["DISCORD_BOT_TOKEN"]

try:
    with open(BASE_DIR / "config.json", encoding="utf-8") as f:
        GUILD_ID = int(json.load(f)["GUILD_ID"])
except (FileNotFoundError, KeyError):
    GUILD_ID = int(os.environ["GUILD_ID"])


async def main() -> None:
    intents = discord.Intents.default()
    intents.members = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            guild = client.get_guild(GUILD_ID) or await client.fetch_guild(GUILD_ID)
            channels = await guild.fetch_channels()
            roles = await guild.fetch_roles()
            me = guild.me

            def chan(c):
                return {
                    "id": c.id,
                    "name": c.name,
                    "type": str(c.type),
                    "category_id": getattr(c, "category_id", None),
                    "position": c.position,
                }

            out = {
                "guild": {"id": guild.id, "name": guild.name, "owner_id": guild.owner_id},
                "me": {
                    "id": me.id,
                    "top_role": me.top_role.name,
                    "top_role_position": me.top_role.position,
                    "guild_permissions": [p for p, v in me.guild_permissions if v],
                },
                "roles": [
                    {
                        "id": r.id,
                        "name": r.name,
                        "position": r.position,
                        "permissions": r.permissions.value,
                        "member_count": len(r.members),
                        "managed": r.managed,
                    }
                    for r in sorted(roles, key=lambda r: -r.position)
                ],
                "categories": [chan(c) for c in channels if isinstance(c, discord.CategoryChannel)],
                "channels": [chan(c) for c in channels if not isinstance(c, discord.CategoryChannel)],
            }
            print(json.dumps(out, indent=2, default=str))
        finally:
            await client.close()

    await client.start(TOKEN)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
