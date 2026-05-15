"""
LightSignal — transform_build_cost.py
======================================
Transforms hex_master_r6.csv into bc_r6_clean.csv using the improved
bore cost calculation from Bore_Cost_LongHaul.xlsx.

FORMULA:
  unit_cost_ft = base_cost × diam_factor × soil_multiplier × bedrock_adj × slope_factor

Where:
  - base_cost: $23/ft (soft), $45/ft (mixed), $70/ft (rock)
  - diam_factor: 1.081231 (constant for 5" diameter bore)
  - soil_multiplier: 1 + (rock_frag_pct × 0.018)  ← CONTINUOUS, not discrete
  - bedrock_adj: 1.20 if depth < 100cm, 1.00 if 100-300cm, 0.92 if >= 300cm
  - slope_factor: 1.0 if slope <= 5%, else 1 + ((slope_pct - 5) × 0.020)

All constants are defined in utils/config.py and can be adjusted there.

This calculation is more accurate than the old discrete soil_multiplier
because it accounts for continuous variation in rock fragment percentage
and includes slope/bedrock adjustments.

Output: bc_r6_clean.csv with h3_id and unit_cost_ft columns
"""

import sys
import logging
from pathlib import Path

import pandas as pd
import numpy as np

# ── Path setup ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from utils.config import (
    FILE_HEX, PROCESSED_DIR,
    BC_BASE_COST_SOFT, BC_BASE_COST_MIXED, BC_BASE_COST_ROCK,
    BC_DIAM_FACTOR, BC_SOIL_COEFF,
    BC_BEDROCK_LOW_CM, BC_BEDROCK_MID_CM,
    BC_BEDROCK_ADJ_LOW, BC_BEDROCK_ADJ_MID, BC_BEDROCK_ADJ_HIGH,
    BC_SLOPE_THRESHOLD, BC_SLOPE_COEFF,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Output ────────────────────────────────────────────────────────────────────
FILE_BC_CLEAN = PROCESSED_DIR / "bc_r6_clean.csv"


def calc_bore_cost_per_ft(row):
    """
    Calculate bore cost per foot using improved continuous formula.
    
    Args:
        row: DataFrame row with columns:
            - soil_rock_class: 'soft', 'mixed', or 'rock'
            - soil_rock_frag_pct: % rock fragments (0-100)
            - soil_slope_pct: % slope
            - soil_depth_to_bedrock_cm: depth to bedrock in cm
    
    Returns:
        float: unit_cost_ft in $/ft
    """
    # Base cost by rock class
    rock_class = row.get('soil_rock_class', 'soft')
    if rock_class == 'mixed':
        base_cost = BC_BASE_COST_MIXED
    elif rock_class == 'rock':
        base_cost = BC_BASE_COST_ROCK
    else:
        base_cost = BC_BASE_COST_SOFT
    
    # Continuous soil multiplier (accounts for rock fragment percentage)
    rock_frag_pct = row.get('soil_rock_frag_pct', 0.0)
    if pd.isna(rock_frag_pct):
        rock_frag_pct = 0.0
    soil_mult = 1 + (rock_frag_pct * BC_SOIL_COEFF)
    
    # Bedrock adjustment (shallower = harder/more expensive)
    depth_cm = row.get('soil_depth_to_bedrock_cm')
    if pd.isna(depth_cm) or depth_cm == 0:
        bedrock_adj = BC_BEDROCK_ADJ_LOW  # Conservative: treat missing as shallow
    elif depth_cm < BC_BEDROCK_LOW_CM:
        bedrock_adj = BC_BEDROCK_ADJ_LOW
    elif depth_cm < BC_BEDROCK_MID_CM:
        bedrock_adj = BC_BEDROCK_ADJ_MID
    else:
        bedrock_adj = BC_BEDROCK_ADJ_HIGH
    
    # Slope factor (steeper = harder/more expensive)
    slope_pct = row.get('soil_slope_pct', 0.0)
    if pd.isna(slope_pct) or slope_pct <= BC_SLOPE_THRESHOLD:
        slope_factor = 1.0
    else:
        slope_factor = 1 + ((slope_pct - BC_SLOPE_THRESHOLD) * BC_SLOPE_COEFF)
    
    # Final cost per foot
    unit_cost_ft = base_cost * BC_DIAM_FACTOR * soil_mult * bedrock_adj * slope_factor
    
    return unit_cost_ft


def transform_build_cost():
    log.info("=" * 55)
    log.info("  LightSignal — Transform Build Cost (v2)")
    log.info("=" * 55)
    
    if not FILE_HEX.exists():
        log.error(f"  Input not found: {FILE_HEX}")
        log.error(f"  Place hex_master_r6.csv in data/raw/inputs/")
        sys.exit(1)
    
    log.info(f"  Loading: {FILE_HEX.name}")
    df = pd.read_csv(FILE_HEX, dtype={'h3_r6': str})
    log.info(f"  Rows: {len(df):,}")
    
    # Check for required columns
    required_cols = ['h3_r6', 'soil_rock_class', 'soil_rock_frag_pct', 
                     'soil_slope_pct', 'soil_depth_to_bedrock_cm']
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        log.error(f"  Missing columns: {missing}")
        log.error(f"  Check that hex_master_r6.csv has the expected schema.")
        sys.exit(1)
    
    log.info("  Calculating unit_cost_ft using improved formula...")
    df['unit_cost_ft'] = df.apply(calc_bore_cost_per_ft, axis=1)
    
    # Summary stats
    log.info(f"  unit_cost_ft statistics:")
    log.info(f"    Min:    ${df['unit_cost_ft'].min():.2f}/ft")
    log.info(f"    Max:    ${df['unit_cost_ft'].max():.2f}/ft")
    log.info(f"    Mean:   ${df['unit_cost_ft'].mean():.2f}/ft")
    log.info(f"    Median: ${df['unit_cost_ft'].median():.2f}/ft")
    
    # Distribution by rock class
    if 'soil_rock_class' in df.columns:
        log.info("  Mean cost by rock class:")
        for rc in ['soft', 'mixed', 'rock']:
            subset = df[df['soil_rock_class'] == rc]
            if len(subset) > 0:
                log.info(f"    {rc:>6}: ${subset['unit_cost_ft'].mean():>6.2f}/ft  (n={len(subset):,})")
    
    # Write clean output with just h3_id and unit_cost_ft
    output = df[['h3_r6', 'unit_cost_ft']].copy()
    output.rename(columns={'h3_r6': 'h3_id'}, inplace=True)
    
    FILE_BC_CLEAN.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(FILE_BC_CLEAN, index=False)
    log.info(f"  Written: {FILE_BC_CLEAN.name} ({len(output):,} rows)")
    log.info("Transform build cost complete.")


if __name__ == "__main__":
    transform_build_cost()
