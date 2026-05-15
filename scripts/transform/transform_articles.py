"""
LightSignal — transform_articles.py
=====================================
Transforms raw news_feed.csv into two clean output files:

  1. articles_dc_linked.csv
     Articles tagged to one or more specific data centers.
     One row per article-DC pair (exploded on DC_ID).
     These surface inside individual DC popups in the KMZ.

  2. articles_state_linked.csv
     Articles tagged to a state but NOT to a specific DC.
     One row per article-state pair (exploded on States).
     These surface on the state centroid dot layer in the KMZ.

Articles with no DC tag AND no state tag are excluded entirely.
Duplicate articles (Is_Duplicate = True) are left for source cleanup
but flagged in the log.

What this script does:
  1. Reads news_feed.csv from data/raw/inputs/
  2. Parses JSON-wrapped DC_ID and States fields
  3. Splits into DC-linked and state-linked subsets
  4. Explodes multi-DC and multi-state articles into one row per link
  5. Joins state centroid lat/lon onto state-linked articles
  6. Sorts both outputs by PublishedDate descending (newest first)
  7. Writes both CSVs to data/processed/
  8. Also writes state_centroids.csv if it doesn't exist yet

Run directly:
  python scripts/transform/transform_articles.py

Or called automatically by:
  python scripts/run_all.py
"""

import sys
import json
import re
import logging
from pathlib import Path
from datetime import datetime

import pandas as pd

# ── Path setup ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from utils.config import (
    FILE_ARTICLES,
    FILE_ARTICLES_DC, FILE_ARTICLES_STATE, FILE_STATE_CENTROIDS,
    ART_FIELD_ID, ART_FIELD_TITLE, ART_FIELD_URL,
    ART_FIELD_SOURCE, ART_FIELD_DATE, ART_FIELD_SUMMARY,
    ART_FIELD_CATEGORY, ART_FIELD_STATES,
    ART_FIELD_DC_ID, ART_FIELD_DUPLICATE,
    STATE_CENTROIDS,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def parse_json_list(value: str) -> list:
    """
    Parses a JSON-array-wrapped string field.
    Examples:
        '["GA"]'              → ["GA"]
        '["VA","PA","NC"]'    → ["VA", "PA", "NC"]
        'DC-12345,DC-67890'   → ["DC-12345", "DC-67890"]  (comma-sep, no JSON)
        ''  or  None          → []
    """
    if not value or not isinstance(value, str):
        return []
    value = value.strip()
    if not value or value in ("[]", "None"):
        return []

    # Try JSON first
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(v).strip() for v in parsed if str(v).strip()]
        return [str(parsed).strip()]
    except (json.JSONDecodeError, ValueError):
        pass

    # Fallback: strip brackets/quotes and split on comma
    cleaned = re.sub(r'[\[\]"]', '', value)
    parts = [p.strip() for p in cleaned.split(",") if p.strip()]
    return parts


