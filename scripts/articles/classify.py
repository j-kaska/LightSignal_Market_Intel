"""
LightSignal — classify.py
===========================
Stage 4 of the article pipeline.

For each article with classify_status = "pending" and a summary in
staged_articles.csv:
  - Runs sentence-transformers semantic duplicate detection (backstop for Stage 1.5)
  - Classifies non-duplicates with Gemini 2.0 Flash (falls back to Anthropic on quota exhaustion)
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
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openai import OpenAI
from sentence_transformers import SentenceTransformer
import numpy as np

# ── Path setup ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from utils.config import (
    FILE_STAGED, FILE_NEWS_FEED, FILE_DUPLICATE_CACHE,
    ARTICLES_MODEL, GEMINI_BASE_URL,
    API_PROVIDER, ANTHROPIC_MODEL,
    SENTENCE_TRANSFORMER_MODEL,
    DUPLICATE_THRESHOLD, DUPLICATE_WINDOW_DAYS,
    CORE_FOOTPRINT, EXPANSION_MARKETS,
)

# ── Logging ───────────────────────────────────────────────────────────────────
log = logging.getLogger(__name__)

# ── Quota sentinel ────────────────────────────────────────────────────────────
QUOTA_EXHAUSTED = object()

# ── SSL bypass (corporate network) ───────────────────────────────────────────
_http_client = httpx.Client(verify=False)

# ── Provider-agnostic client + call ──────────────────────────────────────────

def _make_client(provider=None):
    p = provider or API_PROVIDER
    if p == "anthropic":
        import anthropic
        return anthropic.Anthropic(http_client=_http_client)
    return OpenAI(
        base_url=GEMINI_BASE_URL,
        api_key=os.environ.get("GEMINI_API_KEY", ""),
        http_client=_http_client,
    )


def _is_rate_limit(e: Exception) -> bool:
    return getattr(e, "status_code", None) == 429 or "RateLimitError" in type(e).__name__


def _call_llm(client, system_prompt: str, user_content: str, max_tokens: int, provider=None) -> str:
    p = provider or API_PROVIDER
    if p == "anthropic":
        import anthropic
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        return response.content[0].text.strip()
    response = client.chat.completions.create(
        model=ARTICLES_MODEL,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_content},
        ],
    )
    return response.choices[0].message.content.strip()

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
Core footprint states: NY, NJ, CT, MA, PA, OH, AZ
Florida presence is Miami/South Florida ONLY — not statewide.
Expansion markets: TX, WI, IL, MO, IN, MI, VA, WV, UT

PRIMARY CATEGORIES (pick exactly one):
- Data Center Development: New DC announcements, groundbreakings, expansions, campus developments
- Fiber & Network Infrastructure: Fiber builds, network expansions, submarine cables, long-haul routes
- Hyperscaler Strategy: AWS, Azure, Google, Meta, Apple, Oracle, Anthropic, OpenAI, xAI — strategic moves, investment plans, capacity announcements
- M&A & Capital Markets: Acquisitions, mergers, funding rounds, IPOs, asset sales in infrastructure
- Power & Utilities: Power procurement, grid connections, energy agreements, utility constraints for DCs
- Regulatory & Community Pushback: Zoning disputes, moratoriums, legislation, environmental challenges to DC/fiber builds
- Technology & Architecture: AI chips, cooling tech, network architecture shifts that drive infrastructure demand

SECONDARY CATEGORIES (0-2, only if substantially covered — not just mentioned):
Same list as above. Leave empty [] if none apply.

STATES: List 2-letter US state codes explicitly mentioned in the article. Use [] for national/global stories.

STRATEGY ALIGNMENT SCORE (1-5):
5 = Strong specific signal: named project/site with committed capital, signed contract, legislation
    with binding timeline, or major hyperscaler capacity commitment that directly affects route decisions
4 = Clear actionable signal: announced project with named location and developer, significant
    regulatory decision with real construction impact — NOT general industry trends
3 = Moderate signal: industry trend or market movement that could indirectly affect routing decisions,
    general hyperscaler plans without named locations
2 = Weak signal: background context, company financial news, early-stage rumors, technology
    announcements without direct infrastructure implications
1 = No infrastructure angle; OR investment/earnings content (stock picks, price targets, earnings recaps,
    analyst ratings); OR org/people announcements (new hires, appointments, departures, restructurings);
    OR opinion/commentary columns with no actionable signal

RELEVANCE SCORE (1-5) — geographic proximity to our network markets:
5 = Core footprint state (NY, NJ, CT, MA, PA, OH, AZ) with specific named location;
    OR Miami/South Florida specifically (our FL presence is Miami-only — not statewide)
4 = Expansion market (TX, WI, IL, MO, IN, MI, VA, WV, UT) OR adjacent state
    (GA, NC, MD, DE, NH, RI, VT, SC, KY, KS) with specific named location
3 = Non-adjacent US state with specific named location; OR national story with clear
    named-market impact; OR Florida story outside Miami/South Florida
2 = Non-footprint US state with no named location; OR general US story without named-market impact
1 = International story (any non-US country, even if US companies are mentioned);
    OR any article scoring Strategy=1 (investment content, earnings, org news — geography irrelevant)

IMPORTANT: If an article scores Strategy=1 for ANY reason, Relevance MUST also be 1.
Geographic proximity is irrelevant when there is no infrastructure signal.

IMPORTANT: SeekingAlpha, The Motley Fool, Investor.com, and similar investment sites publish
stock analysis, not infrastructure intelligence. Score Strategy=1, Relevance=1 regardless of content.

IMPORTANT: Articles about infrastructure in non-US countries (UK, EU, Asia, Australia, Canada, etc.)
score Relevance=1 even if major US companies are mentioned. Our network does not reach international
locations.

MENTIONS_SPECIFIC_DC: true only if the article references a data center by a specific identifying
name — a facility/campus name (e.g. "QTS Richmond", "Equinix NY5", "Project Gravity", "Project Blue"),
a well-known campus designation (e.g. "Google's The Dalles facility", "Meta's Prineville campus"),
or a named project with a distinct identifier. A city or county name alone does NOT qualify —
"a data center in Ashburn" or "proposed data center in Box Elder County" are NOT specific enough.
The facility must have a name that distinguishes it from other data centers in the same area.

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
    if not cache:
        return {}
    # Guard against Voyage (512-dim) → sentence-transformers (384-dim) migration
    first = next(iter(cache.values()))
    emb = first.get("embedding", [])
    if len(emb) not in (0, 384):
        log.warning(
            f"  duplicate_cache.json has {len(emb)}-dim embeddings "
            f"(expected 384 for {SENTENCE_TRANSFORMER_MODEL}). Cache cleared."
        )
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=DUPLICATE_WINDOW_DAYS)).isoformat()
    return {k: v for k, v in cache.items() if v.get("date", "") >= cutoff}


def save_cache(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(cache, f, indent=2)


_st_model = None

def _get_st_model() -> SentenceTransformer:
    global _st_model
    if _st_model is None:
        log.info(f"  Loading sentence-transformer model: {SENTENCE_TRANSFORMER_MODEL}")
        _st_model = SentenceTransformer(SENTENCE_TRANSFORMER_MODEL)
        log.info("  Model loaded.")
    return _st_model


def st_embed(model: SentenceTransformer, text: str) -> list:
    return model.encode(text, show_progress_bar=False).tolist()


# ── Classification ────────────────────────────────────────────────────────────

def classify_article(client, title: str, summary: str, provider=None):
    """
    Call configured LLM to classify one article.
    Returns classification dict, None on non-quota failure, or QUOTA_EXHAUSTED sentinel
    if all retries failed with rate-limit/billing errors.
    """
    rate_limit_count = 0
    for attempt in range(3):
        try:
            raw = _call_llm(client, SYSTEM_PROMPT, build_classification_prompt(title, summary), max_tokens=400, provider=provider)
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw).strip()
            return json.loads(raw)
        except json.JSONDecodeError as e:
            log.warning(f"    JSON parse error (attempt {attempt + 1}): {e}")
            time.sleep(5)
        except Exception as e:
            if _is_rate_limit(e):
                rate_limit_count += 1
                wait = 60 * (attempt + 1)
                log.warning(f"    Rate limited (attempt {attempt + 1}): waiting {wait}s")
            else:
                wait = 10 * (attempt + 1)
                log.warning(f"    LLM error (attempt {attempt + 1}): {str(e)[:80]} — waiting {wait}s")
            time.sleep(wait)
    if rate_limit_count == 3:
        return QUOTA_EXHAUSTED
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
    log.info(f"  Provider: {API_PROVIDER}")
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

    active_provider    = API_PROVIDER
    active_client      = _make_client()
    fallback_triggered = False
    st_model           = _get_st_model()
    cache              = load_cache(FILE_DUPLICATE_CACHE)
    existing_ids       = load_existing_news_feed_ids(FILE_NEWS_FEED)
    row_by_id          = {r["article_id"]: r for r in rows}

    classified_count  = 0
    duplicate_count   = 0
    failed_count      = 0
    consecutive_fails = 0
    MAX_CONSECUTIVE   = 5
    news_feed_rows    = []

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
            embedding = st_embed(st_model, embed_text)
        except Exception as e:
            log.error(f"         ✗  Embedding error: {e}")
            failed_count += 1
            continue

        is_dup    = False
        dup_of_id = ""
        for cached_id, cached in cache.items():
            if cached_id == article_id:
                continue  # never flag an article as a duplicate of itself
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
        cl = classify_article(active_client, title, summary, provider=active_provider)

        # Gemini quota exhausted — switch to Anthropic and retry this article
        if cl is QUOTA_EXHAUSTED and not fallback_triggered and active_provider == "gemini":
            log.warning("  ⚠  Gemini quota exhausted — switching to Anthropic fallback for this run")
            active_client      = _make_client("anthropic")
            active_provider    = "anthropic"
            fallback_triggered = True
            cl = classify_article(active_client, title, summary, provider="anthropic")

        if cl and cl is not QUOTA_EXHAUSTED:
            classified_count  += 1
            consecutive_fails  = 0
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
            failed_count      += 1
            consecutive_fails += 1
            row_by_id[article_id]["classify_status"] = "failed"
            log.warning("         ✗  Classification failed")
            if consecutive_fails >= MAX_CONSECUTIVE:
                log.error(f"  {MAX_CONSECUTIVE} consecutive failures — daily quota likely exhausted. Stopping.")
                break

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
