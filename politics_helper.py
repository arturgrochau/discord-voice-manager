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
BUMP_XP = 200          # very generous: ~50 messages worth of XP
REMINDER_TTL = 3 * 60 * 60   # general-chat reminder self-destructs after 3h
AWARD_TTL = 24 * 60 * 60     # award announcements clean up after a day
QUOTE_INTERVAL = 4 * 60 * 60
NADEKO_DB = Path.home() / "Projects/nadekobot/nadeko-osx-arm64/data/NadekoBot.db"
QUOTES_PATH = BASE_DIR / "philosophy_quotes.json"


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
        self.general_channel_id = int(config.get("GENERAL_CHANNEL_ID", 0) or 0)
        self._bump_task = None
        self._last_reward_at = 0.0
        self._reminder_msgs: list = []  # live reminder messages to clear on bump

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
        if self.general_channel_id:
            self.loop.create_task(self._quote_loop())

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
        """Announce the open bump window in the bump channel and general chat.

        Reminders self-destruct after REMINDER_TTL and are also removed the
        moment someone bumps, so no stale pings linger.
        """
        self._reminder_msgs = [m for m in self._reminder_msgs if m]
        for channel_id, text in (
            (self.bump_channel_id,
             f"⏰ **Bump available!** Run `/bump` — first bumper earns **+{BUMP_XP} XP** 🎖️"),
            (self.general_channel_id,
             f"⏰ The server can be bumped again — `/bump` in <#{self.bump_channel_id}> "
             f"pays **+{BUMP_XP} XP** to the first bumper 🎖️"),
        ):
            channel = self.get_channel(channel_id)
            if not channel:
                continue
            try:
                msg = await channel.send(text, delete_after=REMINDER_TTL)
                self._reminder_msgs.append(msg)
            except discord.HTTPException as e:
                log.warning("Bump reminder failed in %s: %s", channel_id, e)
        log.info("Posted bump reminders (%d)", len(self._reminder_msgs))

    async def _clear_reminders(self):
        for msg in self._reminder_msgs:
            try:
                await msg.delete()
            except discord.HTTPException:
                pass
        self._reminder_msgs = []

    def _award_bump_xp(self, user_id: int, guild_id: int) -> int:
        """Add BUMP_XP to the bumper's Nadeko server XP (WAL-safe upsert)."""
        import sqlite3

        with sqlite3.connect(NADEKO_DB, timeout=10) as db:
            db.execute(
                "INSERT INTO UserXpStats (UserId, GuildId, Xp, DateAdded) "
                "VALUES (?, ?, ?, datetime('now')) "
                "ON CONFLICT(UserId, GuildId) DO UPDATE SET Xp = Xp + excluded.Xp",
                (user_id, guild_id, BUMP_XP),
            )
            total = db.execute(
                "SELECT Xp FROM UserXpStats WHERE UserId=? AND GuildId=?",
                (user_id, guild_id),
            ).fetchone()[0]
        return total

    async def on_message(self, message: discord.Message):
        import asyncio

        if message.channel.id != self.bump_channel_id or not self._is_bump_done(message):
            return
        await self._clear_reminders()

        # reward the bumper — only one reward per legitimate bump window
        meta = getattr(message, "interaction_metadata", None) or getattr(message, "interaction", None)
        bumper = getattr(meta, "user", None)
        if bumper and not bumper.bot and time.monotonic() - self._last_reward_at > BUMP_COOLDOWN - 120:
            self._last_reward_at = time.monotonic()
            try:
                total = await asyncio.to_thread(self._award_bump_xp, bumper.id, message.guild.id)
                await message.channel.send(
                    f"🎖️ {bumper.mention} bumped the server — **+{BUMP_XP} XP** awarded! "
                    f"(server XP: {total})",
                    delete_after=AWARD_TTL,
                )
                log.info("Awarded %d bump XP to %s (total %d)", BUMP_XP, bumper, total)
            except Exception:
                log.exception("Bump XP award failed for %s", bumper)

        log.info("Bump detected; next reminder in %d min", BUMP_COOLDOWN // 60)
        if self._bump_task and not self._bump_task.done():
            self._bump_task.cancel()

        async def remind_later():
            await asyncio.sleep(BUMP_COOLDOWN)
            await self._post_bump_reminder()

        self._bump_task = self.loop.create_task(remind_later())

    # -- philosophy quotes -------------------------------------------------

    async def _quote_loop(self):
        import asyncio
        import random

        try:
            quotes = json.loads(QUOTES_PATH.read_text())
        except Exception:
            log.exception("No quotes file; quote loop disabled")
            return
        recent: list[int] = []
        # restart-safe: figure out how long since our last quote actually posted
        first_delay = QUOTE_INTERVAL
        channel = self.get_channel(self.general_channel_id)
        if channel:
            from datetime import datetime, timezone
            try:
                async for m in channel.history(limit=40):
                    if m.author.id == self.user.id and m.embeds and (m.embeds[0].description or "").startswith("*“"):
                        age = (datetime.now(timezone.utc) - m.created_at).total_seconds()
                        first_delay = max(60, QUOTE_INTERVAL - age)
                        break
                else:
                    first_delay = 60
            except discord.HTTPException:
                pass
        await asyncio.sleep(first_delay)
        while True:
            channel = self.get_channel(self.general_channel_id)
            if channel is None:
                await asyncio.sleep(QUOTE_INTERVAL)
                continue
            try:
                # skip dead hours: don't stack quotes on top of our own quote
                last = [m async for m in channel.history(limit=1)]
                if last and last[0].author.id == self.user.id and last[0].embeds:
                    await asyncio.sleep(QUOTE_INTERVAL)
                    continue
                idx = random.choice([i for i in range(len(quotes)) if i not in recent])
                recent.append(idx)
                if len(recent) > 20:
                    recent.pop(0)
                q = quotes[idx]
                embed = discord.Embed(
                    description=f"*“{q['text']}”*\n\n— **{q['author']}**",
                    color=0xC0A062,
                )
                await channel.send(embed=embed)
                log.info("Posted quote by %s", q["author"])
            except discord.HTTPException as e:
                log.warning("Quote post failed: %s", e)
            await asyncio.sleep(QUOTE_INTERVAL)

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
