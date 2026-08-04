"""Post (or refresh) the exhaustive room guide into the Join-to-Create #info.

Usage: python3 scripts/post_room_guide.py --server pp|hk

Idempotent like post_mod_guide.py: deletes the bot's previous messages in
the channel and reposts. Keep in sync with vc_rooms.py and music.py.
"""

import argparse
import json
import re
import time
import urllib.request
from pathlib import Path

UA = "DiscordBot (https://github.com/arturgrochau/discord-voice-manager, 1.0)"

SERVERS = {
    "pp": {
        "channel": "1296844416753078344",   # info (Join to Create category)
        "token_file": Path.home() / "Projects/nadekobot/nadeko-osx-arm64/data/creds.yml",
        "color": 0xC0A062,
        "emblem": "🏛️",
        "trigger": "👉Join To Create👈",
        "music": True,
        "afk": True,
        "minors": True,
    },
    "hk": {
        "channel": "1254103808749862954",   # info (Join to Create category)
        "token_file": Path.home() / "Projects/hk-sentinel/.env",
        "color": 0x1ABC9C,
        "emblem": "🏺",
        "trigger": "Join To Create",
        "music": False,
        "afk": False,
        "minors": False,
    },
}


def read_token(path: Path) -> str:
    text = path.read_text()
    m = re.search(r"^token:\s*'?([\w.\-]+)'?", text, re.M) \
        or re.search(r"DISCORD_BOT_TOKEN=([\w.\-]+)", text)
    return m.group(1)


def api(token: str, method: str, path: str, body=None):
    req = urllib.request.Request(
        "https://discord.com/api/v10" + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bot {token}", "User-Agent": UA,
                 **({"Content-Type": "application/json"} if body is not None else {})})
    r = urllib.request.urlopen(req)
    return json.load(r) if r.status != 204 else None


