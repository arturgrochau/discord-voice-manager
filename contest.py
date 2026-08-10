"""Nitro giveaway contest engine for the Politics Bot helper.

A time-boxed activity contest with a live, self-updating leaderboard:

  score = XP earned since contest start        (chat + voice, via Nadeko's DB)
        + INVITE_POINTS per invite that sticks (invite-uses diff attribution;
                                                deducted if the invitee leaves)
        + VC_POINTS_PER_MIN per minute spent unmuted in voice with at least
          one other human (no AFK-alone farming)

The leaderboard embed in the contest channel is edited in place every
UPDATE_MINUTES so members watch their standing move in near-real-time.

Config (politics_helper.json):
  "CONTEST": {
    "ACTIVE": true,
    "CHANNEL_ID": "…",          # where the leaderboard lives
    "END": "2026-08-31T23:59:00+00:00",
    "INVITE_POINTS": 400,
    "VC_POINTS_PER_MIN": 3,
    "UPDATE_MINUTES": 10
  }

State (contest_state.json): XP baseline snapshot, invite credits and who
invited whom, unmuted-VC seconds, and the leaderboard message id, all
restart-safe. Delete the state file to start a fresh contest.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import discord

log = logging.getLogger("politics-helper.contest")

MEDALS = ["🥇", "🥈", "🥉", "🏅", "🏅"]  # top 5 all carry tangible prizes


class Contest:
    def __init__(self, client: discord.Client, config: dict, base_dir: Path,
                 nadeko_db: Path, guild_id: int):
        cfg = config.get("CONTEST", {})
        self.client = client
        self.active = bool(cfg.get("ACTIVE"))
        self.channel_id = int(cfg.get("CHANNEL_ID", 0) or 0)
        self.end_iso = cfg.get("END", "")
        self.invite_pts = int(cfg.get("INVITE_POINTS", 400))
        self.vc_pts = float(cfg.get("VC_POINTS_PER_MIN", 3))
        self.update_min = max(1, int(cfg.get("UPDATE_MINUTES", 10)))
        # staff/alt accounts that must never appear on the board
        self.exclude = {str(x) for x in cfg.get("EXCLUDE_IDS", [])}
        self.nadeko_db = nadeko_db
        self.guild_id = guild_id
        self.state_path = base_dir / "contest_state.json"
        self.state = self._load()
        # Bucket invariants: events (invite join/leave) can fire before start()
        # snapshots the baseline, so every bucket must exist unconditionally or
        # the handlers KeyError. baseline stays None until start() fills it.
        for k, v in (("started", None), ("baseline", None), ("invites", {}),
                     ("invited_by", {}), ("vc_seconds", {}), ("messages", {}),
                     ("vc_daily", {}), ("message_id", 0)):
            self.state.setdefault(k, v)
        self._inv_cache: dict[str, int] = {}
        self._last_msg: dict[str, float] = {}  # anti-spam window (30 s)

    # -- persistence -------------------------------------------------------

    def _load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except FileNotFoundError:
            return {}
        except Exception as e:
            # Corrupt (e.g. torn write): preserve it for recovery, start clean
            # rather than silently overwriting on the next _save.
            log.error("Contest: state unreadable (%s) — quarantining", e)
            try:
                self.state_path.replace(self.state_path.with_suffix(".json.corrupt"))
            except OSError:
                pass
            return {}

    def _save(self) -> None:
        # Atomic + durable: a crash/power-cut leaves the old or new whole file,
        # never a truncated one that would silently reset the whole contest.
        tmp = self.state_path.with_suffix(".json.tmp")
        try:
            with open(tmp, "w") as f:
                json.dump(self.state, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_path)
        except OSError as e:
            log.error("Contest: state save failed: %s", e)

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if not (self.active and self.channel_id):
            return
        guild = self.client.get_guild(self.guild_id)
        if guild is None:
            return
        # Baseline the XP snapshot once, when it is still missing. A locked DB
        # must NOT abort start() — the loops (and the caller's on_ready) have to
        # keep going; a null baseline is retried on the next render tick.
        if self.state.get("baseline") is None:
            try:
                self.state["baseline"] = self._xp_snapshot()
                self.state["started"] = time.time()
                self._save()
                log.info("Contest: NEW contest baselined, %d users", len(self.state["baseline"]))
            except sqlite3.Error as e:
                log.error("Contest: baseline snapshot failed, will retry: %s", e)
        try:
            self._inv_cache = {i.code: i.uses or 0 for i in await guild.invites()}
            log.info("Contest: %d invites cached", len(self._inv_cache))
        except discord.HTTPException as e:
            log.warning("Contest: invite cache failed: %s", e)
        self.client.loop.create_task(self._vc_loop())
        self.client.loop.create_task(self._render_loop())
        log.info("Contest: engine up (update every %d min)", self.update_min)

    def _ended(self) -> bool:
        """True once the advertised deadline has passed (or it was finalized)."""
        if self.state.get("finalized"):
            return True
        e = self._end_ts()
        return 0 < e <= time.time()

    def _xp_snapshot(self) -> dict:
        with sqlite3.connect(f"file:{self.nadeko_db}?mode=ro", uri=True, timeout=10) as db:
            rows = db.execute("SELECT UserId, Xp FROM UserXpStats WHERE GuildId=?",
                              (self.guild_id,)).fetchall()
        return {str(u): x for u, x in rows}

    # -- invite attribution ------------------------------------------------

    async def on_invite_create(self, invite: discord.Invite) -> None:
        if self.active:
            self._inv_cache[invite.code] = invite.uses or 0

    async def on_invite_delete(self, invite: discord.Invite) -> None:
        self._inv_cache.pop(invite.code, None)

    async def on_member_join(self, member: discord.Member) -> None:
        if not self.active or self._ended() or member.bot or member.guild.id != self.guild_id:
            return
        try:
            invites = await member.guild.invites()
        except discord.HTTPException:
            return
        inviter = None
        for inv in invites:
            if (inv.uses or 0) > self._inv_cache.get(inv.code, 0):
                inviter = inv.inviter
            self._inv_cache[inv.code] = inv.uses or 0
        if inviter and not inviter.bot and inviter.id != member.id:
            uid = str(inviter.id)
            self.state["invites"][uid] = self.state["invites"].get(uid, 0) + 1
            self.state["invited_by"][str(member.id)] = uid
            self._save()
            log.info("Contest: %s invited %s (+%d pts)", inviter, member, self.invite_pts)

    async def on_member_remove(self, member: discord.Member) -> None:
        if not self.active or self._ended() or member.guild.id != self.guild_id:
            return
        inviter = self.state.get("invited_by", {}).pop(str(member.id), None)
        if inviter is not None:
            self.state["invites"][inviter] = max(0, self.state["invites"].get(inviter, 0) - 1)
            self._save()
            log.info("Contest: invitee %s left, deducted from %s", member, inviter)

    # -- message counting (display only; XP already scores messages) ------

    async def on_message(self, message: discord.Message) -> None:
        if not self.active or self._ended() or message.author.bot \
                or message.guild is None or message.guild.id != self.guild_id:
            return
        uid = str(message.author.id)
        now = time.time()
        if now - self._last_msg.get(uid, 0) < 30:
            return
        self._last_msg[uid] = now
        msgs = self.state.setdefault("messages", {})
        msgs[uid] = msgs.get(uid, 0) + 1
        self._save()

    # -- unmuted voice tracking -------------------------------------------

    # Daily cap on creditable voice minutes so two accounts can't park overnight
    # and farm thousands of points with zero interaction.
    VC_DAILY_CAP_MIN = 240

    async def _vc_loop(self) -> None:
        from datetime import date
        while self.active:
            await asyncio.sleep(60)
            if self._ended():
                continue
            try:
                guild = self.client.get_guild(self.guild_id)
                if guild is None:
                    continue
                changed = False
                today = date.today().isoformat()
                daily = self.state.setdefault("vc_daily", {})
                music_ch = getattr(getattr(self.client, "music", None), "channel_id", 0)
                afk_ch = int(self.client.config.get("AFK_CHANNEL_ID", 0) or 0) \
                    if hasattr(self.client, "config") else 0
                for vc in guild.voice_channels:
                    if vc == guild.afk_channel:
                        continue
                    # no VC points while the music player sits in the channel
                    # (music must not be a point farm), nor in the AFK room
                    if vc.id in (music_ch, afk_ch):
                        continue
                    # "Company" and "credit" both require present-and-not-deafened:
                    # a deafened body neither earns nor counts as a second human,
                    # so a self-deafened alt can't prop up a muted farmer.
                    present = [m for m in vc.members if not m.bot
                              and m.voice and not (m.voice.self_deaf or m.voice.deaf)]
                    if len(present) < 2:
                        continue  # no points for idling alone (or with a deaf alt)
                    for m in present:
                        uid = str(m.id)
                        rec = daily.get(uid)
                        if not rec or rec[0] != today:
                            rec = [today, 0]
                        if rec[1] >= self.VC_DAILY_CAP_MIN * 60:
                            continue  # hit the daily voice cap — stop crediting
                        rec[1] += 60
                        daily[uid] = rec
                        self.state["vc_seconds"][uid] = self.state["vc_seconds"].get(uid, 0) + 60
                        changed = True
                if changed:
                    self._save()
            except Exception:
                log.exception("Contest: vc loop error")

    # -- scoring & leaderboard --------------------------------------------

    def scores(self) -> list[tuple[int, int, dict]]:
        """[(uid, points, breakdown)] sorted descending."""
        baseline = self.state.get("baseline")
        if baseline is None:
            return []  # baseline not captured yet (locked DB at start) — retried next tick
        current = self._xp_snapshot()
        uids = set(current) | set(self.state.get("invites", {})) | set(self.state.get("vc_seconds", {}))
        guild = self.client.get_guild(self.guild_id)
        out = []
        for uid in uids:
            if uid in self.exclude:
                continue
            # drop anyone who left the server — they can't win, so hide them
            if guild is not None and guild.get_member(int(uid)) is None:
                continue
            # deliberately unclamped: mods can push a score down with a
            # negative .xpadd and the correction lands on the next render
            xp = int(current.get(uid, 0)) - int(baseline.get(uid, 0))
            inv = self.state.get("invites", {}).get(uid, 0)
            vc_min = self.state.get("vc_seconds", {}).get(uid, 0) // 60
            msgs = self.state.get("messages", {}).get(uid, 0)
            pts = xp + inv * self.invite_pts + int(vc_min * self.vc_pts)
            if pts > 0:
                out.append((int(uid), pts, {"xp": xp, "inv": inv, "vc": vc_min, "msgs": msgs}))
        out.sort(key=lambda t: -t[1])
        return out

    def _end_ts(self) -> int:
        try:
            return int(datetime.fromisoformat(self.end_iso).timestamp())
        except Exception:
            return 0

    def build_embed(self, guild: discord.Guild, ranked=None) -> discord.Embed:
        if ranked is None:
            ranked = self.scores()
        ranked = ranked[:10]
        rows = []
        for i in range(10):
            if i < 3:
                badge = MEDALS[i]
            elif i < 5:
                badge = f"{MEDALS[i]} **{i + 1}.**"
            else:
                badge = f"**{i + 1}.**"
            if i < len(ranked):
                uid, pts, b = ranked[i]
                bits = []
                if b["msgs"]:
                    bits.append(f"{b['msgs']} msg{'s' if b['msgs'] != 1 else ''}")
                if b["vc"]:
                    bits.append(f"{b['vc']} min voice")
                if b["inv"]:
                    bits.append(f"{b['inv']} invite{'s' if b['inv'] != 1 else ''}")
                detail = f"  ({' / '.join(bits)})" if bits else ""
                rows.append(f"{badge} <@{uid}> · **{pts:,} pts**{detail}")
            else:
                rows.append(f"{badge} *no one yet, could be you*")
        end = self._end_ts()
        e = discord.Embed(
            title="🎁 Nitro Giveaway · Live Standings",
            description=(
                "\n".join(rows)
                + "\n\n**How to score**\n"
                "• All XP you earn counts. Chat and voice both work, voice pays ~2x faster\n"
                f"• Each friend you invite who stays: **+{self.invite_pts} pts**\n"
                f"• Every minute in voice with others (muted is fine, just don't deafen): **+{self.vc_pts:g} pts**\n\n"
                "**Prizes** (everyone in the top 10 wins XP)\n"
                "🥇 1 month of Nitro + the **Mythical** role + **50,000 XP**\n"
                "🥈 1 month of Nitro + **30,000 XP**\n"
                "🥉 1 month of Nitro + **20,000 XP**\n"
                "🏅 4th: **Nitro Basic** + **12,000 XP**\n"
                "🏅 5th: **Nitro Basic** + **10,000 XP**\n"
                "6th to 10th: **8,000 / 6,000 / 5,000 / 4,000 / 3,000 XP**\n\n"
                "-# Anti-spam: only one message every 30 seconds counts.\n"
                "-# Numbers look off? DM the bot and the mod team will adjust manually."
            ),
            color=0xF47FFF,
        )
        if end:
            e.description += f"\n\n⏳ **Contest ends <t:{end}:D> (<t:{end}:R>)**"
        e.set_footer(text=f"Standings update every {self.update_min} min")
        return e

    async def _render_loop(self) -> None:
        await asyncio.sleep(10)
        while self.active:
            try:
                await self.render()
            except Exception:
                log.exception("Contest: render error")
            # Once past the deadline, do one final render then stop the loop.
            if self._ended() and self.state.get("finalized"):
                log.info("Contest: ended — final standings rendered, loop stopping")
                return
            await asyncio.sleep(self.update_min * 60)

    async def render(self) -> None:
        guild = self.client.get_guild(self.guild_id)
        channel = self.client.get_channel(self.channel_id)
        if guild is None or channel is None:
            return
        ranked = self.scores()   # computed once; reused for embed + log
        final = self._ended() and not self.state.get("finalized")
        embed = self.build_embed(guild, ranked)
        if self._ended():
            end = self._end_ts()
            note = "🏁 **FINAL standings — the contest has ended.**"
            embed.description = embed.description.replace(
                f"⏳ **Contest ends <t:{end}:D> (<t:{end}:R>)**", note)
            if note not in embed.description:
                embed.description += f"\n\n{note}"
            embed.set_footer(text="Contest ended")
        msg = None
        if self.state.get("message_id"):
            try:
                msg = await channel.fetch_message(self.state["message_id"])
            except discord.HTTPException:
                msg = None
        if msg:
            await msg.edit(embed=embed)
        else:
            msg = await channel.send(embed=embed)
            self.state["message_id"] = msg.id
            self._save()
            try:
                await msg.pin(reason="Contest leaderboard")
            except discord.HTTPException:
                pass
        if final:
            self.state["finalized"] = True
            self._save()
        log.info("Contest: leaderboard rendered (%d scorers)", len(ranked))
