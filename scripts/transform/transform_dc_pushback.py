"""
LightSignal - transform_dc_pushback.py
=====================================
Transforms raw dc_pushback.csv into a clean dc_pushback_clean.csv
with canonical dashboard categories and normalized outcomes.

What this script does:
  1. Reads dc_pushback.csv from data/raw/inputs/
  2. Inverts community_outcome win/loss from source perspective
  3. Normalizes action_type to dashboard picklist values
  4. Normalizes issue_category to dashboard picklist values
  5. Derives canonical status using status + community_outcome
  6. Preserves source order for primary/secondary multi-value fields
  7. Writes clean dc_pushback_clean.csv to data/processed/

Run directly:
  python scripts/transform/transform_dc_pushback.py

Or called automatically by:
  python scripts/run_all.py
"""

import re
import sys
import logging
from pathlib import Path
from collections import Counter

import pandas as pd

# Path setup
SCRIPT_DIR = Path(__file__).parent
ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from utils.config import (
    FILE_PUSHBACK,
    FILE_PUSHBACK_CLEAN,
    PUSHBACK_FIELD_ACTION_TYPE,
    PUSHBACK_FIELD_ISSUE_CATEGORY,
    PUSHBACK_FIELD_STATUS,
    PUSHBACK_FIELD_COMMUNITY_OUTCOME,
    PUSHBACK_FIELD_DEVELOPMENT_OUTCOME,
    PUSHBACK_ACTION_MAP,
    PUSHBACK_ISSUE_MAP,
    PUSHBACK_ACTION_OTHER,
    PUSHBACK_ISSUE_OTHER,
    PUSHBACK_STATUS_PENDING,
    PUSHBACK_STATUS_RESOLVED_FAVORABLE,
    PUSHBACK_STATUS_RESOLVED_UNFAVORABLE,
    PUSHBACK_STATUS_RESOLVED_MIXED,
    PUSHBACK_OUTCOME_FAVORABLE,
    PUSHBACK_OUTCOME_UNFAVORABLE,
    PUSHBACK_OUTCOME_MIXED,
    PUSHBACK_OUTCOME_PENDING,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def normalize_key(value: str) -> str:
    """Lower-case token and normalize separators for robust dictionary lookups."""
    if value is None:
        return ""
    key = str(value).strip().lower()
    key = key.replace("&", "and")
    key = re.sub(r"[\s\-/]+", "_", key)
    key = re.sub(r"_+", "_", key)
    return key.strip("_")


def split_tokens(value: str) -> list[str]:
    """Split semicolon-delimited metadata fields while preserving source order."""
    if value is None:
        return []
    parts = [p.strip() for p in str(value).split(";")]
    return [p for p in parts if p]


def dedupe_preserve_order(values: list[str]) -> list[str]:
    """Drop duplicates while preserving first-seen order."""
    seen = set()
    out = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def map_multi_value(
    raw_value: str,
    mapping: dict,
    other_label: str,
    unknown_counter: Counter,
) -> tuple[str, str]:
    """
    Maps a semicolon-delimited field to canonical labels.
    Returns (primary, secondary_semicolon_string).
    """
    tokens = split_tokens(raw_value)

    if not tokens:
        return other_label, ""

    mapped = []
    for token in tokens:
        key = normalize_key(token)
        label = mapping.get(key)
        if label is None:
            unknown_counter[key or "<blank>"] += 1
            label = other_label
        mapped.append(label)

    mapped = dedupe_preserve_order(mapped)
    primary = mapped[0]
    secondary = "; ".join(mapped[1:]) if len(mapped) > 1 else ""
    return primary, secondary


def normalize_community_outcome(raw_value: str) -> str:
    """
    Source is anti-data-center perspective. Invert win/loss.
    Keep mixed/pending unchanged.
    """
    key = normalize_key(raw_value)

    if key in {"win", "favorable_for_communities", "community_win"}:
        return "loss"
    if key in {"loss", "unfavorable_for_communities", "community_loss"}:
        return "win"
    if key in {"favorable_for_development", "development_win"}:
        return "win"
    if key in {"unfavorable_for_development", "development_loss"}:
        return "loss"
    if key in {"mixed", "pending"}:
        return key
    if key == "":
        return ""
    return key


def format_development_outcome(outcome_norm: str) -> str:
    if outcome_norm == "win":
        return PUSHBACK_OUTCOME_FAVORABLE
    if outcome_norm == "loss":
        return PUSHBACK_OUTCOME_UNFAVORABLE
    if outcome_norm == "mixed":
        return PUSHBACK_OUTCOME_MIXED
    if outcome_norm == "pending":
        return PUSHBACK_OUTCOME_PENDING
    return ""


def derive_status(status_raw: str, outcome_norm: str) -> str:
    """Derives canonical status using both lifecycle state and normalized outcome."""
    status_key = normalize_key(status_raw)

    pending_keys = {
        "active",
        "pending",
        "open",
        "in_progress",
        "under_review",
        "proposed",
        "draft",
    }
    resolved_keys = {
        "expired",
        "defeated",
        "passed",
        "cancelled",
        "approved",
        "rejected",
        "denied",
        "withdrawn",
        "closed",
    }

    if status_key in pending_keys or outcome_norm == "pending":
        return PUSHBACK_STATUS_PENDING

    if outcome_norm == "win":
        return PUSHBACK_STATUS_RESOLVED_FAVORABLE
    if outcome_norm == "loss":
        return PUSHBACK_STATUS_RESOLVED_UNFAVORABLE
    if outcome_norm == "mixed":
        return PUSHBACK_STATUS_RESOLVED_MIXED

    if status_key in resolved_keys:
        return PUSHBACK_STATUS_RESOLVED_MIXED

    return PUSHBACK_STATUS_PENDING


def transform_dc_pushback():
    log.info("=" * 55)
    log.info("  LightSignal - DC Pushback Transform")
    log.info("=" * 55)
    log.info(f"  Input : {FILE_PUSHBACK}")
    log.info(f"  Output: {FILE_PUSHBACK_CLEAN}")

    if not FILE_PUSHBACK.exists():
        log.error(f"Input file not found: {FILE_PUSHBACK}")
        log.error("Place dc_pushback.csv in data/raw/inputs/ and re-run.")
        sys.exit(1)

    df = pd.read_csv(FILE_PUSHBACK, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    in_count = len(df)
    log.info(f"  Loaded {in_count:,} records, {len(df.columns)} columns")

    unknown_actions = Counter()
    unknown_issues = Counter()

    action_pairs = df[PUSHBACK_FIELD_ACTION_TYPE].apply(
        lambda v: map_multi_value(v, PUSHBACK_ACTION_MAP, PUSHBACK_ACTION_OTHER, unknown_actions)
    )
    issue_pairs = df[PUSHBACK_FIELD_ISSUE_CATEGORY].apply(
        lambda v: map_multi_value(v, PUSHBACK_ISSUE_MAP, PUSHBACK_ISSUE_OTHER, unknown_issues)
    )

    df["action_type_primary"] = action_pairs.apply(lambda x: x[0])
    df["action_type_secondary"] = action_pairs.apply(lambda x: x[1])
    df["issue_category_primary"] = issue_pairs.apply(lambda x: x[0])
    df["issue_category_secondary"] = issue_pairs.apply(lambda x: x[1])

    df["community_outcome_normalized"] = df[PUSHBACK_FIELD_COMMUNITY_OUTCOME].apply(
        normalize_community_outcome
    )
    df[PUSHBACK_FIELD_DEVELOPMENT_OUTCOME] = df["community_outcome_normalized"].apply(
        format_development_outcome
    )
    df["status_normalized"] = df.apply(
        lambda r: derive_status(r[PUSHBACK_FIELD_STATUS], r["community_outcome_normalized"]),
        axis=1,
    )

    # Overwrite canonical filter fields, retain raw fields for auditability.
    df["action_type_raw"] = df[PUSHBACK_FIELD_ACTION_TYPE]
    df["issue_category_raw"] = df[PUSHBACK_FIELD_ISSUE_CATEGORY]
    df["status_raw"] = df[PUSHBACK_FIELD_STATUS]
    df["community_outcome_raw"] = df[PUSHBACK_FIELD_COMMUNITY_OUTCOME]

    df[PUSHBACK_FIELD_ACTION_TYPE] = df["action_type_primary"]
    df[PUSHBACK_FIELD_ISSUE_CATEGORY] = df["issue_category_primary"]
    df[PUSHBACK_FIELD_STATUS] = df["status_normalized"]
    df[PUSHBACK_FIELD_COMMUNITY_OUTCOME] = df[PUSHBACK_FIELD_DEVELOPMENT_OUTCOME]

    action_counts = df[PUSHBACK_FIELD_ACTION_TYPE].value_counts()
    issue_counts = df[PUSHBACK_FIELD_ISSUE_CATEGORY].value_counts()
    status_counts = df[PUSHBACK_FIELD_STATUS].value_counts()

    log.info("  Action type distribution:")
    for label, count in action_counts.items():
        log.info(f"    {label:<40} {count:>5,}")

    log.info("  Issue category distribution:")
    for label, count in issue_counts.items():
        log.info(f"    {label:<40} {count:>5,}")

    log.info("  Status distribution:")
    for label, count in status_counts.items():
        log.info(f"    {label:<40} {count:>5,}")

    if unknown_actions:
        log.warning("  Unmapped action_type values routed to Other:")
        for key, count in unknown_actions.most_common():
            log.warning(f"    {key:<40} {count:>5,}")

    if unknown_issues:
        log.warning("  Unmapped issue_category values routed to Other:")
        for key, count in unknown_issues.most_common():
            log.warning(f"    {key:<40} {count:>5,}")

    FILE_PUSHBACK_CLEAN.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(FILE_PUSHBACK_CLEAN, index=False, encoding="utf-8")

    out_count = len(df)
    log.info(f"  Written {out_count:,} records -> {FILE_PUSHBACK_CLEAN}")
    if in_count != out_count:
        log.warning(f"  Row-count mismatch: input={in_count:,}, output={out_count:,}")

    log.info("DC pushback transform complete.")
    return df


if __name__ == "__main__":
    transform_dc_pushback()
