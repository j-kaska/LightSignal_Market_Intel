"""
LightSignal — generate_h3_composites.py
=========================================
Final H3 generation step. Reads the consolidated h3_r*.csv files
(which already contain DC, PP, and BC raw values) and adds:

  1. Percentile rank columns (0.0–1.0) for all 7 raw value columns
  2. Build cost inversion  (bc_pct = 1.0 - percentile_rank(bc_soil_multiplier))
  3. Zero mask column      (is_zero = True where dc_total_mw=0 AND pp_total_mw=0)
  4. is_zero mask (no DC + no PP activity)

Note: Composite scoring is now computed live in the dashboard.
      Users can adjust variable weights interactively without re-running the pipeline.

Run directly:
  python scripts/h3/generate_h3_composites.py

Or called automatically by:
  python scripts/run_all.py
"""

import sys
import logging
from pathlib import Path

import numpy as np
import pandas as pd

# ── Path setup ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from utils.config import (
    H3_FILES, H3_RESOLUTIONS,
    H3_COL_ID,
    H3_COL_DC_OPERATIONAL, H3_COL_DC_PIPELINE, H3_COL_DC_TOTAL,
    H3_COL_PP_OPERATIONAL, H3_COL_PP_PLANNED,  H3_COL_PP_TOTAL,
    H3_COL_BC,
    H3_COL_DC_OPERATIONAL_PCT, H3_COL_DC_PIPELINE_PCT, H3_COL_DC_TOTAL_PCT,
    H3_COL_PP_OPERATIONAL_PCT, H3_COL_PP_PLANNED_PCT,  H3_COL_PP_TOTAL_PCT,
    H3_COL_BC_PCT,
    H3_COL_IS_ZERO,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def percentile_rank(series: pd.Series) -> pd.Series:
    """
    Computes percentile rank for a series, returning values 0.0–1.0.

    - NaN values → NaN (preserved, not ranked)
    - Zero values → 0.0 (they're at the bottom of the distribution)
    - Ties receive the average rank (method='average')

    This uses scipy-style logic via pandas rank, normalised to [0, 1].
    """
    # Only rank non-null values
    not_null = series.notna()
    ranks = series.copy().astype(float)

    if not_null.sum() == 0:
        return ranks  # all null

    # rank() returns 1-based ranks; divide by count to get 0–1
    n = not_null.sum()
    ranked = series[not_null].rank(method="average", na_option="keep")
    # Normalise: (rank - 1) / (n - 1) gives true 0.0 to 1.0
    # But if n=1, avoid division by zero
    if n == 1:
        ranks[not_null] = 0.5
    else:
        ranks[not_null] = (ranked - 1) / (n - 1)

    return ranks


def compute_composites(df: pd.DataFrame, resolution: int) -> pd.DataFrame:
    """
    Adds percentile rank, zero mask, and composite score columns
    to the h3 DataFrame for one resolution.
    """
    df = df.copy()

    required_cols = [
        H3_COL_DC_OPERATIONAL, H3_COL_DC_PIPELINE, H3_COL_DC_TOTAL,
        H3_COL_PP_OPERATIONAL, H3_COL_PP_PLANNED,  H3_COL_PP_TOTAL,
        H3_COL_BC,
    ]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        log.error(
            f"  r{resolution}: Missing required columns: {missing}\n"
            f"  Ensure all H3 generation scripts have been run first."
        )
        sys.exit(1)

    log.info(f"    Computing percentile ranks for {len(df):,} rows...")

    # ── Percentile ranks for DC and PP (higher = better, no inversion) ────────
    pct_map = {
        H3_COL_DC_OPERATIONAL_PCT: H3_COL_DC_OPERATIONAL,
        H3_COL_DC_PIPELINE_PCT:    H3_COL_DC_PIPELINE,
        H3_COL_DC_TOTAL_PCT:       H3_COL_DC_TOTAL,
        H3_COL_PP_OPERATIONAL_PCT: H3_COL_PP_OPERATIONAL,
        H3_COL_PP_PLANNED_PCT:     H3_COL_PP_PLANNED,
        H3_COL_PP_TOTAL_PCT:       H3_COL_PP_TOTAL,
    }
    for pct_col, raw_col in pct_map.items():
        df[pct_col] = percentile_rank(df[raw_col])
        log.info(
            f"      {pct_col:<30}  "
            f"min={df[pct_col].min():.3f}  "
            f"max={df[pct_col].max():.3f}  "
            f"mean={df[pct_col].mean():.3f}"
        )

    # ── Build cost — percentile rank then INVERT (low cost = high score) ──────
    bc_raw_pct = percentile_rank(df[H3_COL_BC])
    df[H3_COL_BC_PCT] = 1.0 - bc_raw_pct
    # Cells where bc was null remain null after inversion
    df.loc[df[H3_COL_BC].isna(), H3_COL_BC_PCT] = np.nan
    log.info(
        f"      {H3_COL_BC_PCT:<30}  "
        f"min={df[H3_COL_BC_PCT].min():.3f}  "
        f"max={df[H3_COL_BC_PCT].max():.3f}  "
        f"mean={df[H3_COL_BC_PCT].mean():.3f}  (inverted)"
    )

    # ── Zero mask ─────────────────────────────────────────────────────────────
    df[H3_COL_IS_ZERO] = (
        (df[H3_COL_DC_TOTAL].fillna(0.0) == 0.0) &
        (df[H3_COL_PP_TOTAL].fillna(0.0) == 0.0)
    )
    zero_count = df[H3_COL_IS_ZERO].sum()
    log.info(
        f"    Zero-masked cells (no DC + no PP): "
        f"{zero_count:,}  ({zero_count/len(df)*100:.1f}%)"
    )

    log.info(
        f"    Percentile columns ready — composite scoring is now done "
        f"live in the dashboard (no baked composites)."
    )

    return df


def generate_h3_composites():
    log.info("=" * 55)
    log.info("  LightSignal — H3 Composite Scoring")
    log.info("=" * 55)
    log.info("  Mode: percentile normalization only — composite scoring is live in dashboard")

    for res in H3_RESOLUTIONS:
        log.info(f"  Processing resolution {res}...")
        filepath = H3_FILES[res]

        if not filepath.exists():
            log.error(
                f"  {filepath.name} not found. "
                f"Run all three H3 generation scripts first."
            )
            sys.exit(1)

        df = pd.read_csv(filepath, dtype={H3_COL_ID: str})
        log.info(f"    Loaded {len(df):,} rows from {filepath.name}")

        df = compute_composites(df, res)

        df.to_csv(filepath, index=False, encoding="utf-8")
        log.info(f"    Saved {len(df):,} rows → {filepath.name}")
        log.info(f"    Final columns: {list(df.columns)}")

    log.info("H3 Composite Scoring complete.")


if __name__ == "__main__":
    generate_h3_composites()
