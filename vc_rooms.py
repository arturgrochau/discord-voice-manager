"""TempVoice-style room control for Join-to-Create rooms.

Design follows the open-source temp-VC bots (TempVoice, InterfaceGUI's
Discord-TempVoice-Bot): when a room spawns, the bot posts a control panel
into the room's text chat — a persistent button dashboard plus `..command`
text commands — and every setting a user chooses (room name, size, lock,
speaking default, bans, mods) is saved per-user and re-applied the next
time they create a room.

Room powers:
  owner  — everything: rename/status/size/lock/voice default/minors toggle,
           ban/unban/hush, mod/demod, transfer/abandon (+ native right-click
           mute/deafen/move via channel overwrites granted at creation)
  mods   — ban/unban/hush/unhush (room-scoped)
  anyone — claim an abandoned room (owner no longer inside)

State files (in the helper's BASE_DIR):
  vc_prefs.json — per-user saved room settings, survive across sessions
  vc_rooms.json — live rooms (owner/mods/bans/hush), survive restarts
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from pathlib import Path

import discord

log = logging.getLogger("politics-helper.rooms")

BAN_DENY = discord.PermissionOverwrite(view_channel=False, connect=False)
DEFAULT_BAN_MIN = 1440  # bans default to one day unless a length is given

_DUR_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([a-z]*)$")


def parse_duration(text: str) -> int | None:
    """'30m' / '6h' / '3d' / '2w' / '1mo' / '1y' / '45' (minutes) -> minutes.

    'perm' (and friends) -> 0 (permanent, saved to prefs). None = unparsable.
    """
    t = text.strip().lower()
    if t in ("perm", "permanent", "forever", "always", "0"):
        return 0
    m = _DUR_RE.match(t)
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2)
    if unit == "":
        return max(1, int(n))  # bare number = minutes
    for prefix, mult in (("mo", 43200), ("m", 1), ("h", 60), ("d", 1440),
                         ("w", 10080), ("y", 525600)):
        if unit.startswith(prefix):
            return max(1, int(n * mult))
    return None


def fmt_span(minutes: int) -> str:
    """1440 -> '1 day', 90 -> '1.5 hours', 30 -> '30 min'."""
    for size, word in ((525600, "year"), (43200, "month"), (10080, "week"),
                       (1440, "day"), (60, "hour")):
        if minutes >= size:
            n = round(minutes / size, 1)
            n = int(n) if n == int(n) else n
            return f"{n} {word}{'s' if n != 1 else ''}"
    return f"{minutes} min"
HUSH_DENY_KEY = "send_messages"
MOD_ALLOW = discord.PermissionOverwrite(
    connect=True, mute_members=True, deafen_members=True)

RANDOM_NAMES = [
    "🎧 late night lounge", "🏛️ the forum", "🔥 fireside", "🌌 star chamber",
    "🍷 symposium", "⚔️ war room", "🌊 the deep end", "🗼 watchtower",
    "🎲 game night", "📚 reading room", "🌤️ high ground", "🕰️ smoke-filled room",
]


def _owner_overwrite() -> discord.PermissionOverwrite:
    # NOTE: no move_members — Discord validates it against the destination
    # channel, so granting it on the owner's room would let them pull anyone
    # in from other channels. Kicking is bot-mediated (..kick / Kick button).
    return discord.PermissionOverwrite(
        view_channel=True, connect=True, speak=True, manage_channels=True,
        mute_members=True, deafen_members=True,
        priority_speaker=True, set_voice_channel_status=True,
    )


class RoomManager:
    def __init__(self, client: discord.Client, config: dict, base_dir: Path):
        self.client = client
        self.config = config
        self.prefs_path = base_dir / "vc_prefs.json"
        self.rooms_path = base_dir / "vc_rooms.json"
        self.minor_role_id = int(config.get("VC_MINOR_ROLE_ID", 0) or 0)
        self.rules_channel_id = int(config.get("RULES_CHANNEL_ID", 0) or 0)
        self.info_channel_id = int(config.get("VC_INFO_CHANNEL_ID", 0) or 0)
        # per-server aesthetic (falls back to Discord blurple)
        self.color = int(str(config.get("PANEL_COLOR", "0x5865F2")), 16)
        self.emblem = config.get("PANEL_EMBLEM", "🔊")
        self.log_channel_id = int(config.get("VC_LOG_CHANNEL_ID", 0) or 0)
        self.prefs: dict = self._load(self.prefs_path)
        self.rooms: dict = self._load(self.rooms_path)   # cid(str) -> state
        self._ban_task = None
        # set by the helper: coroutine that deletes a temp VC cleanly
        self.delete_cb = None

    # -- persistence -------------------------------------------------------

    @staticmethod
    def _load(path: Path) -> dict:
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}

    def _save(self) -> None:
        self.prefs_path.write_text(json.dumps(self.prefs))
        self.rooms_path.write_text(json.dumps(self.rooms))

    def room(self, channel_id: int) -> dict | None:
        return self.rooms.get(str(channel_id))

    def is_owner(self, channel_id: int, user_id: int) -> bool:
        r = self.room(channel_id)
        return bool(r) and r.get("owner") == user_id

    def is_staff(self, channel_id: int, user_id: int) -> bool:
        r = self.room(channel_id)
        return bool(r) and (r.get("owner") == user_id or user_id in r.get("mods", []))

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Re-arm persistent views, drop vanished rooms, purge empty ones.

        Rooms are tracked by id in vc_rooms.json (never by name), so renamed
        rooms survive restarts with full tracking — and any room that emptied
        while the bot was down is deleted right here, not a minute later.
        """
        gone = [cid for cid in list(self.rooms)
                if self.client.get_channel(int(cid)) is None]
        for cid in gone:
            self.rooms.pop(cid, None)
        if gone:
            self._save()
        for cid in list(self.rooms):
            vc = self.client.get_channel(int(cid))
            if vc is not None and not vc.members and self.delete_cb:
                log.info("Startup: deleting room %r left empty while offline", vc.name)
                await self.delete_cb(vc)
        self.client.add_view(RoomPanel(self))
        if self._ban_task is None:
            self._ban_task = self.client.loop.create_task(self._timed_ban_loop())
        log.info("Room manager up: %d live room(s)", len(self.rooms))

    async def setup_room(self, vc: discord.VoiceChannel, owner: discord.Member) -> None:
        """Register a fresh room, re-apply the owner's saved prefs, post panel."""
        p = self.prefs.get(str(owner.id), {})
        state = {"owner": owner.id, "mods": [], "bans": {}, "hushed": [],
                 "locked": False, "voice_default": True, "created": time.time()}
        self.rooms[str(vc.id)] = state
        self._save()

        try:
            edits = {}
            if p.get("name"):
                edits["name"] = p["name"][:100]
            if p.get("limit") is not None:
                edits["user_limit"] = max(0, min(99, int(p["limit"])))
            if edits:
                await vc.edit(**edits, reason="Room prefs restored")
            for uid in p.get("mods", []):
                m = vc.guild.get_member(int(uid))
                if m and m.id != owner.id:
                    await vc.set_permissions(m, overwrite=MOD_ALLOW, reason="Room mod (saved)")
                    state["mods"].append(int(uid))
            for uid in p.get("bans", []):
                m = vc.guild.get_member(int(uid))
                if m:
                    await vc.set_permissions(m, overwrite=BAN_DENY, reason="Room ban (saved)")
                    state["bans"][str(uid)] = 0
            for uid in p.get("hushed", []):
                m = vc.guild.get_member(int(uid))
                if m and m.id != owner.id:
                    ow = discord.PermissionOverwrite()
                    setattr(ow, HUSH_DENY_KEY, False)
                    await vc.set_permissions(m, overwrite=ow, reason="Room hush (saved)")
                    state["hushed"].append(int(uid))
            if p.get("locked"):
                await self._apply_lock(vc, True)
                state["locked"] = True
            if p.get("voice_default") is False:
                await self._apply_voice_default(vc, False)
                state["voice_default"] = False
            self._save()
        except discord.HTTPException as e:
            log.warning("Pref restore failed for %s: %s", owner, e)

        try:
            await vc.send(embed=self.panel_embed(vc, owner), view=RoomPanel(self))
        except discord.HTTPException as e:
            log.warning("Panel post failed in %s: %s", vc, e)
        try:
            await owner.send(embed=self.owner_dm_embed(vc.guild))
        except discord.HTTPException:
            pass  # DMs closed — the panel covers everything anyway

    def forget_room(self, channel_id: int) -> None:
        """Room deleted: snapshot the owner's current settings as their prefs."""
        state = self.rooms.pop(str(channel_id), None)
        if state:
            vc = self.client.get_channel(channel_id)
            uid = str(state["owner"])
            saved = self.prefs.setdefault(uid, {})
            if vc is not None and state.get("owner"):
                saved["name"] = vc.name
                saved["limit"] = vc.user_limit
            saved["locked"] = state.get("locked", False)
            saved["voice_default"] = state.get("voice_default", True)
            saved["mods"] = state.get("mods", [])
            saved["hushed"] = state.get("hushed", [])
            saved["bans"] = [int(u) for u, exp in state.get("bans", {}).items() if not exp]
        self._save()

    # -- panel -------------------------------------------------------------

    def _info_link(self) -> str:
        return f"<#{self.info_channel_id}>" if self.info_channel_id else "the info channel"

    def panel_embed(self, vc: discord.VoiceChannel, owner: discord.Member) -> discord.Embed:
        e = discord.Embed(
            description=(
                f"### {self.emblem} {owner.mention} owns this room\n"
                "Buttons below, `..commands` for the full list, "
                f"every detail in {self._info_link()}.\n"
                "-# ⚙️ Your setup (name, size, lock, mods, bans, hushes) is remembered.\n"
                "-# 📜 Discord ToS applies everywhere in this server."
            ),
            color=self.color,
        )
        if self.rules_channel_id:
            e.set_footer(text="Room names & statuses must follow the server rules.")
        return e

    def owner_dm_embed(self, guild: discord.Guild) -> discord.Embed:
        e = discord.Embed(
            title=f"{self.emblem} Your room in {guild.name}",
            description=(
                "You own it. Buttons live in the room chat; the essentials:\n\n"
                "`..rename <name>` · `..setsize <0-99>` · `..lock` · `..voice`\n"
                "`..ban <name> [30m|6h|3d|1mo|1y|perm]` — no length = **1 day**\n"
                "`..mod` / `..unmod` · `..hush` / `..unhush` · `..kick` · `..unban`\n"
                "`..transfer` · `..abandon` · `..claim`\n\n"
                f"📖 Every command in full detail: {self._info_link()}\n"
                "-# ⚙️ Everything you set is saved and restored on your next room.\n"
                "-# 📜 Discord ToS applies. You never owe anyone a reason for a room "
                "ban or mute, but punishing anyone over race, religion, gender, "
                "sexuality or any protected trait is a server-rule violation."
            ),
            color=self.color,
        )
        if self.rules_channel_id:
            e.set_footer(text="Keep names and statuses within the server rules.")
        return e

    def commands_embed(self) -> discord.Embed:
        """The in-chat `..commands` card for room chats."""
        minors = " · `..nominors`" if self.minor_role_id else ""
        return discord.Embed(
            title=f"{self.emblem} Room commands",
            description=(
                "**Setup** — `..rename <name>` · `..status <text>` · "
                f"`..setsize <0-99>` · `..lock` · `..voice`{minors}\n"
                "**Bans** — `..ban <name> [30m|6h|3d|2w|1mo|1y|perm]` "
                "(no length = **1 day**) · `..unban <name>` · `..bans`\n"
                "**People** — `..mod`/`..unmod` · `..hush`/`..unhush` · `..kick`\n"
                "**Ownership** — `..transfer <name>` · `..abandon` · `..claim`\n"
                "**Panel** — `..panel` re-posts the buttons\n\n"
                f"📖 Full reference: {self._info_link()}\n"
                "-# ⚙️ Names work with partial matches; mentions and ids too. "
                "Mods, permanent bans and hushes stick for your future rooms."
            ),
            color=self.color,
        )

    # -- primitives --------------------------------------------------------

    async def _apply_lock(self, vc: discord.VoiceChannel, locked: bool) -> None:
        ow = vc.overwrites_for(vc.guild.default_role)
        ow.connect = False if locked else None
        await vc.set_permissions(vc.guild.default_role, overwrite=ow, reason="Room lock toggle")

    async def _apply_voice_default(self, vc: discord.VoiceChannel, speak: bool) -> None:
        ow = vc.overwrites_for(vc.guild.default_role)
        ow.speak = None if speak else False
        await vc.set_permissions(vc.guild.default_role, overwrite=ow, reason="Room voice-default toggle")

    async def _log(self, vc, text: str) -> None:
        """Room events go to the voice log so moderation stays transparent."""
        channel = self.client.get_channel(self.log_channel_id)
        if channel is None:
            return
        try:
            await channel.send(embed=discord.Embed(
                description=f"{self.emblem} **{vc.name}** — {text}", color=self.color))
        except discord.HTTPException:
            pass

    @staticmethod
    async def _dm(member: discord.Member, text: str) -> None:
        try:
            await member.send(text)
        except discord.HTTPException:
            pass

    async def set_status(self, vc: discord.VoiceChannel, status: str) -> None:
        route = discord.http.Route("PUT", "/channels/{channel_id}/voice-status",
                                   channel_id=vc.id)
        await self.client.http.request(route, json={"status": status[:500]})

    @staticmethod
    def _resolve_member(guild: discord.Guild, token: str,
                        vc: discord.VoiceChannel | None = None) -> discord.Member | None:
        """Mention/id, else name matching — room members first, then the guild.

        Match order per pool: exact name/display -> unique prefix -> unique
        substring, all case-insensitive, so `..mod half` finds `halfway`.
        """
        token = token.strip().lstrip("<@!&").rstrip(">")
        if token.isdigit():
            return guild.get_member(int(token))
        t = token.casefold()
        pools = ([list(vc.members)] if vc else []) + [guild.members]
        for pool in pools:
            names = [(m, m.name.casefold(), m.display_name.casefold()) for m in pool]
            for m, n, d in names:
                if t in (n, d):
                    return m
            starts = [m for m, n, d in names if n.startswith(t) or d.startswith(t)]
            if len(starts) == 1:
                return starts[0]
            subs = [m for m, n, d in names if t in n or t in d]
            if len(subs) == 1:
                return subs[0]
        return None

    # -- actions (shared by buttons and ..commands) ------------------------

    async def act_rename(self, vc, actor, name: str) -> str:
        if not self.is_owner(vc.id, actor.id):
            return "⛔ Only the room owner can rename."
        self.prefs.setdefault(str(actor.id), {})["name"] = name[:100]
        self._save()
        try:
            await asyncio.wait_for(vc.edit(name=name[:100], reason=f"Rename by {actor}"), timeout=5)
        except asyncio.TimeoutError:
            return ("⏳ Discord limits channel renames to **2 per 10 minutes** — this one "
                    "didn't go through, but the name is saved and will apply to your next room.")
        return f"✅ Room renamed to **{name[:100]}**."

    async def act_status(self, vc, actor, status: str) -> str:
        if not self.is_owner(vc.id, actor.id):
            return "⛔ Only the room owner can set the status."
        try:
            await self.set_status(vc, status)
        except discord.HTTPException:
            return "⚠️ Couldn't set the status (Discord rejected it)."
        return f"✅ Status set: {status[:100]}"

    async def act_size(self, vc, actor, size: int) -> str:
        if not self.is_owner(vc.id, actor.id):
            return "⛔ Only the room owner can set the size."
        size = max(0, min(99, size))
        await vc.edit(user_limit=size, reason=f"Size by {actor}")
        self.prefs.setdefault(str(actor.id), {})["limit"] = size
        self._save()
        return f"✅ Room size set to **{size or 'unlimited'}**."

    async def act_lock(self, vc, actor) -> str:
        state = self.room(vc.id)
        if not self.is_owner(vc.id, actor.id):
            return "⛔ Only the room owner can lock/unlock."
        state["locked"] = not state.get("locked", False)
        await self._apply_lock(vc, state["locked"])
        self.prefs.setdefault(str(actor.id), {})["locked"] = state["locked"]
        self._save()
        return "🔒 Room **locked** — nobody new can join." if state["locked"] else "🔓 Room **unlocked**."

    async def act_voice(self, vc, actor) -> str:
        state = self.room(vc.id)
        if not self.is_owner(vc.id, actor.id):
            return "⛔ Only the room owner can toggle the speaking default."
        state["voice_default"] = not state.get("voice_default", True)
        await self._apply_voice_default(vc, state["voice_default"])
        self.prefs.setdefault(str(actor.id), {})["voice_default"] = state["voice_default"]
        self._save()
        return ("🎙️ Speaking default **ON** — everyone can speak."
                if state["voice_default"]
                else "🔇 Speaking default **OFF** — new joiners can't speak until you allow them (right-click → mute off / priority).")

    async def act_nominors(self, vc, actor) -> str:
        if not self.minor_role_id:
            return "⚠️ No minors role is configured on this server."
        if not self.is_owner(vc.id, actor.id):
            return "⛔ Only the room owner can toggle this."
        role = vc.guild.get_role(self.minor_role_id)
        if role is None:
            return "⚠️ Minors role not found."
        ow = vc.overwrites_for(role)
        turning_on = ow.connect is not False
        ow.connect = False if turning_on else None
        ow.view_channel = False if turning_on else None
        await vc.set_permissions(role, overwrite=ow, reason="Minors toggle")
        return "🔞 Minors can no longer join this room." if turning_on else "✅ Minors may join again."

    async def act_ban(self, vc, actor, member: discord.Member, minutes: int = 0) -> str:
        state = self.room(vc.id)
        if not self.is_staff(vc.id, actor.id):
            return "⛔ Only the owner or room mods can ban."
        if member.id == state["owner"]:
            return "⛔ You can't ban the room owner."
        if member.guild_permissions.administrator:
            return "⛔ You can't room-ban a server admin."
        await vc.set_permissions(member, overwrite=BAN_DENY,
                                 reason=f"Room ban by {actor}" + (f" ({minutes}m)" if minutes else " (permanent)"))
        if member.voice and member.voice.channel and member.voice.channel.id == vc.id:
            try:
                await member.move_to(None, reason="Room ban")
            except discord.HTTPException:
                pass
        state["bans"][str(member.id)] = time.time() + minutes * 60 if minutes else 0
        if not minutes and member.id not in self.prefs.setdefault(str(state["owner"]), {}).setdefault("bans", []):
            self.prefs[str(state["owner"])]["bans"].append(member.id)
        self._save()
        span = f" for {fmt_span(minutes)}" if minutes else ""
        await self._log(vc, f"{member.mention} was **room-banned**{span} by {actor.mention}.")
        await self._dm(member, f"🔨 You were banned from the voice room **{vc.name}**{span} "
                               f"by **{actor.display_name}** ({vc.guild.name}).")
        return (f"🔨 {member.mention} banned from this room"
                + (f" for **{fmt_span(minutes)}**." if minutes else
                   " **permanently** (saved for your future rooms)."))

    async def act_unban(self, vc, actor, member: discord.Member) -> str:
        state = self.room(vc.id)
        if not self.is_staff(vc.id, actor.id):
            return "⛔ Only the owner or room mods can unban."
        await vc.set_permissions(member, overwrite=None, reason=f"Room unban by {actor}")
        state["bans"].pop(str(member.id), None)
        owner_prefs = self.prefs.setdefault(str(state["owner"]), {})
        if member.id in owner_prefs.get("bans", []):
            owner_prefs["bans"].remove(member.id)
        self._save()
        await self._log(vc, f"{member.mention} was **unbanned** by {actor.mention}.")
        await self._dm(member, f"♻️ Your ban from the voice room **{vc.name}** was lifted "
                               f"by **{actor.display_name}** ({vc.guild.name}).")
        return f"♻️ {member.mention} unbanned from this room."

    def bans_text(self, vc) -> str:
        state = self.room(vc.id) or {}
        if not state.get("bans"):
            return "Nobody is banned from this room. 🎉"
        lines = []
        for uid, exp in state["bans"].items():
            left = f" — {fmt_span(max(1, int((exp - time.time()) / 60)))} left" if exp else " — permanent (saved)"
            lines.append(f"• <@{uid}>{left}")
        return "**Room bans:**\n" + "\n".join(lines)

    async def act_mod(self, vc, actor, member: discord.Member, on: bool) -> str:
        state = self.room(vc.id)
        if not self.is_owner(vc.id, actor.id):
            return "⛔ Only the room owner can manage room mods."
        if on:
            await vc.set_permissions(member, overwrite=MOD_ALLOW, reason=f"Room mod by {actor}")
            if member.id not in state["mods"]:
                state["mods"].append(member.id)
            msg = f"🛡️ {member.mention} is now a **room mod** (can ban & hush here)."
            await self._log(vc, f"{member.mention} was made **room mod** by {actor.mention}.")
        else:
            await vc.set_permissions(member, overwrite=None, reason=f"Room demod by {actor}")
            if member.id in state["mods"]:
                state["mods"].remove(member.id)
            msg = f"✅ {member.mention} is no longer a room mod."
        self.prefs.setdefault(str(state["owner"]), {})["mods"] = state["mods"]
        self._save()
        return msg

    async def act_hush(self, vc, actor, member: discord.Member, on: bool) -> str:
        state = self.room(vc.id)
        if not self.is_staff(vc.id, actor.id):
            return "⛔ Only the owner or room mods can hush."
        if member.id == state["owner"]:
            return "⛔ You can't hush the room owner."
        ow = vc.overwrites_for(member)
        setattr(ow, HUSH_DENY_KEY, False if on else None)
        if ow.is_empty():
            await vc.set_permissions(member, overwrite=None, reason=f"Unhush by {actor}")
        else:
            await vc.set_permissions(member, overwrite=ow, reason=f"{'Hush' if on else 'Unhush'} by {actor}")
        if on and member.id not in state["hushed"]:
            state["hushed"].append(member.id)
        if not on and member.id in state["hushed"]:
            state["hushed"].remove(member.id)
        self._save()
        await self._log(vc, f"{member.mention} was **{'hushed' if on else 'unhushed'}** by {actor.mention}.")
        if on:
            await self._dm(member, f"🤫 You were hushed (chat-muted) in the voice room "
                                   f"**{vc.name}** by **{actor.display_name}** ({vc.guild.name}).")
        return (f"🤫 {member.mention} can no longer type in this chat."
                if on else f"💬 {member.mention} can type again.")

    async def act_kick(self, vc, actor, member: discord.Member) -> str:
        state = self.room(vc.id)
        if not self.is_staff(vc.id, actor.id):
            return "⛔ Only the owner or room mods can kick."
        if member.id == state["owner"]:
            return "⛔ You can't kick the room owner."
        if member.guild_permissions.administrator:
            return "⛔ You can't kick a server admin."
        if not (member.voice and member.voice.channel and member.voice.channel.id == vc.id):
            return "⛔ They're not in this room."
        try:
            await member.move_to(None, reason=f"Room kick by {actor}")
        except discord.HTTPException:
            return "⚠️ Kick failed — try again."
        await self._log(vc, f"{member.mention} was **kicked** by {actor.mention}.")
        await self._dm(member, f"👢 You were kicked from the voice room **{vc.name}** "
                               f"by **{actor.display_name}** ({vc.guild.name}). "
                               "You can rejoin unless you get banned.")
        return f"👢 {member.mention} kicked from the room."

    async def act_claim(self, vc, actor) -> str:
        state = self.room(vc.id)
        owner = vc.guild.get_member(state["owner"])
        if owner and owner in vc.members:
            return "⛔ The owner is still here — the room can't be claimed."
        if actor not in vc.members:
            return "⛔ Join the room to claim it."
        return await self._make_owner(vc, actor, "claimed")

    async def act_transfer(self, vc, actor, member: discord.Member) -> str:
        if not self.is_owner(vc.id, actor.id):
            return "⛔ Only the room owner can transfer ownership."
        if member.bot or member not in vc.members:
            return "⛔ Transfer target must be a person currently in the room."
        old = vc.guild.get_member(self.room(vc.id)["owner"])
        if old:
            await vc.set_permissions(old, overwrite=None, reason="Ownership transferred")
        return await self._make_owner(vc, member, "received ownership of")

    async def act_abandon(self, vc, actor) -> str:
        if not self.is_owner(vc.id, actor.id):
            return "⛔ Only the room owner can abandon it."
        state = self.room(vc.id)
        old = vc.guild.get_member(state["owner"])
        if old:
            await vc.set_permissions(old, overwrite=None, reason="Room abandoned")
        state["owner"] = 0
        self._save()
        return "🏳️ Room abandoned — anyone inside can now `..claim` it."

    async def _make_owner(self, vc, member, verb: str) -> str:
        state = self.room(vc.id)
        state["owner"] = member.id
        self._save()
        await vc.set_permissions(member, overwrite=_owner_overwrite(), reason=f"Room {verb}")
        await self._log(vc, f"{member.mention} {verb} the room.")
        return f"👑 {member.mention} has {verb} the room!"

    async def act_random(self, vc, actor) -> str:
        return await self.act_rename(vc, actor, random.choice(RANDOM_NAMES))

    # -- timed bans --------------------------------------------------------

    async def _timed_ban_loop(self):
        while True:
            await asyncio.sleep(60)
            try:
                now = time.time()
                # sweep rooms that emptied while the bot wasn't looking
                # (e.g. someone left during a restart) — nothing may linger
                for cid in list(self.rooms):
                    vc = self.client.get_channel(int(cid))
                    if vc is None:
                        self.forget_room(int(cid))
                        continue
                    age = now - self.rooms[cid].get("created", 0)
                    if age > 90 and not vc.members and self.delete_cb:
                        log.info("Sweeper: deleting orphaned empty room %r", vc.name)
                        await self.delete_cb(vc)
                for cid, state in list(self.rooms.items()):
                    vc = self.client.get_channel(int(cid))
                    expired = [uid for uid, exp in state.get("bans", {}).items()
                               if exp and exp <= now]
                    for uid in expired:
                        state["bans"].pop(uid, None)
                        if vc:
                            member = vc.guild.get_member(int(uid))
                            if member:
                                try:
                                    await vc.set_permissions(member, overwrite=None,
                                                             reason="Room ban expired")
                                except discord.HTTPException:
                                    pass
                    if expired:
                        self._save()
            except Exception:
                log.exception("Timed-ban loop error")

    # -- ..commands in room chat ------------------------------------------

    async def handle_message(self, message: discord.Message) -> None:
        content = (message.content or "").strip()
        if not content.startswith(".."):
            return
        vc = message.channel
        actor = message.author
        cmd, _, arg = content[2:].partition(" ")
        cmd, arg = cmd.lower(), arg.strip()

        async def need_member(fn, *extra):
            tokens = arg.split()
            member = self._resolve_member(message.guild, tokens[0], vc) if tokens else None
            if member is None:
                return (f"Couldn't find **{tokens[0]}** — try a mention, exact name, or id."
                        if tokens else f"Usage: `..{cmd} <name, @user or id>`")
            return await fn(vc, actor, member, *extra)

        try:
            if cmd == "rename" and arg:
                out = await self.act_rename(vc, actor, arg)
            elif cmd == "status" and arg:
                out = await self.act_status(vc, actor, arg)
            elif cmd in ("setsize", "size", "limit") and arg.split()[0].isdigit():
                out = await self.act_size(vc, actor, int(arg.split()[0]))
            elif cmd == "lock":
                out = await self.act_lock(vc, actor)
            elif cmd == "voice":
                out = await self.act_voice(vc, actor)
            elif cmd == "nominors":
                out = await self.act_nominors(vc, actor)
            elif cmd == "ban":
                tokens = arg.split()
                minutes = DEFAULT_BAN_MIN
                if len(tokens) > 1:
                    parsed = parse_duration(tokens[1])
                    if parsed is None:
                        minutes = None
                    else:
                        minutes = parsed
                if minutes is None:
                    out = ("Couldn't read that ban length. Use `..ban <name> "
                           "[30m | 6h | 3d | 2w | 1mo | 1y | perm]`. "
                           "Plain numbers are minutes; no length means **1 day**.")
                else:
                    out = await need_member(self.act_ban, minutes)
            elif cmd == "unban":
                out = await need_member(self.act_unban)
            elif cmd == "kick":
                out = await need_member(self.act_kick)
            elif cmd in ("bans", "banlist", "displaybans", "display_bans"):
                out = self.bans_text(vc)
            elif cmd == "mod":
                out = await need_member(self.act_mod, True)
            elif cmd in ("demod", "unmod"):
                out = await need_member(self.act_mod, False)
            elif cmd == "hush":
                out = await need_member(self.act_hush, True)
            elif cmd == "unhush":
                out = await need_member(self.act_hush, False)
            elif cmd == "claim":
                out = await self.act_claim(vc, actor)
            elif cmd == "transfer":
                out = await need_member(self.act_transfer)
            elif cmd == "abandon":
                out = await self.act_abandon(vc, actor)
            elif cmd in ("help", "panel"):
                owner = message.guild.get_member((self.room(vc.id) or {}).get("owner", 0)) or actor
                return await vc.send(embed=self.panel_embed(vc, owner), view=RoomPanel(self))
            else:
                return
        except discord.HTTPException as e:
            out = f"⚠️ Discord rejected that: {e.status}"
        try:
            await message.channel.send(out, delete_after=30)
            await message.delete(delay=15)
        except discord.HTTPException:
            pass


