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
- Modmail: DMing the bot opens a ticket channel in the modmail category;
  everything the user DMs is relayed there, plain mod messages in the ticket
  are relayed back to the user's DMs, and `=close [reason]` closes the ticket
  (transcript goes to the log channel).

Config: politics_helper.json next to this file.
"""

import json
import logging
import logging.handlers
import re
import os
import sys
import time
from pathlib import Path

import discord

# HELPER_HOME lets other servers run their own instance off this codebase.
BASE_DIR = Path(os.environ.get("HELPER_HOME", Path(__file__).resolve().parent))
CONFIG_PATH = BASE_DIR / "politics_helper.json"

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


def politics_bot_token(config: dict) -> str:
    src = Path(config.get("TOKEN_FILE",
                          Path.home() / "Projects/nadekobot/nadeko-osx-arm64/data/creds.yml"))
    text = src.read_text()
    m = re.search(r"^token:\s*'?([A-Za-z0-9_.\-]+)'?", text, re.M) \
        or re.search(r"DISCORD_BOT_TOKEN=([A-Za-z0-9_.\-]+)", text)
    return m.group(1)


TEMP_VC_PREFIX = "🔊│"
DISBOARD_ID = 302050872383242240
BUMP_COOLDOWN = 2 * 60 * 60
BUMP_XP = 200          # very generous: ~50 messages worth of XP
REMINDER_TTL = 3 * 60 * 60   # general-chat reminder self-destructs after 3h
AWARD_TTL = 24 * 60 * 60     # award announcements clean up after a day
QUOTE_INTERVAL = 4 * 60 * 60
NADEKO_DB = Path.home() / "Projects/nadekobot/nadeko-osx-arm64/data/NadekoBot.db"
QUOTES_PATH = Path(__file__).resolve().parent / "philosophy_quotes.json"


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
        # ordered low -> high; gaining a higher rung removes all lower ones
        self.ladder: list[int] = [int(r) for r in config.get("LADDER", [])]
        self._spawning: set[int] = set()  # members mid-room-creation (debounce)
        # modmail
        self.modmail_category_id = int(config.get("MODMAIL_CATEGORY_ID", 0) or 0)
        self.modmail_ping_roles = [int(r) for r in config.get("MODMAIL_PING_ROLE_IDS", [])]
        self.modmail_log_id = int(config.get("MODMAIL_LOG_CHANNEL_ID", 0) or 0)
        self.tickets: dict[int, int] = {}       # user id -> ticket channel id
        self.ticket_users: dict[int, int] = {}  # ticket channel id -> user id

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
        if self.ladder and self.config.get("LADDER_MIN_RANK_DAYS"):
            self.loop.create_task(self._pending_promotions_loop())

        # rebuild the open-ticket map from channel topics (restart-safe)
        if self.modmail_category_id:
            mm_cat = guild.get_channel(self.modmail_category_id)
            for ch in (mm_cat.text_channels if mm_cat else []):
                m = re.fullmatch(r"modmail:(\d+)", ch.topic or "")
                if m:
                    uid = int(m.group(1))
                    self.tickets[uid] = ch.id
                    self.ticket_users[ch.id] = uid
            log.info("Modmail active: %d open ticket(s)", len(self.tickets))

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

        # modmail: DMs open/continue tickets, ticket channels relay back
        if message.guild is None:
            if self.modmail_category_id and not message.author.bot:
                await self._modmail_inbound(message)
            return
        if message.channel.id in self.ticket_users:
            if not message.author.bot:
                await self._modmail_outbound(message)
            return

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

    # -- modmail -----------------------------------------------------------

    MODMAIL_COLOR_IN = 0x3498DB   # user -> mods
    MODMAIL_COLOR_OUT = 0x2ECC71  # mods -> user

    @staticmethod
    def _attachment_lines(message: discord.Message) -> str:
        return "\n".join(f"📎 {a.url}" for a in message.attachments)

    async def _modmail_inbound(self, message: discord.Message):
        """A user DMed the bot: open (or continue) their ticket."""
        guild = self.get_guild(self.guild_id)
        category = guild.get_channel(self.modmail_category_id) if guild else None
        if category is None:
            return
        channel = self.get_channel(self.tickets.get(message.author.id, 0))
        opened = False
        if channel is None:
            member = guild.get_member(message.author.id)
            try:
                channel = await guild.create_text_channel(
                    f"📬〡{message.author.name}"[:100],
                    category=category,
                    topic=f"modmail:{message.author.id}",
                    overwrites=category.overwrites,
                    reason=f"Modmail ticket opened by {message.author}",
                )
            except discord.HTTPException as e:
                log.warning("Modmail ticket creation failed for %s: %s", message.author, e)
                return
            self.tickets[message.author.id] = channel.id
            self.ticket_users[channel.id] = message.author.id
            opened = True
            header = discord.Embed(
                title="📬 New modmail ticket",
                description=(
                    f"**User:** {message.author.mention} (`{message.author}`, id {message.author.id})\n"
                    f"**Account created:** {discord.utils.format_dt(message.author.created_at, 'R')}\n"
                    + (f"**Joined server:** {discord.utils.format_dt(member.joined_at, 'R')}\n"
                       f"**Roles:** {', '.join(r.mention for r in member.roles[1:]) or '—'}"
                       if member else "**Not currently a member of the server.**")
                ),
                color=self.MODMAIL_COLOR_IN,
            )
            header.set_footer(text="Reply with a plain message to answer • =close [reason] to close")
            ping = " ".join(f"<@&{r}>" for r in self.modmail_ping_roles)
            try:
                await channel.send(ping or None, embed=header)
            except discord.HTTPException:
                pass
            log.info("Modmail ticket opened for %s (#%s)", message.author, channel.name)

        embed = discord.Embed(
            description=(message.content or "").strip() or "*(no text)*",
            color=self.MODMAIL_COLOR_IN,
            timestamp=message.created_at,
        )
        embed.set_author(name=f"{message.author.display_name} (user)",
                         icon_url=message.author.display_avatar.url)
        if message.attachments:
            embed.add_field(name="Attachments", value=self._attachment_lines(message)[:1024], inline=False)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            log.warning("Modmail relay to %s failed: %s", channel, e)
            return
        if opened:
            await self._try_dm(
                message.author,
                f"📬 Thanks for reaching out! Your message has been delivered to the "
                f"**{guild.name}** mod team. You'll get a reply right here — it can take "
                f"up to 24 hours. Anything else you send me is added to the same ticket.",
            )
        else:
            try:
                await message.add_reaction("📨")
            except discord.HTTPException:
                pass

    async def _modmail_outbound(self, message: discord.Message):
        """A mod wrote in a ticket channel: relay to the user or run a command."""
        uid = self.ticket_users.get(message.channel.id)
        content = (message.content or "").strip()
        if content.startswith("="):
            cmd, _, arg = content[1:].partition(" ")
            if cmd.lower() == "close":
                await self._modmail_close(message, uid, arg.strip())
            else:
                await message.channel.send(
                    "Commands here: `=close [reason]` — plain messages are sent to the user.",
                    delete_after=15,
                )
            return
        # other bots' command prefixes stay internal to the ticket
        if content.startswith((".", ";", "/", "!")):
            return
        if not content and not message.attachments:
            return
        guild = message.guild
        embed = discord.Embed(
            description=content or "*(no text)*",
            color=self.MODMAIL_COLOR_OUT,
            timestamp=message.created_at,
        )
        embed.set_author(name=f"{message.author.display_name} — {guild.name} Mod Team",
                         icon_url=message.author.display_avatar.url)
        if message.attachments:
            embed.add_field(name="Attachments", value=self._attachment_lines(message)[:1024], inline=False)
        try:
            user = self.get_user(uid) or await self.fetch_user(uid)
            await user.send(embed=embed)
            await message.add_reaction("✅")
        except discord.HTTPException as e:
            await message.channel.send(
                f"⚠️ Couldn't deliver that — the user may have left or blocked DMs. ({e.status})",
            )

    async def _modmail_close(self, message: discord.Message, uid: int, reason: str):
        channel = message.channel
        # transcript for the log channel
        lines = [f"Modmail transcript — #{channel.name} (user id {uid}), "
                 f"closed by {message.author} ({reason or 'no reason given'})", ""]
        try:
            async for m in channel.history(limit=500, oldest_first=True):
                stamp = m.created_at.strftime("%Y-%m-%d %H:%M")
                if m.embeds and m.embeds[0].author.name:
                    lines.append(f"[{stamp}] {m.embeds[0].author.name}: {m.embeds[0].description or ''}")
                elif m.content:
                    lines.append(f"[{stamp}] {m.author.display_name}: {m.content}")
        except discord.HTTPException:
            pass
        log_channel = self.get_channel(self.modmail_log_id)
        if log_channel:
            import io
            try:
                await log_channel.send(
                    f"📪 Modmail ticket for <@{uid}> closed by {message.author.mention}."
                    + (f" Reason: {reason}" if reason else ""),
                    file=discord.File(io.BytesIO("\n".join(lines).encode()),
                                      filename=f"modmail-{uid}.txt"),
                )
            except discord.HTTPException:
                log.warning("Modmail transcript post failed")
        try:
            user = self.get_user(uid) or await self.fetch_user(uid)
            await user.send(
                f"📪 Your ticket with the **{message.guild.name}** mod team has been closed."
                + (f"\nNote from the team: {reason}" if reason else "")
                + "\nIf you need anything else, just send me another message."
            )
        except discord.HTTPException:
            pass
        self.tickets.pop(uid, None)
        self.ticket_users.pop(channel.id, None)
        try:
            await channel.delete(reason=f"Modmail closed by {message.author}")
        except discord.HTTPException as e:
            log.warning("Modmail channel delete failed: %s", e)
        log.info("Modmail ticket for %s closed by %s", uid, message.author)

    @staticmethod
    async def _try_dm(user: discord.abc.User, text: str):
        try:
            await user.send(text)
        except discord.HTTPException:
            pass

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

    # -- ladder promotions -------------------------------------------------
    # Promotion keeps only the highest rung. Optionally (LADDER_MIN_RANK_DAYS)
    # a promotion also requires TIME at the previous rank: XP alone reaches the
    # gate, but the new role only sticks once the tenure is served — the helper
    # defers it and applies it automatically when the member qualifies.

    def _ladder_state_load(self) -> dict:
        try:
            return json.loads((BASE_DIR / "ladder_state.json").read_text())
        except Exception:
            return {"since": {}, "pending": {}}

    def _ladder_state_save(self, state: dict) -> None:
        (BASE_DIR / "ladder_state.json").write_text(json.dumps(state))

    def _min_days(self, rung_index: int) -> float:
        days = self.config.get("LADDER_MIN_RANK_DAYS", [])
        try:
            return float(days[rung_index])
        except (IndexError, TypeError, ValueError):
            return 0.0

    async def on_member_update(self, before, after):
        if not self.ladder or after.guild.id != self.guild_id:
            return
        before_ids = {r.id for r in before.roles}
        gained = [r for r in self.ladder if r in {x.id for x in after.roles} and r not in before_ids]
        if not gained:
            return
        import time as _t

        state = self._ladder_state_load()
        uid = str(after.id)
        top = max(self.ladder.index(r) for r in gained)
        top_role_id = self.ladder[top]
        required = self._min_days(top) * 86400

        # tenure gate: how long has the member held the rung below?
        held_since = state["since"].get(uid, {}).get(str(self.ladder[top - 1])) if top else None
        pending = state["pending"].get(uid)
        gate = required > 0 and (
            pending is not None  # already waiting on a promotion: keep waiting
            or (held_since is not None and _t.time() - held_since < required)
        )
        if gate:
            eligible_at = (pending or {}).get("eligible_at") or (held_since + required)
            role = after.guild.get_role(top_role_id)
            try:
                if role and role in after.roles:
                    await after.remove_roles(role, reason="Ladder: tenure not yet served — promotion deferred")
            except discord.HTTPException as e:
                log.warning("Deferred-promotion removal failed for %s: %s", after, e)
            state["pending"][uid] = {"role": top_role_id, "eligible_at": eligible_at}
            self._ladder_state_save(state)
            days_left = max(0.0, (eligible_at - _t.time()) / 86400)
            await self._try_dm(
                after,
                f"🎖️ You've earned the XP for **{role.name if role else 'your next rank'}** in "
                f"**{after.guild.name}**! Ranks also take time — yours unlocks automatically in "
                f"about **{days_left:.1f} day(s)**. Keep it up!",
            )
            log.info("Deferred promotion for %s: %s in %.1fd", after, top_role_id, days_left)
            return

        # promotion proceeds: record tenure start, strip superseded rungs
        state["since"].setdefault(uid, {})[str(top_role_id)] = _t.time()
        state["pending"].pop(uid, None)
        self._ladder_state_save(state)
        lower = [after.guild.get_role(r) for r in self.ladder[:top]]
        lower = [r for r in lower if r and r in after.roles]
        if lower:
            try:
                await after.remove_roles(*lower, reason="Ladder promotion: superseded rank removed")
                log.info("Promoted %s: removed %s", after, [r.name for r in lower])
            except discord.HTTPException as e:
                log.warning("Ladder cleanup failed for %s: %s", after, e)

    async def _pending_promotions_loop(self):
        """Apply deferred promotions the moment their tenure is served."""
        import asyncio
        import time as _t

        while True:
            await asyncio.sleep(1800)
            state = self._ladder_state_load()
            due = {uid: p for uid, p in state["pending"].items()
                   if p.get("eligible_at", 0) <= _t.time()}
            if not due:
                continue
            guild = self.get_guild(self.guild_id)
            if guild is None:
                continue
            for uid, p in due.items():
                member = guild.get_member(int(uid))
                role = guild.get_role(int(p["role"]))
                state["pending"].pop(uid, None)
                if member is None or role is None:
                    self._ladder_state_save(state)
                    continue
                state["since"].setdefault(uid, {})[str(role.id)] = _t.time()
                # save BEFORE the role add: the resulting member-update event
                # must not see a stale pending entry and re-defer the promotion
                self._ladder_state_save(state)
                try:
                    await member.add_roles(role, reason="Ladder: tenure served — deferred promotion applied")
                    await self._try_dm(member, f"🎖️ Tenure served — you are now **{role.name}** in **{guild.name}**!")
                    log.info("Applied deferred promotion: %s -> %s", member, role.name)
                except discord.HTTPException as e:
                    log.warning("Deferred promotion failed for %s: %s", member, e)

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
        # spawn a room (debounced: duplicate voice events arrive while moving)
        if after.channel and after.channel.id == self.trigger_id:
            if member.id in self._spawning:
                return
            self._spawning.add(member.id)
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
            finally:
                self.loop.create_task(self._release_spawn(member.id))
            return
        # clean up an emptied room (grace period covers the create->move gap)
        if before.channel and before.channel.id in self.temp_vcs and not before.channel.members:
            if time.monotonic() - self.temp_vcs[before.channel.id] < 30:
                self.loop.create_task(self._delayed_cleanup(before.channel.id))
                return
            await self._delete_temp(before.channel)

    async def _release_spawn(self, member_id: int):
        import asyncio

        await asyncio.sleep(10)
        self._spawning.discard(member_id)

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
    config = load_config()
    client = Helper(config)
    client.run(politics_bot_token(config), log_handler=None)


if __name__ == "__main__":
    main()
