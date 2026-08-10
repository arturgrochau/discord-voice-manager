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
import re
import time
import urllib.request
from pathlib import Path

import discord

log = logging.getLogger("politics-helper.music")

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"
FFMPEG_OPTS = "-vn"
IDLE_SECONDS_DEFAULT = 300
MAX_QUEUE = 100
PLAYLIST_CAP = 25

COMMANDS = {
    "play", "p", "spotify", "sp", "summon", "music", "join", "skip", "next",
    "pause", "resume", "stop", "leave", "disconnect", "dc", "queue", "q", "np",
    "nowplaying", "vol", "volume", "shuffle", "clear",
}

_SPOTIFY_RE = re.compile(
    r"(?:open\.spotify\.com/(?:intl-\w+/)?|spotify:)(track|album|playlist)[/:]([A-Za-z0-9]+)")
_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def is_spotify(text: str) -> bool:
    return bool(_SPOTIFY_RE.search(text or ""))


def _deep_find(obj, key):
    """First value for `key` anywhere in a nested dict/list."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if key in cur:
                return cur[key]
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def spotify_tracks(url: str) -> tuple[str, list[tuple[str, int]]] | None:
    """Read a Spotify track/album/playlist link via its public embed page.

    Spotify audio is DRM'd and cannot be streamed by a bot, so we lift the
    track list (title + artist + duration) and each song is played from
    YouTube instead. No Spotify API key needed — the embed page ships the
    data in a __NEXT_DATA__ blob. Returns (collection_name, [(query, ms)]).
    """
    m = _SPOTIFY_RE.search(url)
    if not m:
        return None
    kind, sid = m.group(1), m.group(2)
    try:
        req = urllib.request.Request(
            f"https://open.spotify.com/embed/{kind}/{sid}",
            headers={"User-Agent": _BROWSER_UA})
        html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
    except Exception as e:
        log.warning("Spotify fetch failed for %s: %s", url, e)
        return None
    mm = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not mm:
        return None
    try:
        data = json.loads(mm.group(1))
    except json.JSONDecodeError:
        return None
    name = _deep_find(data, "name") or ""
    if name in ("", "Page not found"):  # private / deleted / unreadable
        return None
    track_list = _deep_find(data, "trackList") or []
    out: list[tuple[str, int]] = []
    if track_list:  # album or playlist
        for t in track_list:
            title = (t.get("title") or "").strip()
            artist = (t.get("subtitle") or "").strip()
            if title:
                out.append((f"{artist} {title}".strip(), int(t.get("duration") or 0)))
    elif kind == "track":  # single track: the entity itself is the song
        artist = _deep_find(data, "subtitle") or ""
        out.append((f"{artist} {name}".strip(), int(_deep_find(data, "duration") or 0)))
    # album/playlist with no readable tracks => private or region-locked
    return (name, out) if out else None


def _fmt_dur(seconds) -> str:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "live"
    return f"{s // 3600}:{s % 3600 // 60:02}:{s % 60:02}" if s >= 3600 else f"{s // 60}:{s % 60:02}"


DOWNLOAD_MAX_SECONDS = 15 * 60  # songs get fully downloaded; longer = stream
MUSIC_MAX_CANDIDATES = 4        # try up to N search hits before giving up on a track


class Track:
    __slots__ = ("title", "url", "duration", "requester_id", "retried", "path")

    def __init__(self, title, url, duration, requester_id):
        self.title = title
        self.url = url
        self.duration = duration
        self.requester_id = requester_id
        self.retried = False  # one free retry when a stream dies mid-track
        self.path = None      # local file when the track was pre-downloaded


class MusicPlayer:
    def __init__(self, client: discord.Client, config: dict, base_dir: Path | None = None):
        self.client = client
        self.state_path = (base_dir or Path(".")) / "music_state.json"
        self.cache_dir = (base_dir or Path(".")) / "music_cache"
        self._last_channel_id = 0  # watchdog reconnect target
        # never occupy the Join-to-Create trigger channel: camping it there
        # blocks the room-spawn flow and leaves members stuck
        self.trigger_id = int(config.get("JOIN_TO_CREATE_CHANNEL_ID", 0) or 0)
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
        # never resume inside the Join-to-Create trigger channel
        if ch is None or (self.trigger_id and ch.id == self.trigger_id):
            self._clear_state()
            return
        humans = [m for m in ch.members if not m.bot]
        if not humans:
            self._clear_state()
            return
        self.text_channel = self.client.get_channel(int(data.get("text") or 0))
        self.volume = float(data.get("volume", 1.0))
        self.queue = [Track(*t) for t in data["queue"]]
        try:
            self.voice = await ch.connect(self_deaf=True)
        except Exception as e:
            # Keep the saved queue/state intact — the idle-loop watchdog retries
            # the reconnect every 30s. Wiping it here loses the whole queue on a
            # single transient failure (the M1's periodic DNS blips).
            log.warning("Music restore: reconnect failed (will retry): %s", e)
            self._last_channel_id = ch.id
            return
        self._last_channel_id = ch.id
        await self._maximize_channel(ch)
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

    def _search_candidates(self, query: str) -> list[str]:
        """Flat-search YouTube and return several candidate video URLs (best
        first), so a blocked/dead top hit falls back to the next real result.
        Filters livestreams and non-song-length hits to stay on actual music."""
        import yt_dlp
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noprogress": True,
                                   "extract_flat": True, "socket_timeout": 15}) as y:
                res = y.extract_info(f"ytsearch{MUSIC_MAX_CANDIDATES}:{query}", download=False)
        except Exception as e:
            log.warning("Search failed for %r: %s", query[:60], e)
            return [f"ytsearch1:{query}"]   # let the caller try a plain search
        out = []
        for e in (res.get("entries") or []):
            if not e:
                continue
            if e.get("is_live") or e.get("live_status") == "is_live":
                continue
            dur = e.get("duration") or 0
            if dur and dur > 3600:          # >1h = almost never the song (mix/stream)
                continue
            url = e.get("url") or e.get("webpage_url") or e.get("id")
            if url:
                out.append(url if str(url).startswith("http") else f"https://www.youtube.com/watch?v={url}")
        return out or [f"ytsearch1:{query}"]

    async def _prepare_source(self, track: Track) -> tuple[str, bool] | None:
        """Get something ffmpeg can play. Returns (source, is_local).

        Song-length tracks are fully downloaded first — a local file cannot
        die to a YouTube 403/throttle mid-play, which is the classic way
        music bots 'randomly stop'. Long tracks and livestreams still stream
        (with ffmpeg reconnect flags) to avoid huge downloads.
        """
        if track.path and Path(track.path).exists():
            return track.path, True  # retry reuses the finished download

        def work():
            import yt_dlp
            want_dl = track.duration is not None and 0 < int(track.duration or 0) <= DOWNLOAD_MAX_SECONDS
            # webm-first: YouTube's opus streams (format 251, ~160 kbps VBR)
            # beat the m4a/aac alternatives
            opts = {"format": "bestaudio[ext=webm]/bestaudio/best",
                    "quiet": True, "no_warnings": True,
                    "noplaylist": True, "socket_timeout": 15, "noprogress": True,
                    "retries": 3, "fragment_retries": 5}
            if want_dl:
                self.cache_dir.mkdir(exist_ok=True)
                opts["outtmpl"] = str(self.cache_dir / "%(id)s.%(ext)s")

            # Build an ordered list of candidates to try. A single bad/blocked
            # result should never skip the song — we fall back to the next
            # search hit (and, for a dead direct URL, to a title search).
            candidates: list[str] = []
            if track.url.startswith("ytsearch"):
                query = track.url.split(":", 1)[1] if ":" in track.url else track.url
                candidates = self._search_candidates(query)
            else:
                candidates = [track.url]
                if track.title and track.title != track.url:
                    candidates += self._search_candidates(track.title)

            last_err = None
            for cand in candidates[:MUSIC_MAX_CANDIDATES]:
                try:
                    with yt_dlp.YoutubeDL(opts) as y:
                        info = y.extract_info(cand, download=want_dl)
                    if info.get("entries") is not None:   # a search/playlist wrapper
                        ents = [e for e in info["entries"] if e]
                        if not ents:
                            continue
                        info = ents[0]
                    if not track.title or track.title == track.url:
                        track.title = info.get("title") or track.title
                    if want_dl:
                        return y.prepare_filename(info), True
                    return info.get("url"), False
                except Exception as e:
                    last_err = e
                    log.warning("Candidate failed (%s): %s", str(cand)[:60], e)
                    continue
            if last_err:
                raise last_err
            return None, False

        try:
            src, local = await self.client.loop.run_in_executor(None, work)
        except Exception as e:
            log.warning("Prepare failed for %s: %s", track.url, e)
            return None
        if src is None:
            return None
        if local:
            track.path = src
        return src, local

    # -- voice lifecycle ---------------------------------------------------

    async def _ensure_voice(self, member: discord.Member,
                            fallback: discord.VoiceChannel | None = None) -> str | None:
        """Join the member's channel. Returns an error string or None."""
        if member.voice is None or member.voice.channel is None:
            # self-authored ops command typed in a voice chat: that channel is
            # the target (the bot has no voice state of its own to follow)
            if fallback is not None and member.id == getattr(self.client.user, "id", 0):
                target = fallback
            else:
                return "Join a voice channel first, then I'll follow you in."
        else:
            target = member.voice.channel
        if self.trigger_id and target.id == self.trigger_id:
            return ("That's the Join-to-Create channel — it makes you a room. "
                    "Hop into your room (or any other voice channel) and I'll follow.")
        if self.voice and self.voice.is_connected():
            if self.voice.channel.id == target.id:
                return None
            if self.current or self.queue:
                humans = [m for m in self.voice.channel.members if not m.bot]
                if humans and member not in self.voice.channel.members:
                    return (f"I'm playing for people in **{self.voice.channel.name}**, "
                            f"join there, or wait until the session ends.")
            await self.voice.move_to(target)
            self._last_channel_id = target.id
            return None
        try:
            self.voice = await target.connect(self_deaf=True)
        except (discord.HTTPException, asyncio.TimeoutError) as e:
            log.warning("Voice connect failed: %s", e)
            return "Couldn't connect to voice, try again in a moment."
        self._last_channel_id = target.id
        log.info("Voice connected: %s", target.name)
        await self._maximize_channel(target)
        self._idle_since = time.monotonic()
        return None

    async def _maximize_channel(self, ch) -> None:
        """Raise the channel to the server's best bitrate; 64k default halves
        what boosts already paid for."""
        try:
            limit = int(ch.guild.bitrate_limit)
            if (ch.bitrate or 0) < limit:
                await ch.edit(bitrate=limit, reason="Music: max audio quality")
                log.info("Bitrate raised to %dkbps in %s", limit // 1000, ch.name)
        except discord.HTTPException as e:
            log.warning("Bitrate bump failed for %s: %s", ch, e)

    def _encoder_opts(self) -> dict:
        """Opus encoder tuned for music at the channel's full bitrate."""
        kbps = 128
        if self.voice and self.voice.is_connected():
            kbps = max(64, min(510, (self.voice.channel.bitrate or 64000) // 1000))
        return {"application": "audio", "signal_type": "music",
                "bitrate": kbps, "bandwidth": "full",
                "fec": True, "expected_packet_loss": 0.01}

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
                self._purge_cache()
                # zombie-connection guard: if the channel we're bound to was
                # deleted (temp rooms get remade constantly), the voice client
                # can still report is_connected() while sending no audio. Treat
                # a vanished channel as a drop and reconnect where listeners are.
                bound = self.voice.channel.id if (self.voice and self.voice.channel) else 0
                channel_gone = bound and self.client.get_channel(bound) is None
                if not (self.voice and self.voice.is_connected()) or channel_gone:
                    if channel_gone:
                        log.warning("Music: bound channel %s vanished, tearing down zombie", bound)
                        try:
                            await self.voice.disconnect(force=True)
                        except discord.HTTPException:
                            pass
                        self.voice = None
                    if (self.current or self.queue) and self._last_channel_id \
                            and self.client.get_channel(self._last_channel_id):
                        await self._watchdog_resume()
                    elif channel_gone:  # nowhere valid to resume: stop cleanly
                        self.current = None
                        self.queue.clear()
                        self._clear_state()
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

    async def _watchdog_resume(self) -> None:
        ch = self.client.get_channel(self._last_channel_id)
        humans = [m for m in ch.members if not m.bot] if ch else []
        if not humans:
            self.current = None
            self.queue.clear()
            self._clear_state()
            return
        log.warning("Music watchdog: voice dropped mid-session, reconnecting to %s", ch.name)
        if self.voice:
            try:
                await self.voice.disconnect(force=True)
            except discord.HTTPException:
                pass
            self.voice = None
        try:
            self.voice = await ch.connect(self_deaf=True, timeout=15)
        except Exception as e:
            log.warning("Music watchdog: reconnect failed (%s), retrying next pass", e)
            return
        await self._maximize_channel(ch)
        if self.current:
            self.queue.insert(0, self.current)
            self.current = None
        await self._advance()

    def _purge_cache(self) -> None:
        """Downloaded tracks are throwaway: drop anything older than 3 h."""
        try:
            if not self.cache_dir.exists():
                return
            cutoff = time.time() - 3 * 3600
            for f in self.cache_dir.iterdir():
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
        except OSError:
            pass

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
                prepared = await self._prepare_source(track)
                if prepared is None:
                    if self.text_channel:
                        try:  # a failed announce must not abort the whole queue
                            await self.text_channel.send(
                                embed=self._embed(f"Skipping **{track.title}**, couldn't load it."),
                                delete_after=60)
                        except discord.HTTPException:
                            pass
                    continue
                source, is_local = prepared
                src = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(
                        source,
                        before_options=None if is_local else FFMPEG_BEFORE,
                        options=FFMPEG_OPTS),
                    volume=self.volume)
                self.current = track
                log.info("Now playing: %s (%s)", track.title,
                         "downloaded" if is_local else "streaming")

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

                    def _log_advance(f):
                        exc = f.exception()
                        if exc:
                            log.error("Music _advance failed: %r", exc)
                    fut.add_done_callback(_log_advance)

                try:
                    self.voice.play(src, after=after, **self._encoder_opts())
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
                "**Start** — `..play <name or link>` (YouTube search, links, "
                "playlists, and **Spotify** links all work) · `..summon` pulls "
                "me into your channel\n"
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

        fallback = message.channel if isinstance(message.channel, discord.VoiceChannel) else None
        if cmd in ("summon", "music", "join"):
            err = await self._ensure_voice(member, fallback)
            await say(err or (
                f"{self.emblem} In **{self.voice.channel.name}**. `..play <name or link>` to start."
                "\n-# Music sessions don't earn giveaway points."))
            return

        if cmd in ("spotify", "sp") and not is_spotify(arg):
            await say("Usage: `..spotify <Spotify track, album or playlist link>`")
            return

        if cmd in ("play", "p", "spotify", "sp"):
            if not arg:
                await say("Usage: `..play <song name, YouTube or Spotify link>`")
                return
            err = await self._ensure_voice(member, fallback)
            if err:
                await say(err)
                return
            if len(self.queue) >= MAX_QUEUE:
                await say(f"Queue is full ({MAX_QUEUE} tracks).")
                return

            # Spotify: resolve the link to song names, each plays from YouTube
            if is_spotify(arg):
                resolved = await self.client.loop.run_in_executor(
                    None, spotify_tracks, arg)
                if not resolved or not resolved[1]:
                    await say("Couldn't read that Spotify link. **Private playlists "
                              "can't be read** — set it to public (Spotify: playlist "
                              "→ ⋯ → Share → make public), or paste a song/album link.")
                    return
                name, songs = resolved
                room = min(len(songs), MAX_QUEUE - len(self.queue), PLAYLIST_CAP)
                for query, ms in songs[:room]:
                    self.queue.append(Track(query, f"ytsearch1:{query}",
                                            (ms // 1000) or None, member.id))
                if room == 1:
                    await say(f"{self.emblem} Queued **{songs[0][0]}** from Spotify.")
                else:
                    await say(f"{self.emblem} Queued **{room} tracks** from the "
                              f"Spotify {'playlist' if room > 1 else 'set'} **{name}**.")
                self._save_state()
                if not self.current:
                    await self._advance()
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
