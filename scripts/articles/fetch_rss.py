"""
LightSignal — fetch_rss.py
============================
Stage 1 of the article pipeline.

Reads RSS feeds from rss_feeds.txt, parses each feed, and writes new
articles to the staging file. Skips exact URL duplicates using a rolling
seen_urls cache so the same article is never staged twice.

Google Alerts RSS format:
  <link> contains the Google redirect URL (e.g. https://news.google.com/rss/articles/...)
  The real URL is resolved later by Selenium in extract.py.

Output: data/articles/staging/staged_articles.csv
        data/articles/staging/seen_urls.json  (rolling exact-dupe cache)

Run directly:
  python scripts/articles/fetch_rss.py

Or called by:
  python scripts/articles/run_articles.py
"""

import csv
import json
import logging
import sys
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import feedparser
import requests

# ── Path setup ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from utils.config import ARTICLES_DIR, FILE_RSS_FEEDS, FILE_STAGED, FILE_SEEN_URLS, BLOCKED_URL_PATTERNS

# ── Logging ───────────────────────────────────────────────────────────────────
log = logging.getLogger(__name__)

# How many days to keep URLs in the seen cache before pruning
SEEN_URL_RETENTION_DAYS = 30

# Staging CSV columns
STAGING_COLUMNS = [
    "article_id",
    "title",
    "source",
    "raw_url",        # Google redirect URL — resolved to clean URL in extract.py
    "clean_url",      # populated by extract.py
    "published_date",
    "rss_description",  # short RSS snippet — used as fallback if extraction fails
    "article_text",   # populated by extract.py
    "summary_ai",     # populated by summarize.py
    "staged_at",
    "extraction_status",  # pending / success / failed
    "summarize_status",   # pending / success / failed
    "classify_status",    # pending / success / failed
]


# ── Seen URL cache ────────────────────────────────────────────────────────────

