"""Politics Bot companion — reaction roles + join-to-create voice channels.

Runs a second gateway session on the Politics Bot token (alongside NadekoBot,
which doesn't provide these features) so both features genuinely come from
Politics Bot with no third-party bots involved.

- Reaction roles: emoji -> role mappings on configured messages, add on react,
  remove on unreact.
- Join to Create: joining the trigger voice channel spawns a personal room in
  the same category, moves the creator in, and grants them channel-scoped
  mute/deafen/move/manage powers (works for any member, no mod role needed).
  Empty temp rooms are deleted, including orphans found at startup.
- Bump reminder: watches Disboard's "Bump done" confirmations in the bump
  channel and posts a reminder when the 2-hour cooldown expires. (Actually
  invoking /bump automatically would be user-account automation, which
  Discord ToS forbids — so the last click stays human.)

Config: politics_helper.json next to this file.
"""

import json
import logging
import logging.handlers
import re
import sys
import time
from pathlib import Path

import discord

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "politics_helper.json"
NADEKO_CREDS = Path.home() / "Projects/nadekobot/nadeko-osx-arm64/data/creds.yml"

log = logging.getLogger("politics-helper")


def setup_logging() -> None:
    logs_dir = BASE_DIR / "logs"
    logs_dir.mkdir(exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        logs_dir / "politics-helper.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
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
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def politics_bot_token() -> str:
    creds = NADEKO_CREDS.read_text()
    return re.search(r"^token:\s*'?([A-Za-z0-9_.\-]+)'?", creds, re.M).group(1)


TEMP_VC_PREFIX = "🔊│"
DISBOARD_ID = 302050872383242240
BUMP_COOLDOWN = 2 * 60 * 60


class Helper(discord.Client):
    def __init__(self, config: dict):
        intents = discord.Intents.default()
        intents.members = True
        intents.voice_states = True
        intents.message_content = True  # to read Disboard's bump confirmations
        super().__init__(intents=intents)
        self.config = config
        self.guild_id = int(config["GUILD_ID"])
        self.trigger_id = int(config["JOIN_TO_CREATE_CHANNEL_ID"])
        self.category_id = int(config["TEMP_VC_CATEGORY_ID"])
        # message_id(str) -> {emoji(str) -> role_id(str)}
        self.reaction_roles: dict = config.get("REACTION_ROLES", {})
        self.temp_vcs: dict[int, float] = {}  # channel id -> created-at monotonic
        self.bump_channel_id = int(config.get("BUMP_CHANNEL_ID", 0) or 0)
        self._bump_task = None

    async def on_ready(self):
        log.info("Politics helper online as %s (%s)", self.user, self.user.id)
        guild = self.get_guild(self.guild_id)
        if not guild:
            return
        category = guild.get_channel(self.category_id)
        if category:
            for ch in category.voice_channels:
                if ch.id == self.trigger_id:
                    continue
                if ch.name.startswith(TEMP_VC_PREFIX):
                    if ch.members:
                        self.temp_vcs[ch.id] = time.monotonic()
                    else:
                        try:
                            await ch.delete(reason="Join-to-Create: orphaned empty room")
                            log.info("Deleted orphaned temp VC %s", ch.name)
                        except discord.HTTPException:
                            pass

        if self.bump_channel_id:
            self.loop.create_task(self._bump_bootstrap())

    # -- bump reminder -----------------------------------------------------

    @staticmethod
    def _is_bump_done(message: discord.Message) -> bool:
        if message.author.id != DISBOARD_ID:
            return False
        blob = " ".join(e.description or "" for e in message.embeds) + (message.content or "")
        return "bump done" in blob.lower()

    async def _bump_bootstrap(self):
        import asyncio
        from datetime import datetime, timezone

        channel = self.get_channel(self.bump_channel_id)
        if channel is None:
            return
        last_bump = None
        reminder_after_bump = False
        async for m in channel.history(limit=50):
            if self._is_bump_done(m):
                last_bump = m.created_at
                break
            if m.author.id == self.user.id and "Bump available" in (m.content or ""):
                reminder_after_bump = True
        if last_bump is None:
            return
        elapsed = (datetime.now(timezone.utc) - last_bump).total_seconds()
        if elapsed < BUMP_COOLDOWN:
            await asyncio.sleep(BUMP_COOLDOWN - elapsed)
            await self._post_bump_reminder()
        elif not reminder_after_bump:
            await self._post_bump_reminder()

    async def _post_bump_reminder(self):
        channel = self.get_channel(self.bump_channel_id)
        if channel:
            try:
                await channel.send("⏰ **Bump available!** Run `/bump` to push us up on Disboard 🤍")
                log.info("Posted bump reminder")
            except discord.HTTPException as e:
                log.warning("Bump reminder failed: %s", e)

    async def on_message(self, message: discord.Message):
        import asyncio

        if message.channel.id != self.bump_channel_id or not self._is_bump_done(message):
            return
        log.info("Bump detected; reminding in %d min", BUMP_COOLDOWN // 60)
        if self._bump_task and not self._bump_task.done():
            self._bump_task.cancel()

        async def remind_later():
            await asyncio.sleep(BUMP_COOLDOWN)
            await self._post_bump_reminder()

        self._bump_task = self.loop.create_task(remind_later())

    # -- reaction roles ----------------------------------------------------

    def _lookup(self, payload) -> int | None:
        mapping = self.reaction_roles.get(str(payload.message_id))
        if not mapping:
            return None
        emoji = str(payload.emoji.name) if payload.emoji.is_unicode_emoji() else str(payload.emoji.id)
        role_id = mapping.get(emoji)
        return int(role_id) if role_id else None

    async def on_raw_reaction_add(self, payload):
        role_id = self._lookup(payload)
        if not role_id or payload.guild_id != self.guild_id:
            return
        guild = self.get_guild(self.guild_id)
        member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
        if member.bot:
            return
        role = guild.get_role(role_id)
        if role and role not in member.roles:
            try:
                await member.add_roles(role, reason="Reaction role")
                log.info("Added %s to %s", role.name, member)
            except discord.HTTPException as e:
                log.warning("Failed adding %s to %s: %s", role.name, member, e)

    async def on_raw_reaction_remove(self, payload):
        role_id = self._lookup(payload)
        if not role_id or payload.guild_id != self.guild_id:
            return
        guild = self.get_guild(self.guild_id)
        member = guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except discord.NotFound:
                return
        if member.bot:
            return
        role = guild.get_role(role_id)
        if role and role in member.roles:
            try:
                await member.remove_roles(role, reason="Reaction role removed")
                log.info("Removed %s from %s", role.name, member)
            except discord.HTTPException as e:
                log.warning("Failed removing %s from %s: %s", role.name, member, e)

    # -- join to create ----------------------------------------------------

    async def on_voice_state_update(self, member, before, after):
        if member.bot:
            return
        # spawn a room
        if after.channel and after.channel.id == self.trigger_id:
            guild = member.guild
            category = guild.get_channel(self.category_id)
            name = f"{TEMP_VC_PREFIX}{member.display_name}'s room"[:100]
            overwrites = {
                member: discord.PermissionOverwrite(
                    view_channel=True,
                    connect=True,
                    speak=True,
                    manage_channels=True,
                    mute_members=True,
                    deafen_members=True,
                    move_members=True,
                    priority_speaker=True,
                    set_voice_channel_status=True,
                )
            }
            try:
                vc = await guild.create_voice_channel(
                    name, category=category, overwrites=overwrites,
                    reason=f"Join-to-Create: room for {member}",
                )
                self.temp_vcs[vc.id] = time.monotonic()
                await member.move_to(vc, reason="Join-to-Create")
                log.info("Created temp VC %r for %s", vc.name, member)
            except discord.HTTPException as e:
                log.warning("Join-to-Create failed for %s: %s", member, e)
            return
        # clean up an emptied room (grace period covers the create->move gap)
        if before.channel and before.channel.id in self.temp_vcs and not before.channel.members:
            if time.monotonic() - self.temp_vcs[before.channel.id] < 30:
                self.loop.create_task(self._delayed_cleanup(before.channel.id))
                return
            await self._delete_temp(before.channel)

    async def _delayed_cleanup(self, channel_id: int):
        import asyncio

        await asyncio.sleep(35)
        if channel_id not in self.temp_vcs:
            return
        channel = self.get_channel(channel_id)
        if channel and not channel.members:
            await self._delete_temp(channel)

    async def _delete_temp(self, channel):
        try:
            await channel.delete(reason="Join-to-Create: room empty")
            self.temp_vcs.pop(channel.id, None)
            log.info("Deleted empty temp VC %r", channel.name)
        except discord.HTTPException as e:
            log.warning("Failed deleting temp VC: %s", e)


def main() -> None:
    setup_logging()
    client = Helper(load_config())
    client.run(politics_bot_token(), log_handler=None)


if __name__ == "__main__":
    main()