# -- UI --------------------------------------------------------------------


class _MemberAction(discord.ui.View):
    """Ephemeral picker listing only the members the action can apply to.

    mod / hush act as toggles: picking someone who already has the state
    removes it (labels mark current state with ✓).
    """

    def __init__(self, mgr: RoomManager, vc, action: str, minutes: int = DEFAULT_BAN_MIN):
        super().__init__(timeout=120)
        self.mgr, self.vc, self.action, self.minutes = mgr, vc, action, minutes
        state = mgr.room(vc.id) or {}
        options = []
        if action == "unban":
            for uid in state.get("bans", {}):
                m = vc.guild.get_member(int(uid))
                options.append(discord.SelectOption(
                    label=(m.display_name if m else f"user {uid}")[:100], value=str(uid)))
        else:
            marked = set(state.get("mods", []) if action == "mod"
                         else state.get("hushed", []) if action == "hush" else [])
            for m in vc.members:
                if m.bot or m.id == state.get("owner"):
                    continue
                options.append(discord.SelectOption(
                    label=m.display_name[:100], value=str(m.id),
                    description="✓ currently" if m.id in marked else None))
        self.empty = not options
        if self.empty:
            return
        picker = discord.ui.Select(placeholder="Pick a member…", options=options[:25])
        picker.callback = self._picked
        self.add_item(picker)

    async def _picked(self, interaction: discord.Interaction):
        member = interaction.guild.get_member(int(self.children[0].values[0]))
        if member is None:
            return await interaction.response.edit_message(
                content="They're no longer around.", view=None)
        state = self.mgr.room(self.vc.id) or {}
        if self.action == "mod":
            out = await self.mgr.act_mod(self.vc, interaction.user, member,
                                         member.id not in state.get("mods", []))
        elif self.action == "hush":
            out = await self.mgr.act_hush(self.vc, interaction.user, member,
                                          member.id not in state.get("hushed", []))
        else:
            fn = {
                "ban": lambda: self.mgr.act_ban(self.vc, interaction.user, member, self.minutes),
                "unban": lambda: self.mgr.act_unban(self.vc, interaction.user, member),
                "kick": lambda: self.mgr.act_kick(self.vc, interaction.user, member),
                "transfer": lambda: self.mgr.act_transfer(self.vc, interaction.user, member),
            }[self.action]
            out = await fn()
        # one output only: the picker itself becomes the result
        await interaction.response.edit_message(content=out, view=None)
        # ownership changes matter to everyone present — announce those once
        if self.action == "transfer" and out.startswith("👑"):
            try:
                await self.vc.send(out)
            except discord.HTTPException:
                pass


