"""Daily news digest for the Politics & Philosophy happenings channels.

Uses the local Grok CLI (headless, live web/X search) to gather the day's most
relevant stories through a Europe-first editorial lens, then posts them **as
Politics Bot** in the channel's historical format: one X/Twitter link per
message so Discord renders rich tweet cards (falling back to a bold headline +
article link when no good X post exists).

    🌎〡world-events   — global geopolitics that matters to Europe
    🛡〡europe-news    — EU/national politics, sovereignty, migration, security
    🚀〡econ-tech      — European economy, energy, industry, tech & AI policy

Two modes:
  full digest (default) — the original 3+4+3 daily roundup
  --pulse             — rolling 15-minute breaking-news check: up to 2 fresh
                        stories per section, expected to post NOTHING most
                        runs; rate-limited so the channels never flood.

Run manually:  .venv/bin/python scripts/news_digest.py [--pulse]
Scheduled by:  com.arturgrochau.pnp-news system daemon on the M1
               (StartInterval 900 → --pulse every 15 minutes)
"""

import argparse
import json
import logging
import logging.handlers
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
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

SECTION_RULES = """Section definitions — file every story in exactly ONE
best-fitting section:
- WORLD EVENTS: geopolitics OUTSIDE Europe — wars, diplomacy, elections and
  crises in the US, Asia, Middle East, Africa, the Americas — prioritising
  what affects Europe's position in the world.
- EUROPE NEWS: politics and society INSIDE Europe — EU institutions,
  national governments and elections, migration and borders, security and
  policing, courts, protests, major social debates. European weather,
  climate events and disasters belong HERE, never in econ-tech.
- ECON & TECH: money and technology — markets, inflation, trade, energy
  prices and supply, industry, corporate news, tech/AI regulation and
  breakthroughs. A story qualifies only if its CORE subject is economic or
  technological: a heatwave is not econ-tech; an energy-price spike caused
  by one is.
If a story does not clearly fit any section, DROP it entirely."""

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
write a URL you did not see in a result. If no X post turned up, say so.

X post quality bar (strict): ENGLISH-language posts only, from major
high-follower news/commentary accounts such as: elonmusk, Reuters, AFP,
SkyNews, BBCWorld, BBCBreaking, FT, Bloomberg, WSJ, POLITICOEurope,
euronews, DWNews, visegrad24, MarioNawfal, RadioGenoa, disclosetv,
spectatorindex, BRICSinfo, GlobeEyeNews, AFpost, EndWokeness. Never use
small unknown accounts or non-English posts. @elonmusk is a priority
source — always check his recent posts (site:x.com/elonmusk) and include
any that are relevant to today's stories. Also check the accounts of major
European political figures he engages with — Farage, Weidel, Salvini,
Abascal, Orbán, Wilders, Meloni — and prefer their takes on European
sovereignty and migration stories when they posted about today's news.

{skip_note}"""

PULSE_RESEARCH_PROMPT = """It is {date}, {time} UTC. You are the standing news
desk for a European politics discussion server. The audience cares about
European sovereignty, EU and national politics, elections, migration and
borders, security and defence, energy independence, and Europe's economic
and technological standing (global stories count when they affect Europe).

Your job every run: surface the **freshest, most significant** stories this
audience has NOT seen yet. Run MANY live web and X searches right now — the
front pages of Reuters/AFP/BBC/FT/Bloomberg/Politico Europe/Euronews, plus
`site:x.com <keywords>` queries — do not answer from memory. Prioritise
stories with a real development in roughly the last 6-12 hours, but a major
ongoing story with a meaningful new angle is fine too.

Return the TOP 2 to 3 stories overall, ranked by significance and freshness.
There is ALWAYS relevant news in the world — on a normal run you WILL find 2-3
worth posting. Only return "no new stories" in the rare case where every
genuinely significant current story is already in the covered list below.

HARD non-redundancy rule: do NOT return anything substantially similar to a
story in the already-covered list — not the same event from another outlet,
not a follow-up with no new information, not a reworded headline. If your best
pick is already covered, skip it and go to the next most significant fresh
story. Also skip pure opinion/explainers/anniversaries; report events.

{section_rules}

