"""
LightSignal — transform_dc.py
==============================
Transforms raw dc_consolidated.csv into a clean dc_points.csv
ready for H3 aggregation and KML marker building.

What this script does:
  1. Reads dc_consolidated.csv from data/raw/inputs/
  2. Parses JSON-wrapped Status and Company Type fields
  3. Normalises blank Company Type → "Unclassified"
  4. Handles the 3 known dual-status records by taking first status
  5. Filters out records missing lat/lon (none expected but defensive)
  6. Casts numeric fields to float
  7. Writes clean dc_points.csv to data/processed/

Run directly:
  python scripts/transform/transform_dc.py

Or called automatically by:
  python scripts/run_all.py
"""

import sys
import json
import re
import logging
from pathlib import Path

import pandas as pd

# ── Path setup ────────────────────────────────────────────────────────────────
# Works whether run directly or imported by run_all.py
SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from utils.config import (
    FILE_DC, FILE_DC_POINTS,
    DC_FIELD_ID, DC_FIELD_NAME, DC_FIELD_OPERATOR,
    DC_FIELD_COMPANY_TYPE, DC_FIELD_STATUS,
    DC_FIELD_TOTAL_SQFT, DC_FIELD_COLO_SQFT,
    DC_FIELD_POWER_CAP_MW, DC_FIELD_ESTIMATED_MW,
    DC_FIELD_ADDRESS, DC_FIELD_CITY, DC_FIELD_STATE,
    DC_FIELD_POSTAL, DC_FIELD_LAT, DC_FIELD_LON,
    DC_COMPANY_UNCLASSIFIED,
    DC_STATUS_OPERATIONAL, DC_STATUS_UNDER_CONSTRUCTION,
    DC_STATUS_PLANNED, DC_STATUS_WITHDRAWN,
    DC_STATUS_LAND_BANK, DC_STATUS_UNKNOWN, DC_STATUS_CLOSED,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Known valid status values ─────────────────────────────────────────────────
VALID_STATUSES = {
    DC_STATUS_OPERATIONAL,
    DC_STATUS_UNDER_CONSTRUCTION,
    DC_STATUS_PLANNED,
    DC_STATUS_WITHDRAWN,
    DC_STATUS_LAND_BANK,
    DC_STATUS_UNKNOWN,
    DC_STATUS_CLOSED,
}


def parse_json_field(value: str) -> list:
    """
    Parses a JSON-array-wrapped string field from the SharePoint export.
    Examples:
        '["Operational"]'          → ["Operational"]
        '["Hyperscale"]'           → ["Hyperscale"]
        '["Planned","Under Construction"]' → ["Planned", "Under Construction"]
        ''  or  None               → []
    """
    if not value or not isinstance(value, str):
        return []
    value = value.strip()
    if not value:
        return []
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(v).strip() for v in parsed if v]
        return [str(parsed).strip()]
    except (json.JSONDecodeError, ValueError):
        # Fallback: strip brackets and quotes manually
        cleaned = re.sub(r'[\[\]"]', '', value)
        parts = [p.strip() for p in cleaned.split(",") if p.strip()]
        return parts


def resolve_status(status_list: list) -> str:
    """
    Resolves a list of status values to a single canonical status.

    - Single value → return it directly
    - Multiple values (dual-status data quality issue) → take the first
      recognised valid status; log a warning
    - Empty list → return DC_STATUS_UNKNOWN
    """
    if not status_list:
        return DC_STATUS_UNKNOWN

    # Filter to known valid values
    valid = [s for s in status_list if s in VALID_STATUSES]

    if len(valid) == 1:
        return valid[0]

    if len(valid) > 1:
        log.warning(
            f"Dual-status record found: {status_list} — "
            f"using first value: '{valid[0]}'. "
            f"Clean this in source data."
        )
        return valid[0]

    # No recognised status — return first raw value with a warning
    log.warning(f"Unrecognised status value(s): {status_list} — marking as Unknown")
    return DC_STATUS_UNKNOWN


def parse_numeric(value) -> float:
    """
    Safely parses a numeric field that may contain commas (e.g., "240,549").
    Returns 0.0 on failure.
    """
    if value is None or value == "":
        return 0.0
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return 0.0


