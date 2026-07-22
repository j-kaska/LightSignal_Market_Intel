"""
LightSignal — Central Configuration
=====================================
All file paths, field names, color values, weights, mappings,
and constants for the LightSignal pipeline live here.

To adjust weights, colors, or mappings: edit this file only.
A full pipeline re-run will pick up all changes.

Run from project root:  python scripts/run_all.py
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load API keys from .env file in project root (never commit .env to version control)
load_dotenv()

# ═══════════════════════════════════════════════════════════════════════════════
# PROJECT IDENTITY
# ═══════════════════════════════════════════════════════════════════════════════

KMZ_OUTPUT_FILENAME = "LightSignal.kmz"

# ═══════════════════════════════════════════════════════════════════════════════
# PATHS  (all relative to project root — fully portable)
# ═══════════════════════════════════════════════════════════════════════════════

# Project root = parent of the scripts/ folder
ROOT = Path(__file__).parent.parent.parent

# ── Raw inputs ────────────────────────────────────────────────────────────────
INPUTS_DIR              = ROOT / "data" / "raw" / "inputs"
ARCHIVE_DIR             = ROOT / "data" / "archive" / "inputs"

FILE_DC                 = INPUTS_DIR / "dc_consolidated.csv"
FILE_ARTICLES           = INPUTS_DIR / "news_feed.csv"
FILE_EIA                = INPUTS_DIR / "eia_generators_latest.xlsx"
FILE_HEX                = INPUTS_DIR / "hex_master_r6.csv"
FILE_PUSHBACK           = INPUTS_DIR / "dc_pushback.csv"

# ── Processed outputs ─────────────────────────────────────────────────────────
PROCESSED_DIR           = ROOT / "data" / "processed"

FILE_DC_POINTS          = PROCESSED_DIR / "dc_points.csv"
FILE_PP_POINTS          = PROCESSED_DIR / "power_plants_points.csv"
FILE_ARTICLES_DC        = PROCESSED_DIR / "articles_dc_linked.csv"
FILE_ARTICLES_STATE     = PROCESSED_DIR / "articles_state_linked.csv"
FILE_STATE_CENTROIDS    = PROCESSED_DIR / "state_centroids.csv"
FILE_PUSHBACK_CLEAN     = PROCESSED_DIR / "dc_pushback_clean.csv"

H3_DIR                  = PROCESSED_DIR / "h3"
FILE_H3_R3              = H3_DIR / "h3_r3.csv"
FILE_H3_R4              = H3_DIR / "h3_r4.csv"
FILE_H3_R5              = H3_DIR / "h3_r5.csv"

H3_FILES = {3: FILE_H3_R3, 4: FILE_H3_R4, 5: FILE_H3_R5}

# ── Article pipeline paths ────────────────────────────────────────────────────
ARTICLES_DIR            = ROOT / "data" / "articles"
ARTICLES_STAGING_DIR    = ARTICLES_DIR / "staging"
ARTICLES_LOG_DIR        = ARTICLES_DIR / "logs"

FILE_RSS_FEEDS          = ROOT / "scripts" / "articles" / "rss_feeds.txt"
FILE_STAGED             = ARTICLES_STAGING_DIR / "staged_articles.csv"
FILE_SEEN_URLS          = ARTICLES_STAGING_DIR / "seen_urls.json"
FILE_DUPLICATE_CACHE    = ARTICLES_STAGING_DIR / "duplicate_cache.json"
FILE_TITLE_CACHE        = ARTICLES_STAGING_DIR / "title_cache.json"
FILE_SEEN_CLEAN_URLS    = ARTICLES_STAGING_DIR / "seen_clean_urls.json"

# DC candidate review workflow (dc_candidates.py → human review → apply_dc_approvals.py).
# dc_candidates.py suggests article→facility matches into the queue; a human copies
# approved rows into the approvals file and sets review_status=approved; then
# apply_dc_approvals.py patches DC_ID into news_feed.csv. DC_ID is never auto-assigned
# (see docs/agent_studio_instructions.md).
FILE_DC_REVIEW_QUEUE    = ARTICLES_STAGING_DIR / "dc_review_queue.csv"
FILE_DC_REVIEW_OVERFLOW = ARTICLES_STAGING_DIR / "dc_review_queue_overflow.csv"
FILE_DC_APPROVALS       = ARTICLES_STAGING_DIR / "dc_review_queue_approved.csv"

# Handoff point — article pipeline writes here, run_all.py reads from here
# FILE_NEWS_FEED and FILE_ARTICLES point to the same file — both names used
# in different parts of the codebase for clarity
FILE_NEWS_FEED          = INPUTS_DIR / "news_feed.csv"
FILE_NEWS_FEED_ROLLING_BACKUP = ARCHIVE_DIR / "news_feed_latest.csv"

# ── Output ────────────────────────────────────────────────────────────────────
OUTPUT_LATEST_DIR       = ROOT / "output" / "latest"
FILE_KMZ_LATEST         = OUTPUT_LATEST_DIR / KMZ_OUTPUT_FILENAME
OUTPUT_NEWSLETTERS_DIR  = ROOT / "output" / "newsletters"

# ── SharePoint Publish (OneDrive Sync) ────────────────────────────────────────
# Set this to the local OneDrive-synced path of your SharePoint document library
# folder where index.html and lib.js should be published.
#
# How to find this path:
#   1. In SharePoint, open the document library where you want to host LightSignal
#   2. Click "Sync" in the toolbar — OneDrive will sync it to your local machine
#   3. Open File Explorer, navigate to the synced folder
#   4. Copy the full path and paste it below (use forward slashes or raw string)
#
# Example:
#   SHAREPOINT_SYNC_PATH = Path(r"C:\Users\jkaska\Altice USA\Major Infrastructure Solutions - Documents\LightSignal Dashboard")
#
# Leave as None to skip publishing (default).
SHAREPOINT_SYNC_PATH    = None

# ── Article pipeline models ───────────────────────────────────────────────────
# Set LIGHTSIGNAL_PROVIDER=anthropic in environment (or via test_pipeline --provider)
# to switch from Gemini to Claude without code changes.
API_PROVIDER                = os.environ.get("LIGHTSIGNAL_PROVIDER", "gemini")

# Gemini 2 Flash: unlimited RPD on paid tier, ~$0.75/month at 150 articles/day.
# Requires billing enabled in Google AI Studio (free tier caps at 20 RPD — unusable).
# Set LIGHTSIGNAL_PROVIDER=anthropic to use Claude Haiku instead.
ARTICLES_MODEL              = "gemini-2.5-flash-lite"
GEMINI_BASE_URL             = "https://generativelanguage.googleapis.com/v1beta/openai/"

ANTHROPIC_MODEL             = "claude-haiku-4-5-20251001"

# Summarize/classify are independent per-article network calls, so they run
# concurrently. Gemini frequently returns 503 "model overloaded"; concurrency lets
# those backoffs overlap instead of stacking up serially.
# Lower this if you start seeing 429s.
LLM_MAX_WORKERS             = int(os.environ.get("LIGHTSIGNAL_LLM_WORKERS", "8"))
# SDK-level retries for 5xx/429. The SDK uses fast jittered backoff (sub-second),
# which absorbs transient 503s far more cheaply than the stage-level retry loop.
LLM_SDK_RETRIES             = 2

# A summarize failure used to be terminal: summarize_status stayed "failed",
# classify_status stayed "pending" forever, and _prune_staged (which only removes
# classify_status="success" rows) could never clear them. A Gemini outage on
# 2026-04-21..28 stranded 1,138 articles this way — they never reached the
# dashboard. Failed rows are now retried automatically on later runs, up to this
# many attempts, so a transient outage heals itself instead of losing a week.
SUMMARIZE_MAX_ATTEMPTS      = 3

SENTENCE_TRANSFORMER_MODEL  = "all-MiniLM-L6-v2"

# ── Duplicate detection ───────────────────────────────────────────────────────
DUPLICATE_THRESHOLD      = 0.85   # cosine similarity — lowered from 0.88 to catch more cross-outlet variants
DUPLICATE_WINDOW_DAYS    = 14     # rolling window for semantic duplicate cache (was 7)

TITLE_DEDUP_THRESHOLD    = 85     # rapidfuzz token_sort_ratio (0–100)
TITLE_DEDUP_WINDOW_DAYS  = 14

# ── DC candidate matching (dc_candidates.py) ──────────────────────────────────
# Minimum rapidfuzz partial_ratio (0–100) for a DC-name-vs-article-text match to
# enter the review queue. partial_ratio scores the best-aligned substring, so it
# is lenient — set high to keep the human queue precise, since every row is a
# suggestion a person must approve. Tune against real queue output.
DC_CANDIDATE_THRESHOLD   = 90
# Cap on the reviewable queue per run; lower-confidence overflow spills to
# dc_review_queue_overflow.csv so the review file stays a manageable size.
MAX_REVIEW_QUEUE_SIZE    = 200

# URLs containing any of these substrings are dropped at fetch time (before staging)
BLOCKED_URL_PATTERNS     = ["youtube.com", "youtu.be"]

# Title substrings that identify YouTube content arriving via Google Alerts redirects
# (raw_url is a Google News link, so URL pattern won't catch these)
BLOCKED_TITLE_PATTERNS   = [" - YouTube", "| YouTube", " — YouTube", "- YouTube"]

# Domains filtered after clean URL resolution — pure investment/financial publications
# with no market-intelligence value for Lightpath infrastructure tracking
BLOCKED_SOURCE_DOMAINS   = [
    "seekingalpha.com",
    "fool.com",          # The Motley Fool
    "benzinga.com",
    "thestreet.com",
    "tastylive.com",
]

# ── Article pipeline geography ────────────────────────────────────────────────
CORE_FOOTPRINT = {"NY", "NJ", "CT", "MA", "PA", "OH", "FL", "AZ"}

EXPANSION_MARKETS = {
    "TX",
    "WI", "IL", "MO", "IN", "MI", "VA", "WV",
    "UT",
}

# ── Daily summary email ───────────────────────────────────────────────────────
# Sent via Outlook desktop (win32com) — no SMTP credentials required
SUMMARY_EMAIL_TO         = ""     # e.g. "jkaska@yourcompany.com" — leave blank to disable
SUMMARY_EMAIL_FROM       = ""     # leave blank to use default Outlook account

# ── Docs ──────────────────────────────────────────────────────────────────────

# ═══════════════════════════════════════════════════════════════════════════════
# DATA CENTER FIELD NAMES  (dc_consolidated.csv)
# ═══════════════════════════════════════════════════════════════════════════════

DC_FIELD_ID             = "Asset_ID"
DC_FIELD_NAME           = "Canonical_Name"
DC_FIELD_OPERATOR       = "Operator"
DC_FIELD_COMPANY_TYPE   = "Company Type"
DC_FIELD_STATUS         = "Status"
DC_FIELD_TOTAL_SQFT     = "Total Space (sqft)"
DC_FIELD_COLO_SQFT      = "Colocation Space (sqft)"
DC_FIELD_POWER_CAP_MW   = "Power Capacity (MW)"
DC_FIELD_ESTIMATED_MW   = "estimated_mw"
DC_FIELD_ADDRESS        = "street_address"
DC_FIELD_CITY           = "city"
DC_FIELD_STATE          = "state"
DC_FIELD_POSTAL         = "postal_code"
DC_FIELD_LAT            = "latitude"
DC_FIELD_LON            = "longitude"

# ═══════════════════════════════════════════════════════════════════════════════
# DC PUSHBACK FIELD NAMES  (dc_pushback.csv)
# ═══════════════════════════════════════════════════════════════════════════════

PUSHBACK_FIELD_ACTION_TYPE       = "action_type"
PUSHBACK_FIELD_ISSUE_CATEGORY    = "issue_category"
PUSHBACK_FIELD_STATUS            = "status"
PUSHBACK_FIELD_COMMUNITY_OUTCOME = "community_outcome"
PUSHBACK_FIELD_DEVELOPMENT_OUTCOME = "development_outcome"

# Canonical action_type labels from dashboard picklist
PUSHBACK_ACTION_ZONING           = "Zoning Restriction"
PUSHBACK_ACTION_LEGISLATION      = "Legislation"
PUSHBACK_ACTION_MORATORIUM       = "Moratorium"
PUSHBACK_ACTION_PUBLIC_COMMENT   = "Public Comment"
PUSHBACK_ACTION_LAWSUIT          = "Lawsuit"
PUSHBACK_ACTION_WITHDRAWAL       = "Project Withdrawal"
PUSHBACK_ACTION_UTILITY_REG      = "Utility Regulation"
PUSHBACK_ACTION_EXEC_ORDER       = "Executive Order"
PUSHBACK_ACTION_OTHER            = "Other"

# Canonical issue_category labels from dashboard picklist
PUSHBACK_ISSUE_ZONING_LAND_USE    = "Zoning / Land Use"
PUSHBACK_ISSUE_WATER              = "Water"
PUSHBACK_ISSUE_ENVIRONMENTAL      = "Environmental"
PUSHBACK_ISSUE_COMMUNITY_IMPACT   = "Community Impact"
PUSHBACK_ISSUE_GRID_ENERGY        = "Grid / Energy"
PUSHBACK_ISSUE_TRANSPARENCY       = "Transparency"
PUSHBACK_ISSUE_RATEPAYER          = "Ratepayer"
PUSHBACK_ISSUE_NOISE              = "Noise"
PUSHBACK_ISSUE_TAX_INCENTIVE      = "Tax / Incentive"
PUSHBACK_ISSUE_FARMLAND           = "Farmland"
PUSHBACK_ISSUE_TRAFFIC            = "Traffic"
PUSHBACK_ISSUE_DESIGN_STANDARDS   = "Design Standards"
PUSHBACK_ISSUE_CONTRACT_GUARANTEE = "Contract Guarantees"
PUSHBACK_ISSUE_ANTI_AI            = "Anti-AI"
PUSHBACK_ISSUE_OTHER              = "Other"

# Canonical status labels from dashboard picklist (from development perspective)
PUSHBACK_STATUS_PENDING                = "Pending"
PUSHBACK_STATUS_RESOLVED_FAVORABLE     = "Resolved - Favorable for Development"
PUSHBACK_STATUS_RESOLVED_UNFAVORABLE   = "Resolved - Unfavorable for Development"
PUSHBACK_STATUS_RESOLVED_MIXED         = "Resolved - Mixed"
PUSHBACK_OUTCOME_FAVORABLE             = "Favorable for Development"
PUSHBACK_OUTCOME_UNFAVORABLE           = "Unfavorable for Development"
PUSHBACK_OUTCOME_MIXED                 = "Mixed"
PUSHBACK_OUTCOME_PENDING               = "Pending"

# Raw source value -> canonical dashboard label mappings
PUSHBACK_ACTION_MAP = {
    "zoning_restriction": PUSHBACK_ACTION_ZONING,
    "legislation": PUSHBACK_ACTION_LEGISLATION,
    "moratorium": PUSHBACK_ACTION_MORATORIUM,
    "public_comment": PUSHBACK_ACTION_PUBLIC_COMMENT,
    "lawsuit": PUSHBACK_ACTION_LAWSUIT,
    "litigation": PUSHBACK_ACTION_LAWSUIT,
    "project_withdrawal": PUSHBACK_ACTION_WITHDRAWAL,
    "utility_regulation": PUSHBACK_ACTION_UTILITY_REG,
    "regulatory_action": PUSHBACK_ACTION_UTILITY_REG,
    "executive_order": PUSHBACK_ACTION_EXEC_ORDER,
    "ordinance": PUSHBACK_ACTION_LEGISLATION,
    "infrastructure_opposition": PUSHBACK_ACTION_PUBLIC_COMMENT,
    "other_opposition": PUSHBACK_ACTION_PUBLIC_COMMENT,
}

PUSHBACK_ISSUE_MAP = {
    "zoning": PUSHBACK_ISSUE_ZONING_LAND_USE,
    "land_use": PUSHBACK_ISSUE_ZONING_LAND_USE,
    "zoning_land_use": PUSHBACK_ISSUE_ZONING_LAND_USE,
    "water": PUSHBACK_ISSUE_WATER,
    "environmental": PUSHBACK_ISSUE_ENVIRONMENTAL,
    "air_quality": PUSHBACK_ISSUE_ENVIRONMENTAL,
    "community_impact": PUSHBACK_ISSUE_COMMUNITY_IMPACT,
    "property_values": PUSHBACK_ISSUE_COMMUNITY_IMPACT,
    "grid_energy": PUSHBACK_ISSUE_GRID_ENERGY,
    "transparency": PUSHBACK_ISSUE_TRANSPARENCY,
    "ratepayer": PUSHBACK_ISSUE_RATEPAYER,
    "noise": PUSHBACK_ISSUE_NOISE,
    "tax_incentive": PUSHBACK_ISSUE_TAX_INCENTIVE,
    "farmland": PUSHBACK_ISSUE_FARMLAND,
    "traffic": PUSHBACK_ISSUE_TRAFFIC,
    "design_standards": PUSHBACK_ISSUE_DESIGN_STANDARDS,
    "contract_guarantees": PUSHBACK_ISSUE_CONTRACT_GUARANTEE,
    "anti_ai": PUSHBACK_ISSUE_ANTI_AI,
}

# Status values (after parsing JSON wrapper)
DC_STATUS_OPERATIONAL       = "Operational"
DC_STATUS_UNDER_CONSTRUCTION = "Under Construction"
DC_STATUS_PLANNED           = "Planned"
DC_STATUS_WITHDRAWN         = "Withdrawn/In Doubt"
DC_STATUS_LAND_BANK         = "Land Bank"
DC_STATUS_UNKNOWN           = "Unknown"
DC_STATUS_CLOSED            = "Closed"

# Pipeline = Under Construction + Planned (used for H3 aggregation)
DC_PIPELINE_STATUSES = [DC_STATUS_UNDER_CONSTRUCTION, DC_STATUS_PLANNED]

# Company type values (after parsing JSON wrapper)
DC_COMPANY_UNCLASSIFIED     = "Unclassified"   # blank in source → normalized to this

# ═══════════════════════════════════════════════════════════════════════════════
# POWER PLANT FIELD NAMES  (eia_generators_latest.xlsx)
# ═══════════════════════════════════════════════════════════════════════════════

EIA_SHEET_OPERATING         = "Operating"
EIA_SHEET_PLANNED           = "Planned"
                                    # openpyxl skiprows handled in transform script

EIA_FIELD_PLANT_ID          = "Plant ID"
EIA_FIELD_PLANT_NAME        = "Plant Name"
EIA_FIELD_STATE             = "Plant State"
EIA_FIELD_COUNTY            = "County"
EIA_FIELD_NAMEPLATE_MW      = "Nameplate Capacity (MW)"
EIA_FIELD_TECHNOLOGY        = "Technology"
EIA_FIELD_STATUS            = "Status"
EIA_FIELD_LAT               = "Latitude"
EIA_FIELD_LON               = "Longitude"

# Derived field added during transform
PP_FIELD_STATUS_LABEL       = "pp_status"   # "Operational" or "Planned"
PP_FIELD_TECH_GROUP         = "tech_group"  # consolidated technology group

# Technology group mapping  (EIA technology string → group label)
PP_TECH_GROUPS = {
    # Natural Gas
    "Natural Gas Fired Combined Cycle":             "Natural Gas",
    "Natural Gas Fired Combustion Turbine":         "Natural Gas",
    "Natural Gas Steam Turbine":                    "Natural Gas",
    "Natural Gas Internal Combustion Engine":       "Natural Gas",
    "Other Natural Gas":                            "Natural Gas",
    "Natural Gas with Compressed Air Storage":      "Natural Gas",
    # Solar
    "Solar Photovoltaic":                           "Solar",
    "Solar Thermal with Energy Storage":            "Solar",
    "Solar Thermal without Energy Storage":         "Solar",
    # Wind
    "Onshore Wind Turbine":                         "Wind",
    "Offshore Wind Turbine":                        "Wind",
    # Coal
    "Conventional Steam Coal":                      "Coal",
    "Coal Integrated Gasification Combined Cycle":  "Coal",
    "Petroleum Coke":                               "Coal",
    # Nuclear
    "Nuclear":                                      "Nuclear",
    # Other / Storage (catch-all)
    "Conventional Hydroelectric":                   "Other/Storage",
    "Hydroelectric Pumped Storage":                 "Other/Storage",
    "Batteries":                                    "Other/Storage",
    "Wood/Wood Waste Biomass":                      "Other/Storage",
    "Other Waste Biomass":                          "Other/Storage",
    "Geothermal":                                   "Other/Storage",
    "Landfill Gas":                                 "Other/Storage",
    "Petroleum Liquids":                            "Other/Storage",
    "Municipal Solid Waste":                        "Other/Storage",
    "Flywheels":                                    "Other/Storage",
    "Other Gases":                                  "Other/Storage",
    "All Other":                                    "Other/Storage",
}
PP_TECH_DEFAULT = "Other/Storage"   # fallback for any unlisted technology

PP_STATUS_OPERATIONAL   = "Operational"
PP_STATUS_PLANNED       = "Planned"

# ═══════════════════════════════════════════════════════════════════════════════
# ARTICLES FIELD NAMES  (news_feed.csv)
# ═══════════════════════════════════════════════════════════════════════════════

ART_FIELD_ID            = "ID"
ART_FIELD_TITLE         = "Title"
ART_FIELD_URL           = "CleanURL"
ART_FIELD_SOURCE        = "Source"
ART_FIELD_DATE          = "PublishedDate"
ART_FIELD_SUMMARY       = "Summary_AI"
ART_FIELD_CATEGORY      = "Primary_Category"
ART_FIELD_STATES        = "States"
ART_FIELD_DC_ID         = "DC_ID"
ART_FIELD_DUPLICATE     = "Is_Duplicate"

# Additional article fields written by the article pipeline
ART_FIELD_STRATEGY_SCORE  = "Strategy_Alignment_Score"
ART_FIELD_RELEVANCE_SCORE = "Relevance_Score"
ART_FIELD_IS_DUPLICATE    = "Is_Duplicate"

# ═══════════════════════════════════════════════════════════════════════════════
# BUILD COST / HEX FIELD NAMES  (hex_master_r6.csv)
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
# H3 SETTINGS
# ═══════════════════════════════════════════════════════════════════════════════

H3_RESOLUTIONS          = [3, 4, 5]

# Column names written to h3_r*.csv files
H3_COL_ID               = "h3_id"

# Raw value columns
H3_COL_DC_OPERATIONAL   = "dc_operational_mw"
H3_COL_DC_PIPELINE      = "dc_pipeline_mw"
H3_COL_DC_TOTAL         = "dc_total_mw"
H3_COL_PP_OPERATIONAL   = "pp_operational_mw"
H3_COL_PP_PLANNED       = "pp_planned_mw"
H3_COL_PP_TOTAL         = "pp_total_mw"
H3_COL_BC               = "bc_unit_cost_ft"  # Bore cost per foot (improved formula)

# Percentile rank columns (0.0–1.0)
H3_COL_DC_OPERATIONAL_PCT   = "dc_operational_pct"
H3_COL_DC_PIPELINE_PCT      = "dc_pipeline_pct"
H3_COL_DC_TOTAL_PCT         = "dc_total_pct"
H3_COL_PP_OPERATIONAL_PCT   = "pp_operational_pct"
H3_COL_PP_PLANNED_PCT       = "pp_planned_pct"
H3_COL_PP_TOTAL_PCT         = "pp_total_pct"
H3_COL_BC_PCT               = "bc_pct"   # INVERTED: easy terrain = high score

# Zero mask
H3_COL_IS_ZERO          = "is_zero"    # True if dc_total_mw=0 AND pp_total_mw=0

# Composite score columns

# ═══════════════════════════════════════════════════════════════════════════════
# COMPOSITE SCORING WEIGHTS
# ═══════════════════════════════════════════════════════════════════════════════
# Must sum to 1.0
# DC = primary demand signal (data center density)
# PP = infrastructure signal (power availability)
# BC = capital efficiency modifier (terrain/build cost)

WEIGHT_DC   = 0.50
WEIGHT_PP   = 0.30
WEIGHT_BC   = 0.20

assert abs(WEIGHT_DC + WEIGHT_PP + WEIGHT_BC - 1.0) < 1e-9, \
    "Composite weights must sum to 1.0"

# Composite definitions: (display_name, dc_pct_col, pp_pct_col, output_col)

# ═══════════════════════════════════════════════════════════════════════════════
# BORE COST FORMULA CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════
# Derived from Bore_Cost_LongHaul.xlsx analysis (Assumptions sheet)
# Formula: unit_cost_ft = base_cost × diam_factor × soil_mult × bedrock_adj × slope_factor

# Base costs by rock class ($/ft)
BC_BASE_COST_SOFT   = 23.0
BC_BASE_COST_MIXED  = 45.0
BC_BASE_COST_ROCK   = 70.0

# Diameter factor (constant for 5" bore)
BC_DIAM_FACTOR      = 1.0812310390375761

# Continuous soil multiplier coefficient (per 1% rock fragments)
BC_SOIL_COEFF       = 0.018
# Formula: soil_multiplier = 1 + (rock_frag_pct × BC_SOIL_COEFF)

# Bedrock depth adjustments
BC_BEDROCK_LOW_CM   = 100.0   # Shallow threshold (cm)
BC_BEDROCK_MID_CM   = 300.0   # Medium threshold (cm)
BC_BEDROCK_ADJ_LOW  = 1.20    # Multiplier if depth < 100cm (hard to bore through)
BC_BEDROCK_ADJ_MID  = 1.00    # Multiplier if 100cm ≤ depth < 300cm
BC_BEDROCK_ADJ_HIGH = 0.92    # Multiplier if depth ≥ 300cm (deep bedrock is easier)

# Slope adjustments
BC_SLOPE_THRESHOLD  = 5.0     # Slope % threshold (below this = no penalty)
BC_SLOPE_COEFF      = 0.020   # Multiplier per % over threshold
# Formula: slope_factor = 1.0 if slope ≤ 5%, else 1 + ((slope - 5) × BC_SLOPE_COEFF)

# ═══════════════════════════════════════════════════════════════════════════════
# COLOR & VISUAL SETTINGS
# ═══════════════════════════════════════════════════════════════════════════════

# H3 fill opacity: 0 = fully transparent, 255 = fully opaque
# 128 = 50% opacity — keeps map labels and marker points visible through H3 fill
H3_OPACITY = 128

# Zero / null cell color (light grey)
COLOR_ZERO_HEX = "#D3D3D3"

# ColorBrewer YlOrRd — 9 buckets, used for ALL H3 layers (individual + composite)
# Format: (min_percentile_exclusive, max_percentile_inclusive, hex_color)
# Bucket 0 (zero/null) handled separately via COLOR_ZERO_HEX
H3_COLOR_BUCKETS = [
    (0.00, 0.11, "#FFFFCC"),   # 1 — Pale Yellow
    (0.11, 0.22, "#FFEDA0"),   # 2 — Light Yellow
    (0.22, 0.33, "#FED976"),   # 3 — Yellow
    (0.33, 0.44, "#FEB24C"),   # 4 — Yellow-Orange
    (0.44, 0.55, "#FD8D3C"),   # 5 — Orange
    (0.55, 0.66, "#FC4E2A"),   # 6 — Orange-Red
    (0.66, 0.77, "#E31A1C"),   # 7 — Red
    (0.77, 0.88, "#BD0026"),   # 8 — Dark Red
    (0.88, 1.00, "#800026"),   # 9 — Deep Red
]

def get_h3_color(percentile_value: float, is_zero: bool = False) -> str:
    """
    Returns the hex color string for a given percentile value.
    Returns COLOR_ZERO_HEX for zero/null cells.
    """
    if is_zero or percentile_value is None or percentile_value <= 0.0:
        return COLOR_ZERO_HEX
    for (lo, hi, color) in H3_COLOR_BUCKETS:
        if lo < percentile_value <= hi:
            return color
    return H3_COLOR_BUCKETS[-1][2]   # cap at deepest red

def hex_to_kml_color(hex_color: str, alpha: int = H3_OPACITY) -> str:
    """
    Converts standard #RRGGBB hex to KML aabbggrr format.
    KML color order is: alpha, blue, green, red (all hex).

    Example: #FEB24C at 50% opacity → 804CB2FE
    """
    hex_color = hex_color.lstrip("#")
    r = hex_color[0:2]
    g = hex_color[2:4]
    b = hex_color[4:6]
    a = format(alpha, "02x")
    return f"{a}{b}{g}{r}".upper()

# ── Marker colors ─────────────────────────────────────────────────────────────
# TBD during visual design phase — placeholder values below
# Replace hex values once final palette is decided

DC_STATUS_COLORS = {
    DC_STATUS_OPERATIONAL:          "#2ECC71",   # Green
    DC_STATUS_PLANNED:              "#3498DB",   # Blue
    DC_STATUS_UNDER_CONSTRUCTION:   "#F39C12",   # Amber
    DC_STATUS_WITHDRAWN:            "#E74C3C",   # Red-Orange
    DC_STATUS_LAND_BANK:            "#9B59B6",   # Purple
    DC_STATUS_UNKNOWN:              "#95A5A6",   # Grey
    DC_STATUS_CLOSED:               "#7F8C8D",   # Dark Grey
}

PP_STATUS_COLORS = {
    PP_STATUS_OPERATIONAL:  "#27AE60",   # Green (distinct shade from DC)
    PP_STATUS_PLANNED:      "#2980B9",   # Blue  (distinct shade from DC)
}

# Pushback heatmap color scheme (red shades for intensity)
PUSHBACK_HEATMAP_COLORS = [
    "#ffffb2",  # very light yellow
    "#fecc5c",  # light yellow-orange
    "#fd8d3c",  # medium orange-red
    "#f03b20",  # darker red
    "#bd0026",  # dark red
]

# ═══════════════════════════════════════════════════════════════════════════════
# MARKER ICON NAMES
# ═══════════════════════════════════════════════════════════════════════════════
# Google Earth compatible PNG filenames (to be placed in a /icons/ folder
# inside the KMZ archive). Shapes TBD — placeholder names below.



# ═══════════════════════════════════════════════════════════════════════════════
# STATE CENTROIDS  (approximate geographic centers for article dot layer)
# ═══════════════════════════════════════════════════════════════════════════════
# Format: { "STATE_ABBR": (latitude, longitude) }

STATE_CENTROIDS = {
    "AL": (32.806671, -86.791130),
    "AK": (61.370716, -152.404419),
    "AZ": (33.729759, -111.431221),
    "AR": (34.969704, -92.373123),
    "CA": (36.116203, -119.681564),
    "CO": (39.059811, -105.311104),
    "CT": (41.597782, -72.755371),
    "DE": (39.318523, -75.507141),
    "FL": (27.766279, -81.686783),
    "GA": (33.040619, -83.643074),
    "HI": (21.094318, -157.498337),
    "ID": (44.240459, -114.478828),
    "IL": (40.349457, -88.986137),
    "IN": (39.849426, -86.258278),
    "IA": (42.011539, -93.210526),
    "KS": (38.526600, -96.726486),
    "KY": (37.668140, -84.670067),
    "LA": (31.169960, -91.867805),
    "ME": (44.693947, -69.381927),
    "MD": (39.063946, -76.802101),
    "MA": (42.230171, -71.530106),
    "MI": (43.326618, -84.536095),
    "MN": (45.694454, -93.900192),
    "MS": (32.741646, -89.678696),
    "MO": (38.456085, -92.288368),
    "MT": (46.921925, -110.454353),
    "NE": (41.125370, -98.268082),
    "NV": (38.313515, -117.055374),
    "NH": (43.452492, -71.563896),
    "NJ": (40.298904, -74.521011),
    "NM": (34.840515, -106.248482),
    "NY": (42.165726, -74.948051),
    "NC": (35.630066, -79.806419),
    "ND": (47.528912, -99.784012),
    "OH": (40.388783, -82.764915),
    "OK": (35.565342, -96.928917),
    "OR": (44.572021, -122.070938),
    "PA": (40.590752, -77.209755),
    "RI": (41.680893, -71.511780),
    "SC": (33.856892, -80.945007),
    "SD": (44.299782, -99.438828),
    "TN": (35.747845, -86.692345),
    "TX": (31.054487, -97.563461),
    "UT": (40.150032, -111.862434),
    "VT": (44.045876, -72.710686),
    "VA": (37.769337, -78.169968),
    "WA": (47.400902, -121.490494),
    "WV": (38.491226, -80.954453),
    "WI": (44.268543, -89.616508),
    "WY": (42.755966, -107.302490),
    "DC": (38.897438, -77.026817),
}