def parse_date(value: str):
    """
    Parses a date string into a datetime object for sorting.
    Returns None on failure (these will sort to the bottom).
    Handles formats like '1/26/2026 7:08 PM'.
    """
    if not value or not isinstance(value, str):
        return None
    formats = [
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


def build_state_centroids_csv():
    """
    Writes state_centroids.csv from the STATE_CENTROIDS dict in config.py
    if it doesn't already exist.
    """
    if FILE_STATE_CENTROIDS.exists():
        log.info(f"  state_centroids.csv already exists — skipping.")
        return

    rows = [
        {"state": state, "lat": lat, "lon": lon}
        for state, (lat, lon) in STATE_CENTROIDS.items()
    ]
    df = pd.DataFrame(rows).sort_values("state")
    FILE_STATE_CENTROIDS.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(FILE_STATE_CENTROIDS, index=False, encoding="utf-8")
    log.info(f"  Written {len(df)} state centroids → {FILE_STATE_CENTROIDS}")


def transform_articles():
    log.info("=" * 55)
    log.info("  LightSignal — Articles Transform")
    log.info("=" * 55)
    log.info(f"  Input : {FILE_ARTICLES}")
    log.info(f"  Output 1: {FILE_ARTICLES_DC}")
    log.info(f"  Output 2: {FILE_ARTICLES_STATE}")

    # ── 1. Check input ────────────────────────────────────────────────────────
    if not FILE_ARTICLES.exists():
        log.error(f"Input file not found: {FILE_ARTICLES}")
        log.error("Place news_feed.csv in data/raw/inputs/ and re-run.")
        sys.exit(1)

    # ── 2. Load raw file ──────────────────────────────────────────────────────
    log.info("Loading raw articles file...")
    # Try UTF-8 first, fall back to Windows-1252 (cp1252) which handles
    # special characters like em-dashes (0x96) from SharePoint exports
    df = None
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            df = pd.read_csv(
                FILE_ARTICLES,
                encoding=encoding,
                dtype=str,
                keep_default_na=False,
            )
            log.info(f"  Loaded {len(df):,} articles, {len(df.columns)} columns (encoding: {encoding})")
            break
        except UnicodeDecodeError:
            log.warning(f"  Encoding {encoding} failed, trying next...")
    if df is None:
        log.error("Could not decode news_feed.csv with any known encoding.")
        sys.exit(1)

    # ── 3. Flag duplicates ────────────────────────────────────────────────────
    dupes = df[df[ART_FIELD_DUPLICATE].str.strip().str.lower() == "true"]
    if len(dupes) > 0:
        log.warning(
            f"  {len(dupes):,} duplicate articles found "
            f"(Is_Duplicate=True) — these remain in source data "
            f"for cleanup but are included here until removed."
        )

    # ── 4. Parse list fields ──────────────────────────────────────────────────
    log.info("Parsing DC_ID and States fields...")
    df["_dc_ids"]  = df[ART_FIELD_DC_ID].apply(parse_json_list)
    df["_states"]  = df[ART_FIELD_STATES].apply(parse_json_list)

    df["_has_dc"]    = df["_dc_ids"].apply(lambda x: len(x) > 0)
    df["_has_state"] = df["_states"].apply(lambda x: len(x) > 0)

    log.info(f"  Articles with DC link:          {df['_has_dc'].sum():>4}")
    log.info(f"  Articles with state only:       {(~df['_has_dc'] & df['_has_state']).sum():>4}")
    log.info(f"  Articles with neither (excluded): {(~df['_has_dc'] & ~df['_has_state']).sum():>4}")

    # ── 5. Parse and sort by date ─────────────────────────────────────────────
    df["_date_parsed"] = df[ART_FIELD_DATE].apply(parse_date)

    # ── 6. Build clean base columns ──────────────────────────────────────────
    # These are the columns we carry into both output files
    BASE_COLS = {
        "article_id":   ART_FIELD_ID,
        "title":        ART_FIELD_TITLE,
        "url":          ART_FIELD_URL,
        "source":       ART_FIELD_SOURCE,
        "published_date": ART_FIELD_DATE,
        "category":     ART_FIELD_CATEGORY,
        "summary":      ART_FIELD_SUMMARY,
    }

    # ── 7. DC-LINKED ARTICLES ─────────────────────────────────────────────────
    log.info("Building DC-linked articles output...")

    dc_rows = []
    for _, row in df[df["_has_dc"]].iterrows():
        for dc_id in row["_dc_ids"]:
            dc_id = dc_id.strip()
            if not dc_id:
                continue
            entry = {col: row[src] for col, src in BASE_COLS.items()}
            entry["dc_id"]       = dc_id
            entry["_date_parsed"] = row["_date_parsed"]
            dc_rows.append(entry)

    df_dc = pd.DataFrame(dc_rows)

    # Sort newest first within each DC
    df_dc = df_dc.sort_values(
        ["dc_id", "_date_parsed"],
        ascending=[True, False],
        na_position="last",
    ).drop(columns=["_date_parsed"])

    FILE_ARTICLES_DC.parent.mkdir(parents=True, exist_ok=True)
    df_dc.to_csv(FILE_ARTICLES_DC, index=False, encoding="utf-8")

    log.info(f"  Written {len(df_dc):,} DC-article link rows → {FILE_ARTICLES_DC}")

    # How many unique DCs have at least one article?
    unique_dcs = df_dc["dc_id"].nunique()
    log.info(f"  Unique data centers with articles: {unique_dcs}")

    # ── 8. STATE-LINKED ARTICLES (no DC tag) ──────────────────────────────────
    log.info("Building state-linked articles output...")

    state_mask = (~df["_has_dc"]) & df["_has_state"]
    state_rows = []

    for _, row in df[state_mask].iterrows():
        for state in row["_states"]:
            state = state.strip().upper()
            if not state:
                continue
            # Look up centroid
            if state in STATE_CENTROIDS:
                lat, lon = STATE_CENTROIDS[state]
            else:
                log.warning(
                    f"  State '{state}' not found in STATE_CENTROIDS — "
                    f"article '{row[ART_FIELD_ID]}' skipped for this state."
                )
                continue

            entry = {col: row[src] for col, src in BASE_COLS.items()}
            entry["state"]        = state
            entry["state_lat"]    = lat
            entry["state_lon"]    = lon
            entry["_date_parsed"] = row["_date_parsed"]
            state_rows.append(entry)

    df_state = pd.DataFrame(state_rows)

    # Sort newest first within each state
    df_state = df_state.sort_values(
        ["state", "_date_parsed"],
        ascending=[True, False],
        na_position="last",
    ).drop(columns=["_date_parsed"])

    FILE_ARTICLES_STATE.parent.mkdir(parents=True, exist_ok=True)
    df_state.to_csv(FILE_ARTICLES_STATE, index=False, encoding="utf-8")

    log.info(f"  Written {len(df_state):,} state-article link rows → {FILE_ARTICLES_STATE}")

    unique_states = df_state["state"].nunique()
    log.info(f"  Unique states with articles: {unique_states}")

    # ── 9. Write state centroids CSV ─────────────────────────────────────────
    log.info("Writing state centroids lookup...")
    build_state_centroids_csv()

    # ── 10. Summary ───────────────────────────────────────────────────────────
    log.info("─" * 55)
    log.info("  Articles transform summary:")
    log.info(f"    Total input articles:         {len(df):,}")
    log.info(f"    DC-linked article rows:       {len(df_dc):,}")
    log.info(f"    State-linked article rows:    {len(df_state):,}")
    log.info(
        f"    Excluded (no geo tag):        "
        f"{(~df['_has_dc'] & ~df['_has_state']).sum():,}"
    )
    log.info("Articles transform complete.")

    return df_dc, df_state


if __name__ == "__main__":
    transform_articles()