def transform_dc():
    log.info("=" * 55)
    log.info("  LightSignal — DC Transform")
    log.info("=" * 55)
    log.info(f"  Input : {FILE_DC}")
    log.info(f"  Output: {FILE_DC_POINTS}")

    # ── 1. Load raw file ──────────────────────────────────────────────────────
    log.info("Loading raw DC file...")
    if not FILE_DC.exists():
        log.error(f"Input file not found: {FILE_DC}")
        log.error("Place dc_consolidated.csv in data/raw/inputs/ and re-run.")
        sys.exit(1)

    df = pd.read_csv(FILE_DC, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    log.info(f"  Loaded {len(df):,} records, {len(df.columns)} columns")

    # ── 2. Parse JSON-wrapped Status and Company Type ─────────────────────────
    log.info("Parsing Status and Company Type fields...")

    df["_status_list"] = df[DC_FIELD_STATUS].apply(parse_json_field)
    df["_ctype_list"]  = df[DC_FIELD_COMPANY_TYPE].apply(parse_json_field)

    df["status_clean"] = df["_status_list"].apply(resolve_status)
    df["company_type_clean"] = df["_ctype_list"].apply(
        lambda lst: lst[0] if lst else DC_COMPANY_UNCLASSIFIED
    )

    # Normalise blanks → Unclassified
    df["company_type_clean"] = df["company_type_clean"].apply(
        lambda v: DC_COMPANY_UNCLASSIFIED if not v.strip() else v
    )

    # ── 3. Status distribution log ────────────────────────────────────────────
    status_counts = df["status_clean"].value_counts()
    log.info("  Status distribution:")
    for status, count in status_counts.items():
        log.info(f"    {status:<30} {count:>5,}")

    company_counts = df["company_type_clean"].value_counts()
    log.info("  Company Type distribution:")
    for ctype, count in company_counts.items():
        log.info(f"    {ctype:<35} {count:>5,}")

    # ── 4. Cast numeric fields ────────────────────────────────────────────────
    log.info("Casting numeric fields...")
    df["lat"]          = df[DC_FIELD_LAT].apply(parse_numeric)
    df["lon"]          = df[DC_FIELD_LON].apply(parse_numeric)
    df["estimated_mw"] = df[DC_FIELD_ESTIMATED_MW].apply(parse_numeric)
    df["total_sqft"]   = df[DC_FIELD_TOTAL_SQFT].apply(parse_numeric)
    df["colo_sqft"]    = df[DC_FIELD_COLO_SQFT].apply(parse_numeric)
    df["power_cap_mw"] = df[DC_FIELD_POWER_CAP_MW].apply(parse_numeric)

    # ── 5. Filter missing lat/lon ─────────────────────────────────────────────
    missing_latlon = df[(df["lat"] == 0.0) & (df["lon"] == 0.0)]
    if len(missing_latlon) > 0:
        log.warning(
            f"  {len(missing_latlon):,} records have lat=0 and lon=0 — "
            f"these will be excluded from KML output."
        )
        log.warning("  Asset IDs excluded:")
        for aid in missing_latlon[DC_FIELD_ID].tolist():
            log.warning(f"    {aid}")
        df = df[~((df["lat"] == 0.0) & (df["lon"] == 0.0))].copy()
    else:
        log.info("  All records have valid lat/lon — no exclusions.")

    # ── 6. Build clean output dataframe ──────────────────────────────────────
    log.info("Building clean output...")
    out = pd.DataFrame({
        "asset_id":         df[DC_FIELD_ID],
        "name":             df[DC_FIELD_NAME],
        "operator":         df[DC_FIELD_OPERATOR],
        "company_type":     df["company_type_clean"],
        "status":           df["status_clean"],
        "estimated_mw":     df["estimated_mw"],
        "total_sqft":       df["total_sqft"],
        "colo_sqft":        df["colo_sqft"],
        "power_cap_mw":     df["power_cap_mw"],
        "address":          df[DC_FIELD_ADDRESS],
        "city":             df[DC_FIELD_CITY],
        "state":            df[DC_FIELD_STATE],
        "postal_code":      df[DC_FIELD_POSTAL],
        "lat":              df["lat"],
        "lon":              df["lon"],
    })

    # ── 7. Write output ───────────────────────────────────────────────────────
    FILE_DC_POINTS.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(FILE_DC_POINTS, index=False, encoding="utf-8")

    log.info(f"  Written {len(out):,} records → {FILE_DC_POINTS}")
    log.info("DC transform complete.")

    return out


if __name__ == "__main__":
    transform_dc()
