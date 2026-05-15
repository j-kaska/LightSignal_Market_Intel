"""
LightSignal — run_articles.py
================================
Entry point for the article ingestion pipeline.
Scheduled twice daily via Windows Task Scheduler.

Pipeline stages:
  1.  fetch_rss.py  — pull new articles from RSS feeds, exact-dupe filter
  1.5 dedup.py      — stacked dedup (title fuzzy + semantic embedding)
  2.  extract.py    — Selenium: resolve clean URL + extract article text
  3.  summarize.py  — Gemini: write 2-4 sentence Summary_AI
  4.  classify.py   — sentence-transformers backstop + Gemini classification
  → Output: data/raw/inputs/news_feed.csv

Only processes articles where Processed = No in staged_articles.csv.
Sets Processed = Yes once an article clears all four stages successfully.
Sends a daily summary email at the end of each run via Outlook.

Usage:
  python scripts/articles/run_articles.py          # full run
  python scripts/articles/run_articles.py --fetch-only    # stage 1 only
  python scripts/articles/run_articles.py --skip-fetch    # stages 2-4 only
  python scripts/articles/run_articles.py --classify-only # stage 4 only
  python scripts/articles/run_articles.py --no-email      # skip summary email

Logs: data/articles/logs/YYYY-MM-DD_HH-MM_articles.log
"""

import argparse
import csv
import logging
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from utils.config import FILE_STAGED, ARTICLES_LOG_DIR

# ── Logging setup ─────────────────────────────────────────────────────────────

def setup_logging() -> Path:
    ARTICLES_LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    log_path  = ARTICLES_LOG_DIR / f"{timestamp}_articles.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  [%(levelname)s]  %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return log_path


# ── Processed flag helpers ────────────────────────────────────────────────────

