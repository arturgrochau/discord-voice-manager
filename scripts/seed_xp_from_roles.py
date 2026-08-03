"""Give members an XP floor matching the ladder role they already hold, so
displayed totals make sense to people who earned ranks under the old system.

For every guild member, finds their highest ladder role and raises their
Nadeko server XP to that role's level threshold (never lowers anyone).
Also strips lower ladder roles so the ladder reads as a clean promotion
(highest rung only), matching the helper's promotion semantics.

Usage:
  SENTINEL_HOME=<instance dir> .venv/bin/python scripts/seed_xp_from_roles.py \
      --nadeko-db /path/to/NadekoBot.db \
      --ladder-config /path/to/politics_helper.json \
      --levels 1,5,10,18,30,45,65,90,120,150 [--dry-run]
"""

import argparse
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

UA = {"User-Agent": "DiscordBot (https://github.com/arturgrochau/discord-voice-manager, 2.0)"}
BASE_DIR = Path(os.environ.get("SENTINEL_HOME", Path(__file__).resolve().parent.parent))


def total_xp_for_level(level: int, a: int = 9, c: int = 27) -> int:
    # Nadeko default formula: cost(L -> L+1) = a*(L+1) + c
    return a * level * (level + 1) // 2 + c * level


def call(token, url, method="GET", data=None):
    h = dict(UA)
    h["Authorization"] = f"Bot {token}"
    if data is not None:
        h["Content-Type"] = "application/json"
        data = json.dumps(data).encode()
    r = urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=h))
    b = r.read()
    return json.loads(b) if b.strip() else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nadeko-db", required=True)
    ap.add_argument("--ladder-config", required=True)
    ap.add_argument("--levels", required=True, help="comma-separated levels matching LADDER order")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    token = (BASE_DIR / ".env").read_text().split("=", 1)[1].strip()
    guild_id = int(json.load(open(BASE_DIR / "config.json"))["GUILD_ID"])
    ladder = [int(r) for r in json.load(open(args.ladder_config))["LADDER"]]
    levels = [int(x) for x in args.levels.split(",")]
    assert len(ladder) == len(levels)

    members, after = [], "0"
    while True:
        batch = call(token, f"https://discord.com/api/v10/guilds/{guild_id}/members?limit=1000&after={after}")
        if not batch:
            break
        members += batch
        after = batch[-1]["user"]["id"]
        time.sleep(0.5)
    print(f"{len(members)} members fetched")

    seeded = stripped = 0
    db = sqlite3.connect(args.nadeko_db, timeout=15)
    for m in members:
        if m["user"].get("bot"):
            continue
        held = [i for i, rid in enumerate(ladder) if str(rid) in m["roles"]]
        if not held:
            continue
        top = max(held)
        floor = total_xp_for_level(levels[top])
        uid = int(m["user"]["id"])
        if not args.dry_run:
            db.execute(
                "INSERT INTO UserXpStats (UserId, GuildId, Xp, DateAdded) "
                "VALUES (?, ?, ?, datetime('now')) "
                "ON CONFLICT(UserId, GuildId) DO UPDATE SET Xp = MAX(Xp, excluded.Xp)",
                (uid, guild_id, floor),
            )
        seeded += 1
        for i in held:
            if i == top:
                continue
            if not args.dry_run:
                try:
                    call(token, f"https://discord.com/api/v10/guilds/{guild_id}/members/{uid}/roles/{ladder[i]}", "DELETE")
                    time.sleep(0.45)
                except urllib.error.HTTPError:
                    pass
            stripped += 1
        print(f"  {m['user']['username']:<24} -> level {levels[top]} floor ({floor} xp)")
    if not args.dry_run:
        db.commit()
    db.close()
    print(f"done: {seeded} members seeded, {stripped} superseded roles removed"
          + (" (dry run)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
