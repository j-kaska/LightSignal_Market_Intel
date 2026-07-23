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
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from shutil import copy2

from sentence_transformers import SentenceTransformer
import numpy as np

# ── Path setup ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from articles._llm import (
    QUOTA_EXHAUSTED, PROVIDER_UNAVAILABLE,
    call_with_retries, make_client as _make_client,
)
from articles._staging import load_staging, save_staging, staged_on_or_after
from articles.dedup import DupIndex, _get_model as _get_dedup_model
from utils.config import (
    FILE_STAGED, FILE_NEWS_FEED, FILE_DUPLICATE_CACHE,
    FILE_NEWS_FEED_ROLLING_BACKUP,
    API_PROVIDER,
    SENTENCE_TRANSFORMER_MODEL,
    DUPLICATE_THRESHOLD, DUPLICATE_WINDOW_DAYS,
    CORE_FOOTPRINT, EXPANSION_MARKETS,
    LLM_MAX_WORKERS,
)

# ── Logging ───────────────────────────────────────────────────────────────────
log = logging.getLogger(__name__)

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
Expansion markets: TX, WI, IL, MO, IN, MI, VA, WB, UT

═══════════════════════════════════════════════════════════════
MANDATORY PRE-SCORING RULES — apply these BEFORE anything else
═══════════════════════════════════════════════════════════════

RULE 1 — INVESTMENT CONTENT: If the article is primarily stock analysis, earnings coverage,
or financial market content, set Strategy=1 AND Relevance=1, regardless of what companies or
data centers are mentioned. This includes:
  • Articles from SeekingAlpha, The Motley Fool, Benzinga, TheStreet, tastylive, or any site
    whose primary purpose is stock/investment coverage
  • Earnings call transcripts or summaries ("Q1 2026 earnings", "quarterly results")
  • Stock price targets, analyst ratings, buy/sell/hold recommendations
  • Articles framed as investment advice ("should you buy X", "best energy stocks")
  • Company financial results covered purely from an investor angle
  Exception: if the article also contains specific infrastructure announcements (a named project,
  a signed contract, a regulatory decision), score the infrastructure signal normally.

RULE 2 — INTERNATIONAL CONTENT: If the primary story location is outside the United States
(UK, EU, Canada, Australia, Asia, Middle East, Africa, Latin America), set Relevance=1,
regardless of whether US companies are mentioned. Lightpath's fiber network is US-only.
  Exception: a story about a US company's global capex plan that explicitly names US locations
  can score Relevance 2-3 for its US component.

RULE 3 — STRATEGY=1 FORCES RELEVANCE=1: Whenever Strategy=1 for any reason, Relevance MUST
also be 1. Geographic proximity is irrelevant when there is no infrastructure signal.

═══════════════════════════════════════════════════════════════
PRIMARY CATEGORIES (pick exactly one)
═══════════════════════════════════════════════════════════════
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

