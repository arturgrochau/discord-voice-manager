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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vc_rooms  # noqa: E402  (TempVoice-style room control panel)
import contest as contest_mod  # noqa: E402  (nitro giveaway engine)
import music as music_mod  # noqa: E402  (..play music player)

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
BOOK_INTERVAL = 3 * 24 * 60 * 60   # a book recommendation every 3 days
FORUM_PROMPT_INTERVAL = 3 * 24 * 60 * 60   # a philosophy-forum prompt every 3 days
DEFAULT_NADEKO_DB = Path.home() / "Projects/nadekobot/nadeko-osx-arm64/data/NadekoBot.db"
QUOTES_PATH = Path(__file__).resolve().parent / "philosophy_quotes.json"
BOOK_RECS_PATH = Path(__file__).resolve().parent / "book_recs.json"
PHIL_PROMPTS_PATH = Path(__file__).resolve().parent / "philosophy_prompts.json"


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
        self.nadeko_db = Path(config.get("NADEKO_DB_PATH", DEFAULT_NADEKO_DB)).expanduser()
        # everyone holds at least this role until their first ladder rank
        self.autorole_id = int(config.get("AUTOROLE_ID", 0) or 0)
        # TempVoice-style room control (panel, ..commands, saved prefs)
        self.rooms = vc_rooms.RoomManager(self, config, BASE_DIR)
        self.rooms.delete_cb = self._delete_temp
        self.contest = contest_mod.Contest(self, config, BASE_DIR, self.nadeko_db, self.guild_id)
        self.music = music_mod.MusicPlayer(self, config, BASE_DIR)
        # AFK: continuously muted, or alone in a call, for this long -> parked
        self.afk_channel_id = int(config.get("AFK_CHANNEL_ID", 0) or 0)
        self.afk_after = int(config.get("AFK_AFTER_MINUTES", 180)) * 60
        self.afk_alone_after = int(config.get("AFK_ALONE_MINUTES", 180)) * 60
        self._muted_since: dict[int, float] = {}
        self._alone_since: dict[int, float] = {}
        # ordered low -> high; gaining a higher rung removes all lower ones
        self.ladder: list[int] = [int(r) for r in config.get("LADDER", [])]
        self._spawning: set[int] = set()  # members mid-room-creation (debounce)
        # modmail
        self.modmail_category_id = int(config.get("MODMAIL_CATEGORY_ID", 0) or 0)
        self.modmail_ping_roles = [int(r) for r in config.get("MODMAIL_PING_ROLE_IDS", [])]
        self.modmail_log_id = int(config.get("MODMAIL_LOG_CHANNEL_ID", 0) or 0)
        self.tickets: dict[int, int] = {}       # user id -> ticket channel id
        self.ticket_users: dict[int, int] = {}  # ticket channel id -> user id
        self._streaks = self._streaks_load()    # daily login streaks
        # anicca: an ephemeral channel that forgets on a rolling window
        self.anicca_channel_id = int(config.get("ANICCA_CHANNEL_ID", 0) or 0)
        self.anicca_retention = float(config.get("ANICCA_RETENTION_HOURS", 12) or 12) * 3600
        self.anicca_sweep = int(config.get("ANICCA_SWEEP_MINUTES", 30) or 30) * 60
        # welcome: a short self-deleting greeting from the bot on join
        self.welcome_enabled = config.get("WELCOME_ENABLED", True)
        self.welcome_delete_after = int(config.get("WELCOME_DELETE_AFTER", 120) or 120)
        self.rules_channel_id = int(config.get("RULES_CHANNEL_ID", 0) or 0)
        self.book_club_channel_id = int(config.get("BOOK_CLUB_CHANNEL_ID", 0) or 0)
        self._welcomed: set[int] = set()
        # role persistence: returning members keep their roles (ladder rank
        # included). The detain role is deliberately excluded — the Sentinel bot
        # is the sole detain authority and re-applies it from its DB on rejoin.
        self.role_persist_enabled = config.get("ROLE_PERSIST_ENABLED", True)
        self.detain_role_id = int(config.get("DETAIN_ROLE_ID", 0) or 0)
        self._restored: set[int] = set()

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
        if self.general_channel_id and self.config.get("QUOTES_ENABLED", True):
            self.loop.create_task(self._quote_loop())
        if int(self.config.get("BOOK_CLUB_CHANNEL_ID", 0) or 0) and self.config.get("BOOK_RECS_ENABLED", True):
            self.loop.create_task(self._book_loop())
        if int(self.config.get("PHILOSOPHY_FORUM_CHANNEL_ID", 0) or 0) and self.config.get("FORUM_PROMPTS_ENABLED", True):
            self.loop.create_task(self._forum_loop())
        if self.ladder and self.config.get("LADDER_MIN_RANK_DAYS"):
            self.loop.create_task(self._pending_promotions_loop())
        if self.ladder:
            self.loop.create_task(self._rank_reconcile_sweep(guild))
        await self.rooms.start()
        await self.contest.start()
        self.music.start()
        if self.afk_channel_id:
            self.loop.create_task(self._afk_loop())
        if self.anicca_channel_id and self.config.get("ANICCA_ENABLED", True):
            self.loop.create_task(self._anicca_loop())
        # every live room is delete-tracked, whatever it was renamed to
        for cid in self.rooms.rooms:
            self.temp_vcs.setdefault(int(cid), time.monotonic())

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
            await self._post_bump_reminder()
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
        # restart-proof: reminders posted by a previous process aren't in the
        # list above — sweep both channels for our own reminder messages
        for channel_id in (self.bump_channel_id, self.general_channel_id):
            channel = self.get_channel(channel_id)
            if channel is None:
                continue
            try:
                async for m in channel.history(limit=30):
                    if m.author.id == self.user.id and (
                            "Bump available" in (m.content or "")
                            or "can be bumped again" in (m.content or "")):
                        await m.delete()
            except discord.HTTPException:
                pass

    def _award_xp(self, user_id: int, guild_id: int, amount: int) -> int:
        """Add `amount` to a member's Nadeko server XP (WAL-safe upsert)."""
        import sqlite3

        with sqlite3.connect(self.nadeko_db, timeout=10) as db:
            db.execute(
                "INSERT INTO UserXpStats (UserId, GuildId, Xp, DateAdded) "
                "VALUES (?, ?, ?, datetime('now')) "
                "ON CONFLICT(UserId, GuildId) DO UPDATE SET Xp = Xp + excluded.Xp",
                (user_id, guild_id, amount),
            )
            total = db.execute(
                "SELECT Xp FROM UserXpStats WHERE UserId=? AND GuildId=?",
                (user_id, guild_id),
            ).fetchone()[0]
        return total

    def _award_bump_xp(self, user_id: int, guild_id: int) -> int:
        return self._award_xp(user_id, guild_id, BUMP_XP)

    # -- daily login streaks -----------------------------------------------
    # A clear reason to show up every day: the first message a member sends
    # on a new day extends their streak and pays escalating XP (with milestone
    # bonuses). Miss a day and the streak resets — loss aversion does the work.

    STREAK_MILESTONES = {7: 500, 14: 1200, 30: 3000, 60: 7000, 100: 15000, 365: 75000}

    def _streaks_load(self) -> dict:
        try:
            return json.loads((BASE_DIR / "streaks.json").read_text())
        except Exception:
            return {}

    def _streaks_save(self) -> None:
        (BASE_DIR / "streaks.json").write_text(json.dumps(self._streaks))

    async def _daily_streak(self, message) -> None:
        if not self.config.get("STREAKS_ENABLED", False):
            return
        import asyncio
        from datetime import date, timedelta

        uid = str(message.author.id)
        today = date.today().isoformat()
        st = self._streaks.get(uid, {})
        if st.get("last") == today:
            return  # already counted for today
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        streak = st.get("streak", 0) + 1 if st.get("last") == yesterday else 1
        best = max(st.get("best", 0), streak)
        self._streaks[uid] = {"last": today, "streak": streak, "best": best}
        self._streaks_save()

        reward = min(50 + streak * 25, 1000) + self.STREAK_MILESTONES.get(streak, 0)
        try:
            await asyncio.to_thread(self._award_xp, message.author.id, message.guild.id, reward)
        except Exception as e:
            log.warning("Streak XP award failed for %s: %s", uid, e)
            return
        # celebrate milestones publicly (never the everyday case, to avoid spam)
        if streak in self.STREAK_MILESTONES:
            try:
                await message.channel.send(
                    f"🔥 {message.author.mention} just hit a **{streak}-day streak**! "
                    f"+{reward:,} XP. Keep showing up.", delete_after=AWARD_TTL)
            except discord.HTTPException:
                pass

    async def _show_streak(self, message) -> None:
        st = self._streaks.get(str(message.author.id), {})
        streak = st.get("streak", 0)
        best = st.get("best", 0)
        from datetime import date
        counted_today = st.get("last") == date.today().isoformat()
        nxt = next((m for m in sorted(self.STREAK_MILESTONES) if m > streak), None)
        lines = [f"🔥 **{message.author.display_name}**'s streak: **{streak} day"
                 f"{'s' if streak != 1 else ''}**  ·  best: {best}"]
        lines.append("✅ Counted for today — see you tomorrow!" if counted_today
                     else "-# Send a message any day to keep the streak alive.")
        if nxt:
            bonus = self.STREAK_MILESTONES[nxt]
            lines.append(f"-# Next milestone: **{nxt} days** for a **+{bonus:,} XP** bonus "
                         f"({nxt - streak} to go).")
        try:
            await message.channel.send("\n".join(lines), delete_after=60)
        except discord.HTTPException:
            pass

    async def _show_anicca(self, message) -> None:
        """Explain the ephemeral channel on demand (this reply is swept too)."""
        hrs = f"{self.anicca_retention / 3600:g}"
        if self.anicca_channel_id and message.channel.id == self.anicca_channel_id:
            txt = (f"⏳ This is **anicca** — messages here are auto-cleared after "
                   f"**{hrs} hours**. Speak freely; nothing lingers. (The pinned note stays.)")
        elif self.anicca_channel_id:
            txt = (f"⏳ **anicca** (<#{self.anicca_channel_id}>) is an ephemeral channel — "
                   f"messages there are auto-cleared after **{hrs} hours**.")
        else:
            txt = "⏳ No ephemeral channel is configured."
        try:
            await message.channel.send(txt, delete_after=120)
        except discord.HTTPException:
            pass

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
        await self.contest.on_message(message)
        if not message.author.bot:
            await self._daily_streak(message)
        # ..music commands work from any chat; room commands only in room chats
        content = (message.content or "").strip()
        # self-authored commands allowed: lets the operator drive the player
        # through the bot's own account for remote tests and DJ duty
        author_ok = (not message.author.bot
                     or (self.user and message.author.id == self.user.id))
        if content.lower() in ("..streak", "..streaks", ".streak") and not message.author.bot:
            await self._show_streak(message)
            return
        if content.lower() in ("..anicca", ".anicca", "..autodel") and not message.author.bot:
            await self._show_anicca(message)
            return
        if content.startswith("..") and author_ok:
            cmd, _, arg = content[2:].partition(" ")
            cmd = cmd.lower()
            if cmd in ("commands", "cmds"):
                embeds = []
                if str(message.channel.id) in self.rooms.rooms:
                    embeds.append(self.rooms.commands_embed())
                if self.music.enabled:
                    embeds.append(self.music.commands_embed(self.rooms._info_link()))
                if embeds:
                    try:
                        await message.channel.send(embeds=embeds, delete_after=600)
                        await message.delete(delay=15)
                    except discord.HTTPException:
                        pass
                    return
            if self.music.enabled and cmd in music_mod.COMMANDS:
                await self.music.handle(message, cmd, arg.strip())
                return
        # ..commands inside a temp room's chat
        if str(message.channel.id) in self.rooms.rooms and not message.author.bot:
            await self.rooms.handle_message(message)
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

    async def _prune_bot_posts(self, channel, match, keep_newest: bool = False):
        """Delete this bot's prior posts matching `match` so only one stands at
        a time. With keep_newest, spare the single most recent match and return
        that message (for pacing/variety); otherwise remove them all, return None."""
        found = []
        try:
            async for m in channel.history(limit=300):
                if m.author.id == self.user.id and match(m):
                    found.append(m)
        except discord.HTTPException:
            return None
        keep = found[0] if (keep_newest and found) else None  # history is newest-first
        for m in found:
            if keep is not None and m.id == keep.id:
                continue
            try:
                await m.delete()
            except discord.HTTPException:
                pass
        return keep

    @staticmethod
    def _is_quote_post(m) -> bool:
        return bool(m.embeds) and (m.embeds[0].description or "").startswith("*“")

    @staticmethod
    def _is_book_post(m) -> bool:
        return bool(m.embeds) and (m.embeds[0].title or "").startswith("📚")

    async def _quote_loop(self):
        import asyncio
        import random

        try:
            quotes = json.loads(QUOTES_PATH.read_text())
        except Exception:
            log.exception("No quotes file; quote loop disabled")
            return
        recent: list[int] = []
        # single-quote policy: clear the backlog now, keep only the newest, and
        # pace the next post from it (restart-safe, no churn on frequent restarts)
        first_delay = 60
        channel = self.get_channel(self.general_channel_id)
        if channel:
            from datetime import datetime, timezone
            newest = await self._prune_bot_posts(channel, self._is_quote_post, keep_newest=True)
            if newest is not None:
                age = (datetime.now(timezone.utc) - newest.created_at).total_seconds()
                first_delay = max(60, QUOTE_INTERVAL - age)
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
                await self._prune_bot_posts(channel, self._is_quote_post)  # retire the old one
                await channel.send(embed=embed)
                log.info("Posted quote by %s", q["author"])
            except discord.HTTPException as e:
                log.warning("Quote post failed: %s", e)
            await asyncio.sleep(QUOTE_INTERVAL)

    # -- book club recommendations -----------------------------------------

    async def _book_loop(self):
        """Post a curated politics/philosophy book rec + discussion prompt to
        the book-club channel every few days to seed activity. Restart-safe:
        cycles through the list by reading which titles were posted recently."""
        import asyncio
        import random

        try:
            books = json.loads(BOOK_RECS_PATH.read_text())
        except Exception:
            log.exception("No book_recs file; book loop disabled")
            return
        channel_id = int(self.config.get("BOOK_CLUB_CHANNEL_ID", 0) or 0)
        color = int(str(self.config.get("PANEL_COLOR", "0xC0A062")), 16)
        emblem = self.config.get("PANEL_EMBLEM", "📚")

        # single-rec policy: keep only the newest pick standing; pace from it
        first_delay = 60
        recent_titles: list[str] = []
        channel = self.get_channel(channel_id)
        if channel:
            from datetime import datetime, timezone
            keep = await self._prune_bot_posts(channel, self._is_book_post, keep_newest=True)
            if keep is not None:
                recent_titles.append(keep.embeds[0].title or "")
                age = (datetime.now(timezone.utc) - keep.created_at).total_seconds()
                first_delay = max(60, BOOK_INTERVAL - age)
        await asyncio.sleep(first_delay)

        while True:
            channel = self.get_channel(channel_id)
            if channel is None:
                await asyncio.sleep(BOOK_INTERVAL)
                continue
            try:
                pool = [b for b in books if not any(b["title"] in t for t in recent_titles[-8:])] or books
                b = random.choice(pool)
                recent_titles.append(f"📚 {b['title']}")
                embed = discord.Embed(
                    title=f"📚 Reading pick: {b['title']}",
                    description=(f"*by {b['author']}*\n\n{b['blurb']}\n\n"
                                 f"**💬 {b['prompt']}**"),
                    color=color,
                )
                embed.set_footer(text="A fresh pick lands here every few days. Jump in with your take.")
                await self._prune_bot_posts(channel, self._is_book_post)  # retire the old one
                await channel.send(embed=embed)
                log.info("Posted book rec: %s", b["title"])
            except discord.HTTPException as e:
                log.warning("Book rec post failed: %s", e)
            await asyncio.sleep(BOOK_INTERVAL)

    # -- philosophy forum prompts ------------------------------------------
    # Seeds the philosophy forum with a fresh discussion prompt every few days:
    # gives newcomers something to jump into, keeps posts from all archiving
    # out of the default view, and nudges people to create/debate topics.
    # Restart-safe: paces the next prompt from the newest one still active.

    async def _forum_loop(self):
        import asyncio
        import random

        try:
            prompts = json.loads(PHIL_PROMPTS_PATH.read_text())
        except Exception:
            log.exception("No philosophy prompts file; forum loop disabled")
            return
        channel_id = int(self.config.get("PHILOSOPHY_FORUM_CHANNEL_ID", 0) or 0)

        first_delay = 60
        recent_titles: list[str] = []
        forum = self.get_channel(channel_id)
        if isinstance(forum, discord.ForumChannel):
            from datetime import datetime, timezone
            newest = None
            for t in forum.threads:
                if t.owner_id == self.user.id:
                    recent_titles.append(t.name)
                    if t.created_at and (newest is None or t.created_at > newest):
                        newest = t.created_at
            if newest is not None:
                age = (datetime.now(timezone.utc) - newest).total_seconds()
                first_delay = max(60, FORUM_PROMPT_INTERVAL - age)
        await asyncio.sleep(first_delay)

        while True:
            forum = self.get_channel(channel_id)
            if not isinstance(forum, discord.ForumChannel):
                await asyncio.sleep(FORUM_PROMPT_INTERVAL)
                continue
            try:
                pool = [p for p in prompts
                        if not any(p["title"] == t for t in recent_titles[-10:])] or prompts
                p = random.choice(pool)
                recent_titles.append(p["title"])
                body = (f"{p['prompt']}\n\n"
                        f"-# Share your take below — or start your own thread with a "
                        f"question you've been chewing on. This is the place for it.")
                # this forum requires a tag on every post
                by_name = {t.name.lower(): t for t in forum.available_tags}
                tag = next((by_name[n] for n in (p.get("tag", "").lower(),
                            "philosophy-general", "politics-general") if n in by_name), None)
                if tag is None and forum.available_tags:
                    tag = forum.available_tags[0]
                await forum.create_thread(name=p["title"][:100], content=body,
                                          applied_tags=[tag] if tag else [])
                log.info("Posted philosophy forum prompt: %s", p["title"])
            except discord.HTTPException as e:
                log.warning("Forum prompt failed: %s", e)
            await asyncio.sleep(FORUM_PROMPT_INTERVAL)

    # -- anicca: the channel that forgets ----------------------------------
    # A rolling ephemeral channel. Messages older than the retention window
    # are swept on every pass (bulk delete under 14 days, single delete for
    # older stragglers after downtime). One pinned explainer survives every
    # sweep so newcomers always have context. Restart-safe and idempotent.

    ANICCA_TITLE = "⏳ anicca"

    async def _anicca_ensure_pin(self, channel) -> None:
        """Guarantee the permanent anicca notice is present and pinned."""
        try:
            pins = await channel.pins()
        except discord.HTTPException:
            return
        for m in pins:
            if (m.author.id == self.user.id and m.embeds
                    and (m.embeds[0].title or "").startswith(self.ANICCA_TITLE)):
                return  # already there — nothing to do
        hrs = f"{self.anicca_retention / 3600:g}"
        color = int(str(self.config.get("PANEL_COLOR", "0xC0A062")), 16)
        embed = discord.Embed(
            title=self.ANICCA_TITLE,
            description=(
                "*Anicca* (Pali: impermanence).\n\n"
                f"Every message in this channel is automatically cleared after {hrs} hours."
            ),
            color=color,
        )
        try:
            msg = await channel.send(embed=embed)
            await msg.pin(reason="anicca: permanent channel notice")
            log.info("anicca: posted and pinned the ephemeral-channel notice")
        except discord.HTTPException as e:
            log.warning("anicca pin failed: %s", e)

    async def _anicca_loop(self) -> None:
        import asyncio
        from datetime import datetime, timezone, timedelta

        await asyncio.sleep(10)  # let the cache settle after on_ready
        channel = self.get_channel(self.anicca_channel_id)
        if channel is None:
            log.warning("anicca: channel %s not found; loop idle", self.anicca_channel_id)
            return
        await self._anicca_ensure_pin(channel)
        hrs = self.anicca_retention / 3600

        while True:
            channel = self.get_channel(self.anicca_channel_id)
            if channel is not None:
                cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.anicca_retention)
                try:
                    # bulk=True bulk-deletes <14d and falls back to single
                    # deletes for anything older (e.g. after a long outage)
                    deleted = await channel.purge(
                        limit=None, before=cutoff, bulk=True,
                        check=lambda m: not m.pinned,
                        reason=f"anicca: rolling {hrs:g}h window",
                    )
                    if deleted:
                        log.info("anicca: swept %d message(s) older than %gh",
                                 len(deleted), hrs)
                except discord.HTTPException as e:
                    log.warning("anicca sweep failed: %s", e)
                await self._anicca_ensure_pin(channel)  # re-assert if unpinned
            await asyncio.sleep(self.anicca_sweep)

    # -- contest event forwarding ------------------------------------------

    async def on_member_join(self, member):
        await self.contest.on_member_join(member)
        # onboarding-gated members can't take roles yet; the pending->done
        # flip in on_member_update covers them instead
        if member.guild.id == self.guild_id and not member.bot and not member.pending:
            await self._restore_roles(member)     # returning members keep roles
            if self.autorole_id:
                await self._grant_autorole(member)
        # welcome only once fully in (skip while onboarding-gated/pending)
        if not member.pending:
            await self._send_welcome(member)

    # -- role persistence --------------------------------------------------
    # Returning members keep the roles they left with (ladder rank included),
    # so leaving/rejoining never wipes progress. Point totals persist anyway
    # (Nadeko keys XP by user id). The detain role is excluded on purpose — the
    # Sentinel bot re-applies detentions from its DB so they can't be dodged.

    def _role_persist_load(self) -> dict:
        try:
            return json.loads((BASE_DIR / "role_persist.json").read_text())
        except Exception:
            return {}

    def _role_persist_save(self, data: dict) -> None:
        (BASE_DIR / "role_persist.json").write_text(json.dumps(data))

    def _snapshot_roles(self, member) -> None:
        if (not self.role_persist_enabled or member.bot
                or member.guild.id != self.guild_id):
            return
        ids = [r.id for r in member.roles
               if not r.is_default() and not r.managed and r.id != self.detain_role_id]
        store = self._role_persist_load()
        store[str(member.id)] = {"roles": ids, "ts": int(time.time())}
        try:
            self._role_persist_save(store)
            log.info("Snapshotted %d role(s) for departing %s", len(ids), member)
        except Exception as e:
            log.warning("Role snapshot save failed for %s: %s", member, e)

    async def _restore_roles(self, member) -> None:
        if (not self.role_persist_enabled or member.bot
                or member.guild.id != self.guild_id or member.id in self._restored):
            return
        rec = self._role_persist_load().get(str(member.id))
        if not rec:
            return
        self._restored.add(member.id)
        guild = member.guild
        me = guild.me
        roles = []
        for rid in rec.get("roles", []):
            role = guild.get_role(int(rid))
            if (role and not role.is_default() and not role.managed
                    and role.id != self.detain_role_id
                    and (me is None or role < me.top_role)):
                roles.append(role)
        if not roles:
            return
        try:
            await member.add_roles(*roles, reason="Role persistence: restored on rejoin")
            log.info("Restored %d role(s) for returning %s", len(roles), member)
        except discord.HTTPException as e:
            log.warning("Role restore failed for %s: %s", member, e)

    async def _send_welcome(self, member) -> None:
        """A brief, self-deleting greeting from the bot. Concise (details live
        in rules-and-info), not pushy, with a lowkey privacy note — the message
        clearing itself is itself the nod. Fires once per member."""
        if (not self.welcome_enabled or not self.general_channel_id
                or member.bot or member.guild.id != self.guild_id
                or member.id in self._welcomed):
            return
        channel = self.get_channel(self.general_channel_id)
        if channel is None:
            return
        self._welcomed.add(member.id)
        msg = (
            f"Welcome {member.mention} 👋  Glad to have you in "
            f"**Politics & Philosophy**. We're a Europe-centric community that's open "
            f"for discussion on politics, philosophy, and the ideas shaping our world. "
            f"We value freedom of speech and a privacy-conscious culture."
        )
        if self.book_club_channel_id:
            msg += (f"\n\nFeel free to jump into a voice chat or create your own here, "
                    f"and check out <#{self.book_club_channel_id}> if you have book "
                    f"recommendations or you're into reading and discussing books.")
        else:
            msg += "\n\nFeel free to jump into a voice chat or create your own here."
        try:
            await channel.send(msg, delete_after=self.welcome_delete_after)
            log.info("Welcomed %s", member)
        except discord.HTTPException as e:
            log.warning("Welcome failed for %s: %s", member, e)

    async def _grant_autorole(self, member):
        role = member.guild.get_role(self.autorole_id)
        if role is None or role in member.roles \
                or any(r.id in self.ladder for r in member.roles):
            return
        try:
            await member.add_roles(role, reason="Base rank: everyone starts as Novice")
            log.info("Autorole: %s -> %s", role.name, member)
        except discord.HTTPException as e:
            log.warning("Autorole failed for %s: %s", member, e)

    async def on_member_remove(self, member):
        await self.contest.on_member_remove(member)
        self._snapshot_roles(member)   # remember roles for a possible return

    async def on_invite_create(self, invite):
        await self.contest.on_invite_create(invite)

    async def on_invite_delete(self, invite):
        await self.contest.on_invite_delete(invite)

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
        if after.guild.id != self.guild_id:
            return
        if (not after.bot and before.pending and not after.pending):
            await self._restore_roles(after)   # onboarding done -> restore roles
            if self.autorole_id:
                await self._grant_autorole(after)
            await self._send_welcome(after)  # onboarding done -> greet now
        if not self.ladder:
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
        lower_ids = list(self.ladder[:top])
        if self.autorole_id:
            lower_ids.append(self.autorole_id)  # first rank supersedes Novice
        lower = [after.guild.get_role(r) for r in lower_ids]
        lower = [r for r in lower if r and r in after.roles]
        if lower:
            try:
                await after.remove_roles(*lower, reason="Ladder promotion: superseded rank removed")
                log.info("Promoted %s: removed %s", after, [r.name for r in lower])
            except discord.HTTPException as e:
                log.warning("Ladder cleanup failed for %s: %s", after, e)

    async def _rank_reconcile_sweep(self, guild):
        """Heal rank state that drifted while the helper was offline.

        Nadeko keeps granting XP roles whether or not we're up, and members
        keep joining — so on startup: anyone holding several rungs keeps only
        the highest (offline promotions stand; tenure gates apply live only),
        Novice is stripped from ranked members, and rankless humans get it.
        """
        import asyncio

        await asyncio.sleep(15)  # let the member cache finish chunking
        rung_set = set(self.ladder)
        novice = guild.get_role(self.autorole_id) if self.autorole_id else None
        fixed = 0
        for member in guild.members:
            if member.bot or member.pending:
                continue
            held = [r for r in member.roles if r.id in rung_set]
            try:
                if held:
                    top = max(held, key=lambda r: self.ladder.index(r.id))
                    stale = [r for r in held if r != top]
                    if novice and novice in member.roles:
                        stale.append(novice)
                    if stale:
                        await member.remove_roles(
                            *stale, reason="Rank sweep: superseded rank held from downtime")
                        fixed += 1
                elif novice and novice not in member.roles:
                    await member.add_roles(
                        novice, reason="Rank sweep: joined while helper was down")
                    fixed += 1
            except discord.HTTPException as e:
                log.warning("Rank sweep failed for %s: %s", member, e)
            if fixed and fixed % 10 == 0:
                await asyncio.sleep(2)  # stay polite to the rate limiter
        log.info("Rank sweep done: %d member(s) reconciled", fixed)

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
        self._afk_track(member, after)
        # spawn a room (debounced: duplicate voice events arrive while moving)
        if after.channel and after.channel.id == self.trigger_id:
            if member.id in self._spawning:
                return
            self._spawning.add(member.id)
            guild = member.guild
            category = guild.get_channel(self.category_id)
            name = f"{TEMP_VC_PREFIX}{member.display_name}'s room"[:100]
            overwrites = {
                member: vc_rooms._owner_overwrite()
            }
            try:
                vc = await guild.create_voice_channel(
                    name, category=category, overwrites=overwrites,
                    bitrate=int(guild.bitrate_limit),  # best audio the tier allows
                    reason=f"Join-to-Create: room for {member}",
                )
                self.temp_vcs[vc.id] = time.monotonic()
                await member.move_to(vc, reason="Join-to-Create")
                await self.rooms.setup_room(vc, member)
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

    # -- AFK parking -------------------------------------------------------
    # Mic muted nonstop for AFK_AFTER_MINUTES -> parked in the AFK channel,
    # where a channel overwrite denies Speak/Stream/Video for everyone.
    # Streaming or camera-on counts as present, not AFK. The timer survives
    # channel hops (still muted = still away) and resets on unmute/disconnect.

    def _afk_track(self, member, state) -> None:
        if not self.afk_channel_id:
            return
        away = (state.channel is not None
                and state.channel.id != self.afk_channel_id
                and (state.self_mute or state.mute or state.self_deaf or state.deaf)
                and not (state.self_stream or state.self_video))
        if away:
            self._muted_since.setdefault(member.id, time.time())
        else:
            self._muted_since.pop(member.id, None)

    async def _afk_loop(self):
        import asyncio

        while True:
            await asyncio.sleep(60)
            try:
                guild = self.get_guild(self.guild_id)
                afk = guild.get_channel(self.afk_channel_id) if guild else None
                if afk is None:
                    continue
                now = time.time()

                # alone tracking: sole human in a channel, any mute state
                still_alone: set[int] = set()
                for vc in guild.voice_channels:
                    if vc.id in (self.afk_channel_id, self.trigger_id):
                        continue
                    humans = [m for m in vc.members if not m.bot]
                    if len(humans) == 1:
                        still_alone.add(humans[0].id)
                        self._alone_since.setdefault(humans[0].id, now)
                for uid in list(self._alone_since):
                    if uid not in still_alone:
                        self._alone_since.pop(uid)

                async def park(member, why: str) -> None:
                    try:
                        await member.move_to(afk, reason=f"AFK: {why}")
                        await self._try_dm(
                            member,
                            f"💤 You were moved to **{afk.name}** in **{guild.name}** "
                            f"after {why}. Join any channel when you're back!")
                        log.info("AFK-parked %s (%s)", member, why)
                    except discord.HTTPException as e:
                        log.warning("AFK move failed for %s: %s", member, e)

                for uid, since in list(self._muted_since.items()):
                    if now - since < self.afk_after:
                        continue
                    member = guild.get_member(uid)
                    v = member.voice if member else None
                    # re-check live state: the dict can lag a missed event
                    if (v is None or v.channel is None or v.channel.id == afk.id
                            or not (v.self_mute or v.mute or v.self_deaf or v.deaf)
                            or v.self_stream or v.self_video):
                        self._muted_since.pop(uid, None)
                        continue
                    self._muted_since.pop(uid, None)
                    self._alone_since.pop(uid, None)
                    await park(member, f"{self.afk_after // 3600} hours muted")

                for uid, since in list(self._alone_since.items()):
                    if now - since < self.afk_alone_after:
                        continue
                    member = guild.get_member(uid)
                    v = member.voice if member else None
                    if (v is None or v.channel is None or v.channel.id == afk.id
                            or len([m for m in v.channel.members if not m.bot]) != 1):
                        self._alone_since.pop(uid, None)
                        continue
                    self._alone_since.pop(uid, None)
                    self._muted_since.pop(uid, None)
                    await park(member, f"{self.afk_alone_after // 3600} hours alone in a call")
            except Exception:
                log.exception("AFK loop error")

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
            self.rooms.forget_room(channel.id)
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
