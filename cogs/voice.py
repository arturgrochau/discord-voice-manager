"""Voice channel management: auto-unmute on join/switch, voice mute logging."""

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("sentinel.voice")

GREEN = 0x2ECC71
RED = 0xE74C3C
ORANGE = 0xE67E22


def now() -> datetime:
    return datetime.now(timezone.utc)


class Voice(commands.Cog):
    """Auto-unmute stale server mutes and log all voice mute activity."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def voice_log(self) -> discord.TextChannel | None:
        return self.bot.get_channel(self.bot.channel_id("VOICE_LOG_CHANNEL_ID"))

    async def send_log(self, embed: discord.Embed) -> None:
        channel = self.voice_log
        if channel:
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                log.warning("Failed to send voice log embed")

    @staticmethod
    async def try_dm(member: discord.Member, message: str) -> None:
        try:
            await member.send(message)
        except discord.HTTPException:
            pass  # DMs closed

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return

        # Auto-unmute: a user who joins/switches channels while server-muted
        # usually carries a stale mute from a previous session. Skip users the
        # mods intentionally sticky-muted, and skip detained users.
        if (
            self.bot.config.get("AUTO_UNMUTE", True)
            and after.channel is not None
            and before.channel != after.channel
            and after.mute
        ):
            if await self.bot.db.is_sticky_muted(member.guild.id, member.id):
                return
            detain_role_id = self.bot.channel_id("DETAIN_ROLE_ID")
            if detain_role_id and member.get_role(detain_role_id):
                return
            try:
                await member.edit(mute=False, reason="Sentinel auto-unmute on channel join")
            except discord.HTTPException as e:
                log.warning("Auto-unmute failed for %s: %s", member, e)
                return
            embed = discord.Embed(
                title="🔊 Voice Auto-Unmute",
                description=f"{member.mention} was automatically unmuted after joining {after.channel.mention}.",
                color=GREEN,
                timestamp=now(),
            )
            embed.set_footer(text=f"User ID: {member.id}")
            await self.send_log(embed)
            await self.try_dm(member, "You were auto-unmuted upon joining a voice channel in Politics & Philosophy.")
            return

        # Log manual server mutes/unmutes with the acting moderator when findable.
        if before.channel is not None and after.channel is not None and before.mute != after.mute:
            actor = None
            try:
                async for entry in member.guild.audit_logs(
                    limit=5, action=discord.AuditLogAction.member_update
                ):
                    if entry.target and entry.target.id == member.id and entry.user != self.bot.user:
                        actor = entry.user
                        break
            except discord.Forbidden:
                pass
            action = "muted" if after.mute else "unmuted"
            desc = f"{member.mention} was {action}"
            if actor:
                desc += f" by {actor.mention}"
            embed = discord.Embed(
                title="🎙️ Voice Mute Update",
                description=desc + ".",
                color=ORANGE if after.mute else GREEN,
                timestamp=now(),
            )
            embed.set_footer(text=f"User ID: {member.id}")
            await self.send_log(embed)

    # -- commands ----------------------------------------------------------

    @app_commands.command(name="stickymute", description="Keep a user server-muted even when they rejoin (blocks auto-unmute).")
    @app_commands.describe(member="Member to sticky-mute", reason="Why")
    @app_commands.default_permissions(mute_members=True)
    async def stickymute(self, interaction: discord.Interaction, member: discord.Member, reason: str | None = None):
        await self.bot.db.set_sticky_mute(interaction.guild_id, member.id, True)
        if member.voice:
            try:
                await member.edit(mute=True, reason=f"Sticky mute by {interaction.user}: {reason or 'no reason'}")
            except discord.HTTPException:
                pass
        embed = discord.Embed(
            title="🔇 Sticky Mute",
            description=f"{member.mention} sticky-muted by {interaction.user.mention}.\n**Reason:** {reason or '—'}",
            color=RED,
            timestamp=now(),
        )
        embed.set_footer(text=f"User ID: {member.id}")
        await self.send_log(embed)
        await interaction.response.send_message(f"🔇 {member.mention} is now sticky-muted.", ephemeral=True)

    @app_commands.command(name="unstickymute", description="Remove a sticky mute and unmute the user.")
    @app_commands.default_permissions(mute_members=True)
    async def unstickymute(self, interaction: discord.Interaction, member: discord.Member):
        await self.bot.db.set_sticky_mute(interaction.guild_id, member.id, False)
        if member.voice and member.voice.mute:
            try:
                await member.edit(mute=False, reason=f"Sticky mute removed by {interaction.user}")
            except discord.HTTPException:
                pass
        embed = discord.Embed(
            title="🔊 Sticky Mute Removed",
            description=f"{member.mention} un-sticky-muted by {interaction.user.mention}.",
            color=GREEN,
            timestamp=now(),
        )
        embed.set_footer(text=f"User ID: {member.id}")
        await self.send_log(embed)
        await interaction.response.send_message(f"🔊 {member.mention} sticky mute removed.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Voice(bot))
