"""Moderation: detain/undetain, bans, kicks, timeouts, purge, warnings, channel locks."""

import logging
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

    @staticmethod
    def check_hierarchy(actor: discord.Member, target: discord.Member) -> str | None:
        if target == actor:
            return "You can't moderate yourself."
        if target.guild.owner_id == target.id:
            return "You can't moderate the server owner."
        if actor.guild.owner_id != actor.id and target.top_role >= actor.top_role:
            return "You can't moderate someone with an equal or higher role."
        me = target.guild.me
        if target.top_role >= me.top_role:
            return "My role is too low to moderate that member — move my role higher."
        return None

    # -- detain ------------------------------------------------------------

    @commands.hybrid_command(name="detain", aliases=["d"], description="Detain a member: restrict them to the detainment channels.")
    @app_commands.describe(member="Member to detain", reason="Why they are being detained")
    @app_commands.default_permissions(manage_roles=True)
    @commands.has_permissions(manage_roles=True)
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
    @commands.has_permissions(manage_roles=True)
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
    @commands.has_permissions(ban_members=True)
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
    @commands.has_permissions(ban_members=True)
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
    @commands.has_permissions(kick_members=True)
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
    @commands.has_permissions(moderate_members=True)
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
    @commands.has_permissions(moderate_members=True)
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
    @commands.has_permissions(moderate_members=True)
    async def warn(self, ctx: commands.Context, member: discord.Member, *, reason: str):
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
    @commands.has_permissions(moderate_members=True)
    async def warnings(self, ctx: commands.Context, member: discord.Member):
        rows = await self.bot.db.warnings_for(ctx.guild.id, member.id)
        if not rows:
            return await ctx.reply(f"{member.mention} has no warnings. 🎉", ephemeral=True)
        lines = [f"• **#{wid}** {created[:10]} by <@{mod_id}> — {reason or 'no reason'}" for wid, mod_id, reason, created in rows[-20:]]
        embed = mod_embed(f"Warnings — {member.display_name} ({len(rows)})", "\n".join(lines), BLUE, member.id)
        await ctx.reply(embed=embed, ephemeral=True)

    @commands.hybrid_command(name="clearwarnings", aliases=["cw"], description="Clear all warnings for a member.")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def clearwarnings(self, ctx: commands.Context, member: discord.Member):
        n = await self.bot.db.clear_warnings(ctx.guild.id, member.id)
        await ctx.reply(f"🧹 Cleared {n} warning(s) for {member.mention}.")
        await self.send_log(mod_embed("🧹 Warnings Cleared",
                                      f"{n} warning(s) for {member.mention} cleared by {ctx.author.mention}.",
                                      BLUE, member.id))

    # -- channel tools -----------------------------------------------------

    async def _do_purge(self, ctx_or_itx, channel, author, amount: int, member):
        check = (lambda m: m.author.id == member.id) if member else (lambda m: True)
        deleted = await channel.purge(limit=amount, check=check, reason=f"Purge by {author}")
        await self.send_log(mod_embed("🧹 Messages Purged",
                                      f"{len(deleted)} message(s) purged in {channel.mention} by {author.mention}"
                                      + (f" (from {member.mention})" if member else "") + ".",
                                      BLUE))
        return len(deleted)

    @app_commands.command(name="purge", description="Delete the last N messages in this channel (max 100).")
    @app_commands.describe(amount="How many messages", member="Only delete this member's messages")
    @app_commands.default_permissions(manage_messages=True)
    async def purge_slash(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100], member: discord.Member | None = None):
        await interaction.response.defer(ephemeral=True)
        n = await self._do_purge(interaction, interaction.channel, interaction.user, amount, member)
        await interaction.followup.send(f"🧹 Deleted {n} message(s).", ephemeral=True)

    @commands.command(name="purge", aliases=["prune", "p"])
    @commands.has_permissions(manage_messages=True)
    async def purge_prefix(self, ctx: commands.Context,
                           first: discord.Member | int,
                           second: discord.Member | int | None = None):
        """.prune N — delete last N messages; .prune @user N (either order) — only theirs."""
        amount = next((a for a in (first, second) if isinstance(a, int)), None)
        member = next((a for a in (first, second) if isinstance(a, discord.Member)), None)
        if amount is None:
            return await ctx.reply("Usage: `.prune N` or `.prune @user N`")
        amount = max(1, min(100, amount))
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass
        n = await self._do_purge(ctx, ctx.channel, ctx.author, amount, member)
        await ctx.send(f"🧹 Deleted {n} message(s).", delete_after=5)

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
