"""Idempotent server setup: ensure the Detained role, log channels, and
permission overwrites exist on the Politics & Philosophy server, then write
their IDs back into config.json.

- Reuses anything that already exists (matched by name, case-insensitive).
- Never deletes anything.
- Detained role: denied Send Messages/Speak/Connect everywhere except the
  detainment channel, where they can talk to staff.

Usage: .venv/bin/python scripts/setup_server.py
"""

import asyncio
import json
import os
from pathlib import Path

import discord
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.json"

load_dotenv(BASE_DIR / ".env")
TOKEN = os.environ["DISCORD_BOT_TOKEN"]

with open(CONFIG_PATH, encoding="utf-8") as f:
    config = json.load(f)
GUILD_ID = int(config["GUILD_ID"])

DETAIN_ROLE_NAME = "Detained"
MODERATION_CATEGORY = "moderation"


def find_role(guild: discord.Guild, name: str) -> discord.Role | None:
    return next((r for r in guild.roles if r.name.lower() == name.lower()), None)


def find_channel(guild: discord.Guild, name: str, type_=discord.TextChannel):
    return next(
        (c for c in guild.channels if isinstance(c, type_) and c.name.lower().lstrip("#| ").endswith(name.lower())),
        None,
    )


def find_category(guild: discord.Guild, name: str) -> discord.CategoryChannel | None:
    return next(
        (c for c in guild.categories if name.lower() in c.name.lower()),
        None,
    )


async def run(client: discord.Client) -> None:
    guild = client.get_guild(GUILD_ID) or await client.fetch_guild(GUILD_ID)
    await guild.chunk()
    me = guild.me
    print(f"Connected as {me} — top role: {me.top_role.name} (pos {me.top_role.position})")
    print(f"Permissions: admin={me.guild_permissions.administrator}, "
          f"manage_roles={me.guild_permissions.manage_roles}, "
          f"manage_channels={me.guild_permissions.manage_channels}")

    if not (me.guild_permissions.administrator or (
        me.guild_permissions.manage_roles and me.guild_permissions.manage_channels
    )):
        raise SystemExit("Bot lacks manage_roles+manage_channels — elevate its role first.")

    # -- Detained role -----------------------------------------------------
    detain_role = find_role(guild, DETAIN_ROLE_NAME)
    if detain_role is None:
        detain_role = await guild.create_role(
            name=DETAIN_ROLE_NAME,
            color=discord.Color.dark_red(),
            hoist=True,
            mentionable=False,
            reason="Sentinel setup: detainment role",
        )
        print(f"Created role {detain_role.name} ({detain_role.id})")
    else:
        print(f"Reusing role {detain_role.name} ({detain_role.id})")

    # -- moderation category + channels -----------------------------------
    category = find_category(guild, MODERATION_CATEGORY)
    if category is None:
        category = await guild.create_category(
            "🔷moderation🔷", reason="Sentinel setup"
        )
        print(f"Created category {category.name}")
    else:
        print(f"Reusing category {category.name} ({category.id})")

    staff_only = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        me.top_role: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }

    def ensure(name: str, existing: discord.TextChannel | None, **kwargs):
        async def _inner():
            if existing:
                print(f"Reusing #{existing.name} ({existing.id})")
                return existing
            ch = await guild.create_text_channel(name, category=category, reason="Sentinel setup", **kwargs)
            print(f"Created #{ch.name} ({ch.id})")
            return ch
        return _inner()

    mod_log = await ensure("mod-log", find_channel(guild, "mod-log"), overwrites=staff_only)
    detain_log = await ensure("detains", find_channel(guild, "detains"), overwrites=staff_only)
    voice_log = await ensure("voice-log", find_channel(guild, "voice-log"), overwrites=staff_only)

    # Detainment channel: visible to Detained + staff, so detainees can appeal.
    detainment = find_channel(guild, "detainment-center")
    if detainment is None:
        detainment = await guild.create_text_channel(
            "detainment-center",
            category=category,
            reason="Sentinel setup: appeal channel for detained users",
            overwrites={
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                detain_role: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                ),
            },
        )
        print(f"Created #{detainment.name} ({detainment.id})")
    else:
        print(f"Reusing #{detainment.name} ({detainment.id})")

    # -- Detained role denies on every category ----------------------------
    deny = discord.PermissionOverwrite(
        send_messages=False,
        send_messages_in_threads=False,
        create_public_threads=False,
        create_private_threads=False,
        add_reactions=False,
        speak=False,
        connect=False,
        stream=False,
    )
    for cat in guild.categories:
        if cat.id == category.id:
            continue
        current = cat.overwrites_for(detain_role)
        if current != deny:
            try:
                await cat.set_permissions(detain_role, overwrite=deny, reason="Sentinel setup: detain lockdown")
                print(f"Locked category {cat.name!r} for Detained")
            except discord.Forbidden:
                print(f"!! No permission to edit category {cat.name!r}")

    # -- write config back -------------------------------------------------
    config.update(
        DETAIN_ROLE_ID=str(detain_role.id),
        MOD_LOG_CHANNEL_ID=str(mod_log.id),
        DETAIN_LOG_CHANNEL_ID=str(detain_log.id),
        VOICE_LOG_CHANNEL_ID=str(voice_log.id),
    )
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    print("config.json updated:")
    print(json.dumps(config, indent=2))


async def main() -> None:
    intents = discord.Intents.default()
    intents.members = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            await run(client)
        finally:
            await client.close()

    await client.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
