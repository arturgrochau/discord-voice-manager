# 🎙️ P&P Sentinel — Discord Voice Manager

A voice-management and moderation bot built for the **Politics & Philosophy** Discord server, self-hosted on macOS. Modernized to discord.py 2.x with slash commands.

Originally a small companion bot for VoiceMaster-style temp voice channels (auto-clearing stale server mutes); now a full moderation sidekick with persistent records.

## ✨ Features

### 🔊 Voice management
- **Auto-unmute** — users who join/switch voice channels while carrying a stale server mute are unmuted automatically, logged, and DM'd.
- **Sticky mutes** — `/stickymute` keeps a user muted across rejoins (exempt from auto-unmute); `/unstickymute` releases them.
- Manual mute/unmute actions are logged to the voice log with the acting moderator (from the audit log).

### ⛓️ Detainment
- `/detain` + `.detain` — assign the Detained role, disconnect from voice, DM the user, log the action, and record it in SQLite.
- `/undetain` + `.undetain` — release, with the same trail.
- `/detainhistory` — full per-user detention history.
- Manual role add/removals are detected and logged too.

### 🛡️ Moderation
`/ban` (with message-deletion window) · `/unban` · `/kick` · `/timeout` · `/untimeout` · `/warn` · `/warnings` · `/clearwarnings` · `/purge` · `/slowmode` · `/lock` · `/unlock`

All actions: hierarchy-checked, reason-logged to the mod log, DM'd to the target when possible, and persisted (warnings/detentions) in `sentinel.db`.

### ℹ️ Info
`/userinfo` · `/serverinfo` · `/credits` (also `.credits`)

## 🛠️ Setup

Requirements: Python 3.10+, a bot token with **Server Members** and **Message Content** privileged intents enabled.

```bash
git clone https://github.com/arturgrochau/discord-voice-manager.git
cd discord-voice-manager
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config_template.json config.json   # fill in your IDs
echo 'DISCORD_BOT_TOKEN=your-token' > .env
```

`config.json`:

| Key | Meaning |
|---|---|
| `GUILD_ID` | Your server ID (slash commands sync here instantly) |
| `DETAIN_ROLE_ID` | Role assigned by `/detain` |
| `MOD_LOG_CHANNEL_ID` | Channel for moderation action embeds |
| `DETAIN_LOG_CHANNEL_ID` | Channel for detain/release embeds (falls back to mod log) |
| `VOICE_LOG_CHANNEL_ID` | Channel for voice mute/unmute embeds |
| `AUTO_UNMUTE` | `true`/`false` — enable the auto-unmute behavior |

Run directly:

```bash
.venv/bin/python bot.py
```

## 🚀 Run 24/7 on macOS (launchd)

```bash
./scripts/install.sh
```

Installs a LaunchAgent (`com.arturgrochau.pnp-sentinel`) that starts the bot at login, keeps it alive, restarts it on crash/network recovery, and writes rotating logs to `logs/`.

```bash
tail -f logs/sentinel.log                                  # watch
launchctl kickstart -k gui/$(id -u)/com.arturgrochau.pnp-sentinel   # restart
launchctl bootout gui/$(id -u)/com.arturgrochau.pnp-sentinel        # stop
```

## 🧩 Architecture

```
bot.py               # entrypoint: intents, config, cog loading, slash sync
db.py                # aiosqlite persistence (warnings, detentions, sticky mutes)
cogs/voice.py        # auto-unmute + voice logging + sticky mutes
cogs/moderation.py   # detain + full moderation suite
cogs/info.py         # credits/userinfo/serverinfo
launchd/…plist       # macOS service definition
scripts/install.sh   # one-shot service install
```

The bot pairs with a self-hosted [NadekoBot](https://nadekobot.readthedocs.io/) instance that handles spam/raid protection, filters, and general utility — Sentinel keeps the `.` prefix surface minimal (`.detain`, `.undetain`, `.credits`) to avoid colliding with it.

## 📜 License

MIT — built by [Artur Grochau](https://github.com/arturgrochau).
