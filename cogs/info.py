"""Informational commands: credits, help, serverinfo, userinfo."""

from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

BLUE = 0x3498DB


class Info(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="credits", description="Bot creator and license information.")
    async def credits(self, ctx: commands.Context):
        embed = discord.Embed(
            title=self.bot.config.get("BOT_NAME", "Sentinel"),
            description=(
                f"Voice management & moderation bot for **{ctx.guild.name}**.\n\n"
                "Built by **Artur Grochau** — [GitHub](https://github.com/arturgrochau/discord-voice-manager)\n"
                "Licensed under the MIT License."
            ),
            color=BLUE,
        )
        await ctx.reply(embed=embed)

    @app_commands.command(name="userinfo", description="Show info about a member.")
    async def userinfo(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        embed = discord.Embed(title=member.display_name, color=member.color or BLUE)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Username", value=str(member), inline=True)
        embed.add_field(name="ID", value=str(member.id), inline=True)
        embed.add_field(name="Joined", value=f"<t:{int(member.joined_at.timestamp())}:R>" if member.joined_at else "?", inline=True)
        embed.add_field(name="Created", value=f"<t:{int(member.created_at.timestamp())}:R>", inline=True)
        roles = [r.mention for r in reversed(member.roles[1:])][:15]
        embed.add_field(name=f"Roles ({len(member.roles) - 1})", value=" ".join(roles) or "—", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="serverinfo", description="Show info about this server.")
    async def serverinfo(self, interaction: discord.Interaction):
        g = interaction.guild
        embed = discord.Embed(title=g.name, color=BLUE)
        if g.icon:
            embed.set_thumbnail(url=g.icon.url)
        embed.add_field(name="Members", value=str(g.member_count), inline=True)
        embed.add_field(name="Channels", value=str(len(g.channels)), inline=True)
        embed.add_field(name="Roles", value=str(len(g.roles)), inline=True)
        embed.add_field(name="Owner", value=f"<@{g.owner_id}>", inline=True)
        embed.add_field(name="Created", value=f"<t:{int(g.created_at.timestamp())}:D>", inline=True)
        embed.add_field(name="Boosts", value=str(g.premium_subscription_count), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Info(bot))