Write a short markdown list of your 2-3 picks, each labelled WORLD EVENTS /
EUROPE NEWS / ECON & TECH. For each: a short factual headline, then every URL
you actually saw in your search results — X status URLs
(x.com/<account>/status/<id>) and the article URL. Copy URLs
character-for-character from results; NEVER write a URL you did not see. If no
strong X post exists for a story, give the article URL and say so.

X post quality bar (strict): ENGLISH-language posts only, from major
high-follower news/commentary accounts such as: elonmusk, Reuters, AFP,
SkyNews, BBCWorld, BBCBreaking, FT, Bloomberg, WSJ, business, POLITICOEurope,
euronews, DWNews, spectatorindex, visegrad24, MarioNawfal, RadioGenoa,
disclosetv, GlobeEyeNews, AFpost, BNONews, AP. @elonmusk is a priority
source — check site:x.com/elonmusk for relevant recent posts, and the
news/commentary accounts he amplifies. Prefer takes from major European
sovereignty-minded figures (Farage, Weidel, Salvini, Abascal, Orbán,
Wilders, Meloni) when they posted about a current story.

{skip_note}"""

STRUCTURE_PROMPT = """Convert the following researched news list into JSON.
Sections: world_events (3 stories), europe_news (4), econ_tech (3).

{section_rules}

Re-check the researcher's filing against these definitions and move any
misfiled story to its correct section.

For each story: title; x_url = an X status URL present in the text verbatim
(or "" if that story has none); article_url = an article URL present in the
text verbatim (or "" if none). Copy URLs exactly — do not invent or repair
them.

{research}"""

PULSE_STRUCTURE_PROMPT = """Convert the following researched news list into
JSON. Sections: world_events, europe_news, econ_tech — each 0 to 2 stories,
ONLY stories actually present in the text. If the text reports no new
stories for a section, return an empty array for it — never invent stories.

{section_rules}

Re-check the researcher's filing against these definitions and move any
misfiled story to its correct section (e.g. a European weather story
labelled econ-tech belongs in europe_news). Drop stories fitting no section.

For each story: title; x_url = an X status URL present in the text verbatim
(or "" if that story has none); article_url = an article URL present in the
text verbatim (or "" if none). Copy URLs exactly — do not invent or repair
them.

