"""XP transparency: .level shows rank, progress, and time to the next rung.

Reads NadekoBot's database directly (read-only) plus the helper's ladder
config/state, so members always know exactly where they stand: total XP,
next role, XP still needed (with voice/text time estimates — voice XP is
deliberately ~3x faster), and any tenure gate on the promotion.

Sentinel config keys:
  NADEKO_DB_PATH — NadekoBot.db of this server's Nadeko instance
  HELPER_HOME    — directory holding politics_helper.json + ladder_state.json
"""

import json
import math
import sqlite3
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

GOLD = 0xF1C40F

# Nadeko v7 XP curve: cost(L -> L+1) = 9(L+1) + 27  =>  total(L) = 4.5L^2 + 31.5L
def level_from_xp(xp: int) -> int:
    return int((-31.5 + math.sqrt(31.5 ** 2 + 18 * xp)) // 9)


def total_xp_for_level(level: int) -> int:
    return int(4.5 * level * level + 31.5 * level)


def bar(frac: float, width: int = 12) -> str:
    filled = round(max(0.0, min(1.0, frac)) * width)
    return "▰" * filled + "▱" * (width - filled)


def fmt_dur(seconds: float) -> str:
    """Human-friendly tenure duration: '30 min', '2 h', '1.5 days'."""
    s = max(0.0, float(seconds))
    if s < 3600:
        return f"{round(s / 60)} min"
    if s < 86400:
        h = s / 3600
        return f"{h:.0f} h" if abs(h - round(h)) < 0.05 else f"{h:.1f} h"
    d = s / 86400
    return f"{d:.0f} day{'s' if round(d) != 1 else ''}" if abs(d - round(d)) < 0.05 else f"{d:.1f} days"


class Xp(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _helper_home(self) -> Path | None:
        home = self.bot.config.get("HELPER_HOME")
        return Path(home).expanduser() if home else None

    def _ladder(self) -> tuple[list[int], list[int], list[float]]:
        home = self._helper_home()
        if not home:
            return [], [], []
        try:
            cfg = json.loads((home / "politics_helper.json").read_text())
        except Exception:
            return [], [], []
        roles = [int(r) for r in cfg.get("LADDER", [])]
        levels = [int(x) for x in cfg.get("LADDER_LEVELS", [])]
        # tenure in seconds: prefer fine-grained minutes, else legacy days
        mins = cfg.get("LADDER_MIN_RANK_MINUTES")
        if mins:
            min_secs = [float(x) * 60 for x in mins]
        else:
            min_secs = [float(x) * 86400 for x in cfg.get("LADDER_MIN_RANK_DAYS", [])]
        return roles, levels, min_secs

    def _state(self) -> dict:
        home = self._helper_home()
        try:
            return json.loads((home / "ladder_state.json").read_text())
        except Exception:
            return {"since": {}, "pending": {}}

    def _xp_of(self, guild_id: int, user_id: int) -> int:
        db_path = self.bot.config.get("NADEKO_DB_PATH")
        if not db_path:
            return 0
        with sqlite3.connect(f"file:{Path(db_path).expanduser()}?mode=ro", uri=True, timeout=5) as db:
            row = db.execute(
                "SELECT Xp FROM UserXpStats WHERE UserId=? AND GuildId=?",
                (user_id, guild_id),
            ).fetchone()
        return int(row[0]) if row else 0

    @commands.hybrid_command(
        name="level", aliases=["lvl", "next"],
        description="Your XP, rank, and exactly what the next promotion takes.",
    )
    @app_commands.describe(member="Whose progress to show (default: you)")
    async def level(self, ctx: commands.Context, member: discord.Member | None = None):
        if ctx.guild is None:
            return
        member = member or ctx.author
        roles, levels, min_secs = self._ladder()
        try:
            xp = self._xp_of(ctx.guild.id, member.id)
        except Exception:
            return await ctx.reply("⚠️ XP database unavailable right now — try again shortly.")
        level = level_from_xp(xp)

        lines = [f"**Level {level}** · **{xp:,} XP** total"]
        held = [i for i, r in enumerate(roles) if ctx.guild.get_role(r) in member.roles]
        rank_i = max(held) if held else None
        if rank_i is not None:
            lines.append(f"**Rank:** {ctx.guild.get_role(roles[rank_i]).mention}")

        next_i = (rank_i + 1) if rank_i is not None else 0
        if roles and next_i < len(roles) and next_i < len(levels):
            next_role = ctx.guild.get_role(roles[next_i])
            need_level = levels[next_i]
            need_xp = total_xp_for_level(need_level)
            state = self._state()
            pending = state.get("pending", {}).get(str(member.id))
            if pending and int(pending.get("role", 0)) in roles:
                p_role = ctx.guild.get_role(int(pending["role"]))
                left = max(0.0, pending.get("eligible_at", 0) - time.time())
                lines.append(
                    f"**Next:** {p_role.mention} — ✅ XP earned! Unlocks in "
                    f"**{fmt_dur(left)}** (tenure gate)."
                )
            elif xp >= need_xp:
                lines.append(f"**Next:** {next_role.mention} — XP reached; promotion lands on your next activity.")
            else:
                to_go = need_xp - xp
                frac = 0.0 if need_xp == 0 else xp / need_xp
                est_voice = to_go / (3 * 60)    # 3 XP/min in voice
                est_msgs = math.ceil(to_go / 2)  # 2 XP/message
                lines.append(
                    f"**Next:** {next_role.mention} at level {need_level} "
                    f"({need_xp:,} XP)\n{bar(frac)} `{xp:,}/{need_xp:,}`\n"
                    f"**{to_go:,} XP to go** ≈ {est_voice:.1f}h in voice 🎙️ "
                    f"or ~{est_msgs:,} messages 💬 (voice levels ~2× faster)"
                )
                if next_i < len(min_secs) and min_secs[next_i] > 0:
                    since = state.get("since", {}).get(str(member.id), {}).get(str(roles[rank_i])) if rank_i is not None else None
                    if since:
                        served = time.time() - since
                        lines.append(f"**Tenure:** {fmt_dur(served)} / {fmt_dur(min_secs[next_i])} at current rank")
                    else:
                        lines.append(f"**Tenure:** {fmt_dur(min_secs[next_i])} at current rank also required")
        elif roles and rank_i == len(roles) - 1:
            lines.append("🏆 **Top of the ladder — nothing left to climb.**")

        embed = discord.Embed(description="\n".join(lines), color=GOLD)
        embed.set_author(name=f"{member.display_name} — rank progress",
                         icon_url=member.display_avatar.url)
        await ctx.reply(embed=embed, delete_after=60, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Xp(bot))
