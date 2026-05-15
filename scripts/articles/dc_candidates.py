"""
LightSignal — dc_candidates.py
================================
Stage 5 of the article pipeline (suggest-only, never auto-writes DC_ID).

For each article in news_feed.csv where DC_ID is empty and
Mentions_Specific_DC is True, attempt to find candidate DC matches using
RapidFuzz against the dc_consolidated master list.

Writes results to dc_review_queue.csv (capped at MAX_REVIEW_QUEUE_SIZE).
Overflow goes to dc_review_queue_overflow.csv.

HUMAN REVIEW WORKFLOW:
  1. Review dc_review_queue.csv in Excel / any CSV editor.
  2. Copy rows you approve to dc_review_queue_approved.csv and set
     review_status = "approved".
  3. Run:  python scripts/articles/apply_dc_approvals.py
     This patches DC_ID into news_feed.csv for approved rows.

Queue CSV columns:
  article_id, article_title, published_date, source,
  dc_id, canonical_name, operator, dc_state,
  confidence, match_method, matched_text, review_status

Run directly:
  python scripts/articles/dc_candidates.py

Or called automatically by:
  python scripts/articles/run_articles.py
"""

import csv
import logging
import sys
from pathlib import Path

from rapidfuzz import fuzz, process as rfuzz_process

# ── Path setup ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from utils.config import (
    FILE_DC, FILE_NEWS_FEED,
    FILE_DC_REVIEW_QUEUE, FILE_DC_REVIEW_OVERFLOW,
    DC_CANDIDATE_THRESHOLD, MAX_REVIEW_QUEUE_SIZE,
)

log = logging.getLogger(__name__)

QUEUE_COLUMNS = [
    "article_id", "article_title", "published_date", "source",
    "dc_id", "canonical_name", "operator", "dc_state",
    "confidence", "match_method", "matched_text", "review_status",
]

# Top-N candidates per article (before queue cap)
MAX_CANDIDATES_PER_ARTICLE = 3


# ── DC master helpers ─────────────────────────────────────────────────────────