{research}"""

log = logging.getLogger("news-digest")
HISTORY_PATH = BASE_DIR / "digest_history.json"
PULSE_STATE_PATH = BASE_DIR / "pulse_state.json"

# flow guards: with a 30-minute cadence and 2-3 fresh stories per run this
# gives a steady ~4-6 posts/hour without flooding; dedup keeps them distinct
PULSE_HOURLY_CAP = 6
PULSE_SECTION_CAP = 2
PULSE_RUN_CAP = 3


def pulse_recent_posts() -> list[float]:
    try:
        stamps = json.loads(PULSE_STATE_PATH.read_text())
    except Exception:
        stamps = []
    cutoff = time.time() - 6 * 3600
    return [t for t in stamps if t > cutoff]


def pulse_record_posts(n: int) -> None:
    stamps = pulse_recent_posts() + [time.time()] * n
    PULSE_STATE_PATH.write_text(json.dumps(stamps))


def load_history() -> list[dict]:
    """Entries: {"u": url, "t": title, "ts": epoch}. Migrates the old
    plain-URL format on read."""
    try:
        raw = json.loads(HISTORY_PATH.read_text())
    except Exception:
        return []
    return [{"u": e, "t": "", "ts": 0} if isinstance(e, str) else e for e in raw]


def save_history(entries: list[dict]) -> None:
    HISTORY_PATH.write_text(json.dumps(entries[-300:]))


def history_urls(history: list[dict]) -> set[str]:
    return {e["u"] for e in history if e.get("u")}


def recent_titles(history: list[dict], hours: int = 48) -> list[str]:
    cutoff = time.time() - hours * 3600
    return [e["t"] for e in history if e.get("t") and e.get("ts", 0) > cutoff]


_STOPWORDS = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "as",
              "at", "by", "with", "after", "over", "amid", "its", "his",
              "her", "new", "says", "say"}


def _title_words(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", title.lower())
            if w not in _STOPWORDS and len(w) > 2}


def similar_title(title: str, others: list[str], threshold: float = 0.5) -> str | None:
    """Backstop dedup: same story re-reported under a rephrased headline."""
    words = _title_words(title)
    if not words:
        return None
    for other in others:
        ow = _title_words(other)
        if not ow:
            continue
        overlap = len(words & ow) / min(len(words), len(ow))
        if overlap >= threshold:
            return other
    return None


def build_skip_note(history: list[dict]) -> str:
    titles = recent_titles(history)[-30:]
    urls = list(history_urls(history))[-25:]
    if not (titles or urls):
        return ""
    note = "ALREADY COVERED in the last 48 hours — do NOT re-report these stories (any outlet, any framing) unless there is a genuinely NEW hard development:\n"
    note += "".join(f"- {t}\n" for t in titles)
    if urls:
        note += "Never re-use these URLs: " + " ".join(urls)
    return note


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
            history = load_history()
            research_raw = run_grok(
                RESEARCH_PROMPT.format(date=datetime.now().strftime("%A, %d %B %Y"),
                                       skip_note=build_skip_note(history))
            )
            research = extract_text_field(research_raw)
            log.info("Research phase returned %d chars; structuring", len(research))
            data = parse_grok_output(run_grok(
                STRUCTURE_PROMPT.format(section_rules=SECTION_RULES, research=research), SCHEMA))
            validated = validate_stories(data)
            if all(validated.get(k) for k in CHANNELS):
                return validated
            log.warning("Validation left empty sections: %s",
                        {k: len(validated.get(k, [])) for k in CHANNELS})
        except Exception as e:
            log.warning("Attempt %d failed: %s", attempt, e)
        time.sleep(30)
    raise RuntimeError("Grok digest failed after retries")


def gather_pulse() -> dict:
    """One breaking-news sweep; empty results are normal, not an error."""
    history = load_history()
    now = datetime.now(timezone.utc)
    research_raw = run_grok(
        PULSE_RESEARCH_PROMPT.format(date=now.strftime("%A, %d %B %Y"),
                                     time=now.strftime("%H:%M"),
                                     section_rules=SECTION_RULES,
                                     skip_note=build_skip_note(history))
    )
    research = extract_text_field(research_raw)
    log.info("Pulse research returned %d chars", len(research))
    if len(research) < 40 or "no new stories" in research.lower()[:200]:
        return {k: [] for k in CHANNELS}
    data = parse_grok_output(run_grok(
        PULSE_STRUCTURE_PROMPT.format(section_rules=SECTION_RULES, research=research), SCHEMA))
    validated = validate_stories(data)
    validated = {k: v[:PULSE_SECTION_CAP] for k, v in validated.items()}
    # global per-run cap: quality over volume, whatever grok found
    budget = PULSE_RUN_CAP
    for k in CHANNELS:
        take = min(budget, len(validated.get(k, [])))
        validated[k] = validated.get(k, [])[:take]
        budget -= take
    return validated


def validate_stories(data: dict) -> dict:
    """Keep only stories whose tweet or article URL verifiably exists.

    Also dedupes across sections — grok sometimes files one story under two.
    """
    out: dict = {}
    history = load_history()
    seen: set[str] = history_urls(history)
    seen_titles: list[str] = recent_titles(history)
    for section in CHANNELS:
        kept = []
        for s in (data.get(section) or [])[:5]:
            title = (s.get("title") or "").strip()
            x_url = (s.get("x_url") or "").strip()
            article = (s.get("article_url") or "").strip()
            if x_url in seen or (article and article in seen):
                log.info("Deduped by URL: %s", title)
                continue
            dup = similar_title(title, seen_titles)
            if dup:
                log.info("Deduped by title similarity: %r ~ %r", title, dup)
                continue
            if X_URL_RE.match(x_url) and tweet_ok(x_url):
                kept.append({"content": x_url, "url": x_url, "title": title})
            elif article.startswith("http") and url_alive(article):
                kept.append({"content": f"**{title}**\n{article}",
                             "url": article, "title": title})
            else:
                log.warning("Dropped unvalidated story: %s (x=%s, article=%s)",
                            title, x_url or "-", article or "-")
                continue
            seen.update({x_url, article} - {""})
            seen_titles.append(title)
        out[section] = kept
    return out


X_URL_RE = re.compile(r"^https://(?:x|twitter)\.com/([A-Za-z0-9_]+)/status/\d+", re.I)

# English-only major outlets: notability given, no language check needed
# (their tweets are often just "headline + link", too short for heuristics).
ENGLISH_SAFE_ACCOUNTS = {
    "elonmusk", "reuters", "afp", "skynews", "bbcworld", "bbcbreaking",
    "ft", "financialtimes", "bloomberg", "business", "wsj", "politicoeurope",
    "politico", "euronews", "dwnews", "dw_politics", "spectatorindex",
    "eucouncil", "eu_commission", "ap", "apnews", "bnonews", "cnbc",
    "guardian", "guardiannews", "thetimes", "telegraph", "economist",
    "nytimes", "cnn", "abc", "cbsnews", "nbcnews", "sky", "france24",
}
# Commentary/aggregator accounts: allowed, but tweet text must read as English.
MIXED_ACCOUNTS = {
    "visegrad24", "marionawfal", "radiogenoa", "disclosetv", "bricsinfo",
    "globeeyenews", "afpost", "endwokeness", "europeinvasionn", "idf",
    "zelenskyyua", "emmanuelmacron",
    # major European right-leaning political figures (per server curation)
    "nigel_farage", "alice_weidel", "matteosalvinimi", "santi_abascal",
    "pm_viktororban", "geertwilderspvv", "georgiameloni",
}
ACCOUNT_ALLOWLIST = ENGLISH_SAFE_ACCOUNTS | MIXED_ACCOUNTS

ENGLISH_HINTS = re.compile(
    r"\b(the|of|and|to|in|is|for|on|with|has|have|will|was|were|are|that|this|from|by|be|it)\b",
    re.I,
)


def tweet_ok(url: str) -> bool:
    """A tweet may be posted bare only if it exists, its author is on the
    allowlist, and its text reads as English (oEmbed-backed checks)."""
    m = X_URL_RE.match(url)
    if not m or m.group(1).lower() not in ACCOUNT_ALLOWLIST:
        if m:
            log.info("Rejecting tweet from non-allowlisted account @%s", m.group(1))
        return False
    try:
        q = urllib.parse.urlencode({"url": url, "omit_script": "true"})
        req = urllib.request.Request(
            f"https://publish.twitter.com/oembed?{q}",
            headers={"User-Agent": "Mozilla/5.0 (news-digest validator)"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            if r.status != 200:
                return False
            data = json.loads(r.read())
    except Exception:
        return False
    if m.group(1).lower() in ENGLISH_SAFE_ACCOUNTS:
        return True
    text = re.sub(r"<[^>]+>", " ", data.get("html", ""))
    hits = len(ENGLISH_HINTS.findall(text))
    if hits < 2:
        log.info("Rejecting non-English/low-text tweet %s (en-hits=%d)", url, hits)
        return False
    return True


def url_alive(url: str) -> bool:
    """Anti-hallucination check: fabricated article URLs 404; bot-blocked
    real articles (Reuters/Bloomberg 401/403) still count as alive."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status < 400
    except urllib.error.HTTPError as e:
        return e.code in (401, 403, 429)
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--pulse", action="store_true",
                    help="rolling breaking-news check (empty result = success)")
    args = ap.parse_args()
    setup_logging()
    token = politics_bot_token()

    if args.pulse:
        recent = pulse_recent_posts()
        last_hour = [t for t in recent if t > time.time() - 3600]
        if len(last_hour) >= PULSE_HOURLY_CAP:
            log.info("Pulse skipped: hourly cap reached (%d posts)", len(last_hour))
            return
        try:
            data = gather_pulse()
        except Exception:
            log.exception("Pulse gather failed; next run in 15 min")
            return
        total = sum(post_section(section, data[section], token)
                    for section in CHANNELS if data.get(section))
        if total:
            now = time.time()
            entries = [{"u": s["url"], "t": s["title"], "ts": now}
                       for sec in data.values() for s in sec]
            save_history(load_history() + entries)
            pulse_record_posts(total)
        log.info("Pulse complete: %d new stories.", total)
        return

    data = gather()
    total = sum(post_section(section, data[section], token) for section in CHANNELS)
    now = time.time()
    entries = [{"u": s["url"], "t": s["title"], "ts": now}
               for sec in data.values() for s in sec]
    save_history(load_history() + entries)
    if total == 0:
        sys.exit(1)
    log.info("Digest complete: %d stories.", total)


if __name__ == "__main__":
    main()
