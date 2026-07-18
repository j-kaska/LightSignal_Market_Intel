"""
LightSignal — dedup.py
========================
Stage 1.5 of the article pipeline.

Stacked deduplication against all articles with extraction_status = "pending".
Runs AFTER fetch_rss.py and BEFORE extract.py, catching duplicates before
Selenium extraction and Gemini API costs are incurred.

Layer 1 — Title fuzzy match (rapidfuzz, no model):
  Catches same-story-different-outlet reposts where titles share >= 88%
  token overlap after normalizing out the outlet name suffix.

Layer 2 — Semantic embedding (sentence-transformers, local/free):
  Embeds title + rss_description for all pending articles in one batch call.
  Compares against a rolling 14-day embedding cache via cosine similarity.
  Catches paraphrased cross-outlet variants that share the same underlying facts.

Articles flagged as duplicates:
  - extraction_status  = "duplicate"  → skipped by extract.py
  - summarize_status   = "skipped"    → skipped by summarize.py
  - classify_status    = "success"    → counted as processed by run_articles.py
  - Written to news_feed.csv immediately with Is_Duplicate = True

Non-duplicates:
  - Added to title_cache.json and duplicate_cache.json for future comparisons
  - extraction_status stays "pending"  → Selenium runs normally

Run directly:
  python scripts/articles/dedup.py

Or called by:
  python scripts/articles/run_articles.py
"""

import csv
import json
import logging
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import numpy as np
from rapidfuzz import fuzz

# ── Path setup ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from utils.config import (
    FILE_STAGED, FILE_NEWS_FEED, FILE_DUPLICATE_CACHE, FILE_TITLE_CACHE,
    FILE_SEEN_CLEAN_URLS,
    DUPLICATE_THRESHOLD, DUPLICATE_WINDOW_DAYS,
    TITLE_DEDUP_THRESHOLD, TITLE_DEDUP_WINDOW_DAYS,
    SENTENCE_TRANSFORMER_MODEL,
)

# ── Logging ───────────────────────────────────────────────────────────────────
log = logging.getLogger(__name__)

# ── Lazy model loader ─────────────────────────────────────────────────────────
_model = None

def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        log.info(f"  Loading sentence-transformer model: {SENTENCE_TRANSFORMER_MODEL}")
        try:
            _model = SentenceTransformer(SENTENCE_TRANSFORMER_MODEL, local_files_only=True)
        except Exception:
            _model = SentenceTransformer(SENTENCE_TRANSFORMER_MODEL)
        log.info("  Model loaded.")
    return _model


# ── Title normalization ───────────────────────────────────────────────────────

def _normalize_title(title: str) -> str:
    """
    Strip trailing outlet suffix, lowercase, remove punctuation.
    Google Alerts RSS titles typically end with ' - Outlet' or ' | Outlet'.
    Only strips the suffix if it is <= 60 chars (outlet names are short;
    legitimate mid-title dashes like 'X - after delays - breaks ground' are long).

    Unicode normalization runs first so that en-dash (–), em-dash (—), and
    non-breaking spaces produce the same normalized key as their ASCII equivalents.
    """
    # Map all Unicode dash variants → ASCII hyphen; space separators → regular space
    normalized = []
    for ch in title:
        cat = unicodedata.category(ch)
        if cat == "Pd":    # Dash punctuation: en-dash, em-dash, figure dash, etc.
            normalized.append("-")
        elif cat == "Zs":  # Space separator: non-breaking space, thin space, etc.
            normalized.append(" ")
        else:
            normalized.append(ch)
    title = "".join(normalized)

    for sep in (" | ", " - "):
        if sep in title:
            parts = title.rsplit(sep, 1)
            if len(parts[1].strip()) <= 60:
                title = parts[0].strip()
            break
    title = title.lower()
    title = re.sub(r"[^\w\s]", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title


# ── Cosine similarity ─────────────────────────────────────────────────────────

def cosine_similarity(a, b) -> float:
    va, vb = np.array(a), np.array(b)
    norm = np.linalg.norm(va) * np.linalg.norm(vb)
    return float(np.dot(va, vb) / norm) if norm > 0 else 0.0


# ── Title cache ───────────────────────────────────────────────────────────────

def _load_title_cache(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            cache = json.load(f)
    except Exception:
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=TITLE_DEDUP_WINDOW_DAYS)).isoformat()
    return {k: v for k, v in cache.items() if v.get("date", "") >= cutoff}


