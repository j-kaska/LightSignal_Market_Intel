"""
LightSignal — generate_h3_pp.py
=================================
Bins power plant points into H3 hex cells at resolutions 3, 4, and 5,
aggregating nameplate_mw into three buckets per resolution:
  - Operational MW  (plants from Operating sheet)
  - Planned MW      (plants from Planned sheet)
  - Total MW        (all plants)

Results are written as columns into the consolidated h3_r*.csv files.

Run directly:
  python scripts/h3/generate_h3_pp.py

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
    FILE_PP_POINTS,
    H3_FILES, H3_RESOLUTIONS,
    H3_COL_ID,
    H3_COL_PP_OPERATIONAL, H3_COL_PP_PLANNED, H3_COL_PP_TOTAL,
    PP_FIELD_STATUS_LABEL,
    PP_STATUS_OPERATIONAL, PP_STATUS_PLANNED,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def load_h3_file(filepath: Path) -> pd.DataFrame:
    """Loads existing h3_r*.csv. Raises if not found — DC must run first."""
    if not filepath.exists():
        raise FileNotFoundError(
            f"{filepath.name} not found. "
            f"Run generate_h3_dc.py before generate_h3_pp.py."
        )
    df = pd.read_csv(filepath, dtype={H3_COL_ID: str})
    log.info(f"    Loaded {filepath.name}: {len(df):,} rows")
    return df


def upsert_columns(base_df: pd.DataFrame, new_cols_df: pd.DataFrame) -> pd.DataFrame:
    """Merges new columns into base_df on H3_COL_ID, replacing existing."""
    cols_to_add = [c for c in new_cols_df.columns if c != H3_COL_ID]
    base_df = base_df.drop(
        columns=[c for c in cols_to_add if c in base_df.columns],
        errors="ignore",
    )
    return base_df.merge(new_cols_df, on=H3_COL_ID, how="outer")


def aggregate_pp_to_h3(df_pp: pd.DataFrame, resolution: int) -> pd.DataFrame:
    """
    Bins each power plant into its parent H3 cell at the given resolution,
    then sums nameplate_mw by Operational, Planned, and Total buckets.

    Returns a DataFrame with columns:
      h3_id, pp_operational_mw, pp_planned_mw, pp_total_mw
    """
    log.info(f"    Binning {len(df_pp):,} plants into H3 resolution {resolution}...")

    df = df_pp.copy()

    # Assign H3 cell ID for each plant
    df["h3_id"] = df.apply(
        lambda row: latlng_to_cell(row["lat"], row["lon"], resolution),
        axis=1,
    )

    # Build status masks
    is_operational = df[PP_FIELD_STATUS_LABEL] == PP_STATUS_OPERATIONAL
    is_planned     = df[PP_FIELD_STATUS_LABEL] == PP_STATUS_PLANNED

    # Sum MW by hex and bucket
    op_mw   = df[is_operational].groupby("h3_id")["nameplate_mw"].sum().rename(H3_COL_PP_OPERATIONAL)
    plan_mw = df[is_planned].groupby("h3_id")["nameplate_mw"].sum().rename(H3_COL_PP_PLANNED)
    tot_mw  = df.groupby("h3_id")["nameplate_mw"].sum().rename(H3_COL_PP_TOTAL)

    # Combine into one DataFrame
    result = pd.DataFrame({H3_COL_ID: tot_mw.index})
    result = result.merge(op_mw,   on=H3_COL_ID, how="left")
    result = result.merge(plan_mw, on=H3_COL_ID, how="left")
    result = result.merge(tot_mw,  on=H3_COL_ID, how="left")

    # Fill nulls with 0
    result[H3_COL_PP_OPERATIONAL] = result[H3_COL_PP_OPERATIONAL].fillna(0.0)
    result[H3_COL_PP_PLANNED]     = result[H3_COL_PP_PLANNED].fillna(0.0)
    result[H3_COL_PP_TOTAL]       = result[H3_COL_PP_TOTAL].fillna(0.0)

    # Stats
    non_zero = (result[H3_COL_PP_TOTAL] > 0).sum()
    log.info(f"      H3 cells with any PP MW: {non_zero:,}  "
             f"(of {len(result):,} total active cells)")
    log.info(f"      Total operational MW aggregated: "
             f"{result[H3_COL_PP_OPERATIONAL].sum():,.1f}")
    log.info(f"      Total planned MW aggregated:     "
             f"{result[H3_COL_PP_PLANNED].sum():,.1f}")
    log.info(f"      Total MW aggregated:             "
             f"{result[H3_COL_PP_TOTAL].sum():,.1f}")

    return result


def generate_h3_pp():
    log.info("=" * 55)
    log.info("  LightSignal — H3 Power Plants Generation")
    log.info("=" * 55)
    log.info(f"  Input : {FILE_PP_POINTS}")

    # ── 1. Load clean PP points ───────────────────────────────────────────────
    if not FILE_PP_POINTS.exists():
        log.error(f"Power plants file not found: {FILE_PP_POINTS}")
        log.error("Run transform_power_plants.py first.")
        sys.exit(1)

    df_pp = pd.read_csv(FILE_PP_POINTS)
    log.info(f"  Loaded {len(df_pp):,} power plants")

    # Drop missing lat/lon
    before = len(df_pp)
    df_pp = df_pp.dropna(subset=["lat", "lon"])
    df_pp = df_pp[(df_pp["lat"] != 0.0) | (df_pp["lon"] != 0.0)]
    if len(df_pp) < before:
        log.warning(f"  Dropped {before - len(df_pp)} rows with missing lat/lon")

    # ── 2. Process each resolution ────────────────────────────────────────────
    for res in H3_RESOLUTIONS:
        log.info(f"  Processing resolution {res}...")
        filepath = H3_FILES[res]

        pp_agg = aggregate_pp_to_h3(df_pp, res)

        try:
            h3_df = load_h3_file(filepath)
        except FileNotFoundError as e:
            log.error(str(e))
            sys.exit(1)

        # Merge PP columns in
        cols_to_add = [c for c in pp_agg.columns if c != H3_COL_ID]
        h3_df = h3_df.drop(
            columns=[c for c in cols_to_add if c in h3_df.columns],
            errors="ignore",
        )
        h3_df = h3_df.merge(pp_agg, on=H3_COL_ID, how="outer")

        # Fill any NaN MW values introduced by outer join with 0
        for col in [H3_COL_PP_OPERATIONAL, H3_COL_PP_PLANNED, H3_COL_PP_TOTAL]:
            if col in h3_df.columns:
                h3_df[col] = h3_df[col].fillna(0.0)

        h3_df.to_csv(filepath, index=False, encoding="utf-8")
        log.info(f"    Saved {len(h3_df):,} rows → {filepath.name}")

    log.info("H3 Power Plants generation complete.")


if __name__ == "__main__":
    generate_h3_pp()
