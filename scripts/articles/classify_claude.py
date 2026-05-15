"""
LEGACY ROLLBACK — classify_claude.py
======================================
Original Claude + Voyage-based classification. Kept as rollback only.
To reactivate: in run_articles.py, import from articles.classify_claude
instead of articles.classify.

Original: LightSignal — classify.py
===========================
Stage 4 of the article pipeline.

For each article with classify_status = "pending" and a summary in
staged_articles.csv:
  - Runs Voyage AI semantic duplicate detection
  - Classifies non-duplicates with Claude
  - Updates the staging file with all classification fields
  - Appends results to news_feed.csv (the handoff to the LightSignal pipeline)

Run directly:
  python scripts/articles/classify.py

Or called by:
  python scripts/articles/run_articles.py
"""

import csv
import httpx
import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import voyageai
import numpy as np

# ── Path setup ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from utils.config import (
    FILE_STAGED, FILE_NEWS_FEED, FILE_DUPLICATE_CACHE,
    ARTICLES_MODEL, ARTICLES_EMBEDDING_MODEL,
    DUPLICATE_THRESHOLD, DUPLICATE_WINDOW_DAYS,
    CORE_FOOTPRINT, EXPANSION_MARKETS,
)

# ── Logging ───────────────────────────────────────────────────────────────────
log = logging.getLogger(__name__)

# ── SSL bypass (corporate network) ───────────────────────────────────────────
_http_client = httpx.Client(verify=False)

# ── Geography ─────────────────────────────────────────────────────────────────
RELEVANT_STATES = CORE_FOOTPRINT | EXPANSION_MARKETS

# ── News feed columns (must match transform_articles.py expectations) ─────────
NEWS_FEED_COLUMNS = [
    "ID", "Title", "CleanURL", "Source", "PublishedDate",
    "Summary_AI", "Primary_Category", "Secondary_Categories",
    "States", "DC_ID", "Is_Duplicate", "Duplicate_Of",
    "Strategy_Alignment_Score", "Relevance_Score",
    "Mentions_Specific_DC", "Article_Text",
]

# ── Classification prompt ─────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a market intelligence analyst for a long-haul fiber and network 
infrastructure company (Lightpath) tracking data center and network investment signals.

COMPANY CONTEXT:
Core footprint states: NY, NJ, CT, MA, PA, OH, FL, AZ
Expansion markets: TX, WI, IL, MO, IN, MI, VA, WV, UT

PRIMARY CATEGORIES (pick exactly one):
- Data Center Development: New DC announcements, groundbreakings, expansions, campus developments
- Fiber & Network Infrastructure: Fiber builds, network expansions, submarine cables, long-haul routes
- Hyperscaler Strategy: AWS, Azure, Google, Meta, Apple, Oracle — strategic moves, investment plans, capacity announcements
- M&A & Capital Markets: Acquisitions, mergers, funding rounds, IPOs, asset sales in infrastructure
- Power & Utilities: Power procurement, grid connections, energy agreements, utility constraints for DCs
- Regulatory & Community Pushback: Zoning disputes, moratoriums, legislation, environmental challenges to DC/fiber builds
- Technology & Architecture: AI chips, cooling tech, network architecture shifts that drive infrastructure demand

SECONDARY CATEGORIES (0-2, only if substantially covered — not just mentioned):
Same list as above. Leave empty [] if none apply.

STATES: List 2-letter US state codes explicitly mentioned in the article. Use [] for national/global stories.

STRATEGY ALIGNMENT SCORE (1-5):
5 = Strong specific signal: named project, major commitment, legislation with direct timeline impact
4 = Clear signal: announced project, significant investment, notable regulatory development
3 = Moderate signal: industry trend, general market movement, adjacent market activity  
2 = Weak signal: background context, minor mention, early-stage rumor
1 = No infrastructure angle OR investment publication content (stock picks, earnings recaps, price targets)

RELEVANCE SCORE (1-5) — based on geographic proximity to our markets:
5 = Core footprint state (NY, NJ, CT, MA, PA, OH, FL, AZ) with specific addressable opportunity
4 = Expansion market state (TX, WI, IL, MO, IN, MI, VA, WV, UT) OR adjacent state (GA, NC, MD, DE, NH, RI, VT, SC, KY, KS)
3 = Nearby/regional state with indirect relevance OR national story with clear footprint impact
2 = Non-footprint US state with general relevance
1 = International story, no US angle, or no geographic infrastructure relevance