def _save_title_cache(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=TITLE_DEDUP_WINDOW_DAYS)).isoformat()
    pruned = {k: v for k, v in cache.items() if v.get("date", "") >= cutoff}
    with open(path, "w") as f:
        json.dump(pruned, f, indent=2)


# ── Embedding cache ───────────────────────────────────────────────────────────

def _load_embedding_cache(path: Path) -> dict:
    """
    Load duplicate_cache.json. Clears in-memory cache if embedding dimension
    != 384, handling the one-time migration from Voyage AI (512-dim) to
    all-MiniLM-L6-v2 (384-dim). The file itself is overwritten on save.
    """
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            cache = json.load(f)
    except Exception:
        return {}
    if not cache:
        return {}
    first = next(iter(cache.values()))
    emb = first.get("embedding", [])
    if len(emb) not in (0, 384):
        log.warning(
            f"  duplicate_cache.json has {len(emb)}-dim embeddings "
            f"(expected 384 for {SENTENCE_TRANSFORMER_MODEL}). "
            f"Cache cleared — 14-day window restarting from today."
        )
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=DUPLICATE_WINDOW_DAYS)).isoformat()
    return {k: v for k, v in cache.items() if v.get("date", "") >= cutoff}


def _save_embedding_cache(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=DUPLICATE_WINDOW_DAYS)).isoformat()
    pruned = {k: v for k, v in cache.items() if v.get("date", "") >= cutoff}
    with open(path, "w") as f:
        json.dump(pruned, f, indent=2)


# ── Staging / news feed helpers (duplicated per project convention) ───────────

NEWS_FEED_COLUMNS = [
    "ID", "Title", "CleanURL", "Source", "PublishedDate",
    "Summary_AI", "Primary_Category", "Secondary_Categories",
    "States", "DC_ID", "Is_Duplicate", "Duplicate_Of",
    "Strategy_Alignment_Score", "Relevance_Score",
    "Mentions_Specific_DC", "Article_Text",
]


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


def _load_existing_news_feed_ids(path: Path) -> set:
    if not path.exists():
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        return {row.get("ID", "") for row in csv.DictReader(f)}


def _append_to_news_feed(path: Path, rows: list) -> None:
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

