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

RESEARCH_PROMPT = """You are compiling today's ({date}) news digest for a
European politics discussion server. The audience cares most about European
sovereignty, EU and national politics, migration and border policy, security
and defence, energy independence, and Europe's economic and technological
standing.

Run MANY live web searches now — do not answer from memory. Search current
news sites AND X posts (queries like `site:x.com <story keywords>`) for the
most significant stories of the last 24 hours. Then write a markdown list of:

- 3 WORLD EVENTS stories (global geopolitics/conflicts/diplomacy, prioritising
  what affects Europe's position in the world)
- 4 EUROPE NEWS stories (EU institutions and national politics, elections,
  migration and border policy, security, major social debates in Europe)
- 3 ECON & TECH stories (European economy, energy, industry, markets, tech
  regulation, AI; global econ/tech with European impact)

For each story: a short factual headline, then every URL you actually saw in
your search results for it — X status URLs (x.com/<account>/status/<id>) and
article URLs. Copy URLs character-for-character from search results; NEVER
write a URL you did not see in a result. If no X post turned up, say so."""

STRUCTURE_PROMPT = """Convert the following researched news list into JSON.
Sections: world_events (3 stories), europe_news (4), econ_tech (3).
For each story: title; x_url = an X status URL present in the text verbatim
(or "" if that story has none); article_url = an article URL present in the
text verbatim (or "" if none). Copy URLs exactly — do not invent or repair
them.

{research}"""

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


def extract_text_field(raw: str) -> str:
    """Pull the assistant's text out of the grok CLI wrapper JSON; fall back to raw."""
    decoder = json.JSONDecoder()
    idx = 0
    while True:
        start = raw.find("{", idx)
        if start == -1:
            break
        try:
            obj, end = decoder.raw_decode(raw, start)
            if isinstance(obj, dict) and isinstance(obj.get("text"), str) and obj["text"].strip():
                return obj["text"]
            idx = end
        except json.JSONDecodeError:
            idx = start + 1
    return raw


def run_grok(prompt: str, schema: str | None = None) -> str:
    cmd = [str(GROK), "-p", prompt, "--always-approve"]
    if schema:
        cmd += ["--json-schema", schema]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=str(Path.home()))
    return proc.stdout


def gather(max_attempts: int = 3) -> dict:
    """Research (agentic search) then structure, retrying until validation passes."""
    for attempt in range(1, max_attempts + 1):
        log.info("Digest attempt %d/%d: research phase", attempt, max_attempts)
        try:
            research_raw = run_grok(
                RESEARCH_PROMPT.format(date=datetime.now().strftime("%A, %d %B %Y"))
            )
            research = extract_text_field(research_raw)
            log.info("Research phase returned %d chars; structuring", len(research))
            data = parse_grok_output(run_grok(STRUCTURE_PROMPT.format(research=research), SCHEMA))
            validated = validate_stories(data)
            if all(validated.get(k) for k in CHANNELS):
                return validated
            log.warning("Validation left empty sections: %s",
                        {k: len(validated.get(k, [])) for k in CHANNELS})
        except Exception as e:
            log.warning("Attempt %d failed: %s", attempt, e)
        time.sleep(30)
    raise RuntimeError("Grok digest failed after retries")


def validate_stories(data: dict) -> dict:
    """Keep only stories whose tweet or article URL verifiably exists."""
    out: dict = {}
    for section in CHANNELS:
        kept = []
        for s in (data.get(section) or [])[:5]:
            x_url = (s.get("x_url") or "").strip()
            article = (s.get("article_url") or "").strip()
            if X_URL_RE.match(x_url) and tweet_exists(x_url):
                kept.append({"content": x_url})
            elif article.startswith("http") and url_alive(article):
                kept.append({"content": f"**{s.get('title', '').strip()}**\n{article}"})
            else:
                log.warning("Dropped unvalidated story: %s (x=%s, article=%s)",
                            s.get("title"), x_url or "-", article or "-")
        out[section] = kept
    return out


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
    for s in stories:
        try:
            send(CHANNELS[section], s["content"], token)
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