class _TextModal(discord.ui.Modal):
    def __init__(self, mgr, vc, action: str, title: str, label: str, max_len: int):
        super().__init__(title=title)
        self.mgr, self.vc, self.action = mgr, vc, action
        self.field = discord.ui.TextInput(label=label, max_length=max_len)
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction):
        value = str(self.field.value).strip()
        if self.action == "rename":
            out = await self.mgr.act_rename(self.vc, interaction.user, value)
        elif self.action == "status":
            out = await self.mgr.act_status(self.vc, interaction.user, value)
        else:  # size
            if not value.isdigit():
                out = "Size must be a number 0–99."
            else:
                out = await self.mgr.act_size(self.vc, interaction.user, int(value))
        await interaction.response.send_message(out, ephemeral=True)


class RoomPanel(discord.ui.View):
    """Persistent dashboard; identifies the room from the channel it lives in."""

    def __init__(self, mgr: RoomManager):
        super().__init__(timeout=None)
        self.mgr = mgr
        # third slot of the access row: Minors where configured, Random elsewhere
        if mgr.minor_role_id:
            btn = discord.ui.Button(label="Minors", emoji="🔞",
                                    style=discord.ButtonStyle.secondary,
                                    custom_id="vcr:minors", row=1)
            async def _cb(interaction: discord.Interaction):
                if (vc := await self._guard(interaction)):
                    await self._reply(interaction, await self.mgr.act_nominors(vc, interaction.user), announce=True)
        else:
            btn = discord.ui.Button(label="Random", emoji="🎲",
                                    style=discord.ButtonStyle.secondary,
                                    custom_id="vcr:random", row=1)
            async def _cb(interaction: discord.Interaction):
                if (vc := await self._guard(interaction)):
                    await self._reply(interaction, await self.mgr.act_random(vc, interaction.user))
        btn.callback = _cb
        self.add_item(btn)

        kick = discord.ui.Button(label="Kick", emoji="👢",
                                 style=discord.ButtonStyle.secondary,
                                 custom_id="vcr:kick", row=2)
        async def _kick(interaction: discord.Interaction):
            if (vc := await self._guard(interaction)):
                view = _MemberAction(self.mgr, vc, "kick")
                if view.empty:
                    return await interaction.response.send_message(
                        "Nobody else is in the room to kick.", ephemeral=True)
                await interaction.response.send_message(
                    "Kick who? (they can rejoin unless banned)", view=view, ephemeral=True)
        kick.callback = _kick
        self.add_item(kick)

    async def _guard(self, interaction: discord.Interaction) -> discord.VoiceChannel | None:
        vc = interaction.channel
        if not isinstance(vc, discord.VoiceChannel) or self.mgr.room(vc.id) is None:
            await interaction.response.send_message(
                "This room is no longer active.", ephemeral=True)
            return None
        return vc

    async def _reply(self, interaction, out: str, announce: bool = False):
        if announce and not out.startswith(("⛔", "⚠️")):
            await interaction.response.send_message(out)   # public, exactly once
        else:
            await interaction.response.send_message(out, ephemeral=True)

    @discord.ui.button(label="Rename", emoji="📝", style=discord.ButtonStyle.primary,
                       custom_id="vcr:rename", row=0)
    async def b_rename(self, interaction, _):
        if (vc := await self._guard(interaction)):
            await interaction.response.send_modal(
                _TextModal(self.mgr, vc, "rename", "Rename room", "New room name", 100))

    @discord.ui.button(label="Status", emoji="💬", style=discord.ButtonStyle.primary,
                       custom_id="vcr:status", row=0)
    async def b_status(self, interaction, _):
        if (vc := await self._guard(interaction)):
            await interaction.response.send_modal(
                _TextModal(self.mgr, vc, "status", "Set room status", "Status", 500))

    @discord.ui.button(label="Size", emoji="👥", style=discord.ButtonStyle.primary,
                       custom_id="vcr:size", row=0)
    async def b_size(self, interaction, _):
        if (vc := await self._guard(interaction)):
            await interaction.response.send_modal(
                _TextModal(self.mgr, vc, "size", "Room size", "Max members (0 = unlimited)", 2))

    @discord.ui.button(label="Lock", emoji="🔒", style=discord.ButtonStyle.secondary,
                       custom_id="vcr:lock", row=1)
    async def b_lock(self, interaction, _):
        if (vc := await self._guard(interaction)):
            await self._reply(interaction, await self.mgr.act_lock(vc, interaction.user), announce=True)

    @discord.ui.button(label="Voice", emoji="🎙️", style=discord.ButtonStyle.secondary,
                       custom_id="vcr:voice", row=1)
    async def b_voice(self, interaction, _):
        if (vc := await self._guard(interaction)):
            await self._reply(interaction, await self.mgr.act_voice(vc, interaction.user), announce=True)

    @discord.ui.button(label="Ban", emoji="🔨", style=discord.ButtonStyle.danger,
                       custom_id="vcr:ban", row=2)
    async def b_ban(self, interaction, _):
        if (vc := await self._guard(interaction)):
            view = _MemberAction(self.mgr, vc, "ban")
            if view.empty:
                return await interaction.response.send_message(
                    "Nobody else is in the room — `..ban <name> [30m|6h|3d|perm]` works "
                    "for anyone, present or not (default 1 day).", ephemeral=True)
            await interaction.response.send_message(
                "Ban who? (`..ban <name> 30` = 30-min ban, works for absent members too)",
                view=view, ephemeral=True)

    @discord.ui.button(label="Unban", emoji="♻️", style=discord.ButtonStyle.secondary,
                       custom_id="vcr:unban", row=2)
    async def b_unban(self, interaction, _):
        if (vc := await self._guard(interaction)):
            view = _MemberAction(self.mgr, vc, "unban")
            if view.empty:
                return await interaction.response.send_message(
                    "Nobody is banned from this room. 🎉", ephemeral=True)
            await interaction.response.send_message("Unban who?", view=view, ephemeral=True)

    @discord.ui.button(label="Bans", emoji="📋", style=discord.ButtonStyle.secondary,
                       custom_id="vcr:bans", row=3)
    async def b_bans(self, interaction, _):
        if (vc := await self._guard(interaction)):
            await interaction.response.send_message(self.mgr.bans_text(vc), ephemeral=True)

    @discord.ui.button(label="Mod", emoji="🛡️", style=discord.ButtonStyle.secondary,
                       custom_id="vcr:mod", row=3)
    async def b_mod(self, interaction, _):
        if (vc := await self._guard(interaction)):
            view = _MemberAction(self.mgr, vc, "mod")
            if view.empty:
                return await interaction.response.send_message(
                    "Nobody else is in the room to promote.", ephemeral=True)
            await interaction.response.send_message(
                "Toggle room mod for… (✓ marks current mods)", view=view, ephemeral=True)

    @discord.ui.button(label="Hush", emoji="🤫", style=discord.ButtonStyle.secondary,
                       custom_id="vcr:hush", row=3)
    async def b_hush(self, interaction, _):
        if (vc := await self._guard(interaction)):
            view = _MemberAction(self.mgr, vc, "hush")
            if view.empty:
                return await interaction.response.send_message(
                    "Nobody else is in the room to hush.", ephemeral=True)
            await interaction.response.send_message(
                "Toggle hush for… (✓ marks currently hushed)", view=view, ephemeral=True)

    @discord.ui.button(label="Claim", emoji="👑", style=discord.ButtonStyle.success,
                       custom_id="vcr:claim", row=4)
    async def b_claim(self, interaction, _):
        if (vc := await self._guard(interaction)):
            await self._reply(interaction, await self.mgr.act_claim(vc, interaction.user), announce=True)

    @discord.ui.button(label="Transfer", emoji="➡️", style=discord.ButtonStyle.secondary,
                       custom_id="vcr:transfer", row=4)
    async def b_transfer(self, interaction, _):
        if (vc := await self._guard(interaction)):
            view = _MemberAction(self.mgr, vc, "transfer")
            if view.empty:
                return await interaction.response.send_message(
                    "Nobody else is in the room to transfer to.", ephemeral=True)
            await interaction.response.send_message("Transfer ownership to…", view=view, ephemeral=True)

    @discord.ui.button(label="Abandon", emoji="🏳️", style=discord.ButtonStyle.danger,
                       custom_id="vcr:abandon", row=4)
    async def b_abandon(self, interaction, _):
        if (vc := await self._guard(interaction)):
            await self._reply(interaction, await self.mgr.act_abandon(vc, interaction.user), announce=True)
