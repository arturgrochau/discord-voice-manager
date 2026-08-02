"""Daily news digest for the Politics & Philosophy happenings channels.

Uses the local Grok CLI (headless, live web search) to gather the day's most
relevant stories through a Europe-first editorial lens, then posts one embed
per section via the P&P Sentinel bot token:

    🌎〡world-events   — global geopolitics that matters to Europe
    🛡〡europe-news    — EU/national politics, sovereignty, migration, security
    🚀〡econ-tech      — European economy, energy, industry, tech & AI policy

Run manually:  .venv/bin/python scripts/news_digest.py
Scheduled by:  ~/Library/LaunchAgents/com.arturgrochau.pnp-news.plist (daily)
"""

import json
import logging
import logging.handlers
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
GROK = Path.home() / ".grok/bin/grok"

CHANNELS = {
    "world_events": "1285580352236163092",
    "europe_news": "1285580524945145857",
    "econ_tech": "1285579927852417034",
}
SECTION_META = {
    "world_events": ("🌎 World Events — Daily Digest", 0x3498DB),
    "europe_news": ("🛡️ Europe News — Daily Digest", 0x2C3E50),
    "econ_tech": ("🚀 Econ & Tech — Daily Digest", 0xE67E22),
}

SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "world_events": {"type": "array", "items": {"$ref": "#/$defs/story"}},
        "europe_news": {"type": "array", "items": {"$ref": "#/$defs/story"}},
        "econ_tech": {"type": "array", "items": {"$ref": "#/$defs/story"}},
    },
    "required": ["world_events", "europe_news", "econ_tech"],
    "$defs": {
        "story": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "source": {"type": "string"},
                "url": {"type": "string"},
            },
            "required": ["title", "summary", "source", "url"],
        }
    },
})

PROMPT = """Use live web search to compile today's news digest ({date}) for a
European politics discussion server. The audience cares most about European
sovereignty, EU and national politics, migration and border policy, security
and defence, energy independence, and Europe's economic and technological
standing in the world.

Find the most significant stories from the last 24 hours (search several
reputable outlets — e.g. Politico Europe, Reuters, FT, Euractiv, national
European press) and sort them into exactly three sections:

- world_events: 3 stories. Global geopolitics, conflicts, and diplomacy,
  prioritising what affects Europe's position in the world.
- europe_news: 4 stories. EU institutions and national politics, elections,
  migration and border policy, security, social debates within Europe.
- econ_tech: 3 stories. European economy, energy, industry, markets, tech
  regulation, AI — plus major global econ/tech news with European impact.

For each story give a punchy factual title, a 2-3 sentence neutral summary of
what happened and why it matters to Europe, the source outlet name, and the
direct article URL. Do not editorialise, do not invent stories, use only
articles you actually found in search. Prefer today's reporting."""

log = logging.getLogger("news-digest")


def setup_logging() -> None:
    logs_dir = BASE_DIR / "logs"
    logs_dir.mkdir(exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        logs_dir / "news-digest.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")
    handler.setFormatter(fmt)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger().addHandler(handler)
    logging.getLogger().addHandler(stream)


def gather(max_attempts: int = 3) -> dict:
    prompt = PROMPT.format(date=datetime.now().strftime("%A, %d %B %Y"))
    for attempt in range(1, max_attempts + 1):
        log.info("Grok attempt %d/%d", attempt, max_attempts)
        try:
            proc = subprocess.run(
                [str(GROK), "-p", prompt, "--json-schema", SCHEMA, "--always-approve"],
                capture_output=True, text=True, timeout=600, cwd=str(Path.home()),
            )
            raw = proc.stdout.strip()
            start, end = raw.find("{"), raw.rfind("}")
            data = json.loads(raw[start:end + 1])
            if "text" in data and isinstance(data["text"], str):
                # grok CLI wraps the schema-constrained answer in {"text": "..."}
                data = json.loads(data["text"])
            if all(data.get(k) for k in CHANNELS):
                return data
            log.warning("Incomplete sections: %s", {k: len(data.get(k, [])) for k in CHANNELS})
        except Exception as e:
            log.warning("Attempt %d failed: %s", attempt, e)
        time.sleep(30)
    raise RuntimeError("Grok digest failed after retries")


def post(section: str, stories: list[dict], token: str) -> None:
    title, color = SECTION_META[section]
    fields = []
    for s in stories[:5]:
        summary = s["summary"].strip()
        if len(summary) > 900:
            summary = summary[:897] + "..."
        fields.append({
            "name": s["title"][:256],
            "value": f"{summary}\n[{s['source']}]({s['url']})"[:1024],
            "inline": False,
        })
    embed = {
        "title": title,
        "color": color,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Daily digest • Grok live search"},
    }
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{CHANNELS[section]}/messages",
        data=json.dumps({"embeds": [embed]}).encode(),
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (https://github.com/arturgrochau/discord-voice-manager, 2.0)",
        },
    )
    with urllib.request.urlopen(req) as r:
        log.info("Posted %s (%d stories) -> HTTP %s", section, len(fields), r.status)


def main() -> None:
    setup_logging()
    token = (BASE_DIR / ".env").read_text().split("=", 1)[1].strip()
    data = gather()
    failures = 0
    for section in CHANNELS:
        try:
            post(section, data[section], token)
        except Exception:
            log.exception("Failed to post %s", section)
            failures += 1
    if failures:
        sys.exit(1)
    log.info("Digest complete.")


if __name__ == "__main__":
    main()
