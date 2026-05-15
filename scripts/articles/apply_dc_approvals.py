"""
LightSignal — apply_dc_approvals.py
=====================================
Applies human-reviewed DC linkages from dc_review_queue_approved.csv
back into news_feed.csv.

WORKFLOW:
  1. After running run_articles.py (which calls dc_candidates.py),
     review dc_review_queue.csv in Excel or any CSV editor.
  2. Copy approved rows to dc_review_queue_approved.csv and set:
       review_status = "approved"
     (Rejected rows: set review_status = "rejected" or simply omit them.)
  3. Run:
       python scripts/articles/apply_dc_approvals.py
  4. This script patches DC_ID into news_feed.csv for approved rows.

SAFETY:
  - Only rows with review_status == "approved" are applied.
  - A row is skipped if news_feed DC_ID is already non-empty.
  - A backup of news_feed.csv is written before any edits.
  - All changes are logged at INFO level.
  - This script is idempotent — safe to run multiple times.

Run directly:
  python scripts/articles/apply_dc_approvals.py
"""

import csv
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from utils.config import (
    FILE_NEWS_FEED,
    FILE_DC_APPROVALS,
)

log = logging.getLogger(__name__)


def apply_approvals() -> tuple:
    """
    Patch DC_ID into news_feed.csv for approved rows.
    Returns (applied_count, skipped_count).
    """
    log.info("=" * 60)
    log.info("  Apply DC Approvals")
    log.info("=" * 60)

    if not FILE_DC_APPROVALS.exists():
        log.info(f"  Approvals file not found: {FILE_DC_APPROVALS}")
        log.info("  Nothing to apply.")
        return 0, 0

    if not FILE_NEWS_FEED.exists():
        log.info(f"  news_feed.csv not found: {FILE_NEWS_FEED}")
        return 0, 0

    # Load approved rows
    approved: dict = {}  # article_id -> dc_id
    with open(FILE_DC_APPROVALS, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("review_status", "").strip().lower() == "approved":
                article_id = row.get("article_id", "").strip()
                dc_id      = row.get("dc_id", "").strip()
                if article_id and dc_id:
                    approved[article_id] = dc_id

    log.info(f"  Approved rows loaded: {len(approved)}")
    if not approved:
        log.info("  No approved rows — nothing to apply.")
        return 0, 0

    # Load news_feed.csv
    with open(FILE_NEWS_FEED, newline="", encoding="utf-8") as f:
        reader   = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        news_rows  = list(reader)

    # Backup before modifying
    ts      = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup  = FILE_NEWS_FEED.parent / f"news_feed_backup_{ts}.csv"
    shutil.copy2(FILE_NEWS_FEED, backup)
    log.info(f"  Backup written: {backup}")

    # Apply patches
    applied_count  = 0
    skipped_count  = 0

    for row in news_rows:
        article_id = row.get("ID", "").strip()
        if article_id not in approved:
            continue
        existing_dc_id = row.get("DC_ID", "").strip()
        if existing_dc_id:
            log.info(
                f"  SKIP {article_id}: DC_ID already set to '{existing_dc_id}'"
            )
            skipped_count += 1
            continue

        new_dc_id     = approved[article_id]
        row["DC_ID"]  = new_dc_id
        applied_count += 1
        log.info(f"  SET  {article_id}: DC_ID = {new_dc_id}")

    # Write updated news_feed.csv
    with open(FILE_NEWS_FEED, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(news_rows)

    log.info(f"  Applied: {applied_count}")
    log.info(f"  Skipped: {skipped_count}")
    log.info(f"  Updated: {FILE_NEWS_FEED}")

    return applied_count, skipped_count


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  [%(levelname)s]  %(message)s",
        datefmt="%H:%M:%S",
    )
    apply_approvals()
