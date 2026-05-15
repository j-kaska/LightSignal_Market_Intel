"""
LightSignal — generate_newsletter.py
=====================================
Generates the weekly LightSignal Market Intel HTML newsletter.

Usage:
    python scripts/generate_newsletter.py            # end date = today
    python scripts/generate_newsletter.py --date 2026-03-07

Output:
    output/newsletters/LightSignal_Weekly_YYYY-MM-DD.html

Date window: end_date - 6 days through end_date (7 days inclusive).
Score: combined = Strategy_Alignment_Score + Relevance_Score (max 10).
"""

import sys
import json
import logging
import argparse
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd

SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent
sys.path.insert(0, str(ROOT / "scripts"))

from utils.config import (
    FILE_ARTICLES,
    FILE_DC,
    OUTPUT_NEWSLETTERS_DIR,
    ART_FIELD_ID,
    ART_FIELD_TITLE,
    ART_FIELD_URL,
    ART_FIELD_SOURCE,
    ART_FIELD_DATE,
    ART_FIELD_SUMMARY,
    ART_FIELD_CATEGORY,
    ART_FIELD_DC_ID,
    ART_FIELD_IS_DUPLICATE,
    ART_FIELD_STRATEGY_SCORE,
    ART_FIELD_RELEVANCE_SCORE,
    DC_FIELD_ID,
    DC_FIELD_NAME,
    DC_FIELD_OPERATOR,
    DC_FIELD_CITY,
    DC_FIELD_STATE,
    DC_FIELD_ESTIMATED_MW,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Category badge colors ──────────────────────────────────────────────────────
CATEGORY_COLORS = {
    "Data Center Development":        {"bg": "#1a2a3a", "border": "#3498DB", "text": "#5dade2"},
    "Fiber & Network Infrastructure": {"bg": "#1a2e2a", "border": "#1ABC9C", "text": "#1abc9c"},
    "Hyperscaler Strategy":           {"bg": "#261a3a", "border": "#9B59B6", "text": "#a569bd"},
    "M&A & Capital Markets":          {"bg": "#2e2210", "border": "#F39C12", "text": "#f0b429"},
    "Power & Utilities":              {"bg": "#2e1a1a", "border": "#E74C3C", "text": "#e74c3c"},
    "Regulatory & Community Pushback":{"bg": "#2e2010", "border": "#E67E22", "text": "#e67e22"},
    "Technology & Architecture":      {"bg": "#1a2a1e", "border": "#27AE60", "text": "#2ecc71"},
}
DEFAULT_CATEGORY_COLORS = {"bg": "#1e1e2e", "border": "#444", "text": "#aaa"}

TOP_N     = 10
MIN_SCORE = 7   # combined score threshold (out of 10); stories below this are excluded


# ═══════════════════════════════════════════════════════════════════════════════
# DATA LOADING
# ═══════════════════════════════════════════════════════════════════════════════

def load_articles(end_date: datetime) -> pd.DataFrame:
    """Load news_feed.csv, filter to 7-day window, compute combined score."""
    try:
        df = pd.read_csv(FILE_ARTICLES, encoding="utf-8-sig", low_memory=False)
    except Exception as e:
        log.error(f"Failed to read news_feed.csv: {e}")
        sys.exit(1)

    # Drop confirmed duplicates
    dup_col = ART_FIELD_IS_DUPLICATE
    if dup_col in df.columns:
        df = df[df[dup_col].astype(str).str.upper() != "TRUE"].copy()

    # Parse dates
    df[ART_FIELD_DATE] = pd.to_datetime(df[ART_FIELD_DATE], errors="coerce")
    df = df.dropna(subset=[ART_FIELD_DATE])

    # Filter to 7-day window (end_date inclusive, normalized to midnight)
    end_dt   = pd.Timestamp(end_date.date())
    start_dt = end_dt - timedelta(days=6)
    df = df[(df[ART_FIELD_DATE].dt.normalize() >= start_dt) &
            (df[ART_FIELD_DATE].dt.normalize() <= end_dt)].copy()

    log.info(f"  Date window: {start_dt.date()} – {end_dt.date()}")
    log.info(f"  Articles in window (after dedup): {len(df)}")

    # Scores — fill missing with 0
    for col in [ART_FIELD_STRATEGY_SCORE, ART_FIELD_RELEVANCE_SCORE]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)

    df["combined_score"] = df[ART_FIELD_STRATEGY_SCORE] + df[ART_FIELD_RELEVANCE_SCORE]

    return df


