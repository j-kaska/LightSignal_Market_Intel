"""
LightSignal — migrate_cache.py
================================
One-time migration utility. Run once when switching from the old
Voyage AI dedup cache to the new local dedup cache.

Actions:
  1. Renames duplicate_cache.json  → duplicate_cache.json.bak
     (old Voyage embeddings; format is incompatible and cannot be reused)
  2. Leaves seen_urls.json         unchanged
     (URL dedup schema is identical; no migration needed)
  3. Initialises local_dedup_cache.json as empty {}
     (the new LocalDeduplicator will build it fresh on first run)

Optionally back-fills the local cache from recent staged_articles.csv
entries (--backfill flag). This is useful to pre-populate fingerprints and
fuzzy keys from the last N days so that the first pipeline run after
migration can still detect same-day duplicates.

Usage:
  python scripts/articles/migrate_cache.py               # dry run (report only)
  python scripts/articles/migrate_cache.py --apply       # execute migration
  python scripts/articles/migrate_cache.py --apply --backfill  # + seed from staging
  python scripts/articles/migrate_cache.py --apply --backfill --days 60
"""

import argparse
import csv
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from utils.config import (
    FILE_LOCAL_DEDUP_CACHE,
    FILE_STAGED,
    ARTICLES_STAGING_DIR,
)
from utils.text_utils import normalize_title, title_fingerprint

log = logging.getLogger(__name__)

OLD_VOYAGE_CACHE = ARTICLES_STAGING_DIR / "duplicate_cache.json"
SEEN_URLS_CACHE  = ARTICLES_STAGING_DIR / "seen_urls.json"


def report(dry_run: bool):
    """Print current cache state without making any changes."""
    print("\n== LightSignal Cache Migration Report ==\n")

    if OLD_VOYAGE_CACHE.exists():
        try:
            with open(OLD_VOYAGE_CACHE) as f:
                old = json.load(f)
            print(f"  [OLD]  duplicate_cache.json     : {len(old)} entries  (will be backed up)")
        except Exception:
            print(f"  [OLD]  duplicate_cache.json     : exists but unreadable  (will be backed up)")
    else:
        print(f"  [OLD]  duplicate_cache.json     : not found  (already migrated or never existed)")

    if SEEN_URLS_CACHE.exists():
        try:
            with open(SEEN_URLS_CACHE) as f:
                urls = json.load(f)
            print(f"  [KEEP] seen_urls.json           : {len(urls)} entries  (unchanged)")
        except Exception:
            print(f"  [KEEP] seen_urls.json           : exists but unreadable")
    else:
        print(f"  [KEEP] seen_urls.json           : not found")

    if FILE_LOCAL_DEDUP_CACHE.exists():
        try:
            with open(FILE_LOCAL_DEDUP_CACHE) as f:
                local = json.load(f)
            print(f"  [NEW]  local_dedup_cache.json   : {len(local)} entries  (already initialised)")
        except Exception:
            print(f"  [NEW]  local_dedup_cache.json   : exists but unreadable")
    else:
        print(f"  [NEW]  local_dedup_cache.json   : not found  (will be created)")

    if FILE_STAGED.exists():
        with open(FILE_STAGED, newline="", encoding="utf-8") as f:
            staged_count = sum(1 for _ in csv.DictReader(f))
        print(f"\n  staged_articles.csv             : {staged_count} total rows")
    else:
        print(f"\n  staged_articles.csv             : not found")

    if dry_run:
        print("\n  Run with --apply to execute migration.")
    print()


def migrate(backfill: bool, backfill_days: int):
    """Execute the migration."""
    ARTICLES_STAGING_DIR.mkdir(parents=True, exist_ok=True)

    # ── Step 1: Back up old Voyage cache ──────────────────────────────────────
    if OLD_VOYAGE_CACHE.exists():
        backup = OLD_VOYAGE_CACHE.with_suffix(".json.bak")
        OLD_VOYAGE_CACHE.rename(backup)
        log.info(f"  Backed up old Voyage cache → {backup}")
    else:
        log.info(f"  No old Voyage cache found at {OLD_VOYAGE_CACHE} — skipping backup step")

    # ── Step 2: Initialise new local cache ────────────────────────────────────
    if FILE_LOCAL_DEDUP_CACHE.exists():
        try:
            with open(FILE_LOCAL_DEDUP_CACHE) as f:
                existing = json.load(f)
            log.info(
                f"  local_dedup_cache.json already exists ({len(existing)} entries) — leaving intact"
            )
            seeded_cache = existing
        except Exception:
            log.warning("  local_dedup_cache.json unreadable — reinitialising as empty")
            seeded_cache = {}
            with open(FILE_LOCAL_DEDUP_CACHE, "w") as f:
                json.dump(seeded_cache, f)
    else:
        seeded_cache = {}
        with open(FILE_LOCAL_DEDUP_CACHE, "w") as f:
            json.dump(seeded_cache, f)
        log.info(f"  Initialised empty local_dedup_cache.json at {FILE_LOCAL_DEDUP_CACHE}")

    # ── Step 3: Optional backfill from staged_articles.csv ───────────────────
    if not backfill:
        log.info("  Backfill skipped (use --backfill to seed from staging)")
        return

    if not FILE_STAGED.exists():
        log.warning(f"  --backfill requested but staged_articles.csv not found: {FILE_STAGED}")
        return

    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=backfill_days)
    ).isoformat()[:10]  # YYYY-MM-DD prefix for date comparison

    seeded = 0
    skipped = 0

    with open(FILE_STAGED, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            article_id = row.get("article_id", "").strip()
            title      = row.get("title", "").strip()
            published  = row.get("published_date", "").strip()

            if not article_id or not title:
                continue

            # Only backfill recent articles
            if published < cutoff:
                skipped += 1
                continue

            # Skip if already in the local cache
            if article_id in seeded_cache:
                continue

            norm = normalize_title(title)
            fp   = title_fingerprint(title)
            seeded_cache[article_id] = {
                "date"      : published + "T00:00:00+00:00" if "T" not in published else published,
                "title"     : norm,
                "title_fp"  : fp,
                "embedding" : None,  # embeddings will be built on next classify run
                "cluster_id": article_id,
            }
            seeded += 1

    # Write updated cache
    with open(FILE_LOCAL_DEDUP_CACHE, "w") as f:
        json.dump(seeded_cache, f)

    log.info(
        f"  Backfill complete: seeded {seeded} articles "
        f"(skipped {skipped} older than {backfill_days} days)"
    )
    log.info(
        "  Note: embeddings are null — semantic dedup will be fully active "
        "only for articles classified after this migration."
    )


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  [%(levelname)s]  %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="One-time migration from Voyage AI dedup cache to local cache"
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Execute migration (default is dry-run / report only)"
    )
    parser.add_argument(
        "--backfill", action="store_true",
        help="Seed local cache with fingerprints from recent staged_articles.csv"
    )
    parser.add_argument(
        "--days", type=int, default=60,
        help="How many days back to backfill (default: 60)"
    )
    args = parser.parse_args()

    if not args.apply:
        report(dry_run=True)
        return

    log.info("=" * 60)
    log.info("  LightSignal — Cache Migration")
    log.info("=" * 60)
    report(dry_run=False)
    migrate(backfill=args.backfill, backfill_days=args.days)
    log.info("  Migration complete.")


if __name__ == "__main__":
    main()
