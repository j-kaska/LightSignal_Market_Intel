"""
LightSignal — test_pipeline.py
=================================
Standalone test harness: runs the full article pipeline on Article_Past.csv
using isolated paths under data/articles/test/ so production files are untouched.

Pipeline stages:
  1.5  dedup      — title fuzzy + semantic embedding duplicate detection
  2    extract    — Selenium text extraction  (skipped with --skip-extract)
  3    summarize  — Gemini 2.0 Flash summary
  4    classify   — Gemini 2.0 Flash classification + scoring

Usage:
  python scripts/articles/test_pipeline.py                          # full run (Selenium + Gemini)
  python scripts/articles/test_pipeline.py --skip-extract           # fast: use title as text proxy
  python scripts/articles/test_pipeline.py --reset --sample 150     # fresh 150-article sample
  python scripts/articles/test_pipeline.py --resume                 # skip dedup+extract, run summarize+classify only
  python scripts/articles/test_pipeline.py --classify-only          # re-run stage 4 only
  python scripts/articles/test_pipeline.py --provider anthropic     # use Claude instead of Gemini

Output: data/articles/test/news_feed_test.csv
"""

import argparse
import csv
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# ── Detect --provider early so config.py picks it up via os.environ ───────────
# Must happen before `import utils.config` since API_PROVIDER is set at load time.
for _i, _a in enumerate(sys.argv):
    if _a == "--provider" and _i + 1 < len(sys.argv):
        os.environ["LIGHTSIGNAL_PROVIDER"] = sys.argv[_i + 1]
        break

# ── Patch config paths BEFORE importing any pipeline modules ──────────────────
# Pipeline modules do `from utils.config import FILE_STAGED` at module load time.
# Patching the module object here means those imports pick up the test paths.
import utils.config as cfg

TEST_DIR                 = ROOT / "data" / "articles" / "test"
cfg.FILE_STAGED          = TEST_DIR / "staged_test.csv"
cfg.FILE_NEWS_FEED       = TEST_DIR / "news_feed_test.csv"
cfg.FILE_DUPLICATE_CACHE = TEST_DIR / "duplicate_cache_test.json"
cfg.FILE_TITLE_CACHE     = TEST_DIR / "title_cache_test.json"

# ── Now import pipeline modules (they bind the patched paths at import time) ──
from articles.dedup     import dedup_articles
from articles.extract   import extract_articles
from articles.summarize import summarize_articles
from articles.classify  import classify_articles

# ── Source data ───────────────────────────────────────────────────────────────
SOURCE_CSV = TEST_DIR / "Article_Past.csv"

STAGING_COLUMNS = [
    "article_id", "title", "source", "raw_url", "clean_url",
    "published_date", "rss_description", "article_text",
    "summary_ai", "staged_at", "extraction_status",
    "summarize_status", "classify_status", "Processed",
]