def load_dc_lookup() -> dict:
    """Load dc_consolidated.csv and return Asset_ID → dict of DC details."""
    try:
        dc = pd.read_csv(FILE_DC, encoding="utf-8-sig", low_memory=False)
    except Exception as e:
        log.error(f"Failed to read dc_consolidated.csv: {e}")
        return {}

    lookup = {}
    for _, row in dc.iterrows():
        asset_id = str(row.get(DC_FIELD_ID, "")).strip()
        if not asset_id:
            continue
        mw = row.get(DC_FIELD_ESTIMATED_MW, None)
        try:
            mw_val = f"{float(mw):,.0f} MW" if pd.notna(mw) else "—"
        except (ValueError, TypeError):
            mw_val = "—"
        lookup[asset_id] = {
            "name":     str(row.get(DC_FIELD_NAME,     "")).strip() or "Unknown",
            "operator": str(row.get(DC_FIELD_OPERATOR, "")).strip() or "Unknown",
            "city":     str(row.get(DC_FIELD_CITY,     "")).strip() or "—",
            "state":    str(row.get(DC_FIELD_STATE,    "")).strip() or "—",
            "mw":       mw_val,
        }
    log.info(f"  DC lookup loaded: {len(lookup)} records")
    return lookup


def parse_dc_ids(raw: str) -> list:
    """Parse DC_IDs from a cell value.
    Handles JSON arrays (['DC-123']), plain strings ('DC-123'),
    and comma-separated strings ('DC-123, DC-456').
    Returns a list of non-empty ID strings.
    """
    if not raw or pd.isna(raw):
        return []
    raw = str(raw).strip()
    if raw in ("[]", "", "nan", "None"):
        return []
    # Try JSON array first
    try:
        ids = json.loads(raw)
        return [str(i).strip() for i in ids if str(i).strip()]
    except (json.JSONDecodeError, TypeError):
        pass
    # Fall back to comma-separated plain string
    return [part.strip() for part in raw.split(",") if part.strip()]


def get_dc_stories(df: pd.DataFrame) -> pd.DataFrame:
    """DC-tagged stories with combined_score >= MIN_SCORE, up to TOP_N, sorted desc."""
    dc_mask = df[ART_FIELD_DC_ID].apply(lambda x: len(parse_dc_ids(x)) > 0)
    dc_df   = (df[dc_mask & (df["combined_score"] >= MIN_SCORE)]
               .sort_values("combined_score", ascending=False)
               .head(TOP_N))
    log.info(f"  DC-linked stories (score >= {MIN_SCORE}): {len(dc_df)}")
    return dc_df


def get_other_stories(df: pd.DataFrame, dc_stories: pd.DataFrame) -> pd.DataFrame:
    """All stories NOT in dc_stories with combined_score >= MIN_SCORE, up to TOP_N, sorted desc."""
    exclude_ids = set(dc_stories[ART_FIELD_ID].astype(str))
    other_df = (df[~df[ART_FIELD_ID].astype(str).isin(exclude_ids) &
                   (df["combined_score"] >= MIN_SCORE)]
                .sort_values("combined_score", ascending=False)
                .head(TOP_N))
    log.info(f"  Other stories (score >= {MIN_SCORE}, excl. DC section): {len(other_df)}")
    return other_df


# ═══════════════════════════════════════════════════════════════════════════════
# HTML BUILDING
# ═══════════════════════════════════════════════════════════════════════════════

