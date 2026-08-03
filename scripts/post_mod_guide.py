"""Post (or refresh) the staff command guide into each server's #mod-guide.

Usage: python3 scripts/post_mod_guide.py --server pp|hk

Idempotent: deletes the bot's previous messages in the guide channel and
reposts, so rerun it whenever commands change. Content is defined below —
keep it in sync with the cogs and politics_helper.py.
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
        "channel": "1533867024747466914",   # 📖〡mod-guide (Staff Space)
        "token_file": Path.home() / "Projects/nadekobot/nadeko-osx-arm64/data/creds.yml",
        "bot": "Politics Bot",
        "ping": "@Admin / @Mod",
        "modmail_log": "#mod-log",
        "bans_log": "#bans",
        "detains_log": "#detains",
        "ladder": ("Politics Junior 1 · Chancellor 5 · Archon 10 · Logothete 18 · "
                   "Erudite 30 · Sovereign 45 · Imperator 65 · Supreme 90 · "
                   "Leviathan 120 · Mythical 150"),
        "extras": True,
        "admin_role": "@Admin",
        "mod_role": "@Mod",
    },
    "hk": {
        "channel": "1533867021761122584",   # 📖〡mod-guide (admin)
        "token_file": Path.home() / "Projects/hk-sentinel/.env",
        "bot": "HUMANKIND BOT",
        "ping": "@★ / @☆",
        "modmail_log": "#modmail-log",
        "bans_log": "#🔨〡bans",
        "detains_log": "#👮〡detains",
        "ladder": ("Neolithic 1 · Ancient Era 5 · Classical Era 10 · Medieval Era 18 · "
                   "Early Modern Era 30 · Industrial Era 45 · Contemporary Era 65 · "
                   "Era Star 90 · Fame Legend 120 · Endless 150"),
        "extras": False,
        "admin_role": "★ (filled star)",
        "mod_role": "☆ (empty star)",
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
    tiers = (
        f"Staff power is **role-based and enforced by the bot** — moderators need "
        f"no dangerous Discord permissions; the bot acts on their behalf, applies "
        f"the limits below, and logs every action.\n\n"
        f"**{s['admin_role']} — Admin tier**\n"
        f"Everything: ban/unban/kick, clearwarnings, lock/unlock/slowmode, "
        f"prune up to 100 — plus all moderator tools.\n\n"
        f"**{s['mod_role']} — Moderator tier**\n"
        f"Day-to-day tools: `.warn` · `.detain`/`.undetain` · `.timeout`/"
        f"`.untimeout` · `.warnings` · prune (**max 15 per command**, anti "
        f"mass-delete) · modmail (view, reply, `=close`).\n"
        f"❌ No bans, kicks, channel locks or warning wipes.\n\n"
        f"**Protections**\n"
        f"• Staff can't be moderated by moderators; admins only by the owner.\n"
        f"• Warn/detain/ban all run the same hierarchy check.\n"
        f"• Prefix shortcuts work for whichever tier owns the command; slash "
        f"commands may stay hidden for mods (Discord UI) — the `.` commands are "
        f"the moderator interface."
    )
    mod = (
        "All moderation lives in the sentinel — prefix `.` (shortcuts in parentheses); "
        "everything except prune is also a `/` slash command. "
        "**Mentions and raw user IDs both work.**\n\n"
        "**⛓️ Detain**\n"
        "`.detain (.d) @user [reason]` — restrict to the detainment channels, "
        f"DMs the user, logged publicly in {s['detains_log']}\n"
        "`.undetain (.ud) @user [reason]` — release\n"
        "`/detainhistory @user` — full detention record *(slash only)*\n\n"
        "**🔨 Removal** *(admin tier)* — all logged publicly in " + s["bans_log"] + "\n"
        "`.ban (.b) @user [reason]` · `.kick (.k) @user [reason]`\n"
        "`.unban (.ub) <id or name>`\n\n"
        "**⏳ Timeouts**\n"
        "`.timeout (.t) @user <minutes> [reason]` — max 40320 (28 days)\n"
        "`.untimeout (.ut) @user`\n\n"
        "**⚠️ Warnings**\n"
        "`.warn (.w) @user <reason>` — the bot's warning message **stays in the "
        "channel** for transparency (your command still auto-deletes); user is DMed\n"
        "`.warnings (.ws) @user` — list a member's warnings (private reply)\n"
        "`.clearwarnings (.cw) @user` — wipe them *(admin tier)*\n\n"
        "**🧹 Cleanup**\n"
        "`.prune (.purge, .p) N` — delete the last N messages "
        "(admins max 100, **mods max 15**)\n"
        "`.prune @user N` *or* `.prune N @user` — only that user's messages\n"
        "`/slowmode <seconds>` · `/lock` · `/unlock` — channel controls "
        "*(slash only, admin tier)*\n\n"
        "**🔊 Voice**\n"
        "`/stickymute @user [reason]` — keep server-muted across rejoins · `/unstickymute`\n"
        "Stale server-mutes are auto-lifted (detained & sticky-muted users are skipped).\n\n"
        "**ℹ️ Info** — `/userinfo` · `/serverinfo` · `.credits`\n\n"
        "*Hygiene: command invocations auto-delete; bot replies self-destruct after "
        "15 s — only warnings and public logs persist.*"
    )
    modmail = (
        f"Members DM **{s['bot']}** → a ticket channel `📬〡username` opens in the "
        f"ModMail category and pings {s['ping']}, with an info header (account age, "
        "join date, roles).\n\n"
        "• Their DMs arrive as **blue** embeds; attachments are linked.\n"
        "• **Type a plain message in the ticket to reply** — it's delivered to their "
        "DMs as a green Mod Team embed; the ✅ reaction confirms delivery.\n"
        "• Messages starting with `.` `;` `/` `!` `=` are **not** relayed, so you can "
        "run bot commands inside a ticket safely.\n"
        f"• `=close [reason]` — user gets a closing DM (with your reason), the full "
        f"transcript is filed in {s['modmail_log']}, and the channel is deleted.\n"
        "• Tickets survive bot restarts."
    )
    xp = (
        "Nadeko (prefix `;`) awards **4 XP per message** (60 s cooldown) and "
        "**12 XP/min in voice** — voice levels you ~3× faster by design. Rank "
        "roles are granted automatically and each promotion **replaces** the "
        "previous rank role.\n\n"
        "**Promotions also take time**: each rank has a minimum tenure at the "
        "previous rank (1 d for the second rung, scaling to 30 d at the top). "
        "Reach the XP early and the promotion is deferred — the member is DMed "
        "and the role lands automatically the moment tenure is served.\n\n"
        "`.level` (`.lvl`, `.next`) — anyone can check their level, total XP, "
        "next rank, XP to go (with voice/text time estimates), and any tenure "
        "countdown.\n\n"
        f"**Ladder (role · level):** {s['ladder']}\n\n"
        "`;xp` — your card · `;xplb` — leaderboard · `;xprr` — role rewards\n"
        "`;xpadd @user N` — grant XP *(admin)*"
        + ("\n\n**Disboard bumps:** the first `/bump` when the window opens earns "
           "**+200 XP** — reminders post automatically and clean themselves up."
           if s["extras"] else "")
    )
    autom = (
        "**🔺 Join to Create** — joining the trigger voice channel spawns a personal "
        "room with a **control panel** posted in its chat: buttons + `..commands` for "
        "rename/status/size, lock, speaking default, room bans (timed or permanent), "
        "room mods, hush, claim/transfer/abandon — plus native right-click "
        "mute/deafen/move for the owner. Every owner's settings (name, size, lock, "
        "bans, mods) are **saved and re-applied** on their next room. Empty rooms "
        "self-delete.\n"
        + ("**📚 Reaction roles** — #rules-and-info: 📚 book club · 📰 news ping · "
           "🏛️ debate ping\n"
           "**🏛️ Philosophy quotes** — every ~4 h in general chat (skips dead hours)\n"
           "**📰 News digest** — 3×/day into the happenings channels (validated X links)\n"
           if s["extras"] else "")
        + "**🧹 Command hygiene** — Nadeko also deletes command invocations "
        "(`;delmsgoncmd` is on)."
    )
    return [
        {"title": f"🛡️ {s['bot']} — Staff Command Guide", "description": mod, "color": 0x3498DB},
        {"title": "👥 Staff Tiers", "description": tiers, "color": 0xE67E22},
        {"title": "📬 Modmail", "description": modmail, "color": 0x9B59B6},
        {"title": "🎖️ XP & Ranks", "description": xp, "color": 0xF1C40F},
        {"title": "🤖 Automations", "description": autom, "color": 0x2ECC71},
    ]


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
    print(f"Guide posted to {args.server} #mod-guide")


if __name__ == "__main__":
    main()