IMPORTANT: Investment publications writing stock analysis, earnings recaps, or price targets 
should score Strategy=1 regardless of how many data centers are mentioned.

National stories about hyperscaler investment plans that could affect your markets score 3+ on relevance.

MENTIONS_SPECIFIC_DC: true only if a named data center facility is mentioned (not just "a data center").

Respond with ONLY valid JSON, no markdown, no explanation:
{
  "primary_category": "...",
  "secondary_categories": [],
  "states": [],
  "strategy_alignment_score": 1,
  "relevance_score": 1,
  "mentions_specific_dc": false,
  "is_duplicate": false
}"""


def build_classification_prompt(title: str, summary: str) -> str:
    return f"Classify this article:\n\nTitle: {title}\n\nSummary: {summary}"


# ── Duplicate detection ───────────────────────────────────────────────────────

def cosine_similarity(a: list, b: list) -> float:
    va, vb = np.array(a), np.array(b)
    norm = np.linalg.norm(va) * np.linalg.norm(vb)
    return float(np.dot(va, vb) / norm) if norm > 0 else 0.0


def load_cache(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        cache = json.load(f)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=DUPLICATE_WINDOW_DAYS)).isoformat()
    return {k: v for k, v in cache.items() if v.get("date", "") >= cutoff}


def save_cache(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(cache, f, indent=2)


def voyage_embed(vc: voyageai.Client, text: str) -> list:
    """Embed text with automatic retry on rate limit."""
    for attempt in range(10):
        try:
            result = vc.embed([text], model=ARTICLES_EMBEDDING_MODEL, input_type="document")
            return result.embeddings[0]
        except Exception as e:
            if "RateLimit" in type(e).__name__ or "rate_limit" in str(e).lower():
                wait = 20 * (attempt + 1)
                log.warning(f"    Voyage rate limit — waiting {wait}s...")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("Voyage rate limit: max retries exceeded")


# ── Classification ────────────────────────────────────────────────────────────

def classify_article(client: anthropic.Anthropic, title: str, summary: str) -> dict | None:
    """Call Claude to classify one article. Returns classification dict or None."""
    for attempt in range(3):
        try:
            response = client.messages.create(
                model=ARTICLES_MODEL,
                max_tokens=400,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": build_classification_prompt(title, summary)}],
            )
            raw = response.content[0].text.strip()
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw).strip()
            return json.loads(raw)
        except json.JSONDecodeError as e:
            log.warning(f"    JSON parse error (attempt {attempt + 1}): {e}")
            time.sleep(5)
        except Exception as e:
            wait = 10 * (attempt + 1)
            log.warning(f"    Claude error (attempt {attempt + 1}): {str(e)[:80]} — waiting {wait}s")
            time.sleep(wait)
    return None


# ── Staging / news feed helpers ───────────────────────────────────────────────

def load_staging(path: Path) -> list:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_staging(path: Path, rows: list) -> None:
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def load_existing_news_feed_ids(path: Path) -> set:
    """Load article IDs already written to news_feed.csv."""
    if not path.exists():
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return {row.get("ID", "") for row in reader}


def append_to_news_feed(path: Path, rows: list) -> None:
    """Append new classified articles to news_feed.csv."""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=NEWS_FEED_COLUMNS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


# ── Main ──────────────────────────────────────────────────────────────────────

def classify_articles() -> tuple:
    """
    Classify all pending articles, write results to news_feed.csv.
    Returns (classified_count, duplicate_count, failed_count).
    """
    log.info("=" * 60)
    log.info("  Stage 4: Classify Articles")
    log.info("=" * 60)

    rows    = load_staging(FILE_STAGED)
    pending = [
        r for r in rows
        if r.get("classify_status") == "pending"
        and r.get("summarize_status") == "success"
        and r.get("summary_ai")
    ]

    log.info(f"  Pending classification: {len(pending)}")

    if not pending:
        log.info("  Nothing to classify.")
        return 0, 0, 0

    client        = anthropic.Anthropic(http_client=_http_client)
    vc            = voyageai.Client()
    cache         = load_cache(FILE_DUPLICATE_CACHE)
    existing_ids  = load_existing_news_feed_ids(FILE_NEWS_FEED)
    row_by_id     = {r["article_id"]: r for r in rows}

    classified_count = 0
    duplicate_count  = 0
    failed_count     = 0
    news_feed_rows   = []

    for i, article in enumerate(pending, 1):
        article_id = article["article_id"]
        title      = article.get("title", "")
        summary    = article.get("summary_ai", "")

        log.info(f"  [{i:3}/{len(pending)}] {title[:65]}")

        # Skip if already written to news feed (checkpoint-style safety)
        if article_id in existing_ids:
            row_by_id[article_id]["classify_status"] = "success"
            log.info(f"         ↩  Already in news_feed — skipping")
            continue

        # --- Duplicate detection ---
        embed_text = f"{title} {summary}"
        try:
            embedding = voyage_embed(vc, embed_text)
        except Exception as e:
            log.error(f"         ✗  Voyage error: {e}")
            failed_count += 1
            continue

        is_dup    = False
        dup_of_id = ""
        for cached_id, cached in cache.items():
            sim = cosine_similarity(embedding, cached["embedding"])
            if sim >= DUPLICATE_THRESHOLD:
                is_dup    = True
                dup_of_id = cached_id
                log.info(f"         ⚠  DUPLICATE (sim={sim:.3f}) of {cached_id}")
                log.info(f"         Original: {cached['title'][:60]}")
                duplicate_count += 1
                break

        # Add to rolling cache regardless of duplicate status
        cache[article_id] = {
            "date"     : datetime.now(timezone.utc).isoformat(),
            "title"    : title,
            "embedding": embedding,
        }

        if is_dup:
            row_by_id[article_id]["classify_status"] = "success"
            news_feed_rows.append({
                "ID"                      : article_id,
                "Title"                   : title,
                "CleanURL"                : article.get("clean_url", ""),
                "Source"                  : article.get("source", ""),
                "PublishedDate"           : article.get("published_date", ""),
                "Summary_AI"              : summary,
                "Primary_Category"        : "",
                "Secondary_Categories"    : "",
                "States"                  : "[]",
                "DC_ID"                   : "",
                "Is_Duplicate"            : "True",
                "Duplicate_Of"            : dup_of_id,
                "Strategy_Alignment_Score": "",
                "Relevance_Score"         : "",
                "Mentions_Specific_DC"    : "",
                "Article_Text"            : article.get("article_text", "")[:2000],
            })
            continue

        # --- Classify ---
        cl = classify_article(client, title, summary)

        if cl:
            classified_count += 1
            states_str = json.dumps(cl.get("states", []))
            secondary_str = "; ".join(cl.get("secondary_categories", []))
            log.info(f"         Primary:  {cl.get('primary_category', '')}")
            log.info(f"         Strategy: {cl.get('strategy_alignment_score')}  "
                     f"Relevance: {cl.get('relevance_score')}  "
                     f"DC: {cl.get('mentions_specific_dc')}")
            log.info(f"         States:   {cl.get('states', [])}")

            row_by_id[article_id]["classify_status"] = "success"
            news_feed_rows.append({
                "ID"                      : article_id,
                "Title"                   : title,
                "CleanURL"                : article.get("clean_url", ""),
                "Source"                  : article.get("source", ""),
                "PublishedDate"           : article.get("published_date", ""),
                "Summary_AI"              : summary,
                "Primary_Category"        : cl.get("primary_category", ""),
                "Secondary_Categories"    : secondary_str,
                "States"                  : states_str,
                "DC_ID"                   : "",   # populated later by transform_articles.py
                "Is_Duplicate"            : "False",
                "Duplicate_Of"            : "",
                "Strategy_Alignment_Score": cl.get("strategy_alignment_score", ""),
                "Relevance_Score"         : cl.get("relevance_score", ""),
                "Mentions_Specific_DC"    : str(cl.get("mentions_specific_dc", False)),
                "Article_Text"            : article.get("article_text", "")[:2000],
            })
        else:
            failed_count += 1
            row_by_id[article_id]["classify_status"] = "failed"
            log.warning(f"         ✗  Classification failed")

    # Write results
    save_staging(FILE_STAGED, list(row_by_id.values()))
    save_cache(FILE_DUPLICATE_CACHE, cache)
    append_to_news_feed(FILE_NEWS_FEED, news_feed_rows)

    log.info(f"  Classified:  {classified_count}")
    log.info(f"  Duplicates:  {duplicate_count}")
    log.info(f"  Failed:      {failed_count}")
    log.info(f"  Written to:  {FILE_NEWS_FEED}")

    return classified_count, duplicate_count, failed_count


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  [%(levelname)s]  %(message)s",
        datefmt="%H:%M:%S",
    )
    classify_articles()
