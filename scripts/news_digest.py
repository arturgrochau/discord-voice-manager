"""Daily news digest for the Politics & Philosophy happenings channels.

Uses the local Grok CLI (headless, live web/X search) to gather the day's most
relevant stories through a Europe-first editorial lens, then posts them **as
Politics Bot** in the channel's historical format: one X/Twitter link per
message so Discord renders rich tweet cards (falling back to a bold headline +
article link when no good X post exists).

    🌎〡world-events   — global geopolitics that matters to Europe
    🛡〡europe-news    — EU/national politics, sovereignty, migration, security
    🚀〡econ-tech      — European economy, energy, industry, tech & AI policy

Run manually:  .venv/bin/python scripts/news_digest.py
Scheduled by:  ~/Library/LaunchAgents/com.arturgrochau.pnp-news.plist (daily)
"""

import json
import logging
import logging.handlers
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
GROK = Path.home() / ".grok/bin/grok"
NADEKO_CREDS = Path.home() / "Projects/nadekobot/nadeko-osx-arm64/data/creds.yml"

CHANNELS = {
    "world_events": "1285580352236163092",
    "europe_news": "1285580524945145857",
    "econ_tech": "1285579927852417034",
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
                "x_url": {"type": "string"},
                "article_url": {"type": "string"},
            },
            "required": ["title", "x_url", "article_url"],
        }
    },
})

PROMPT = """Use live web and X (twitter) search to compile today's news digest
({date}) for a European politics discussion server. The audience cares most
about European sovereignty, EU and national politics, migration and border
policy, security and defence, energy independence, and Europe's economic and
technological standing.

Find the most significant stories of the last 24 hours and sort them into
exactly three sections:

- world_events: 3 stories. Global geopolitics, conflicts, diplomacy —
  prioritising what affects Europe's position in the world.
- europe_news: 4 stories. EU institutions and national politics, elections,
  migration and border policy, security, major social debates within Europe.
- econ_tech: 3 stories. European economy, energy, industry, markets, tech
  regulation, AI — plus global econ/tech with European impact.

For each story provide:
- title: a short factual headline.
- x_url: the URL of a real, existing X post (x.com/<account>/status/<id>)
  covering this story from a large news or commentary account (e.g.
  visegrad24, Reuters, SkyNews, MarioNawfal, RadioGenoa, DW, Politico).
  To find it, run additional web searches like: site:x.com <story keywords>.
  Copy the status URL character-for-character from an actual search result.
  Every x_url will be machine-validated against X's oEmbed API — a fabricated
  or misremembered status ID is worse than none, so when your search results
  do not contain a directly usable status URL, set x_url to "".
- article_url: direct URL of a news article covering the story, copied
  verbatim from your search results.

Only include stories you actually found via search. Prefer today's reporting."""

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


def politics_bot_token() -> str:
    creds = NADEKO_CREDS.read_text()
    return re.search(r"^token:\s*'?([A-Za-z0-9_.\-]+)'?", creds, re.M).group(1)


def parse_grok_output(raw: str) -> dict:
    """Grok CLI may print several JSON objects; find the one holding the answer.

    The schema-constrained answer is wrapped as {"text": "<json string>", ...}.
    Scan every top-level JSON object in the output and unwrap the first usable one.
    """
    decoder = json.JSONDecoder()
    idx, candidates = 0, []
    while True:
        start = raw.find("{", idx)
        if start == -1:
            break
        try:
            obj, end = decoder.raw_decode(raw, start)
            candidates.append(obj)
            idx = end
        except json.JSONDecodeError:
            idx = start + 1
    for obj in candidates:
        if isinstance(obj, dict) and isinstance(obj.get("text"), str):
            try:
                inner, _ = decoder.raw_decode(obj["text"].strip())
                return inner
            except json.JSONDecodeError:
                continue
        if isinstance(obj, dict) and any(k in obj for k in CHANNELS):
            return obj
    raise ValueError("No parseable digest JSON in grok output")


def gather(max_attempts: int = 3) -> dict:
    prompt = PROMPT.format(date=datetime.now().strftime("%A, %d %B %Y"))
    for attempt in range(1, max_attempts + 1):
        log.info("Grok attempt %d/%d", attempt, max_attempts)
        try:
            proc = subprocess.run(
                [str(GROK), "-p", prompt, "--json-schema", SCHEMA, "--always-approve"],
                capture_output=True, text=True, timeout=600, cwd=str(Path.home()),
            )
            data = parse_grok_output(proc.stdout)
            if all(data.get(k) for k in CHANNELS):
                return data
            log.warning("Incomplete sections: %s", {k: len(data.get(k, [])) for k in CHANNELS})
        except Exception as e:
            log.warning("Attempt %d failed: %s", attempt, e)
        time.sleep(30)
    raise RuntimeError("Grok digest failed after retries")


X_URL_RE = re.compile(r"^https://(x|twitter)\.com/[A-Za-z0-9_]+/status/\d+", re.I)


def tweet_exists(url: str) -> bool:
    """Validate a tweet via X's public oEmbed endpoint (404 for fabricated IDs)."""
    try:
        q = urllib.parse.urlencode({"url": url})
        req = urllib.request.Request(
            f"https://publish.twitter.com/oembed?{q}",
            headers={"User-Agent": "Mozilla/5.0 (news-digest validator)"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except Exception:
        return False


def url_alive(url: str) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status < 400
    except Exception:
        return False


def send(channel_id: str, content: str, token: str) -> None:
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        data=json.dumps({"content": content}).encode(),
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (https://github.com/arturgrochau/discord-voice-manager, 2.0)",
        },
    )
    with urllib.request.urlopen(req) as r:
        r.read()


def post_section(section: str, stories: list[dict], token: str) -> int:
    posted = 0
    for s in stories[:5]:
        x_url = (s.get("x_url") or "").strip()
        article = (s.get("article_url") or "").strip()
        if X_URL_RE.match(x_url) and tweet_exists(x_url):
            # historical Politics Bot format: bare X link -> rich tweet card
            content = x_url
        elif article.startswith("http") and url_alive(article):
            content = f"**{s['title'].strip()}**\n{article}"
        else:
            log.warning("Skipping story, nothing validated: %s (x=%s, article=%s)",
                        s.get("title"), x_url or "-", article or "-")
            continue
        try:
            send(CHANNELS[section], content, token)
            posted += 1
            time.sleep(1.5)
        except Exception:
            log.exception("Failed to post story in %s", section)
    log.info("Posted %d stories to %s", posted, section)
    return posted


def main() -> None:
    setup_logging()
    token = politics_bot_token()
    data = gather()
    total = sum(post_section(section, data[section], token) for section in CHANNELS)
    if total == 0:
        sys.exit(1)
    log.info("Digest complete: %d stories.", total)


if __name__ == "__main__":
    main()
