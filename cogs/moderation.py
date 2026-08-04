"""Moderation: detain/undetain, bans, kicks, timeouts, purge, warnings, channel locks."""

import logging
import re
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("sentinel.moderation")

GREEN = 0x2ECC71
RED = 0xE74C3C
ORANGE = 0xE67E22
BLUE = 0x3498DB


def now() -> datetime:
    return datetime.now(timezone.utc)


def mod_embed(title: str, description: str, color: int, user_id: int | None = None) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color, timestamp=now())
    if user_id:
        embed.set_footer(text=f"User ID: {user_id}")
    return embed


# -- staff tiers -----------------------------------------------------------
# Two-tier staff model driven by config role ids, so moderators need NO
# dangerous Discord permissions — the bot acts with its own permissions and
# every action is logged. ADMIN_ROLE_IDS (or the Discord administrator
# permission) unlocks everything; MOD_ROLE_IDS unlocks the day-to-day tools
# (warn/detain/timeout/limited prune) but not bans, kicks or channel controls.

def _role_ids(bot, key: str) -> set[int]:
    return {int(r) for r in bot.config.get(key, [])}


def is_staff(bot, member: discord.Member, *, admin_only: bool = False) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator or member.guild.owner_id == member.id:
        return True
    held = {r.id for r in member.roles}
    if held & _role_ids(bot, "ADMIN_ROLE_IDS"):
        return True
    return not admin_only and bool(held & _role_ids(bot, "MOD_ROLE_IDS"))