def load_seen_urls(path: Path) -> dict:
    """Load seen URLs cache. Returns {url: iso_date_string}."""
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def save_seen_urls(path: Path, seen: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Prune entries older than retention period
    cutoff = datetime.now(timezone.utc).timestamp() - (SEEN_URL_RETENTION_DAYS * 86400)
    pruned = {
        url: ts for url, ts in seen.items()
        if datetime.fromisoformat(ts).timestamp() > cutoff
    }
    with open(path, "w") as f:
        json.dump(pruned, f, indent=2)
    if len(pruned) < len(seen):
        log.info(f"  Pruned {len(seen) - len(pruned)} old URLs from seen cache")


# ── Feed parsing ──────────────────────────────────────────────────────────────

def load_feeds(path: Path) -> list:
    """Load feed URLs from rss_feeds.txt, ignoring comments and blank lines."""
    if not path.exists():
        log.error(f"RSS feeds file not found: {path}")
        sys.exit(1)
    urls = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


def parse_date(entry) -> str:
    """Extract and normalize published date from RSS entry."""
    for field in ("published", "updated", "created"):
        raw = getattr(entry, field, None)
        if raw:
            try:
                dt = parsedate_to_datetime(raw)
                return dt.astimezone(timezone.utc).strftime("%m/%d/%Y %I:%M %p")
            except Exception:
                pass
    return datetime.now(timezone.utc).strftime("%m/%d/%Y %I:%M %p")


def clean_html_snippet(text: str) -> str:
    """Strip HTML tags from RSS description snippet."""
    if not text:
        return ""
    clean = re.sub(r"<[^>]+>", "", text)
    clean = re.sub(r"&amp;", "&", clean)
    clean = re.sub(r"&quot;", '"', clean)
    clean = re.sub(r"&#\d+;", "", clean)
    return clean.strip()


def extract_source(entry, feed_url: str) -> str:
    """Best-effort source name from RSS entry."""
    # feedparser sometimes provides source.title
    source = getattr(getattr(entry, "source", None), "title", None)
    if source:
        return source
    # Try tags
    if hasattr(entry, "tags") and entry.tags:
        return entry.tags[0].get("term", "")
    return ""


def generate_article_id() -> str:
    """Generate a unique article ID based on timestamp + microseconds."""
    now = datetime.now(timezone.utc)
    return f"ART-{now.strftime('%Y%m%d%H%M%S%f')}"


def fetch_feed(feed_url: str) -> list:
    """
    Fetch and parse a single RSS feed.
    Returns list of raw entry dicts.
    """
    try:
        # feedparser handles the HTTP request
        feed = feedparser.parse(feed_url)
        if feed.bozo and not feed.entries:
            log.warning(f"  Feed parse warning ({feed_url[:60]}...): {feed.bozo_exception}")
            return []
        log.info(f"  Fetched {len(feed.entries)} entries from feed")
        return feed.entries
    except Exception as e:
        log.error(f"  Failed to fetch feed {feed_url[:60]}: {e}")
        return []


# ── Main ──────────────────────────────────────────────────────────────────────

def fetch_rss() -> int:
    """
    Fetch all RSS feeds, filter exact URL duplicates, write new articles
    to staged_articles.csv. Returns count of new articles staged.
    """
    log.info("=" * 60)
    log.info("  Stage 1: Fetch RSS")
    log.info("=" * 60)

    feed_urls   = load_feeds(FILE_RSS_FEEDS)
    seen_urls   = load_seen_urls(FILE_SEEN_URLS)
    log.info(f"  Feeds: {len(feed_urls)}  |  Seen URL cache: {len(seen_urls)} entries")

    # Load existing staged articles to avoid overwriting in-progress ones
    existing_staged = set()
    if FILE_STAGED.exists():
        try:
            with open(FILE_STAGED, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    existing_staged.add(row.get("raw_url", ""))
        except Exception:
            pass

    new_articles = []
    total_fetched = 0
    skipped_seen  = 0
    skipped_staged = 0

    for feed_url in feed_urls:
        log.info(f"  Fetching: {feed_url[:70]}")
        entries = fetch_feed(feed_url)
        total_fetched += len(entries)

        for entry in entries:
            raw_url = entry.get("link", "").strip()
            if not raw_url:
                continue

            # Block known non-article sources (e.g. YouTube videos)
            if any(pattern in raw_url for pattern in BLOCKED_URL_PATTERNS):
                log.debug(f"  Blocked URL: {raw_url[:70]}")
                continue

            # Exact URL duplicate check — against seen cache
            if raw_url in seen_urls:
                skipped_seen += 1
                continue

            # Already in current staging file (not yet processed)
            if raw_url in existing_staged:
                skipped_staged += 1
                continue

            title       = clean_html_snippet(entry.get("title", ""))
            description = clean_html_snippet(
                entry.get("summary", "") or entry.get("description", "")
            )
            pub_date    = parse_date(entry)
            source      = extract_source(entry, feed_url)
            article_id  = generate_article_id()

            new_articles.append({
                "article_id"         : article_id,
                "title"              : title,
                "source"             : source,
                "raw_url"            : raw_url,
                "clean_url"          : "",
                "published_date"     : pub_date,
                "rss_description"    : description,
                "article_text"       : "",
                "summary_ai"         : "",
                "staged_at"          : datetime.now(timezone.utc).isoformat(),
                "extraction_status"  : "pending",
                "summarize_status"   : "pending",
                "classify_status"    : "pending",
            })

            # Mark as seen immediately so we don't double-stage within this run
            seen_urls[raw_url] = datetime.now(timezone.utc).isoformat()

    log.info(f"  Total entries fetched:    {total_fetched}")
    log.info(f"  Skipped (seen cache):     {skipped_seen}")
    log.info(f"  Skipped (already staged): {skipped_staged}")
    log.info(f"  New articles to stage:    {len(new_articles)}")

    if new_articles:
        # Append to staging file (or create if doesn't exist)
        FILE_STAGED.parent.mkdir(parents=True, exist_ok=True)
        write_header = not FILE_STAGED.exists()

        with open(FILE_STAGED, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=STAGING_COLUMNS)
            if write_header:
                writer.writeheader()
            writer.writerows(new_articles)

        log.info(f"  Staged → {FILE_STAGED}")

    save_seen_urls(FILE_SEEN_URLS, seen_urls)
    log.info(f"  Seen URL cache saved ({len(seen_urls)} entries)")

    return len(new_articles)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  [%(levelname)s]  %(message)s",
        datefmt="%H:%M:%S",
    )
    fetch_rss()