def mark_processed(article_ids: set) -> None:
    """
    Set Processed = Yes for all article_ids that cleared the full pipeline.
    Updates staged_articles.csv in place.
    """
    if not article_ids or not FILE_STAGED.exists():
        return

    rows = []
    with open(FILE_STAGED, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        for row in reader:
            if row.get("article_id") in article_ids:
                row["Processed"] = "Yes"
            rows.append(row)

    # Ensure Processed column exists in fieldnames
    if "Processed" not in fieldnames:
        fieldnames = list(fieldnames) + ["Processed"]

    with open(FILE_STAGED, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    log = logging.getLogger(__name__)
    log.info(f"  Marked {len(article_ids)} articles as Processed = Yes")


def _prune_staged(path: Path, retention_days: int = 30) -> None:
    """Remove classify_status=success rows older than retention_days from staged_articles.csv."""
    if not path.exists():
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)
    before = len(rows)
    rows = [
        r for r in rows
        if not (r.get("classify_status") == "success" and r.get("staged_at", "9999") < cutoff)
    ]
    if len(rows) < before:
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        log = logging.getLogger(__name__)
        log.info(f"  Staging: pruned {before - len(rows)} completed rows older than {retention_days}d "
                 f"({len(rows)} remaining)")


def get_unprocessed_ids() -> set:
    """Return article IDs where Processed != Yes."""
    if not FILE_STAGED.exists():
        return set()
    unprocessed = set()
    with open(FILE_STAGED, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("Processed") or "No").strip().lower() != "yes":
                unprocessed.add(row.get("article_id", ""))
    return unprocessed


# ── Stage runner ──────────────────────────────────────────────────────────────

def run_stage(label: str, func, errors: list):
    """Run a pipeline stage. Collects errors, continues on failure."""
    log = logging.getLogger(__name__)
    try:
        return func()
    except SystemExit as e:
        msg = f"Pipeline stopped at stage: {label} (exit {e.code})"
        log.error(f"  {msg}")
        errors.append(msg + "\n")
        sys.exit(e.code)
    except Exception as e:
        msg = f"{label} failed: {type(e).__name__}: {e}"
        log.error(f"  {msg}")
        log.error(traceback.format_exc())
        errors.append(msg + "\n")
        return None


# ── Args ──────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="LightSignal Article Pipeline")
    parser.add_argument("--fetch-only",    action="store_true", help="Run Stage 1 only")
    parser.add_argument("--skip-fetch",    action="store_true", help="Skip Stage 1, run 2-4")
    parser.add_argument("--classify-only", action="store_true", help="Run Stage 4 only")
    parser.add_argument("--no-email",      action="store_true", help="Skip summary email")
    return parser.parse_args()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log_path = setup_logging()
    log      = logging.getLogger(__name__)
    args     = parse_args()
    errors   = []
    start    = datetime.now()

    log.info("=" * 60)
    log.info("  LightSignal — Article Pipeline")
    log.info(f"  Started: {start.strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 60)

    # Track which articles were unprocessed at the start of this run
    unprocessed_at_start = get_unprocessed_ids()
    log.info(f"  Unprocessed articles at start: {len(unprocessed_at_start)}")

    # ── Stage 1: Fetch RSS ────────────────────────────────────────────────────
    if not args.skip_fetch and not args.classify_only:
        from articles.fetch_rss import fetch_rss
        new_count = run_stage("Fetch RSS", fetch_rss, errors)
        if new_count == 0:
            log.info("  No new articles fetched — processing any pending staged items.")
    else:
        log.info("  Stage 1: Fetch RSS — skipped")

    if args.fetch_only:
        log.info("  --fetch-only: stopping after Stage 1.")
        _finish(log, start, log_path, errors, args)
        return

    # ── Stage 1.5: Stacked Dedup ─────────────────────────────────────────────
    if not args.classify_only:
        from articles.dedup import dedup_articles
        run_stage("Stacked Dedup", dedup_articles, errors)
    else:
        log.info("  Stage 1.5: Stacked Dedup — skipped")

    # ── Stage 2: Extract ─────────────────────────────────────────────────────
    if not args.classify_only:
        from articles.extract import extract_articles
        run_stage("Extract", extract_articles, errors)
    else:
        log.info("  Stage 2: Extract — skipped")

    # ── Stage 3: Summarize ────────────────────────────────────────────────────
    if not args.classify_only:
        from articles.summarize import summarize_articles
        run_stage("Summarize", summarize_articles, errors)
    else:
        log.info("  Stage 3: Summarize — skipped")

    # ── Stage 4: Classify ─────────────────────────────────────────────────────
    from articles.classify import classify_articles
    run_stage("Classify", classify_articles, errors)

    # ── Mark fully processed articles ─────────────────────────────────────────
    # An article is Processed = Yes when classify_status = success
    if FILE_STAGED.exists():
        newly_processed = set()
        with open(FILE_STAGED, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                aid = row.get("article_id", "")
                if (
                    aid in unprocessed_at_start
                    and row.get("classify_status") == "success"
                    and row.get("Processed", "No").strip().lower() != "yes"
                ):
                    newly_processed.add(aid)

        if newly_processed:
            mark_processed(newly_processed)

    _prune_staged(FILE_STAGED, retention_days=30)
    _finish(log, start, log_path, errors, args)


def _finish(log, start, log_path, errors, args):
    elapsed = datetime.now() - start
    minutes = int(elapsed.total_seconds() // 60)
    seconds = int(elapsed.total_seconds() % 60)

    log.info("")
    log.info("=" * 60)
    if errors:
        log.info(f"  ⚠  Pipeline complete with {len(errors)} error(s) — {minutes}m {seconds}s")
    else:
        log.info(f"  ✓  Article pipeline complete — {minutes}m {seconds}s")
    log.info(f"  Log: {log_path}")
    log.info("=" * 60)

    # Send daily summary email
    if not args.no_email:
        try:
            from articles.notify import send_daily_summary
            send_daily_summary(pipeline_errors=errors)
        except Exception as e:
            log.error(f"  Summary email failed: {e}")
    else:
        log.info("  --no-email: summary email skipped")


if __name__ == "__main__":
    main()