def category_badge_html(category: str) -> str:
    colors = CATEGORY_COLORS.get(category, DEFAULT_CATEGORY_COLORS)
    return (
        f'<span class="cat-badge" style="'
        f'background:{colors["bg"]};border-color:{colors["border"]};color:{colors["text"]}">'
        f'{category}</span>'
    )


def _fmt_day(dt) -> str:
    """Format a date as 'Mar 3, 2026' without leading zero on day."""
    return dt.strftime("%b ") + str(int(dt.strftime("%d"))) + dt.strftime(", %Y")


def format_date(ts) -> str:
    try:
        return _fmt_day(pd.Timestamp(ts))
    except Exception:
        return "—"


def dc_mini_list_html(dc_ids: list, dc_lookup: dict) -> str:
    if not dc_ids:
        return ""
    rows = []
    for dc_id in dc_ids:
        info = dc_lookup.get(dc_id)
        if info:
            rows.append(
                f'<div class="dc-row">'
                f'<span class="dc-name">{info["name"]}</span>'
                f'<span class="dc-meta">{info["operator"]} &middot; {info["city"]}, {info["state"]} &middot; {info["mw"]}</span>'
                f'</div>'
            )
        else:
            rows.append(f'<div class="dc-row"><span class="dc-name">{dc_id}</span><span class="dc-meta">Details unavailable</span></div>')
    return f'<div class="dc-list">{"".join(rows)}</div>'


def story_card_html(rank: int, row: pd.Series, dc_lookup: dict, show_dc: bool) -> str:
    title    = str(row.get(ART_FIELD_TITLE,    "Untitled")).strip()
    url      = str(row.get(ART_FIELD_URL,      "#")).strip()
    summary  = str(row.get(ART_FIELD_SUMMARY,  "")).strip()
    pub_date = format_date(row.get(ART_FIELD_DATE))
    category = str(row.get(ART_FIELD_CATEGORY, "")).strip()
    strat    = int(row.get(ART_FIELD_STRATEGY_SCORE,  0))
    relev    = int(row.get(ART_FIELD_RELEVANCE_SCORE, 0))
    combined = int(row.get("combined_score", 0))

    badge_html  = category_badge_html(category) if category else ""
    dc_ids      = parse_dc_ids(row.get(ART_FIELD_DC_ID, "")) if show_dc else []
    dc_html     = dc_mini_list_html(dc_ids, dc_lookup) if (show_dc and dc_ids) else ""

    return f"""
    <div class="story-card">
      <div class="card-header">
        <span class="rank">#{rank}</span>
        <div class="card-meta">
          {badge_html}
          <span class="pub-date">{pub_date}</span>
        </div>
        <div class="score-pill">
          <span class="score-label">Score</span>
          <span class="score-value">{combined}<span class="score-max">/10</span></span>
        </div>
      </div>
      <a href="{url}" target="_blank" rel="noopener" class="story-title">{title}</a>
      <p class="story-summary">{summary}</p>
      <div class="card-footer">
        <span class="score-breakdown">Strategy: {strat}/5 &nbsp;&middot;&nbsp; Relevance: {relev}/5</span>
      </div>
      {dc_html}
    </div>"""


def section_html(title: str, subtitle: str, stories_df: pd.DataFrame, dc_lookup: dict, show_dc: bool) -> str:
    if stories_df.empty:
        cards = '<p class="empty-notice">No stories found for this period.</p>'
    else:
        cards = "".join(
            story_card_html(i + 1, row, dc_lookup, show_dc)
            for i, (_, row) in enumerate(stories_df.iterrows())
        )
    return f"""
  <section class="nl-section">
    <div class="section-header">
      <h2 class="section-title">{title}</h2>
      <p class="section-subtitle">{subtitle}</p>
    </div>
    <div class="story-list">
      {cards}
    </div>
  </section>"""