def dedup_articles() -> tuple:
    """
    Stage 1.5: Stacked dedup on all extraction_status = 'pending' articles.
    Returns (fuzzy_dupe_count, semantic_dupe_count, non_dupe_count).
    """
    log.info("=" * 60)
    log.info("  Stage 1.5: Stacked Dedup")
    log.info("=" * 60)

    rows    = load_staging(FILE_STAGED)
    pending = [r for r in rows if r.get("extraction_status") == "pending"]

    log.info(f"  Pending articles to check: {len(pending)}")

    if not pending:
        log.info("  Nothing to dedup.")
        return 0, 0, 0

    title_cache    = _load_title_cache(FILE_TITLE_CACHE)
    embed_cache    = _load_embedding_cache(FILE_DUPLICATE_CACHE)
    existing_ids   = _load_existing_news_feed_ids(FILE_NEWS_FEED)
    row_by_id      = {r["article_id"]: r for r in rows}

    log.info(f"  Title cache entries:     {len(title_cache)}")
    log.info(f"  Embedding cache entries: {len(embed_cache)}")

    # Batch-encode all pending articles (falls back to title-only if model unavailable)
    embeddings = None
    try:
        model = _get_model()
        texts = [
            f"{r.get('title', '')} {r.get('rss_description', '')}".strip()
            for r in pending
        ]
        log.info(f"  Encoding {len(texts)} articles...")
        embeddings = model.encode(texts, batch_size=32, show_progress_bar=False)
        log.info("  Encoding complete.")
    except Exception as e:
        log.warning(f"  Semantic embedding unavailable ({type(e).__name__}: {e}); running title-only dedup")

    fuzzy_count    = 0
    semantic_count = 0
    non_dupe_count = 0
    news_feed_rows = []

    for i, article in enumerate(pending):
        article_id = article["article_id"]
        title      = article.get("title", "")
        normalized = _normalize_title(title)
        embedding  = embeddings[i] if embeddings is not None else None
        is_dup    = False
        dup_of_id = ""
        dup_layer = ""

        # Layer 1a: exact title match (fastest — catches same-article-different-feed)
        if normalized in title_cache:
            is_dup    = True
            dup_of_id = title_cache[normalized].get("id", "")
            dup_layer = "title_exact"
            fuzzy_count += 1

        # Layer 1b: fuzzy title match (catches minor wording variants)
        if not is_dup:
            for cached_norm, cached in title_cache.items():
                score = fuzz.token_sort_ratio(normalized, cached_norm)
                if score >= TITLE_DEDUP_THRESHOLD:
                    is_dup    = True
                    dup_of_id = cached.get("id", "")
                    dup_layer = f"title_fuzzy (score={score})"
                    fuzzy_count += 1
                    break

        # Layer 2: semantic embedding (only if title check didn't flag it and model loaded)
        if not is_dup and embedding is not None:
            for cached_id, cached in embed_cache.items():
                if cached_id == article_id:
                    continue  # never flag an article as a duplicate of itself
                sim = cosine_similarity(embedding, cached["embedding"])
                if sim >= DUPLICATE_THRESHOLD:
                    is_dup    = True
                    dup_of_id = cached_id
                    dup_layer = f"semantic (sim={sim:.3f})"
                    semantic_count += 1
                    break

        if is_dup:
            log.info(f"  ⚠  DUP [{dup_layer}]  {title[:65]}")
            log.info(f"         of: {dup_of_id}")
            row_by_id[article_id]["extraction_status"] = "duplicate"
            row_by_id[article_id]["summarize_status"]  = "skipped"
            row_by_id[article_id]["classify_status"]   = "success"

            if article_id not in existing_ids:
                news_feed_rows.append({
                    "ID"                      : article_id,
                    "Title"                   : title,
                    "CleanURL"                : "",
                    "Source"                  : article.get("source", ""),
                    "PublishedDate"           : article.get("published_date", ""),
                    "Summary_AI"              : "",
                    "Primary_Category"        : "",
                    "Secondary_Categories"    : "",
                    "States"                  : "[]",
                    "DC_ID"                   : "",
                    "Is_Duplicate"            : "True",
                    "Duplicate_Of"            : dup_of_id,
                    "Strategy_Alignment_Score": "",
                    "Relevance_Score"         : "",
                    "Mentions_Specific_DC"    : "",
                    "Article_Text"            : article.get("rss_description", "")[:2000],
                })
        else:
            non_dupe_count += 1
            # Add to caches for future comparisons
            title_cache[normalized] = {
                "id"            : article_id,
                "date"          : datetime.now(timezone.utc).isoformat(),
                "source"        : article.get("source", ""),
                "original_title": title,
            }
            if embedding is not None:
                embed_cache[article_id] = {
                    "date"     : datetime.now(timezone.utc).isoformat(),
                    "title"    : title,
                    "embedding": embedding.tolist(),
                }

    save_staging(FILE_STAGED, list(row_by_id.values()))
    _save_title_cache(FILE_TITLE_CACHE, title_cache)
    _save_embedding_cache(FILE_DUPLICATE_CACHE, embed_cache)
    _append_to_news_feed(FILE_NEWS_FEED, news_feed_rows)

    log.info(f"  Fuzzy dupes:    {fuzzy_count}")
    log.info(f"  Semantic dupes: {semantic_count}")
    log.info(f"  Non-dupes:      {non_dupe_count}")

    return fuzzy_count, semantic_count, non_dupe_count


# ── Clean URL cache (Stage 2.5) ───────────────────────────────────────────────

CLEAN_URL_RETENTION_DAYS = 30


