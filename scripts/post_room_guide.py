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
        f"Join **{s['trigger']}** and you get your own room, with a button panel "
        "in its chat. Your setup — name, size, lock, speaking default, room mods, "
        "permanent bans, hushes — is saved and re-applied on your next room. "
        "Empty rooms delete themselves.\n\n"
        "Commands go in the room chat. Names take partial matches, @mentions or "
        "ids, and the person **doesn't need to be in the room**. "
        "`..commands` shows a quick list any time."
    )

    reference = (
        "**Room**\n"
        "`..rename <name>` · `..status <text>` · `..setsize <0-99>` (0 = no cap)\n"
        "`..lock` toggle who can join · `..voice` toggle whether newcomers can speak"
        + (" · `..nominors` block minors" if s["minors"] else "") + "\n"
        "`..panel` re-post the buttons\n\n"
        "**People**\n"
        "`..ban <name> [length]` — default **1 day**; lengths `30m` `6h` `3d` `2w` "
        "`1mo` `1y`, or `perm` to make it stick across all your rooms\n"
        "`..unban <name>` · `..bans` list bans · `..kick <name>` remove once\n"
        "`..mod <name>` / `..unmod <name>` — room mods (can ban, kick, hush)\n"
        "`..hush <name>` / `..unhush <name>` — text-chat mute\n\n"
        "**Ownership**\n"
        "`..transfer <name>` hand over · `..abandon` step down · "
        "`..claim` take an ownerless room\n\n"
        "-# Owners can also right-click members for native mute / deafen. "
        "Owner powers can't touch server staff."
    )

    music = (
        "`..play <name or link>` — YouTube search, direct links, playlists, "
        "and **Spotify** links (track / album / playlist) · "
        "`..summon` bring the bot in first\n"
        "`..skip` next track · `..pause` / `..resume` · `..stop` end the session\n"
        "`..queue` see the list · `..np` current track · `..shuffle` · `..clear` · "
        "`..vol <0-150>`\n\n"
        "-# The bot leaves after 5 quiet minutes or when the channel empties, and "
        "picks the queue back up after restarts. Music channels earn no giveaway points."
    )

    rules = (
        "• Discord's **Terms of Service** apply everywhere, including private rooms.\n"
        "• You never owe anyone a reason for a room ban, mute or kick — but "
        "punishing anyone over race, religion, gender, sexuality or any other "
        "protected trait is a server-rule violation.\n"
        "• Anyone a room action hits gets a DM saying what happened and who did it."
        + ("\n• Muted for 3 hours straight, or alone in a call for 3 hours, and "
           "you'll be moved to 💤〡AFK." if s["afk"] else "")
    )

    embeds = [
        {"title": f"{e} Voice rooms", "description": intro, "color": s["color"]},
        {"title": "📋 Command reference", "description": reference, "color": s["color"]},
    ]
    if s["music"]:
        embeds.append({"title": "🎵 Music", "description": music, "color": s["color"]})
    embeds.append({"title": "📜 Rules & AFK" if s["afk"] else "📜 Rules",
                   "description": rules, "color": s["color"]})
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