def build_html(dc_stories: pd.DataFrame, other_stories: pd.DataFrame,
               dc_lookup: dict, end_date: datetime) -> str:
    end_dt   = end_date.date()
    start_dt = end_dt - timedelta(days=6)
    date_range = (
        f"{start_dt.strftime('%b ')+str(int(start_dt.strftime('%d')))} \u2013 "
        f"{end_dt.strftime('%b ')+str(int(end_dt.strftime('%d')))+end_dt.strftime(', %Y')}"
    )
    generated_at = datetime.now().strftime("%B %d, %Y at %I:%M %p")

    hex_svg = """<svg width="28" height="32" viewBox="0 0 28 32" fill="none" xmlns="http://www.w3.org/2000/svg">
      <polygon points="14,1 26,8 26,24 14,31 2,24 2,8" fill="none" stroke="#E0FF00" stroke-width="2"/>
      <polygon points="14,7 21,11 21,21 14,25 7,21 7,11" fill="#E0FF00" fill-opacity="0.15" stroke="#E0FF00" stroke-width="1"/>
    </svg>"""

    sec1 = section_html(
        "Top Data Center Stories",
        f"DC-tagged stories scoring \u2265 7/10 from the past 7 days",
        dc_stories, dc_lookup, show_dc=True
    )
    sec2 = section_html(
        "All Other Stories",
        f"Remaining stories scoring \u2265 7/10 this week, excluding those shown above",
        other_stories, dc_lookup, show_dc=False
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LightSignal — {date_range}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    background: #0d0d1a;
    color: #ddd;
    font-family: "Segoe UI", Arial, sans-serif;
    font-size: 14px;
    line-height: 1.6;
    min-height: 100vh;
  }}

  /* ── Header ── */
  .nl-header {{
    background: linear-gradient(135deg, #0d0d1a 0%, #13132a 60%, #0d0d1a 100%);
    border-bottom: 1px solid #252540;
    padding: 0;
  }}
  .header-inner {{
    max-width: 900px;
    margin: 0 auto;
    padding: 32px 24px 28px;
    display: flex;
    align-items: center;
    gap: 20px;
  }}
  .logo-block {{
    display: flex;
    align-items: center;
    gap: 14px;
    flex: 1;
  }}
  .logo-text-group {{
    display: flex;
    flex-direction: column;
    gap: 1px;
  }}
  .logo-name {{
    font-size: 26px;
    font-weight: 700;
    letter-spacing: 3px;
    color: #E0FF00;
    line-height: 1;
    text-transform: uppercase;
  }}
  .logo-sub {{
    font-size: 11px;
    letter-spacing: 2.5px;
    color: #888;
    text-transform: uppercase;
    font-weight: 500;
  }}
  .header-right {{
    text-align: right;
  }}
  .date-range {{
    font-size: 15px;
    font-weight: 600;
    color: #ccc;
    letter-spacing: 0.3px;
  }}
  .date-label {{
    font-size: 10px;
    color: #555;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    margin-bottom: 2px;
  }}

  /* ── Layout ── */
  .nl-body {{
    max-width: 900px;
    margin: 0 auto;
    padding: 40px 24px 60px;
    display: flex;
    flex-direction: column;
    gap: 56px;
  }}

  /* ── Section ── */
  .nl-section {{
    display: flex;
    flex-direction: column;
    gap: 16px;
  }}
  .section-header {{
    border-left: 3px solid #E0FF00;
    padding-left: 14px;
    margin-bottom: 4px;
  }}
  .section-title {{
    font-size: 18px;
    font-weight: 700;
    color: #E0FF00;
    letter-spacing: 0.5px;
    text-transform: uppercase;
  }}
  .section-subtitle {{
    font-size: 11px;
    color: #555;
    margin-top: 3px;
    letter-spacing: 0.3px;
  }}

  /* ── Story Cards ── */
  .story-list {{
    display: flex;
    flex-direction: column;
    gap: 12px;
  }}
  .story-card {{
    background: #161628;
    border: 1px solid #252540;
    border-radius: 8px;
    padding: 18px 20px 14px;
    transition: border-color 0.15s;
  }}
  .story-card:hover {{
    border-color: #3a3a5a;
  }}
  .card-header {{
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 10px;
    flex-wrap: wrap;
  }}
  .rank {{
    font-size: 13px;
    font-weight: 700;
    color: #E0FF00;
    min-width: 28px;
    flex-shrink: 0;
  }}
  .card-meta {{
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 1;
    flex-wrap: wrap;
  }}
  .pub-date {{
    font-size: 11px;
    color: #555;
    white-space: nowrap;
  }}
  .score-pill {{
    display: flex;
    align-items: baseline;
    gap: 4px;
    background: #1a1a10;
    border: 1px solid #E0FF0044;
    border-radius: 20px;
    padding: 2px 10px;
    flex-shrink: 0;
  }}
  .score-label {{
    font-size: 10px;
    color: #888;
    letter-spacing: 0.5px;
    text-transform: uppercase;
  }}
  .score-value {{
    font-size: 14px;
    font-weight: 700;
    color: #E0FF00;
    line-height: 1;
  }}
  .score-max {{
    font-size: 10px;
    color: #888;
    font-weight: 400;
  }}
  .cat-badge {{
    display: inline-block;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.4px;
    padding: 2px 9px;
    border-radius: 10px;
    border: 1px solid transparent;
    white-space: nowrap;
    text-transform: uppercase;
  }}
  .story-title {{
    display: block;
    font-size: 15px;
    font-weight: 600;
    color: #ccc;
    text-decoration: none;
    line-height: 1.4;
    margin-bottom: 8px;
    transition: color 0.15s;
  }}
  .story-title:hover {{
    color: #E0FF00;
  }}
  .story-summary {{
    font-size: 13px;
    color: #888;
    line-height: 1.65;
    margin-bottom: 10px;
  }}
  .card-footer {{
    display: flex;
    align-items: center;
    padding-top: 8px;
    border-top: 1px solid #1e1e30;
  }}
  .score-breakdown {{
    font-size: 11px;
    color: #444;
    letter-spacing: 0.2px;
  }}

  /* ── DC Mini List ── */
  .dc-list {{
    background: #0f0f20;
    border: 1px solid #1e1e35;
    border-radius: 6px;
    margin-top: 12px;
    overflow: hidden;
  }}
  .dc-row {{
    display: flex;
    align-items: baseline;
    gap: 10px;
    padding: 7px 14px;
    border-bottom: 1px solid #1a1a30;
  }}
  .dc-row:last-child {{
    border-bottom: none;
  }}
  .dc-row::before {{
    content: "⬡";
    color: #E0FF00;
    font-size: 10px;
    flex-shrink: 0;
    opacity: 0.7;
  }}
  .dc-name {{
    font-size: 12px;
    font-weight: 600;
    color: #bbb;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 260px;
  }}
  .dc-meta {{
    font-size: 11px;
    color: #555;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}

  /* ── Empty state ── */
  .empty-notice {{
    color: #444;
    font-style: italic;
    padding: 20px 0;
  }}

  /* ── Footer ── */
  .nl-footer {{
    border-top: 1px solid #1a1a2e;
    margin-top: 20px;
  }}
  .footer-inner {{
    max-width: 900px;
    margin: 0 auto;
    padding: 20px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
  }}
  .footer-brand {{
    font-size: 11px;
    font-weight: 600;
    color: #E0FF00;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    opacity: 0.6;
  }}
  .footer-meta {{
    font-size: 10px;
    color: #333;
    text-align: right;
  }}

  @media (max-width: 640px) {{
    .header-inner {{ flex-direction: column; align-items: flex-start; }}
    .header-right {{ text-align: left; }}
    .dc-row {{ flex-direction: column; gap: 2px; }}
    .dc-meta {{ white-space: normal; }}
  }}
</style>
</head>
<body>

<header class="nl-header">
  <div class="header-inner">
    <div class="logo-block">
      {hex_svg}
      <div class="logo-text-group">
        <span class="logo-name">LightSignal</span>
        <span class="logo-sub">Lightpath Market Intel</span>
      </div>
    </div>
    <div class="header-right">
      <div class="date-label">Week of</div>
      <div class="date-range">{date_range}</div>
    </div>
  </div>
</header>

<main class="nl-body">
  {sec1}
  {sec2}
</main>

<footer class="nl-footer">
  <div class="footer-inner">
    <span class="footer-brand">LightSignal</span>
    <span class="footer-meta">Generated {generated_at} &nbsp;&middot;&nbsp; Lightpath Strategic Intelligence</span>
  </div>
</footer>

</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Generate LightSignal weekly newsletter")
    parser.add_argument(
        "--date",
        help="End date for the 7-day window (YYYY-MM-DD). Defaults to today.",
        default=None,
    )
    args = parser.parse_args()

    if args.date:
        try:
            end_date = datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            log.error("--date must be in YYYY-MM-DD format")
            sys.exit(1)
    else:
        end_date = datetime.today()

    log.info("=" * 60)
    log.info("LightSignal Newsletter Generator")
    log.info(f"End date: {end_date.date()}")
    log.info("=" * 60)

    # Load data
    log.info("Loading articles...")
    df = load_articles(end_date)

    log.info("Loading DC lookup...")
    dc_lookup = load_dc_lookup()

    if df.empty:
        log.warning("No articles found in the 7-day window. Newsletter will show empty sections.")

    # Build sections
    log.info("Building sections...")
    dc_stories    = get_dc_stories(df)
    other_stories = get_other_stories(df, dc_stories)

    # Build HTML
    log.info("Rendering HTML...")
    html = build_html(dc_stories, other_stories, dc_lookup, end_date)

    # Write output
    OUTPUT_NEWSLETTERS_DIR.mkdir(parents=True, exist_ok=True)
    filename    = f"LightSignal_Weekly_{end_date.strftime('%Y-%m-%d')}.html"
    output_path = OUTPUT_NEWSLETTERS_DIR / filename

    try:
        output_path.write_text(html, encoding="utf-8")
    except Exception as e:
        log.error(f"Failed to write newsletter: {e}")
        sys.exit(1)

    log.info("=" * 60)
    log.info(f"Newsletter saved: {output_path}")
    log.info(f"  DC stories:    {len(dc_stories)}")
    log.info(f"  Other stories: {len(other_stories)}")
    log.info("=" * 60)


def generate_newsletter(end_date=None):
    """Callable entry point for use by run_all.py (no argparse)."""
    from datetime import datetime as _dt
    if end_date is None:
        end_dt = _dt.today()
    elif isinstance(end_date, str):
        end_dt = _dt.strptime(end_date, "%Y-%m-%d")
    else:
        end_dt = end_date

    log.info("=" * 60)
    log.info("LightSignal Newsletter Generator")
    log.info(f"End date: {end_dt.date()}")
    log.info("=" * 60)

    df        = load_articles(end_dt)
    dc_lookup = load_dc_lookup()

    if df.empty:
        log.warning("No articles found in the 7-day window. Newsletter will show empty sections.")

    dc_stories    = get_dc_stories(df)
    other_stories = get_other_stories(df, dc_stories)
    html          = build_html(dc_stories, other_stories, dc_lookup, end_dt)

    OUTPUT_NEWSLETTERS_DIR.mkdir(parents=True, exist_ok=True)
    filename    = f"LightSignal_Weekly_{end_dt.strftime('%Y-%m-%d')}.html"
    output_path = OUTPUT_NEWSLETTERS_DIR / filename
    output_path.write_text(html, encoding="utf-8")

    log.info("=" * 60)
    log.info(f"Newsletter saved: {output_path}")
    log.info(f"  DC stories:    {len(dc_stories)}")
    log.info(f"  Other stories: {len(other_stories)}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
