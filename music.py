"""Music player for the helper — ..play in any chat, yt-dlp + ffmpeg.

Runs on the main bot's own gateway session (no third-party music bot):

  ..play <name or YouTube link>   search / link / playlist (first 25)
  ..summon | ..music | ..join     pull the bot into your voice channel
  ..skip  ..pause  ..resume       transport
  ..queue | ..q   ..np            what's queued / playing
  ..vol <0-150>   ..shuffle  ..clear
  ..stop | ..leave                stop and disconnect

Design notes:
- Stream URLs from yt-dlp expire, so each track is re-extracted right
  before it plays; the queue holds only titles + page URLs.
- Audio path is bestaudio -> ffmpeg -> opus at the channel bitrate, with
  reconnect flags so a network blip doesn't kill the track.
- Auto-leave: disconnects after IDLE_SECONDS with nothing playing, or
  when no humans are left in the channel.
- Contest integration lives in contest.py: the channel the player sits
  in earns no giveaway VC points (music must not be a point farm).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import discord

log = logging.getLogger("politics-helper.music")

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"
FFMPEG_OPTS = "-vn"
IDLE_SECONDS_DEFAULT = 300
MAX_QUEUE = 100
PLAYLIST_CAP = 25

COMMANDS = {
    "play", "p", "summon", "music", "join", "skip", "next", "pause",
    "resume", "stop", "leave", "disconnect", "dc", "queue", "q", "np",
    "nowplaying", "vol", "volume", "shuffle", "clear",
}


def _fmt_dur(seconds) -> str:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "live"
    return f"{s // 3600}:{s % 3600 // 60:02}:{s % 60:02}" if s >= 3600 else f"{s // 60}:{s % 60:02}"


class Track:
    __slots__ = ("title", "url", "duration", "requester_id", "retried")

    def __init__(self, title, url, duration, requester_id):
        self.title = title
        self.url = url
        self.duration = duration
        self.requester_id = requester_id
        self.retried = False  # one free retry when a stream dies mid-track


class MusicPlayer:
    def __init__(self, client: discord.Client, config: dict, base_dir: Path | None = None):
        self.client = client
        self.state_path = (base_dir or Path(".")) / "music_state.json"
        self.enabled = bool(config.get("MUSIC_ENABLED"))
        self.idle_seconds = int(config.get("MUSIC_IDLE_SECONDS", IDLE_SECONDS_DEFAULT))
        self.color = int(str(config.get("PANEL_COLOR", "0x5865F2")), 16)
        self.emblem = config.get("PANEL_EMBLEM", "🎵")
        self.queue: list[Track] = []
        self.current: Track | None = None
        self.voice: discord.VoiceClient | None = None
        self.volume = 1.0
        self.text_channel = None       # where to announce tracks/departures
        self._idle_since = time.monotonic()
        self._advancing = False
        self._ytdl = None

    def start(self) -> None:
        """Call from on_ready (the client loop doesn't exist at __init__)."""
        if self.enabled:
            self.client.loop.create_task(self._idle_loop())
            self.client.loop.create_task(self._restore())

    # -- restart survival --------------------------------------------------

    def _save_state(self) -> None:
        try:
            tracks = ([self.current] if self.current else []) + self.queue
            self.state_path.write_text(json.dumps({
                "voice": self.channel_id,
                "text": self.text_channel.id if self.text_channel else 0,
                "volume": self.volume,
                "queue": [[t.title, t.url, t.duration, t.requester_id] for t in tracks],
            }))
        except OSError:
            pass

    def _clear_state(self) -> None:
        try:
            self.state_path.write_text("{}")
        except OSError:
            pass

    async def _restore(self) -> None:
        """Pick a session back up after a restart killed it mid-track."""
        try:
            data = json.loads(self.state_path.read_text())
        except Exception:
            return
        if not (data.get("voice") and data.get("queue")):
            return
        ch = self.client.get_channel(int(data["voice"]))
        humans = [m for m in ch.members if not m.bot] if ch else []
        if not humans:
            self._clear_state()
            return
        self.text_channel = self.client.get_channel(int(data.get("text") or 0))
        self.volume = float(data.get("volume", 1.0))
        self.queue = [Track(*t) for t in data["queue"]]
        try:
            self.voice = await ch.connect(self_deaf=True)
        except Exception as e:
            log.warning("Music restore: reconnect failed: %s", e)
            self._clear_state()
            return
        log.info("Music restore: resuming %d track(s) in %s", len(self.queue), ch.name)
        if self.text_channel:
            try:
                await self.text_channel.send(
                    embed=self._embed("🔁 Back after a restart, resuming the queue."),
                    delete_after=60)
            except discord.HTTPException:
                pass
        await self._advance()

    @property
    def channel_id(self) -> int:
        """Voice channel the player is in right now (0 when disconnected)."""
        return self.voice.channel.id if self.voice and self.voice.is_connected() else 0

    # -- extraction (blocking yt-dlp work stays off the event loop) --------

    def _ydl(self):
        if self._ytdl is None:
            import yt_dlp
            self._ytdl = yt_dlp.YoutubeDL({
                "format": "bestaudio/best",
                "quiet": True, "no_warnings": True,
                "default_search": "ytsearch1",
                "extract_flat": "in_playlist",
                "playlist_items": f"1-{PLAYLIST_CAP}",
                "socket_timeout": 10,
                "noprogress": True,
            })
        return self._ytdl

    async def _extract(self, query: str, flat: bool = True) -> dict | None:
        def work():
            return self._ydl().extract_info(query, download=False, process=flat)
        return await self.client.loop.run_in_executor(None, work)

    async def _stream_url(self, track: Track) -> str | None:
        """Fresh extraction right before playback: stream URLs expire."""
        def work():
            import yt_dlp
            with yt_dlp.YoutubeDL({"format": "bestaudio/best", "quiet": True,
                                   "no_warnings": True, "noplaylist": True,
                                   "socket_timeout": 10, "noprogress": True}) as y:
                info = y.extract_info(track.url, download=False)
            return info.get("url")
        try:
            return await self.client.loop.run_in_executor(None, work)
        except Exception as e:
            log.warning("Extraction failed for %s: %s", track.url, e)
            return None

    # -- voice lifecycle ---------------------------------------------------

    async def _ensure_voice(self, member: discord.Member) -> str | None:
        """Join the member's channel. Returns an error string or None."""
        if member.voice is None or member.voice.channel is None:
            return "Join a voice channel first, then I'll follow you in."
        target = member.voice.channel
        if self.voice and self.voice.is_connected():
            if self.voice.channel.id == target.id:
                return None
            if self.current or self.queue:
                humans = [m for m in self.voice.channel.members if not m.bot]
                if humans and member not in self.voice.channel.members:
                    return (f"I'm playing for people in **{self.voice.channel.name}**, "
                            f"join there, or wait until the session ends.")
            await self.voice.move_to(target)
            return None
        try:
            self.voice = await target.connect(self_deaf=True)
        except (discord.HTTPException, asyncio.TimeoutError) as e:
            log.warning("Voice connect failed: %s", e)
            return "Couldn't connect to voice, try again in a moment."
        self._idle_since = time.monotonic()
        return None

    async def _disconnect(self, reason: str | None = None) -> None:
        if self.voice:
            try:
                if self.voice.is_playing() or self.voice.is_paused():
                    self.voice.stop()
                await self.voice.disconnect(force=True)
            except discord.HTTPException:
                pass
        self.voice = None
        self.current = None
        self.queue.clear()
        self._clear_state()
        if reason and self.text_channel:
            try:
                await self.text_channel.send(
                    embed=self._embed(reason), delete_after=120)
            except discord.HTTPException:
                pass

    async def _idle_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                if not (self.voice and self.voice.is_connected()):
                    continue
                humans = [m for m in self.voice.channel.members if not m.bot]
                if not humans:
                    await self._disconnect("Everyone left, packing up the records. 👋")
                    continue
                if self.voice.is_playing() or self.voice.is_paused():
                    self._idle_since = time.monotonic()
                elif time.monotonic() - self._idle_since > self.idle_seconds:
                    await self._disconnect(
                        f"Nothing queued for {self.idle_seconds // 60} minutes, leaving voice. "
                        "`..play` brings me back.")
            except Exception:
                log.exception("Music idle loop error")

    # -- playback ----------------------------------------------------------

    async def _advance(self) -> None:
        if self._advancing:
            return
        self._advancing = True
        try:
            while self.queue:
                if not (self.voice and self.voice.is_connected()):
                    return
                track = self.queue.pop(0)
                stream = await self._stream_url(track)
                if stream is None:
                    if self.text_channel:
                        await self.text_channel.send(
                            embed=self._embed(f"Skipping **{track.title}**, couldn't load it."),
                            delete_after=60)
                    continue
                src = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(stream, before_options=FFMPEG_BEFORE,
                                           options=FFMPEG_OPTS),
                    volume=self.volume)
                self.current = track

                def after(err, _self=self, _track=track):
                    if err:
                        log.warning("Playback error on %r: %s", _track.title, err)
                        if not _track.retried:
                            # one retry: streams occasionally die mid-track
                            _track.retried = True
                            _self.queue.insert(0, _track)
                    _self.current = None
                    _self._idle_since = time.monotonic()
                    fut = asyncio.run_coroutine_threadsafe(_self._advance(), _self.client.loop)
                    fut.add_done_callback(lambda f: f.exception())

                try:
                    self.voice.play(src, after=after)
                except discord.ClientException as e:
                    log.warning("voice.play failed: %s", e)
                    self.current = None
                    continue
                self._save_state()
                if self.text_channel:
                    try:
                        await self.text_channel.send(
                            embed=self._embed(
                                f"▶️ Now playing **[{track.title}]({track.url})**"
                                f" · {_fmt_dur(track.duration)}"
                                f" · requested by <@{track.requester_id}>"),
                            delete_after=1800)
                    except discord.HTTPException:
                        pass
                return
            self.current = None
        finally:
            self._advancing = False

    # -- command surface ---------------------------------------------------

    def _embed(self, text: str) -> discord.Embed:
        return discord.Embed(description=text, color=self.color)

    def commands_embed(self, info_link: str = "") -> discord.Embed:
        tail = f"\n📖 Full reference: {info_link}" if info_link else ""
        return discord.Embed(
            title="🎵 Music commands",
            description=(
                "**Start** — `..play <name or link>` (searches YouTube, links and "
                "playlists work) · `..summon` pulls me into your channel\n"
                "**Control** — `..skip` (also `..next`) · `..pause` · `..resume` · "
                "`..stop` (leave)\n"
                "**Queue** — `..queue` (also `..q`) shows what's up · `..np` current "
                "track · `..shuffle` · `..clear`\n"
                "**Sound** — `..vol <0-150>`\n"
                + tail +
                "\n-# ▶️ `..skip`/`..next` jump forward; `..queue`/`..q` just show the list."
                "\n-# 💤 I leave on my own after 5 quiet minutes, or when everyone's gone."
                "\n-# 🎁 Music channels earn no giveaway points."
            ),
            color=self.color,
        )

    async def handle(self, message: discord.Message, cmd: str, arg: str) -> None:
        member = message.author
        self.text_channel = message.channel

        async def say(text, ttl=120):
            try:
                await message.channel.send(embed=self._embed(text), delete_after=ttl)
            except discord.HTTPException:
                pass

        if cmd in ("summon", "music", "join"):
            err = await self._ensure_voice(member)
            await say(err or (
                f"{self.emblem} In **{self.voice.channel.name}**. `..play <name or link>` to start."
                "\n-# Music sessions don't earn giveaway points."))
            return

        if cmd in ("play", "p"):
            if not arg:
                await say("Usage: `..play <song name or YouTube link>`")
                return
            err = await self._ensure_voice(member)
            if err:
                await say(err)
                return
            if len(self.queue) >= MAX_QUEUE:
                await say(f"Queue is full ({MAX_QUEUE} tracks).")
                return
            try:
                info = await self._extract(arg)
            except Exception as e:
                log.warning("Search failed for %r: %s", arg, e)
                info = None
            if not info:
                await say("Couldn't find anything for that, try different words or a direct link.")
                return
            entries = info.get("entries")
            if entries is not None:  # search result or playlist
                entries = [e for e in entries if e][:PLAYLIST_CAP]
                if not entries:
                    await say("Couldn't find anything for that, try different words or a direct link.")
                    return
                if len(entries) > 1:  # a real playlist
                    room = min(len(entries), MAX_QUEUE - len(self.queue))
                    for e in entries[:room]:
                        self.queue.append(Track(
                            e.get("title") or "Unknown", e.get("url") or e.get("webpage_url"),
                            e.get("duration"), member.id))
                    await say(f"{self.emblem} Queued **{room} tracks** from the playlist.")
                else:
                    e = entries[0]
                    self.queue.append(Track(
                        e.get("title") or "Unknown", e.get("url") or e.get("webpage_url"),
                        e.get("duration"), member.id))
                    if self.current:
                        await say(f"{self.emblem} Queued **{e.get('title')}** "
                                  f"(position {len(self.queue)}).")
            else:
                self.queue.append(Track(
                    info.get("title") or "Unknown",
                    info.get("webpage_url") or arg, info.get("duration"), member.id))
                if self.current:
                    await say(f"{self.emblem} Queued **{info.get('title')}** "
                              f"(position {len(self.queue)}).")
            self._save_state()
            if not self.current:
                await self._advance()
            return

        # everything below operates on a live session
        if not (self.voice and self.voice.is_connected()):
            await say("I'm not in a voice channel. `..play` or `..summon` first.")
            return
        in_session = member.voice and member.voice.channel == self.voice.channel
        if cmd in ("skip", "next", "pause", "resume", "stop", "leave",
                   "disconnect", "dc", "vol", "volume", "shuffle", "clear") and not in_session:
            await say("Join the music channel to control playback.")
            return

        if cmd in ("skip", "next"):
            if self.voice.is_playing() or self.voice.is_paused():
                skipped = self.current.title if self.current else "track"
                self.voice.stop()  # after= callback advances the queue
                await say(f"⏭️ Skipped **{skipped}**.")
            else:
                await say("Nothing is playing.")
        elif cmd == "pause":
            if self.voice.is_playing():
                self.voice.pause()
                await say("⏸️ Paused. `..resume` when ready.")
            else:
                await say("Nothing is playing.")
        elif cmd == "resume":
            if self.voice.is_paused():
                self.voice.resume()
                await say("▶️ Resumed.")
            else:
                await say("Nothing is paused.")
        elif cmd in ("stop", "leave", "disconnect", "dc"):
            await self._disconnect()
            await say(f"{self.emblem} Stopped and left voice. `..play` starts a new session.")
        elif cmd in ("vol", "volume"):
            if not arg.isdigit() or not 0 <= int(arg) <= 150:
                await say("Usage: `..vol <0-150>` (100 = normal)")
                return
            self.volume = int(arg) / 100
            if self.voice.source and isinstance(self.voice.source, discord.PCMVolumeTransformer):
                self.voice.source.volume = self.volume
            await say(f"🔊 Volume set to **{arg}%**.")
        elif cmd == "shuffle":
            import random
            random.shuffle(self.queue)
            self._save_state()
            await say(f"🔀 Shuffled **{len(self.queue)}** queued tracks.")
        elif cmd == "clear":
            n = len(self.queue)
            self.queue.clear()
            self._save_state()
            await say(f"🗑️ Cleared **{n}** queued tracks (current song keeps playing).")
        elif cmd in ("queue", "q"):
            lines = []
            if self.current:
                lines.append(f"▶️ **{self.current.title}** · {_fmt_dur(self.current.duration)}")
            for i, t in enumerate(self.queue[:10], 1):
                lines.append(f"`{i}.` {t.title} · {_fmt_dur(t.duration)}")
            if len(self.queue) > 10:
                lines.append(f"-# …and {len(self.queue) - 10} more")
            await say("\n".join(lines) if lines else "Queue is empty. `..play` something.")
        elif cmd in ("np", "nowplaying"):
            if self.current:
                await say(f"▶️ **[{self.current.title}]({self.current.url})**"
                          f" · {_fmt_dur(self.current.duration)}"
                          f" · requested by <@{self.current.requester_id}>")
            else:
                await say("Nothing is playing.")