def load_dc_master(dc_csv: Path) -> list:
    """
    Load dc_consolidated.csv and return list of dicts with prebuilt match key.
    Each dict has: dc_id, canonical_name, operator, state, match_names
    where match_names is a dict {name_string: field_source}.
    """
    if not dc_csv.exists():
        log.warning(f"  DC master not found: {dc_csv}")
        return []

    records = []
    with open(dc_csv, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            canonical  = (row.get("Canonical_Name") or "").strip()
            alias      = (row.get("Alias") or "").strip()
            operator   = (row.get("Operator") or "").strip()
            asset_id   = (row.get("Asset_ID") or "").strip()
            state      = (row.get("state") or "").strip()

            if not canonical:
                continue

            match_names: dict = {}
            match_names[canonical] = "canonical"
            if alias and alias != canonical:
                match_names[alias] = "alias"
            if operator and len(operator) > 3:
                match_names[operator] = "operator"

            records.append({
                "dc_id"         : asset_id,
                "canonical_name": canonical,
                "operator"      : operator,
                "state"         : state,
                "match_names"   : match_names,
            })

    log.info(f"  DC master: {len(records)} records loaded")
    return records


def build_flat_corpus(dc_records: list) -> tuple:
    """
    Returns (flat_names: list[str], name_to_record: dict[str, dict]).
    Used for rfuzz_process.extractOne across all names at once.
    """
    flat_names    = []
    name_to_rec   = {}
    for rec in dc_records:
        for name in rec["match_names"]:
            flat_names.append(name)
            name_to_rec[name] = rec
    return flat_names, name_to_rec


# ── Candidate matching ────────────────────────────────────────────────────────

def find_candidates(
    article_text: str,
    flat_names: list,
    name_to_rec: dict,
    threshold: int = DC_CANDIDATE_THRESHOLD,
    top_n: int = MAX_CANDIDATES_PER_ARTICLE,
) -> list:
    """
    Find top-N DC matches for article_text using RapidFuzz partial_ratio.
    Returns list of (record, score, matched_name, field_source).
    """
    # Use extract (not extractOne) to get multiple candidates
    results = rfuzz_process.extract(
        article_text[:1500],
        flat_names,
        scorer=fuzz.partial_ratio,
        score_cutoff=threshold,
        limit=top_n * 5,  # over-fetch, deduplicate by dc_id below
    )

    seen_dc_ids: set = set()
    candidates = []
    for matched_name, score, _ in results:
        rec = name_to_rec[matched_name]
        dc_id = rec["dc_id"]
        if dc_id in seen_dc_ids:
            continue
        seen_dc_ids.add(dc_id)
        field_src = rec["match_names"].get(matched_name, "unknown")
        candidates.append((rec, score, matched_name, field_src))
        if len(candidates) >= top_n:
            break

    return sorted(candidates, key=lambda x: x[1], reverse=True)


# ── Queue helpers ─────────────────────────────────────────────────────────────

def load_existing_queue_ids(queue_path: Path) -> set:
    """Load article_ids already in the review queue to avoid duplicates."""
    if not queue_path.exists():
        return set()
    with open(queue_path, newline="", encoding="utf-8") as f:
        return {row.get("article_id", "") for row in csv.DictReader(f)}


def append_queue_rows(path: Path, rows: list) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=QUEUE_COLUMNS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


# ── Main ──────────────────────────────────────────────────────────────────────

def link_dc_candidates() -> tuple:
    """
    Suggest DC candidates for articles with no DC_ID in news_feed.csv.
    Returns (candidates_found, written_to_queue, written_to_overflow).
    """
    log.info("=" * 60)
    log.info("  Stage 5: DC Candidate Linking (suggest-only)")
    log.info("=" * 60)

    if not FILE_NEWS_FEED.exists():
        log.info("  news_feed.csv not found — skipping.")
        return 0, 0, 0

    dc_records  = load_dc_master(FILE_DC)
    if not dc_records:
        log.info("  No DC records — skipping.")
        return 0, 0, 0

    flat_names, name_to_rec = build_flat_corpus(dc_records)

    # Load articles: only those with no DC_ID and Mentions_Specific_DC = True
    eligible = []
    with open(FILE_NEWS_FEED, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (
                not row.get("DC_ID", "").strip()
                and row.get("Mentions_Specific_DC", "").strip().lower() == "true"
                and row.get("Is_Duplicate", "").strip().lower() != "true"
            ):
                eligible.append(row)

    log.info(f"  Eligible articles (no DC_ID + mentions DC): {len(eligible)}")
    if not eligible:
        log.info("  Nothing to process.")
        return 0, 0, 0

    existing_ids = load_existing_queue_ids(FILE_DC_REVIEW_QUEUE)
    existing_ids |= load_existing_queue_ids(FILE_DC_REVIEW_OVERFLOW)

    all_candidates  = []
    articles_matched = 0

    for article in eligible:
        article_id   = article.get("ID", "")
        title        = article.get("Title", "")
        published    = article.get("PublishedDate", "")
        source       = article.get("Source", "")
        article_text = f"{title} {article.get('Summary_AI', '')} {article.get('Article_Text', '')}"

        # Skip if already in queue
        if article_id in existing_ids:
            continue

        candidates = find_candidates(
            article_text, flat_names, name_to_rec
        )

        if not candidates:
            continue

        articles_matched += 1
        for rec, score, matched_name, field_src in candidates:
            all_candidates.append({
                "article_id"   : article_id,
                "article_title": title,
                "published_date": published,
                "source"       : source,
                "dc_id"        : rec["dc_id"],
                "canonical_name": rec["canonical_name"],
                "operator"     : rec["operator"],
                "dc_state"     : rec["state"],
                "confidence"   : round(score / 100.0, 3),
                "match_method" : f"fuzzy_{field_src}",
                "matched_text" : matched_name,
                "review_status": "pending",
            })

    log.info(f"  Total candidate rows: {len(all_candidates)} from {articles_matched} articles")

    if not all_candidates:
        log.info("  No new candidates to write.")
        return 0, 0, 0

    # Sort by confidence desc, cap at MAX_REVIEW_QUEUE_SIZE
    all_candidates.sort(key=lambda x: x["confidence"], reverse=True)
    to_queue    = all_candidates[:MAX_REVIEW_QUEUE_SIZE]
    to_overflow = all_candidates[MAX_REVIEW_QUEUE_SIZE:]

    append_queue_rows(FILE_DC_REVIEW_QUEUE, to_queue)
    append_queue_rows(FILE_DC_REVIEW_OVERFLOW, to_overflow)

    log.info(f"  Written to queue:    {len(to_queue)}  ({FILE_DC_REVIEW_QUEUE})")
    if to_overflow:
        log.info(
            f"  Written to overflow: {len(to_overflow)}  "
            f"({FILE_DC_REVIEW_OVERFLOW})"
        )

    return len(all_candidates), len(to_queue), len(to_overflow)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  [%(levelname)s]  %(message)s",
        datefmt="%H:%M:%S",
    )
    link_dc_candidates()
