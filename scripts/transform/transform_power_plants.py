"""
LightSignal — transform_power_plants.py
========================================
Transforms raw eia_generators_latest.xlsx into a clean
power_plants_points.csv ready for H3 aggregation and KML marker building.

What this script does:
  1. Reads Operating and Planned sheets from EIA Excel file
  2. Skips the title row (row 1) and blank row (row 2) to find real headers
  3. Aggregates generator-level rows to plant-level (one row per Plant ID)
  4. Sums Nameplate Capacity (MW) across all generators per plant
  5. Assigns dominant technology group by MW
  6. Labels each plant as Operational or Planned
  7. Plants in both sheets → classified as Operational
  8. Filters out records missing lat/lon (~2 expected)
  9. Writes clean power_plants_points.csv to data/processed/

Run directly:
  python scripts/transform/transform_power_plants.py

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

from utils.config import (
    FILE_EIA, FILE_PP_POINTS,
    EIA_SHEET_OPERATING, EIA_SHEET_PLANNED,
    EIA_FIELD_PLANT_ID, EIA_FIELD_PLANT_NAME,
    EIA_FIELD_STATE, EIA_FIELD_COUNTY,
    EIA_FIELD_NAMEPLATE_MW, EIA_FIELD_TECHNOLOGY,
    EIA_FIELD_STATUS, EIA_FIELD_LAT, EIA_FIELD_LON,
    PP_FIELD_STATUS_LABEL, PP_FIELD_TECH_GROUP,
    PP_STATUS_OPERATIONAL, PP_STATUS_PLANNED,
    PP_TECH_GROUPS, PP_TECH_DEFAULT,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def find_header_row(sheet_df: pd.DataFrame) -> int:
    """
    EIA Excel files have a title row (row 0) and a blank row (row 1)
    before the real column headers appear. This function finds the first
    row that contains 'Plant ID' and returns its index.
    Returns -1 if not found.
    """
    for i, row in sheet_df.iterrows():
        if any(str(v).strip() == "Plant ID" for v in row.values):
            return i
    return -1


def load_eia_sheet(filepath: Path, sheet_name: str) -> pd.DataFrame:
    """
    Loads one sheet from the EIA Excel file, skipping the title and
    blank rows to return a properly-headed DataFrame.
    """
    log.info(f"  Loading sheet: '{sheet_name}'...")

    # Load without headers first so we can find the real header row
    raw = pd.read_excel(
        filepath,
        sheet_name=sheet_name,
        header=None,
        dtype=str,
    )

    header_idx = find_header_row(raw)
    if header_idx == -1:
        raise ValueError(
            f"Could not find header row in sheet '{sheet_name}'. "
            f"Expected a row containing 'Plant ID'."
        )

    log.info(f"    Header row found at index {header_idx}")

    # Re-load using the correct header row
    df = pd.read_excel(
        filepath,
        sheet_name=sheet_name,
        header=header_idx,
        dtype=str,
    )

    # Drop any completely empty rows
    df = df.dropna(how="all").copy()

    # Strip whitespace from column names
    df.columns = [str(c).strip() for c in df.columns]

    log.info(f"    {len(df):,} generator rows, {len(df.columns)} columns")
    return df


def assign_tech_group(tech: str) -> str:
    """
    Maps an EIA technology string to one of the 6 consolidated groups.
    Falls back to PP_TECH_DEFAULT if not found in mapping.
    """
    if not tech or not isinstance(tech, str):
        return PP_TECH_DEFAULT
    return PP_TECH_GROUPS.get(tech.strip(), PP_TECH_DEFAULT)


def safe_float(value) -> float:
    """Safely converts a value to float. Returns 0.0 on failure."""
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return 0.0


def aggregate_to_plants(df: pd.DataFrame, status_label: str) -> pd.DataFrame:
    """
    Aggregates generator-level rows to plant-level.

    For each Plant ID:
      - Nameplate MW  = sum across all generators
      - Tech group    = group of the generator with highest Nameplate MW
      - Lat/Lon       = first non-null value across generators
      - Plant Name    = first value
      - State/County  = first value
      - EIA statuses  = comma-joined list of all unique generator statuses
      - Status label  = the passed-in label (Operational or Planned)
    """
    df = df.copy()

    # Cast numeric fields
    df["_mw"] = df[EIA_FIELD_NAMEPLATE_MW].apply(safe_float)
    df["_lat"] = df[EIA_FIELD_LAT].apply(safe_float)
    df["_lon"] = df[EIA_FIELD_LON].apply(safe_float)

    # Assign tech group per generator
    df["_tech_group"] = df[EIA_FIELD_TECHNOLOGY].apply(assign_tech_group)

    plants = []

    for plant_id, group in df.groupby(EIA_FIELD_PLANT_ID):
        # Sum MW
        total_mw = group["_mw"].sum()

        # Dominant tech group = tech group with highest total MW in this plant
        tech_mw = group.groupby("_tech_group")["_mw"].sum()
        dominant_tech = tech_mw.idxmax() if not tech_mw.empty else PP_TECH_DEFAULT

        # Lat/lon — take first non-zero value
        lat_vals = group[group["_lat"] != 0.0]["_lat"]
        lon_vals = group[group["_lon"] != 0.0]["_lon"]
        lat = lat_vals.iloc[0] if not lat_vals.empty else 0.0
        lon = lon_vals.iloc[0] if not lon_vals.empty else 0.0

        # Metadata — first value
        plant_name = group[EIA_FIELD_PLANT_NAME].iloc[0]
        state      = group[EIA_FIELD_STATE].iloc[0]
        county     = group[EIA_FIELD_COUNTY].iloc[0] if EIA_FIELD_COUNTY in group.columns else ""

        # All unique EIA generator statuses (for popup display)
        eia_statuses = ", ".join(
            sorted(group[EIA_FIELD_STATUS].dropna().unique())
        ) if EIA_FIELD_STATUS in group.columns else ""

        # Generator count
        gen_count = len(group)

        plants.append({
            "plant_id":         plant_id,
            "plant_name":       plant_name,
            "state":            state,
            "county":           county,
            "nameplate_mw":     total_mw,
            "tech_group":       dominant_tech,
            PP_FIELD_STATUS_LABEL: status_label,
            "eia_statuses":     eia_statuses,
            "generator_count":  gen_count,
            "lat":              lat,
            "lon":              lon,
        })

    result = pd.DataFrame(plants)
    log.info(
        f"    Aggregated {len(df):,} generators → "
        f"{len(result):,} unique plants"
    )
    return result


def transform_power_plants():
    log.info("=" * 55)
    log.info("  LightSignal — Power Plants Transform")
    log.info("=" * 55)
    log.info(f"  Input : {FILE_EIA}")
    log.info(f"  Output: {FILE_PP_POINTS}")

    # ── 1. Check input file exists ────────────────────────────────────────────
    if not FILE_EIA.exists():
        log.error(f"Input file not found: {FILE_EIA}")
        log.error(
            "Place the EIA Excel file in data/raw/inputs/ "
            "renamed to eia_generators_latest.xlsx and re-run."
        )
        sys.exit(1)

    # ── 2. Load both sheets ───────────────────────────────────────────────────
    log.info("Loading EIA sheets...")
    df_operating = load_eia_sheet(FILE_EIA, EIA_SHEET_OPERATING)
    df_planned   = load_eia_sheet(FILE_EIA, EIA_SHEET_PLANNED)

    # ── 3. Aggregate to plant level ───────────────────────────────────────────
    log.info("Aggregating to plant level...")
    plants_op   = aggregate_to_plants(df_operating, PP_STATUS_OPERATIONAL)
    plants_plan = aggregate_to_plants(df_planned,   PP_STATUS_PLANNED)

    # ── 4. Handle overlap — plants in both sheets → Operational ──────────────
    overlap_ids = set(plants_op["plant_id"]) & set(plants_plan["plant_id"])
    if overlap_ids:
        log.info(
            f"  {len(overlap_ids):,} plants appear in both sheets "
            f"→ classified as Operational. Removing from Planned."
        )
        plants_plan = plants_plan[
            ~plants_plan["plant_id"].isin(overlap_ids)
        ].copy()

    # ── 5. Combine ────────────────────────────────────────────────────────────
    combined = pd.concat([plants_op, plants_plan], ignore_index=True)
    log.info(f"  Combined total: {len(combined):,} plants")

    # ── 6. Status distribution ────────────────────────────────────────────────
    status_counts = combined[PP_FIELD_STATUS_LABEL].value_counts()
    log.info("  Status distribution:")
    for status, count in status_counts.items():
        log.info(f"    {status:<20} {count:>6,}")

    tech_counts = combined["tech_group"].value_counts()
    log.info("  Technology group distribution:")
    for tech, count in tech_counts.items():
        log.info(f"    {tech:<20} {count:>6,}")

    # ── 7. Filter missing lat/lon ─────────────────────────────────────────────
    missing = combined[(combined["lat"] == 0.0) & (combined["lon"] == 0.0)]
    if len(missing) > 0:
        log.warning(
            f"  {len(missing):,} plants have lat=0 and lon=0 "
            f"— excluded from KML output."
        )
        log.warning("  Plant IDs excluded:")
        for pid in missing["plant_id"].tolist():
            log.warning(f"    {pid}")
        combined = combined[
            ~((combined["lat"] == 0.0) & (combined["lon"] == 0.0))
        ].copy()
    else:
        log.info("  All plants have valid lat/lon — no exclusions.")

    # ── 8. MW range sanity check ──────────────────────────────────────────────
    log.info(
        f"  Nameplate MW range: "
        f"{combined['nameplate_mw'].min():.1f} – "
        f"{combined['nameplate_mw'].max():.1f}"
    )
    log.info(
        f"  Total nameplate MW: "
        f"{combined['nameplate_mw'].sum():,.0f} MW"
    )

    # ── 9. Write output ───────────────────────────────────────────────────────
    FILE_PP_POINTS.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(FILE_PP_POINTS, index=False, encoding="utf-8")

    log.info(f"  Written {len(combined):,} records → {FILE_PP_POINTS}")
    log.info("Power Plants transform complete.")

    return combined


if __name__ == "__main__":
    transform_power_plants()