def _normalize_clean_url(url: str) -> str:
    """
    Canonical form for clean URL dedup: lowercase, strip fragment and trailing slash.
    Keeps query params (some sites use them to distinguish articles).
    """
    try:
        p = urlparse(url.strip())
        canonical = urlunparse((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), p.params, p.query, ""))
        return canonical
    except Exception:
        return url.strip().lower().rstrip("/")


def _load_seen_clean_urls(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            cache = json.load(f)
    except Exception:
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=CLEAN_URL_RETENTION_DAYS)).isoformat()
    return {k: v for k, v in cache.items() if v.get("date", "") >= cutoff}


def _save_seen_clean_urls(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=CLEAN_URL_RETENTION_DAYS)).isoformat()
    pruned = {k: v for k, v in cache.items() if v.get("date", "") >= cutoff}
    with open(path, "w") as f:
        json.dump(pruned, f, indent=2)


# ── Stage 2.5 ─────────────────────────────────────────────────────────────────

def dedup_clean_urls() -> int:
    """
    Stage 2.5: Dedup on resolved clean URLs after extraction.

    Catches the class of duplicates that slip past Stage 1 because they arrive
    via different Google Alerts redirect URLs (different feeds, same underlying
    article). By the time this stage runs, extract.py has resolved each Google
    redirect to the actual article URL, making same-article detection trivial.

    Articles marked as clean-URL duplicates:
      - summarize_status = "skipped"   → not summarized
      - classify_status  = "success"   → counted as processed
      - Written to news_feed.csv with Is_Duplicate = True

    Returns count of clean-URL duplicates caught.
    """
    log.info("=" * 60)
    log.info("  Stage 2.5: Clean URL Dedup")
    log.info("=" * 60)

    rows = load_staging(FILE_STAGED)
    # Only examine articles that were just extracted and are pending summarization
    candidates = [
        r for r in rows
        if r.get("extraction_status") in ("success", "fallback")
        and r.get("summarize_status") == "pending"
        and r.get("clean_url", "").strip()
    ]

    log.info(f"  Newly extracted articles to check: {len(candidates)}")

    if not candidates:
        log.info("  Nothing to check.")
        return 0

    seen_clean    = _load_seen_clean_urls(FILE_SEEN_CLEAN_URLS)
    existing_ids  = _load_existing_news_feed_ids(FILE_NEWS_FEED)
    row_by_id     = {r["article_id"]: r for r in rows}

    log.info(f"  Clean URL cache entries: {len(seen_clean)}")

    dup_count      = 0
    news_feed_rows = []

    for article in candidates:
        article_id    = article["article_id"]
        clean_url     = article.get("clean_url", "").strip()
        title         = article.get("title", "")
        canonical_url = _normalize_clean_url(clean_url)

        if canonical_url and canonical_url in seen_clean:
            dup_of_id = seen_clean[canonical_url].get("id", "")
            log.info(f"  ⚠  CLEAN URL DUP  {title[:65]}")
            log.info(f"         of: {dup_of_id}")
            row_by_id[article_id]["summarize_status"] = "skipped"
            row_by_id[article_id]["classify_status"]  = "success"
            dup_count += 1

            if article_id not in existing_ids:
                news_feed_rows.append({
                    "ID"                      : article_id,
                    "Title"                   : title,
                    "CleanURL"                : clean_url,
                    "Source"                  : article.get("source", ""),
                    "PublishedDate"           : article.get("published_date", ""),
                    "Summary_AI"              : "",
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
        else:
            if canonical_url:
                seen_clean[canonical_url] = {
                    "id"   : article_id,
                    "date" : datetime.now(timezone.utc).isoformat(),
                    "title": title,
                }

    save_staging(FILE_STAGED, list(row_by_id.values()))
    _save_seen_clean_urls(FILE_SEEN_CLEAN_URLS, seen_clean)
    _append_to_news_feed(FILE_NEWS_FEED, news_feed_rows)

    log.info(f"  Clean URL dupes caught: {dup_count}")
    return dup_count


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  [%(levelname)s]  %(message)s",
        datefmt="%H:%M:%S",
    )
    dedup_articles()
