"""
LightSignal — generate_h3_build_cost.py
=========================================
Spatially aggregates R6 build cost hexagons up to R3/R4/R5 resolutions.

Input:  bc_r6_clean.csv (h3_id, unit_cost_ft)
Output: Appends bc_unit_cost_ft column to h3_r3.csv, h3_r4.csv, h3_r5.csv

Aggregation method: AVERAGE unit_cost_ft of all child R6 cells within each parent.

The unit_cost_ft column uses the improved bore cost formula from Bore_Cost_LongHaul.xlsx:
  - Continuous soil multiplier (not discrete)
  - Bedrock depth adjustment
  - Slope factor adjustment

Run directly:
  python scripts/h3/generate_h3_build_cost.py

Or called automatically by:
  python scripts/run_all.py
"""

import sys
import logging
from pathlib import Path

import pandas as pd

# ── Path setup ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from utils.config import PROCESSED_DIR, H3_FILES, H3_RESOLUTIONS, H3_COL_ID
from utils.h3_utils import cell_to_parent

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

FILE_BC = PROCESSED_DIR / "bc_r6_clean.csv"


def aggregate_bc_to_resolution(bc_r6: pd.DataFrame, target_res: int) -> pd.DataFrame:
    """
    Aggregates R6 build cost cells to a target resolution by averaging unit_cost_ft.
    
    Args:
        bc_r6: DataFrame with columns [h3_id, unit_cost_ft]
        target_res: Target H3 resolution (3, 4, or 5)
    
    Returns:
        DataFrame with [h3_id, bc_unit_cost_ft] at target resolution
    """
    log.info(f"  Aggregating R6 → R{target_res}...")
    
    # Map each R6 cell to its parent at target resolution
    bc_r6[f'parent_r{target_res}'] = bc_r6['h3_id'].apply(
        lambda h3_id: cell_to_parent(h3_id, target_res)
    )
    
    # Group by parent and average unit_cost_ft
    agg = bc_r6.groupby(f'parent_r{target_res}')['unit_cost_ft'].mean().reset_index()
    agg.rename(columns={
        f'parent_r{target_res}': 'h3_id',
        'unit_cost_ft': 'bc_unit_cost_ft'
    }, inplace=True)
    
    log.info(f"    R6 cells: {len(bc_r6):,}")
    log.info(f"    R{target_res} cells: {len(agg):,}")
    log.info(f"    Avg cost: ${agg['bc_unit_cost_ft'].mean():.2f}/ft")
    
    return agg


def generate_h3_build_cost():
    log.info("=" * 55)
    log.info("  LightSignal — H3 Build Cost Generation")
    log.info("=" * 55)
    
    if not FILE_BC.exists():
        log.error(f"  Build cost file not found: {FILE_BC}")
        log.error(f"  Run transform_build_cost_v2.py first.")
        sys.exit(1)
    
    log.info(f"  Loading R6 build cost: {FILE_BC.name}")
    bc_r6 = pd.read_csv(FILE_BC, dtype={'h3_id': str})
    log.info(f"  R6 cells: {len(bc_r6):,}")
    
    # Process each target resolution
    for res in H3_RESOLUTIONS:
        log.info(f"  Processing R{res}...")
        
        # Check if H3 file exists
        h3_file = H3_FILES[res]
        if not h3_file.exists():
            log.warning(f"  {h3_file.name} not found — skipping R{res}")
            continue
        
        # Load existing H3 file
        df = pd.read_csv(h3_file, dtype={H3_COL_ID: str})
        log.info(f"    Loaded: {len(df):,} rows")
        
        # Drop old build cost column if it exists (from previous runs)
        old_cols = ['bc_soil_multiplier', 'bc_unit_cost_ft']
        for old_col in old_cols:
            if old_col in df.columns:
                df.drop(columns=[old_col], inplace=True)
                log.info(f"    Dropped old column: {old_col}")
        
        # Aggregate BC from R6 to this resolution
        bc_agg = aggregate_bc_to_resolution(bc_r6, res)
        
        # Merge into H3 file
        df = df.merge(bc_agg, left_on=H3_COL_ID, right_on='h3_id', how='left')
        
        # Drop duplicate h3_id column from merge
        if 'h3_id' in df.columns and 'h3_id' != H3_COL_ID:
            df.drop(columns=['h3_id'], inplace=True)
        
        # Fill missing with median (cells with no R6 coverage get neutral cost)
        # Using median instead of 0 prevents blank hexes from appearing empty
        median_cost = bc_agg['bc_unit_cost_ft'].median()
        df['bc_unit_cost_ft'].fillna(median_cost, inplace=True)
        
        # Save
        df.to_csv(h3_file, index=False)
        log.info(f"    Saved: {h3_file.name}")
        
        # Log coverage
        bc_cells_with_data = (df['bc_unit_cost_ft'] != median_cost).sum()
        log.info(f"    Cells with R6 data: {bc_cells_with_data:,} ({bc_cells_with_data/len(df)*100:.1f}%)")
        log.info(f"    Cells with median fill: {len(df) - bc_cells_with_data:,} (${median_cost:.2f}/ft)")
    
    log.info("H3 Build Cost generation complete.")


if __name__ == "__main__":
    generate_h3_build_cost()