def build_embeds(s: dict) -> list[dict]:
    e = s["emblem"]

    intro = (
        f"Join **{s['trigger']}** and a private room spawns instantly, with you as "
        "owner. The bot moves you in, posts a **button panel** in the room chat, and "
        "DMs you the essentials.\n\n"
        "Everything you set — name, size, lock, speaking default, room mods, "
        "permanent bans, hushes — is **saved to your profile and re-applied** the "
        "next time you create a room. Empty rooms delete themselves.\n\n"
        "Commands are typed in the room's chat (click the 💬 on the voice channel). "
        "Names accept partial matches, @mentions, or ids: `..mod half` finds "
        "`halfway`. People do **not** need to be in the room — you can ban, unban, "
        "mod or unmod anyone on the server from your room chat."
    )

    setup = (
        "`..rename <name>` — rename the room (Discord allows 2 renames per 10 min)\n"
        "`..status <text>` — set the little status line under the channel name\n"
        "`..setsize <0-99>` (also `..size`, `..limit`) — user cap, 0 = unlimited\n"
        "`..lock` — toggle: nobody new can join (people inside stay)\n"
        "`..voice` — toggle the speaking default for newcomers: everyone may talk, "
        "or push-to-listen (you and your mods hand out the mic)\n"
        + ("`..nominors` — toggle: keeps members with the minor role out\n" if s["minors"] else "")
        + "`..panel` — re-post the button panel"
    )

    bans = (
        "`..ban <name> [length]` — kicks them out and keeps them out\n"
        "• **No length = 1 day.**\n"
        "• Lengths run from minutes to years: `30m`, `6h`, `3d`, `2w`, `1mo`, `1y`. "
        "A plain number means minutes.\n"
        "• `perm` (or `forever`) = permanent — saved to your profile, so they're "
        "banned from **every future room of yours** until you unban.\n"
        "`..unban <name>` — lift a ban (works on saved bans too)\n"
        "`..bans` — list current bans with time remaining\n"
        "`..kick <name>` — remove them once, no ban\n\n"
        "Timed bans expire on their own. The room's Ban/Unban buttons do the same "
        "with a picker (button bans use the 1-day default)."
    )

    people = (
        "`..mod <name>` / `..unmod <name>` — room mods can ban, kick and hush in "
        "your room. Saved to your profile: your mods are mods in every room you make.\n"
        "`..hush <name>` / `..unhush <name>` — mute someone in the room **text chat** "
        "(they can still talk in voice). Hushes are saved across your rooms too.\n\n"
        "💡 As owner you also get native powers: right-click anyone in your room "
        "for server mute / deafen. The bot auto-clears those when the person moves "
        "to another channel, so nothing sticks past your room."
    )

    ownership = (
        "`..transfer <name>` — hand the room to someone in it\n"
        "`..abandon` — give up ownership; the room stays until empty\n"
        "`..claim` — take over a room whose owner left (join it first)\n\n"
        "Owner powers can't target server staff, and nobody can ban or kick the owner."
    )

    transparency = (
        "Every ban, unban, hush, kick and mod change is logged publicly in the "
        "voice log, and the affected person gets a DM saying **what happened, "
        "where, and who did it**. Mutes and deafens are logged with the actor too.\n\n"
        "**House rules for room owners**\n"
        "• Discord's **Terms of Service** apply everywhere, including private rooms.\n"
        "• You never owe anyone a reason for a room ban, mute or kick — your room, "
        "your call.\n"
        "• But punishing anyone because of race, religion, nationality, gender, "
        "sexuality, disability or any other protected trait is a **server-rule "
        "violation** and will be handled by staff. Room powers are for keeping "
        "your room comfortable, not for hate."
    )

    music = (
        "The main bot is also the DJ — commands work in **any** chat:\n\n"
        "**Start** — `..play <name or link>`: searches YouTube, takes direct links "
        "and playlists (up to 25 tracks). `..summon` (or `..music`, `..join`) pulls "
        "the bot into your channel without playing yet.\n"
        "**Control** — `..skip` (also `..next`) jumps to the next track · "
        "`..pause` / `..resume` · `..stop` (also `..leave`) ends the session\n"
        "**Queue** — `..queue` (also `..q`) shows the list · `..np` shows the "
        "current track · `..shuffle` mixes the queue · `..clear` empties it "
        "(current track keeps playing)\n"
        "**Sound** — `..vol <0-150>` (100 = normal)\n\n"
        "-# `..skip`/`..next` move forward; `..queue`/`..q` only *show* the list — "
        "they never change it.\n"
        "-# High-quality audio (best available stream). The bot leaves by itself "
        "after 5 quiet minutes or when the channel empties.\n"
        "-# 🎁 Channels with the music bot in them earn no giveaway points."
    )

    afk = (
        "Muted nonstop for **2 hours** (and not streaming or on camera)? You get "
        "moved to **💤〡AFK**, where nobody can speak, and DM'd about it. Join any "
        "channel — or just leave — and you're back to normal. Unmuting at any point "
        "resets the timer."
    )

    embeds = [
        {"title": f"{e} Voice rooms — how it works", "description": intro, "color": s["color"]},
        {"title": "🛠️ Setup commands", "description": setup, "color": s["color"]},
        {"title": "🔨 Bans, kicks & how long they last", "description": bans, "color": s["color"]},
        {"title": "🛡️ Mods & hushes", "description": people, "color": s["color"]},
        {"title": "👑 Ownership", "description": ownership, "color": s["color"]},
    ]
    if s["music"]:
        embeds.append({"title": "🎵 Music", "description": music, "color": s["color"]})
    if s["afk"]:
        embeds.append({"title": "💤 AFK parking", "description": afk, "color": s["color"]})
    embeds.append({"title": "📜 Transparency & rules",
                   "description": transparency, "color": s["color"]})
    return embeds


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", choices=SERVERS, required=True)
    args = ap.parse_args()
    s = SERVERS[args.server]
    token = read_token(s["token_file"])
    me = api(token, "GET", "/users/@me")

    old = api(token, "GET", f"/channels/{s['channel']}/messages?limit=50")
    for m in old:
        if m["author"]["id"] == me["id"]:
            api(token, "DELETE", f"/channels/{s['channel']}/messages/{m['id']}")
            time.sleep(0.5)

    for embed in build_embeds(s):
        api(token, "POST", f"/channels/{s['channel']}/messages", {"embeds": [embed]})
        time.sleep(0.5)
    print(f"Room guide posted to {args.server} #info")


if __name__ == "__main__":
    main()