def staff_command(*, admin_only: bool = False):
    """Gate a hybrid command to the staff tiers (works for prefix and slash)."""

    async def predicate(ctx: commands.Context) -> bool:
        if is_staff(ctx.bot, ctx.author, admin_only=admin_only):
            return True
        raise commands.MissingPermissions(["staff"])

    return commands.check(predicate)


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # user ids whose next detain-role change was made by a bot command,
        # so on_member_update shouldn't log it a second time
        self._suppress_role_log: set[int] = set()

    # -- helpers -----------------------------------------------------------

    @property
    def mod_log(self) -> discord.TextChannel | None:
        return self.bot.get_channel(self.bot.channel_id("MOD_LOG_CHANNEL_ID"))

    @property
    def detain_log(self) -> discord.TextChannel | None:
        return (
            self.bot.get_channel(self.bot.channel_id("DETAIN_LOG_CHANNEL_ID"))
            or self.mod_log
        )

    def detain_role(self, guild: discord.Guild) -> discord.Role | None:
        return guild.get_role(self.bot.channel_id("DETAIN_ROLE_ID"))

    async def send_log(self, embed: discord.Embed, channel: discord.TextChannel | None = None) -> None:
        channel = channel or self.mod_log
        if channel:
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                log.warning("Failed to send mod log embed")

    @staticmethod
    async def try_dm(user: discord.abc.User, message: str) -> None:
        try:
            await user.send(message)
        except discord.HTTPException:
            pass

    def prune_cap(self, actor: discord.Member) -> int:
        """Admins may prune up to 100; moderators are capped (anti mass-delete)."""
        if is_staff(self.bot, actor, admin_only=True):
            return 100
        return int(self.bot.config.get("MOD_PRUNE_LIMIT", 15))

    def check_hierarchy(self, actor: discord.Member, target: discord.Member) -> str | None:
        if target == actor:
            return "You can't moderate yourself."
        if target.guild.owner_id == target.id:
            return "You can't moderate the server owner."
        # staff shield: admins are untouchable except by the owner, and staff
        # can only be moderated by the admin tier (role positions don't matter)
        if is_staff(self.bot, target, admin_only=True) and actor.guild.owner_id != actor.id:
            return "Only the server owner can moderate an admin."
        if is_staff(self.bot, target) and not is_staff(self.bot, actor, admin_only=True):
            return "Moderators can't moderate other staff."
        if actor.guild.owner_id != actor.id and not is_staff(self.bot, actor, admin_only=True) \
                and target.top_role >= actor.top_role:
            return "You can't moderate someone with an equal or higher role."
        me = target.guild.me
        if target.top_role >= me.top_role:
            return "My role is too low to moderate that member — move my role higher."
        return None

    # -- detain ------------------------------------------------------------

    @commands.hybrid_command(name="detain", aliases=["d"], description="Detain a member: restrict them to the detainment channels.")
    @app_commands.describe(member="Member to detain", reason="Why they are being detained")
    @app_commands.default_permissions(manage_roles=True)
    @staff_command()
    async def detain(self, ctx: commands.Context, member: discord.Member, *, reason: str | None = None):
        role = self.detain_role(ctx.guild)
        if not role:
            return await ctx.reply("⚠️ Detain role not configured — set DETAIN_ROLE_ID in config.json.")
        err = self.check_hierarchy(ctx.author, member)
        if err:
            return await ctx.reply(f"⛔ {err}")
        if role in member.roles:
            return await ctx.reply(f"ℹ️ {member.mention} is already detained.")
        self._suppress_role_log.add(member.id)
        await member.add_roles(role, reason=f"Detained by {ctx.author}: {reason or 'no reason'}")
        if member.voice:
            try:
                await member.move_to(None, reason="Detained — disconnected from voice")
            except discord.HTTPException:
                pass
        await self.bot.db.open_detention(ctx.guild.id, member.id, ctx.author.id, reason)
        await ctx.reply(f"⛓️ {member.mention} has been detained.")
        await self.try_dm(member, f"You have been detained in **{ctx.guild.name}**.\nReason: {reason or 'not specified'}")
        await self.send_log(
            mod_embed("⛓️ User Detained",
                      f"{member.mention} detained by {ctx.author.mention}.\n**Reason:** {reason or '—'}",
                      RED, member.id),
            self.detain_log,
        )

    @commands.hybrid_command(name="undetain", aliases=["ud"], description="Release a detained member.")
    @app_commands.describe(member="Member to release", reason="Why they are being released")
    @app_commands.default_permissions(manage_roles=True)
    @staff_command()
    async def undetain(self, ctx: commands.Context, member: discord.Member, *, reason: str | None = None):
        role = self.detain_role(ctx.guild)
        if not role:
            return await ctx.reply("⚠️ Detain role not configured — set DETAIN_ROLE_ID in config.json.")
        if role not in member.roles:
            return await ctx.reply(f"ℹ️ {member.mention} is not detained.")
        self._suppress_role_log.add(member.id)
        await member.remove_roles(role, reason=f"Undetained by {ctx.author}: {reason or 'no reason'}")
        await self.bot.db.close_detention(ctx.guild.id, member.id)
        await ctx.reply(f"🕊️ {member.mention} has been released.")
        await self.try_dm(member, f"You have been released from detainment in **{ctx.guild.name}**.")
        await self.send_log(
            mod_embed("🕊️ User Released",
                      f"{member.mention} released by {ctx.author.mention}.\n**Reason:** {reason or '—'}",
                      GREEN, member.id),
            self.detain_log,
        )

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Log detain role changes made manually (outside bot commands)."""
        role = self.detain_role(after.guild)
        if not role:
            return
        had, has = role in before.roles, role in after.roles
        if had == has:
            return
        if after.id in self._suppress_role_log:
            self._suppress_role_log.discard(after.id)
            return
        if has:
            await self.bot.db.open_detention(after.guild.id, after.id, None, "role added manually")
            await self.try_dm(after, f"You have been detained in **{after.guild.name}**.")
            await self.send_log(
                mod_embed("⛓️ User Detained", f"{after.mention} was detained (role added).", RED, after.id),
                self.detain_log,
            )
        else:
            await self.bot.db.close_detention(after.guild.id, after.id)
            await self.try_dm(after, f"You have been released from detainment in **{after.guild.name}**.")
            await self.send_log(
                mod_embed("🕊️ User Released", f"{after.mention} was released (role removed).", GREEN, after.id),
                self.detain_log,
            )

    @app_commands.command(name="detainhistory", description="Show a member's detention history.")
    @app_commands.default_permissions(manage_roles=True)
    async def detainhistory(self, interaction: discord.Interaction, member: discord.Member):
        rows = await self.bot.db.detention_history(interaction.guild_id, member.id)
        if not rows:
            return await interaction.response.send_message(f"{member.mention} has no detention history.", ephemeral=True)
        lines = []
        for mod_id, reason, detained_at, released_at in rows[-15:]:
            mod = f"<@{mod_id}>" if mod_id else "manual"
            status = f"released {released_at[:10]}" if released_at else "**active**"
            lines.append(f"• {detained_at[:10]} by {mod} — {reason or 'no reason'} ({status})")
        embed = mod_embed(f"Detention history — {member.display_name}", "\n".join(lines), BLUE, member.id)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # -- bans / kicks ------------------------------------------------------

    @property
    def ban_log(self) -> discord.TextChannel | None:
        return (
            self.bot.get_channel(self.bot.channel_id("BAN_LOG_CHANNEL_ID"))
            or self.mod_log
        )

    @commands.hybrid_command(name="ban", aliases=["b"], description="Ban a member.")
    @app_commands.describe(member="Member to ban", reason="Reason")
    @app_commands.default_permissions(ban_members=True)
    @staff_command(admin_only=True)
    async def ban(self, ctx: commands.Context, member: discord.Member, *, reason: str | None = None):
        err = self.check_hierarchy(ctx.author, member)
        if err:
            return await ctx.reply(f"⛔ {err}", ephemeral=True)
        await self.try_dm(member, f"You have been banned from **{ctx.guild.name}**.\nReason: {reason or 'not specified'}")
        await member.ban(reason=f"{ctx.author}: {reason or 'no reason'}")
        await ctx.reply(f"🔨 {member.mention} banned.")
        await self.send_log(mod_embed("🔨 Member Banned",
                                      f"{member.mention} banned by {ctx.author.mention}.\n**Reason:** {reason or '—'}",
                                      RED, member.id), self.ban_log)

    @commands.hybrid_command(name="unban", aliases=["ub"], description="Unban a user by ID or name.")
    @app_commands.describe(user="User ID or username", reason="Reason")
    @app_commands.default_permissions(ban_members=True)
    @staff_command(admin_only=True)
    async def unban(self, ctx: commands.Context, user: str, *, reason: str | None = None):
        target = None
        if user.isdigit():
            try:
                target = await self.bot.fetch_user(int(user))
            except discord.NotFound:
                pass
        if target is None:
            async for entry in ctx.guild.bans(limit=None):
                if str(entry.user) == user or entry.user.name == user:
                    target = entry.user
                    break
        if target is None:
            return await ctx.reply("❌ User not found in ban list.", ephemeral=True)
        await ctx.guild.unban(target, reason=f"{ctx.author}: {reason or 'no reason'}")
        await ctx.reply(f"🕊️ {target.mention} unbanned.")
        await self.send_log(mod_embed("🕊️ Member Unbanned",
                                      f"{target.mention} unbanned by {ctx.author.mention}.\n**Reason:** {reason or '—'}",
                                      GREEN, target.id), self.ban_log)

    @commands.hybrid_command(name="kick", aliases=["k"], description="Kick a member from the server.")
    @app_commands.describe(member="Member to kick", reason="Reason")
    @app_commands.default_permissions(kick_members=True)
    @staff_command(admin_only=True)
    async def kick(self, ctx: commands.Context, member: discord.Member, *, reason: str | None = None):
        err = self.check_hierarchy(ctx.author, member)
        if err:
            return await ctx.reply(f"⛔ {err}", ephemeral=True)
        await self.try_dm(member, f"You have been kicked from **{ctx.guild.name}**.\nReason: {reason or 'not specified'}")
        await member.kick(reason=f"{ctx.author}: {reason or 'no reason'}")
        await ctx.reply(f"👢 {member.mention} kicked.")
        await self.send_log(mod_embed("👢 Member Kicked",
                                      f"{member.mention} kicked by {ctx.author.mention}.\n**Reason:** {reason or '—'}",
                                      ORANGE, member.id), self.ban_log)

    # -- timeouts ----------------------------------------------------------

    @commands.hybrid_command(name="timeout", aliases=["t"], description="Time a member out (minutes, max 28 days).")
    @app_commands.describe(member="Member", minutes="Duration in minutes (max 40320)", reason="Reason")
    @app_commands.default_permissions(moderate_members=True)
    @staff_command()
    async def timeout(self, ctx: commands.Context, member: discord.Member, minutes: commands.Range[int, 1, 40320], *, reason: str | None = None):
        err = self.check_hierarchy(ctx.author, member)
        if err:
            return await ctx.reply(f"⛔ {err}", ephemeral=True)
        until = now() + timedelta(minutes=minutes)
        await member.timeout(until, reason=f"{ctx.author}: {reason or 'no reason'}")
        await ctx.reply(f"⏳ {member.mention} timed out for {minutes} min.")
        await self.try_dm(member, f"You have been timed out in **{ctx.guild.name}** for {minutes} minutes.\nReason: {reason or 'not specified'}")
        await self.send_log(mod_embed("⏳ Member Timed Out",
                                      f"{member.mention} timed out by {ctx.author.mention} until <t:{int(until.timestamp())}:f>.\n**Reason:** {reason or '—'}",
                                      ORANGE, member.id))

    @commands.hybrid_command(name="untimeout", aliases=["ut"], description="Remove a member's timeout.")
    @app_commands.default_permissions(moderate_members=True)
    @staff_command()
    async def untimeout(self, ctx: commands.Context, member: discord.Member, *, reason: str | None = None):
        await member.timeout(None, reason=f"{ctx.author}: {reason or 'no reason'}")
        await ctx.reply(f"✅ Timeout removed for {member.mention}.")
        await self.send_log(mod_embed("✅ Timeout Removed",
                                      f"{member.mention}'s timeout removed by {ctx.author.mention}.",
                                      GREEN, member.id))

    # -- warnings ----------------------------------------------------------

    @commands.hybrid_command(name="warn", aliases=["w"], description="Warn a member (recorded permanently).")
    @app_commands.describe(member="Member to warn", reason="Reason")
    @app_commands.default_permissions(moderate_members=True)
    @staff_command()
    async def warn(self, ctx: commands.Context, member: discord.Member, *, reason: str):
        err = self.check_hierarchy(ctx.author, member)
        if err:
            return await ctx.reply(f"⛔ {err}")
        warning_id = await self.bot.db.add_warning(ctx.guild.id, member.id, ctx.author.id, reason)
        count = len(await self.bot.db.warnings_for(ctx.guild.id, member.id))
        # unlike other command replies this one stays in the channel: the
        # invocation is cleaned up but the warning itself remains visible
        await ctx.send(
            f"⚠️ {member.mention} — **warning #{count}**\n**Reason:** {reason}",
            delete_after=None,
        )
        await self.try_dm(member, f"You have been warned in **{ctx.guild.name}**.\nReason: {reason}\nTotal warnings: {count}")
        await self.send_log(mod_embed("⚠️ Member Warned",
                                      f"{member.mention} warned by {ctx.author.mention} (#{warning_id}, total {count}).\n**Reason:** {reason}",
                                      ORANGE, member.id))

    @commands.hybrid_command(name="warnings", aliases=["ws"], description="List a member's warnings.")
    @app_commands.default_permissions(moderate_members=True)
    @staff_command()
    async def warnings(self, ctx: commands.Context, member: discord.Member):
        rows = await self.bot.db.warnings_for(ctx.guild.id, member.id)
        if not rows:
            return await ctx.reply(f"{member.mention} has no warnings. 🎉", ephemeral=True)
        lines = [f"• **#{wid}** {created[:10]} by <@{mod_id}> — {reason or 'no reason'}" for wid, mod_id, reason, created in rows[-20:]]
        embed = mod_embed(f"Warnings — {member.display_name} ({len(rows)})", "\n".join(lines), BLUE, member.id)
        await ctx.reply(embed=embed, ephemeral=True)

    @commands.hybrid_command(name="clearwarnings", aliases=["cw"], description="Clear all warnings for a member.")
    @app_commands.default_permissions(administrator=True)
    @staff_command(admin_only=True)
    async def clearwarnings(self, ctx: commands.Context, member: discord.Member):
        n = await self.bot.db.clear_warnings(ctx.guild.id, member.id)
        await ctx.reply(f"🧹 Cleared {n} warning(s) for {member.mention}.")
        await self.send_log(mod_embed("🧹 Warnings Cleared",
                                      f"{n} warning(s) for {member.mention} cleared by {ctx.author.mention}.",
                                      BLUE, member.id))

    # -- channel tools -----------------------------------------------------

    async def _do_purge(self, ctx_or_itx, channel, author, amount: int, member,
                        text: str | None = None, negate: bool = False):
        needle = (text or "").casefold()

        def check(m):
            if m.pinned:
                return False  # pins are curated; never bulk-delete them
            if member and m.author.id != member.id:
                return False
            if needle:
                has = needle in (m.content or "").casefold()
                return not has if negate else has
            return True

        deleted = await channel.purge(limit=amount, check=check, reason=f"Purge by {author}")
        detail = (f" (from {member.mention})" if member else "") \
            + (f" ({'NOT ' if negate else ''}containing {text!r})" if text else "")
        await self.send_log(mod_embed("🧹 Messages Purged",
                                      f"{len(deleted)} message(s) purged in {channel.mention} "
                                      f"by {author.mention}{detail}.",
                                      BLUE))
        return len(deleted)

    @app_commands.command(name="purge", description="Delete the last N messages in this channel (max 100).")
    @app_commands.describe(amount="How many messages to scan", member="Only delete this member's messages",
                           contains="Only delete messages containing this text",
                           not_contains="Only delete messages NOT containing this text")
    @app_commands.default_permissions(manage_messages=True)
    async def purge_slash(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100],
                          member: discord.Member | None = None,
                          contains: str | None = None, not_contains: str | None = None):
        cap = self.prune_cap(interaction.user)
        if amount > cap:
            return await interaction.response.send_message(
                f"⛔ Moderators can prune at most **{cap}** messages at a time.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        text, negate = (not_contains, True) if not_contains else (contains, False)
        n = await self._do_purge(interaction, interaction.channel, interaction.user,
                                 amount, member, text, negate)
        await interaction.followup.send(f"🧹 Deleted {n} message(s).", ephemeral=True)

    @commands.command(name="purge", aliases=["prune", "p"])
    @staff_command()
    async def purge_prefix(self, ctx: commands.Context, *, args: str = ""):
        """.p N | .p @user N | .p [N] text | .p [N] !text — any order.

        Text filter deletes only messages containing it (or NOT containing it
        with a leading !), case-insensitive, scanning the last N messages.

        Admin tier only: `.p <@user|id> all` (or a big N) sweeps that user's
        ENTIRE history in the channel — paginating past the 100-message and
        14-day limits with a rate-limit-aware queue that runs in the
        background. A bare id works even if the user already left.
        """
        deep = False
        amount = None
        member = None
        user_id = None
        rest: list[str] = []
        for tok in args.split():
            low = tok.lower()
            if low in ("all", "everything", "*"):
                deep = True
            elif amount is None and tok.isdigit() and len(tok) < 6:
                amount = int(tok)
            elif member is None and (m := re.fullmatch(r"<@!?(\d+)>", tok)):
                member = ctx.guild.get_member(int(m.group(1)))
                user_id = int(m.group(1))
            elif user_id is None and tok.isdigit() and len(tok) >= 6:  # raw id
                user_id = int(tok)
                member = ctx.guild.get_member(user_id)
            else:
                rest.append(tok)
        text = " ".join(rest).strip()
        negate = text.startswith("!")
        text = text.lstrip("!").strip().strip('"“”\'').strip() or None

        is_admin = is_staff(self.bot, ctx.author, admin_only=True)
        # Deep per-user sweep: admin tier, a user target, and either `all` or a
        # count above the normal cap. Runs in the background over full history.
        if user_id is not None and (deep or (amount is not None and amount > self.prune_cap(ctx.author))):
            if not is_admin:
                return await ctx.reply("⛔ Full-history user purge is admin-tier only.")
            limit = 100_000 if deep else amount
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass
            self.bot.loop.create_task(
                self._deep_purge_user(ctx.channel, user_id, limit, ctx.author, text, negate))
            who = member.mention if member else f"`{user_id}`"
            return await ctx.send(
                f"🧹 Queued a full-history purge of {who}'s messages"
                + (f" containing **{text}**" if text else "")
                + ". This runs in the background and can take a while; I'll report when done.",
                delete_after=30)

        cap = self.prune_cap(ctx.author)
        if amount is None:
            amount = cap
        if amount > cap:
            return await ctx.reply(f"⛔ Moderators can prune at most **{cap}** messages at a time.")
        amount = max(1, min(100, amount))
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass
        n = await self._do_purge(ctx, ctx.channel, ctx.author, amount, member, text, negate)
        what = (f" containing **{text}**" if text and not negate else
                f" not containing **{text}**" if text else "")
        await ctx.send(f"🧹 Deleted {n} message(s){what}.", delete_after=5)

    async def _deep_purge_user(self, channel, user_id: int, limit: int, actor,
                               text: str | None = None, negate: bool = False):
        """Delete up to `limit` of one user's messages across the whole channel
        history. Individual deletes with 429 backoff, so it bypasses the
        100-message bulk cap and the 14-day age limit. Bursts via bulk-delete
        for recent (<14d) messages, falls back to single deletes for old ones."""
        import asyncio
        from datetime import datetime, timedelta, timezone

        needle = (text or "").casefold()

        def match(m):
            if m.author.id != user_id or m.pinned:
                return False
            if needle:
                has = needle in (m.content or "").casefold()
                return (not has) if negate else has
            return True

        deleted = 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=13, hours=12)
        recent_batch: list = []
        try:
            async for msg in channel.history(limit=None, oldest_first=False):
                if deleted >= limit:
                    break
                if not match(msg):
                    continue
                if msg.created_at > cutoff:
                    recent_batch.append(msg)
                    if len(recent_batch) >= 100:
                        try:
                            await channel.delete_messages(recent_batch)
                            deleted += len(recent_batch)
                        except discord.HTTPException:
                            for m in recent_batch:  # fall back to singles
                                try:
                                    await m.delete(); deleted += 1
                                    await asyncio.sleep(1.0)
                                except discord.HTTPException:
                                    pass
                        recent_batch.clear()
                        await asyncio.sleep(1.0)
                else:
                    for attempt in range(6):
                        try:
                            await msg.delete(); deleted += 1
                            await asyncio.sleep(1.0)  # old-message delete bucket
                            break
                        except discord.HTTPException as e:
                            if getattr(e, "status", None) == 429:
                                await asyncio.sleep(float(getattr(e, "retry_after", 3)) + 0.5)
                            else:
                                break
            if recent_batch:
                try:
                    await channel.delete_messages(recent_batch)
                    deleted += len(recent_batch)
                except discord.HTTPException:
                    for m in recent_batch:
                        try:
                            await m.delete(); deleted += 1
                            await asyncio.sleep(1.0)
                        except discord.HTTPException:
                            pass
        except Exception:
            log.exception("Deep purge error")
        await self.send_log(mod_embed(
            "🧹 Full-History Purge",
            f"Removed **{deleted}** message(s) from user `{user_id}` in {channel.mention}, "
            f"requested by {actor.mention}" + (f" (filter: {text!r})" if text else "") + ".",
            BLUE))
        try:
            await channel.send(f"🧹 Full-history purge complete: **{deleted}** message(s) removed.",
                               delete_after=30)
        except discord.HTTPException:
            pass

    @app_commands.command(name="slowmode", description="Set slowmode delay for this channel (seconds; 0 to disable).")
    @app_commands.default_permissions(manage_channels=True)
    async def slowmode(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]):
        await interaction.channel.edit(slowmode_delay=seconds, reason=f"Slowmode by {interaction.user}")
        msg = f"🐌 Slowmode set to {seconds}s." if seconds else "🚀 Slowmode disabled."
        await interaction.response.send_message(msg)

    @app_commands.command(name="lock", description="Lock this channel (deny @everyone Send Messages).")
    @app_commands.default_permissions(manage_channels=True)
    async def lock(self, interaction: discord.Interaction, reason: str | None = None):
        overwrite = interaction.channel.overwrites_for(interaction.guild.default_role)
        overwrite.send_messages = False
        await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=f"Lock by {interaction.user}: {reason or 'no reason'}")
        await interaction.response.send_message("🔒 Channel locked.")
        await self.send_log(mod_embed("🔒 Channel Locked",
                                      f"{interaction.channel.mention} locked by {interaction.user.mention}.\n**Reason:** {reason or '—'}",
                                      RED))

    @app_commands.command(name="unlock", description="Unlock this channel.")
    @app_commands.default_permissions(manage_channels=True)
    async def unlock(self, interaction: discord.Interaction):
        overwrite = interaction.channel.overwrites_for(interaction.guild.default_role)
        overwrite.send_messages = None
        await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=f"Unlock by {interaction.user}")
        await interaction.response.send_message("🔓 Channel unlocked.")
        await self.send_log(mod_embed("🔓 Channel Unlocked",
                                      f"{interaction.channel.mention} unlocked by {interaction.user.mention}.",
                                      GREEN))

    # -- error handling ----------------------------------------------------

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("⛔ You don't have permission to do that.")
        elif isinstance(error, commands.MemberNotFound):
            await ctx.reply("❌ Member not found.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.reply(f"Usage: `.{ctx.command.name} @user [reason]`")
        else:
            log.exception("Command error in %s", ctx.command, exc_info=error)
            await ctx.reply("💥 Something went wrong — check the logs.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