def build_staging_rows(source_csv: Path, skip_extract: bool) -> list:
    rows = []
    now = datetime.now(timezone.utc).isoformat()
    with open(source_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            title     = row.get("Title", "")
            clean_url = row.get("CleanURL", "")
            if skip_extract:
                article_text      = title
                extraction_status = "success"
            else:
                article_text      = ""
                extraction_status = "pending"
            rows.append({
                "article_id"        : row["ID"],
                "title"             : title,
                "source"            : row.get("Source", ""),
                "raw_url"           : clean_url,
                "clean_url"         : clean_url,
                "published_date"    : row.get("PublishedDate", ""),
                "rss_description"   : title,
                "article_text"      : article_text,
                "summary_ai"        : "",
                "staged_at"         : now,
                "extraction_status" : extraction_status,
                "summarize_status"  : "pending",
                "classify_status"   : "pending",
                "Processed"         : "No",
            })
    return rows


def write_staging(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=STAGING_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def promote_pending_to_success(path: Path) -> None:
    """Flip extraction_status pending → success (simulates extract stage)."""
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or STAGING_COLUMNS
        for row in reader:
            if row.get("extraction_status") == "pending":
                row["extraction_status"] = "success"
            rows.append(row)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def reset_test_outputs() -> None:
    for f in [
        cfg.FILE_STAGED, cfg.FILE_NEWS_FEED,
        cfg.FILE_DUPLICATE_CACHE, cfg.FILE_TITLE_CACHE,
    ]:
        if f.exists():
            f.unlink()
    for log_file in TEST_DIR.glob("*.log"):
        log_file.unlink()


def main():
    parser = argparse.ArgumentParser(description="LightSignal Test Pipeline")
    parser.add_argument("--skip-extract",  action="store_true",
                        help="Skip Selenium; use title as article text proxy (~5 min vs ~60 min)")
    parser.add_argument("--reset",         action="store_true",
                        help="Delete previous test outputs before running")
    parser.add_argument("--resume",        action="store_true",
                        help="Skip dedup+extract; run summarize+classify on existing staged_test.csv")
    parser.add_argument("--classify-only", action="store_true",
                        help="Skip dedup+extract+summarize; re-run stage 4 only")
    parser.add_argument("--provider",      default="gemini", choices=["gemini", "anthropic"],
                        help="LLM provider for summarize+classify (default: gemini)")
    parser.add_argument("--sample",        type=int, default=0,
                        help="Limit input to first N articles (0 = all)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  [%(levelname)s]  %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger(__name__)

    mode = ("resume"       if args.resume        else
            "classify-only" if args.classify_only  else
            "skip-extract"  if args.skip_extract   else
            "full")

    log.info("=" * 60)
    log.info("  LightSignal — Test Pipeline Run")
    log.info(f"  Source:   {SOURCE_CSV.name}")
    log.info(f"  Output:   {TEST_DIR}")
    log.info(f"  Mode:     {mode}")
    log.info("=" * 60)

    if args.reset:
        reset_test_outputs()
        log.info("  Previous test outputs cleared.")

    start = datetime.now()

    if not args.classify_only and not args.resume:
        rows = build_staging_rows(SOURCE_CSV, args.skip_extract)
        if args.sample and args.sample < len(rows):
            rows = rows[:args.sample]
            log.info(f"  Loaded {args.sample} articles (sampled from {SOURCE_CSV.name})")
        else:
            log.info(f"  Loaded {len(rows)} articles from {SOURCE_CSV.name}")
        write_staging(cfg.FILE_STAGED, rows)

        # Stage 1.5: Dedup
        fuzzy, semantic, non_dupe = dedup_articles()

        # Stage 2: Extract
        if not args.skip_extract:
            extract_articles()
        else:
            promote_pending_to_success(cfg.FILE_STAGED)
            log.info("  Stage 2: Extract — skipped, promoted pending → success")

        # Stage 3: Summarize
        summarized, sum_failed = summarize_articles()

    elif args.resume:
        log.info("  Stages 1.5-2 skipped (--resume: using existing staged_test.csv)")
        fuzzy = semantic = non_dupe = "N/A"

        # Stage 3: Summarize (on whatever is still pending in staged_test.csv)
        summarized, sum_failed = summarize_articles()

    else:
        log.info("  Stages 1.5-3 skipped (--classify-only)")
        fuzzy = semantic = non_dupe = summarized = sum_failed = "N/A"

    # Stage 4: Classify
    classified, dup_backstop, cl_failed = classify_articles()

    elapsed = datetime.now() - start
    mins    = int(elapsed.total_seconds() // 60)
    secs    = int(elapsed.total_seconds() % 60)

    log.info("")
    log.info("=" * 60)
    log.info("  TEST COMPLETE")
    log.info("=" * 60)
    if mode in ("full", "skip-extract"):
        sample_label = f"{len(rows)} (sampled)" if args.sample else len(rows)
        log.info(f"  Total input:          {sample_label}")
        log.info(f"  Fuzzy dupes:          {fuzzy}")
        log.info(f"  Semantic dupes:       {semantic}")
        log.info(f"  Non-dupes forwarded:  {non_dupe}")
    if mode != "classify-only":
        log.info(f"  Summarized:           {summarized}")
        log.info(f"  Summarize failed:     {sum_failed}")
    log.info(f"  Classified:           {classified}")
    log.info(f"  Stage-4 dup backstop: {dup_backstop}")
    log.info(f"  Classify failed:      {cl_failed}")
    log.info(f"  Elapsed:              {mins}m {secs}s")
    log.info(f"  Output: {cfg.FILE_NEWS_FEED}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