═══════════════════════════════════════════════════════════════
STRATEGY ALIGNMENT SCORE (1-5)
═══════════════════════════════════════════════════════════════
(Check Rules 1-3 first. Only score 2-5 if those rules don't apply.)

5 = Strong specific signal: named project/site with committed capital, signed contract, legislation
    with binding timeline, or major hyperscaler capacity commitment that directly drives route decisions
4 = Clear actionable signal: announced project with named location and developer, significant
    regulatory decision with real construction impact. NOT general industry trends.
3 = Moderate signal: a named company announces plans for a named US market, or a significant
    regulatory/legislative development in a named state — but without confirmed capital or timeline.
    Must involve a specific named location or named market. Generic industry trends do NOT qualify.
2 = Weak signal: background context, general industry trend without named location, company financial
    news with indirect infrastructure implication, early-stage rumors, technology announcements
    without direct infrastructure commitment
1 = See Rule 1 above (investment/earnings/org content) OR no infrastructure angle whatsoever

═══════════════════════════════════════════════════════════════
RELEVANCE SCORE (1-5) — geographic proximity to our network markets
═══════════════════════════════════════════════════════════════
(Check Rules 2-3 first. Only score 2-5 if those rules don't apply.)

5 = Core footprint state (NY, NJ, CT, MA, PA, OH, AZ) with specific named location;
    OR Miami/South Florida specifically (our FL presence is Miami-only)
4 = Expansion market (TX, WI, IL, MO, IN, MI, VA, WV, UT) OR adjacent state
    (GA, NC, MD, DE, NH, RI, VT, SC, KY, KS) with specific named location
3 = Non-adjacent US state with specific named location; OR national story with clear
    named-market impact; OR Florida story outside Miami/South Florida
2 = Non-footprint US state with no named location; OR general US story without named-market impact
1 = See Rules 2 and 3 above

═══════════════════════════════════════════════════════════════
MENTIONS_SPECIFIC_DC
═══════════════════════════════════════════════════════════════
true only if the article references a data center by a specific facility or campus name
(e.g. "QTS Richmond", "Equinix NY5", "Project Gravity", "Meta's Prineville campus").
A city, county, or company name alone does NOT qualify — "a data center in Ashburn" or
"proposed data center in Box Elder County" are NOT specific enough.

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


def _get_st_model() -> SentenceTransformer:
    """
    Reuse Stage 1.5's loader. That one tries local_files_only first, which is what
    makes this work behind the corporate SSL intercept — a plain
    SentenceTransformer(name) reaches out to huggingface.co and fails, which
    silently disabled this backstop for 31 consecutive production runs.
    Sharing the loader also avoids holding a second copy of the model in memory,
    since run_articles.py runs Stage 1.5 and Stage 4 in the same process.
    """
    return _get_dedup_model()


def st_embed(model: SentenceTransformer, text: str) -> list:
    return model.encode(text, show_progress_bar=False).tolist()


# ── Classification ────────────────────────────────────────────────────────────

def classify_article(client, title: str, summary: str, provider=None):
    """
    Call configured LLM to classify one article.
    Returns classification dict, None on non-quota failure, or QUOTA_EXHAUSTED sentinel
    if all retries failed with rate-limit/billing errors.
    """
    return call_with_retries(
        client,
        SYSTEM_PROMPT,
        build_classification_prompt(title, summary),
        max_tokens=400,
        provider=provider,
        parse=json.loads,
        label="Classify",
    )


# ── News feed helpers ─────────────────────────────────────────────────────────

def load_existing_news_feed_ids(path: Path) -> set:
    """Load article IDs already written to news_feed.csv."""
    if not path.exists():
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return {row.get("ID", "") for row in reader}


def validate_news_feed_header(path: Path) -> None:
    """Ensure existing news_feed.csv has a valid header row before append."""
    if not path.exists():
        return

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)

    if not header:
        raise RuntimeError(
            f"news_feed.csv exists but is empty at {path}. "
            "Delete it or restore from backup, then re-run classify."
        )

    first_col = (header[0] or "").lstrip("\ufeff").strip()
    if first_col != "ID":
        raise RuntimeError(
            f"news_feed.csv header is invalid at {path}. "
            "Expected first column 'ID'. Restore from backup before re-running."
        )


def refresh_rolling_news_feed_backup(source: Path, backup: Path) -> None:
    """Overwrite the rolling restore point with the current valid news_feed.csv."""
    if not source.exists():
        return

    validate_news_feed_header(source)
    backup.parent.mkdir(parents=True, exist_ok=True)
    copy2(source, backup)
    log.info(f"  Rolling backup refreshed: {backup}")


def append_to_news_feed(path: Path, rows: list) -> None:
    """Append new classified articles to news_feed.csv."""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    validate_news_feed_header(path)
    write_header = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=NEWS_FEED_COLUMNS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


# ── Main ──────────────────────────────────────────────────────────────────────

def classify_articles(since: str = "") -> tuple:
    """
    Classify all pending articles, write results to news_feed.csv.

    `since` (ISO YYYY-MM-DD) restricts the run to articles staged on or after
    that date; older rows stay pending for a later unscoped run.

    Returns (classified_count, duplicate_count, failed_count).
    """
    log.info("=" * 60)
    log.info("  Stage 4: Classify Articles")
    log.info(f"  Provider: {API_PROVIDER}")
    if since:
        log.info(f"  Scope: articles staged on or after {since}")
    log.info("=" * 60)

    rows = load_staging(FILE_STAGED)

    def _ready(r) -> bool:
        return (
            r.get("classify_status") == "pending"
            and r.get("summarize_status") == "success"
            and r.get("summary_ai")
        )

    eligible = [r for r in rows if _ready(r)]
    pending  = [r for r in staged_on_or_after(rows, since) if _ready(r)]

    log.info(f"  Pending classification: {len(pending)}")
    deferred = len(eligible) - len(pending)
    if deferred:
        log.info(f"  Deferred by --since: {deferred} older articles left for a later run")

    if not pending:
        log.info("  Nothing to classify.")
        return 0, 0, 0

    active_provider    = API_PROVIDER
    active_client      = _make_client()
    fallback_triggered = False
    try:
        st_model = _get_st_model()
    except Exception as e:
        st_model = None
        log.warning(f"  Semantic dedup unavailable ({type(e).__name__}: {e}); continuing without embedding backstop")
    cache              = load_cache(FILE_DUPLICATE_CACHE)
    existing_ids       = load_existing_news_feed_ids(FILE_NEWS_FEED)
    row_by_id          = {r["article_id"]: r for r in rows}

    classified_count  = 0
    duplicate_count   = 0
    failed_count      = 0
    consecutive_fails = 0
    MAX_CONSECUTIVE   = 5
    news_feed_rows    = []

    # ── Phase 1: local work — skip already-written rows, then semantic dedup ──
    # All CPU-local, so it runs up front: embeddings are computed in one batch and
    # compared via a rolling matrix index instead of a per-article Python loop.
    to_classify = []   # (index_in_pending, article) still needing an LLM call

    candidates = []
    for i, article in enumerate(pending, 1):
        article_id = article["article_id"]
        # Skip if already written to news feed (checkpoint-style safety)
        if article_id in existing_ids:
            row_by_id[article_id]["classify_status"] = "success"
            log.info(f"  [{i:3}/{len(pending)}] {article.get('title', '')[:65]}")
            log.info(f"         ↩  Already in news_feed — skipping")
            continue
        candidates.append((i, article))

    embeddings = {}
    if st_model is not None and candidates:
        try:
            texts = [f"{a.get('title', '')} {a.get('summary_ai', '')}" for _, a in candidates]
            vecs  = st_model.encode(texts, batch_size=32, show_progress_bar=False)
            embeddings = {a["article_id"]: vecs[k] for k, (_, a) in enumerate(candidates)}
        except Exception as e:
            log.warning(f"  Batch embedding failed ({type(e).__name__}: {e}); "
                        f"proceeding without dedup backstop")

    dup_index = DupIndex(cache, extra_capacity=len(candidates)) if embeddings else None

    for i, article in candidates:
        article_id = article["article_id"]
        title      = article.get("title", "")
        summary    = article.get("summary_ai", "")

        is_dup    = False
        dup_of_id = ""
        embedding = embeddings.get(article_id)

        if embedding is not None and dup_index is not None:
            cached_id, sim = dup_index.best_match(
                embedding, DUPLICATE_THRESHOLD, exclude_id=article_id
            )
            if cached_id:
                is_dup    = True
                dup_of_id = cached_id
                log.info(f"  [{i:3}/{len(pending)}] {title[:65]}")
                log.info(f"         ⚠  DUPLICATE (sim={sim:.3f}) of {cached_id}")
                log.info(f"         Original: {cache.get(cached_id, {}).get('title', '')[:60]}")
                duplicate_count += 1

            # Add to rolling cache when embedding succeeded
            emb_list = embedding.tolist() if hasattr(embedding, "tolist") else list(embedding)
            cache[article_id] = {
                "date"     : datetime.now(timezone.utc).isoformat(),
                "title"    : title,
                "embedding": emb_list,
            }
            dup_index.add(article_id, embedding)

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

        to_classify.append((i, article))

    # ── Phase 2: network work — classify non-duplicates concurrently ─────────
    # Batched so the quota-fallback switch and the consecutive-failure circuit
    # breaker still behave as they did when this ran serially.
    workers = max(1, min(LLM_MAX_WORKERS, len(to_classify))) if to_classify else 1
    if to_classify:
        log.info(f"  Classifying {len(to_classify)} articles — concurrency: {workers} workers")

    stop = False
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for batch_start in range(0, len(to_classify), workers):
            if stop:
                break
            batch = to_classify[batch_start:batch_start + workers]

            client, provider = active_client, active_provider
            results = list(pool.map(
                lambda pair: classify_article(client, pair[1].get("title", ""),
                                              pair[1].get("summary_ai", ""), provider=provider),
                batch,
            ))

            if (not fallback_triggered and active_provider == "gemini"
                    and any(r is QUOTA_EXHAUSTED for r in results)):
                log.warning("  ⚠  Gemini quota exhausted — switching to Anthropic fallback for this run")
                active_client      = _make_client("anthropic")
                active_provider    = "anthropic"
                fallback_triggered = True
                redo = [j for j, r in enumerate(results) if r is QUOTA_EXHAUSTED]
                fb_client = active_client
                retried = list(pool.map(
                    lambda j: classify_article(fb_client, batch[j][1].get("title", ""),
                                               batch[j][1].get("summary_ai", ""),
                                               provider="anthropic"),
                    redo,
                ))
                for j, r in zip(redo, retried):
                    results[j] = r

            for (i, article), cl in zip(batch, results):
                article_id = article["article_id"]
                title      = article.get("title", "")
                summary    = article.get("summary_ai", "")
                log.info(f"  [{i:3}/{len(pending)}] {title[:65]}")

                provider_down = cl is QUOTA_EXHAUSTED or cl is PROVIDER_UNAVAILABLE

                if cl and not provider_down:
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
                elif provider_down:
                    # Provider unavailable, not the article. Leave 'pending' so the
                    # row is picked up again on a later run rather than stranded at
                    # 'failed' (which classify never retries). Heals after any outage.
                    consecutive_fails += 1
                    log.warning("         …  Provider unavailable — left pending, will retry")
                    if consecutive_fails >= MAX_CONSECUTIVE:
                        log.error(f"  {MAX_CONSECUTIVE} consecutive provider failures — likely down. Stopping.")
                        stop = True
                        break
                else:
                    # Genuine content failure — mark failed (classify does not retry these).
                    failed_count      += 1
                    consecutive_fails += 1
                    row_by_id[article_id]["classify_status"] = "failed"
                    log.warning("         ✗  Classification failed")
                    if consecutive_fails >= MAX_CONSECUTIVE:
                        log.error(f"  {MAX_CONSECUTIVE} consecutive failures — daily quota likely exhausted. Stopping.")
                        stop = True
                        break

    # Write results
    refresh_rolling_news_feed_backup(FILE_NEWS_FEED, FILE_NEWS_FEED_ROLLING_BACKUP)
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
