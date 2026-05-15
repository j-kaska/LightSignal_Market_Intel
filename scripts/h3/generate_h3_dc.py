"""
LightSignal — generate_h3_dc.py
=================================
Bins data center points into H3 hex cells at resolutions 3, 4, and 5,
aggregating estimated_mw into three buckets per resolution:
  - Operational MW
  - Pipeline MW  (Under Construction + Planned)
  - Total MW     (all statuses)

Results are written as columns into the consolidated h3_r*.csv files.
If the h3_r*.csv files don't exist yet they are created. If they exist,
DC columns are added/updated without disturbing other columns.

Run directly:
  python scripts/h3/generate_h3_dc.py

Or called automatically by:
  python scripts/run_all.py
"""

import sys
import logging
from pathlib import Path

import pandas as pd
from utils.h3_utils import latlng_to_cell, cell_to_boundary, cell_to_parent, h3_version

# ── Path setup ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from utils.config import (
    FILE_DC_POINTS,
    H3_FILES, H3_RESOLUTIONS,
    H3_COL_ID,
    H3_COL_DC_OPERATIONAL, H3_COL_DC_PIPELINE, H3_COL_DC_TOTAL,
    DC_STATUS_OPERATIONAL, DC_PIPELINE_STATUSES,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def load_or_create_h3_file(filepath: Path) -> pd.DataFrame:
    """
    Loads existing h3_r*.csv or creates an empty DataFrame with just
    the h3_id index column if the file doesn't exist yet.
    """
    if filepath.exists():
        df = pd.read_csv(filepath, dtype={H3_COL_ID: str})
        log.info(f"    Loaded existing {filepath.name}: {len(df):,} rows")
        return df
    else:
        log.info(f"    {filepath.name} not found — will create new file.")
        return pd.DataFrame(columns=[H3_COL_ID])


def upsert_columns(base_df: pd.DataFrame, new_cols_df: pd.DataFrame) -> pd.DataFrame:
    """
    Merges new columns into base_df on H3_COL_ID.
    If columns already exist in base_df they are replaced.
    New H3 IDs from new_cols_df are added as new rows (left join from new).
    """
    # Drop any columns that already exist in base (we'll replace them)
    cols_to_add = [c for c in new_cols_df.columns if c != H3_COL_ID]
    base_df = base_df.drop(
        columns=[c for c in cols_to_add if c in base_df.columns],
        errors="ignore",
    )

    if base_df.empty or len(base_df) == 0:
        return new_cols_df.copy()

    merged = base_df.merge(new_cols_df, on=H3_COL_ID, how="outer")
    return merged


def aggregate_dc_to_h3(df_dc: pd.DataFrame, resolution: int) -> pd.DataFrame:
    """
    Bins each DC point into its parent H3 cell at the given resolution,
    then sums estimated_mw by Operational, Pipeline, and Total buckets.

    Returns a DataFrame with columns:
      h3_id, dc_operational_mw, dc_pipeline_mw, dc_total_mw
    """
    log.info(f"    Binning {len(df_dc):,} DC points into H3 resolution {resolution}...")

    df = df_dc.copy()

    # Assign H3 cell ID for each point
    df["h3_id"] = df.apply(
        lambda row: latlng_to_cell(row["lat"], row["lon"], resolution),
        axis=1,
    )

    # Build status masks
    is_operational = df["status"] == DC_STATUS_OPERATIONAL
    is_pipeline    = df["status"].isin(DC_PIPELINE_STATUSES)

    # Sum MW by hex and bucket
    op_mw   = df[is_operational].groupby("h3_id")["estimated_mw"].sum().rename(H3_COL_DC_OPERATIONAL)
    pipe_mw = df[is_pipeline].groupby("h3_id")["estimated_mw"].sum().rename(H3_COL_DC_PIPELINE)
    tot_mw  = df.groupby("h3_id")["estimated_mw"].sum().rename(H3_COL_DC_TOTAL)

    # Combine into one DataFrame — outer join on h3_id
    result = pd.DataFrame({H3_COL_ID: tot_mw.index})
    result = result.merge(op_mw,   on=H3_COL_ID, how="left")
    result = result.merge(pipe_mw, on=H3_COL_ID, how="left")
    result = result.merge(tot_mw,  on=H3_COL_ID, how="left")

    # Fill nulls with 0 — a hex with no operational DCs gets 0, not NaN
    result[H3_COL_DC_OPERATIONAL] = result[H3_COL_DC_OPERATIONAL].fillna(0.0)
    result[H3_COL_DC_PIPELINE]    = result[H3_COL_DC_PIPELINE].fillna(0.0)
    result[H3_COL_DC_TOTAL]       = result[H3_COL_DC_TOTAL].fillna(0.0)

    # Stats
    non_zero = (result[H3_COL_DC_TOTAL] > 0).sum()
    log.info(f"      H3 cells with any DC MW: {non_zero:,}  "
             f"(of {len(result):,} total active cells)")
    log.info(f"      Total operational MW aggregated: "
             f"{result[H3_COL_DC_OPERATIONAL].sum():,.1f}")
    log.info(f"      Total pipeline MW aggregated:    "
             f"{result[H3_COL_DC_PIPELINE].sum():,.1f}")
    log.info(f"      Total MW aggregated:             "
             f"{result[H3_COL_DC_TOTAL].sum():,.1f}")

    return result


def generate_h3_dc():
    log.info("=" * 55)
    log.info("  LightSignal — H3 DC Generation")
    log.info("=" * 55)
    log.info(f"  Input : {FILE_DC_POINTS}")
    log.info(f"  h3 library version: {h3_version()}")

    # ── 1. Load clean DC points ───────────────────────────────────────────────
    if not FILE_DC_POINTS.exists():
        log.error(f"DC points file not found: {FILE_DC_POINTS}")
        log.error("Run transform_dc.py first.")
        sys.exit(1)

    df_dc = pd.read_csv(FILE_DC_POINTS)
    log.info(f"  Loaded {len(df_dc):,} DC points")

    # Drop any rows still missing lat/lon (shouldn't happen after transform)
    before = len(df_dc)
    df_dc = df_dc.dropna(subset=["lat", "lon"])
    df_dc = df_dc[(df_dc["lat"] != 0.0) | (df_dc["lon"] != 0.0)]
    if len(df_dc) < before:
        log.warning(f"  Dropped {before - len(df_dc)} rows with missing lat/lon")

    # ── 2. Process each resolution ────────────────────────────────────────────
    for res in H3_RESOLUTIONS:
        log.info(f"  Processing resolution {res}...")
        filepath = H3_FILES[res]

        # Aggregate DC points to this resolution
        dc_agg = aggregate_dc_to_h3(df_dc, res)

        # Load or create the h3_r*.csv file
        filepath.parent.mkdir(parents=True, exist_ok=True)
        h3_df = load_or_create_h3_file(filepath)

        # Merge DC columns in
        h3_df = upsert_columns(h3_df, dc_agg)

        # Write back
        h3_df.to_csv(filepath, index=False, encoding="utf-8")
        log.info(f"    Saved {len(h3_df):,} rows → {filepath.name}")

    log.info("H3 DC generation complete.")


if __name__ == "__main__":
    generate_h3_dc()
