"""
LightSignal — build_kmz_dc.py
===============================
Builds a Google Earth KMZ containing only the Data Center marker layer.
Articles linked to each DC are embedded in the popup balloon.
No name labels are displayed on the map (too cluttered at 3,000+ points).
Names still appear in the balloon when you click a marker.

Output: output/latest/LightSignal_DC.kmz

Run directly:
  python scripts/kml/build_kmz_dc.py

Or called automatically by:
  python scripts/run_all.py
"""

import sys
import zipfile
import logging
from collections import defaultdict
from pathlib import Path

import pandas as pd

# ── Path setup ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from utils.config import (
    FILE_DC_POINTS, FILE_ARTICLES_DC,
    OUTPUT_LATEST_DIR,
    DC_STATUS_COLORS,
    DC_STATUS_OPERATIONAL, DC_STATUS_UNDER_CONSTRUCTION,
    DC_STATUS_PLANNED, DC_STATUS_WITHDRAWN,
    DC_STATUS_LAND_BANK, DC_STATUS_UNKNOWN, DC_STATUS_CLOSED,
    DC_COMPANY_UNCLASSIFIED,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

OUTPUT_KMZ = OUTPUT_LATEST_DIR / "LightSignal_DC.kmz"

DC_STATUS_ORDER = [
    DC_STATUS_OPERATIONAL,
    DC_STATUS_UNDER_CONSTRUCTION,
    DC_STATUS_PLANNED,
    DC_STATUS_WITHDRAWN,
    DC_STATUS_LAND_BANK,
    DC_STATUS_UNKNOWN,
    DC_STATUS_CLOSED,
]

# Google Earth icon URLs by company type — shape only (color applied via KML tint)
COMPANY_ICONS = {
    DC_COMPANY_UNCLASSIFIED:        "http://maps.google.com/mapfiles/kml/paddle/wht-circle.png",
    "Carrier Neutral/Real Estate":  "http://maps.google.com/mapfiles/kml/paddle/wht-square.png",
    "Hyperscale":                   "http://maps.google.com/mapfiles/kml/paddle/wht-stars.png",
    "Carrier/Mobile/MSO":           "http://maps.google.com/mapfiles/kml/paddle/wht-diamond.png",
    "Miner":                        "http://maps.google.com/mapfiles/kml/paddle/wht-blank.png",
    "Enterprise/Other":             "http://maps.google.com/mapfiles/kml/paddle/wht-blank.png",
}


def hex_to_kml(hex_color: str, alpha: int = 255) -> str:
    """Converts #RRGGBB to KML aabbggrr."""
    h = hex_color.lstrip("#")
    return f"{alpha:02x}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def esc(text) -> str:
    """Escapes XML special characters."""
    if not isinstance(text, str):
        text = str(text) if text is not None else ""
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;"))


def fmt_mw(val) -> str:
    try:
        v = float(val)
        return f"{v:,.1f} MW" if v > 0 else ""
    except (ValueError, TypeError):
        return ""


def fmt_sqft(val) -> str:
    try:
        v = float(val)
        return f"{v:,.0f} sqft" if v > 0 else ""
    except (ValueError, TypeError):
        return ""


def build_popup(row: dict, articles: list) -> str:
    """Builds the balloon HTML for a single DC."""
    name        = esc(row.get("name", "Unknown"))
    status      = esc(row.get("status", ""))
    operator    = esc(row.get("operator", "") or "")
    company     = esc(row.get("company_type", "") or "")
    est_mw      = fmt_mw(row.get("estimated_mw"))
    cap_mw      = fmt_mw(row.get("power_cap_mw"))
    total_sqft  = fmt_sqft(row.get("total_sqft"))
    colo_sqft   = fmt_sqft(row.get("colo_sqft"))
    asset_id    = esc(row.get("asset_id", ""))
    state       = esc(row.get("state", "") or "")
    city        = esc(row.get("city", "") or "")

    status_color = DC_STATUS_COLORS.get(row.get("status", ""), "#95A5A6")

    def row_html(label, value):
        if not value or value in ("nan", "0.0", "0"):
            return ""
        return (
            f'<tr>'
            f'<td style="color:#888;padding:2px 8px 2px 0;white-space:nowrap;">{label}</td>'
            f'<td style="color:#eee;">{value}</td>'
            f'</tr>'
        )

    # Address line
    addr_parts = [p for p in [city, state] if p and p != "nan"]
    addr = ", ".join(addr_parts)

    html = f"""<![CDATA[
<div style="font-family:Arial,sans-serif;font-size:13px;max-width:380px;
            background:#1a1a1a;color:#eee;padding:4px;line-height:1.5;">
  <h3 style="margin:0 0 6px 0;color:#fff;font-size:15px;">{name}</h3>
  <span style="background:{status_color};color:#fff;padding:2px 10px;
               border-radius:10px;font-size:11px;font-weight:bold;">{status}</span>
  <br/><br/>
  <table style="width:100%;border-collapse:collapse;">
    {row_html("Operator", operator)}
    {row_html("Company Type", company)}
    {row_html("Estimated MW", est_mw)}
    {row_html("Power Cap MW", cap_mw)}
    {row_html("Total Space", total_sqft)}
    {row_html("Colo Space", colo_sqft)}
    {row_html("Location", addr)}
    {row_html("Asset ID", asset_id)}
  </table>"""

    if articles:
        html += f"""
  <div style="background:#2a2a2a;color:#FD8D3C;font-weight:bold;
              padding:5px 8px;margin:10px 0 4px 0;border-radius:3px;">
    📰 Related Articles ({len(articles)})
  </div>"""
        for art in articles:
            title = esc(art.get("title", "Article"))
            url   = esc(art.get("url", ""))
            date  = esc(art.get("published_date", ""))
            src   = esc(art.get("source", ""))
            date_str = f' <span style="color:#888;font-size:11px;">— {date}</span>' if date else ""
            src_str  = f' <span style="color:#666;font-size:11px;">({src})</span>' if src else ""
            if url:
                html += f'<div style="margin:4px 0;"><a href="{url}" style="color:#5dade2;">{title}</a>{date_str}{src_str}</div>'
            else:
                html += f'<div style="margin:4px 0;color:#ccc;">{title}{date_str}{src_str}</div>'

    html += "\n</div>\n]]>"
    return html


def build_styles() -> str:
    """KML Style blocks for all status × company type combinations."""
    parts = []
    for status in DC_STATUS_ORDER:
        kml_color   = hex_to_kml(DC_STATUS_COLORS.get(status, "#95A5A6"))
        status_slug = status.lower().replace(" ", "_").replace("/", "_")
        for company, icon_url in COMPANY_ICONS.items():
            company_slug = company.lower().replace(" ", "_").replace("/", "_")
            style_id = f"dc_{status_slug}_{company_slug}"
            parts.append(
                f'<Style id="{style_id}">'
                f'<IconStyle>'
                f'<color>{kml_color}</color>'
                f'<scale>0.65</scale>'
                f'<Icon><href>{icon_url}</href></Icon>'
                f'</IconStyle>'
                f'<LabelStyle>'
                f'<scale>0</scale>'    # ← This hides the name label on the map
                f'</LabelStyle>'
                f'<BalloonStyle>'
                f'<bgColor>ff1a1a1a</bgColor>'
                f'<textColor>ffffffff</textColor>'
                f'<text>$[description]</text>'
                f'</BalloonStyle>'
                f'</Style>'
            )
    return "\n".join(parts)


def get_style_id(status: str, company_type: str) -> str:
    status_slug  = status.lower().replace(" ", "_").replace("/", "_")
    company_slug = company_type.lower().replace(" ", "_").replace("/", "_")
    return f"dc_{status_slug}_{company_slug}"


def build_kml(df_dc: pd.DataFrame, articles_by_dc: dict) -> str:
    """Assembles the full KML document string."""
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2">',
        '<Document>',
        '<name>LightSignal — Data Centers</name>',
        '<open>1</open>',
        build_styles(),
    ]

    # Group by status
    by_status = defaultdict(list)
    for _, row in df_dc.iterrows():
        by_status[row["status"]].append(row.to_dict())

    for status in DC_STATUS_ORDER:
        rows = by_status.get(status, [])
        if not rows:
            continue
        parts.append(
            f'<Folder>'
            f'<name>{esc(status)}  ({len(rows):,})</name>'
            f'<open>0</open>'
            f'<visibility>1</visibility>'
        )
        for row in rows:
            asset_id = str(row.get("asset_id", ""))
            company  = str(row.get("company_type") or DC_COMPANY_UNCLASSIFIED)
            arts     = articles_by_dc.get(asset_id, [])
            style_id = get_style_id(status, company)
            popup    = build_popup(row, arts)
            lat      = float(row["lat"])
            lon      = float(row["lon"])

            # Note: <name> is empty string — removes label from map
            # The actual name is shown in the balloon via build_popup()
            parts.append(
                f'<Placemark>'
                f'<name></name>'
                f'<visibility>1</visibility>'
                f'<description>{popup}</description>'
                f'<styleUrl>#{style_id}</styleUrl>'
                f'<Point><coordinates>{lon},{lat},0</coordinates></Point>'
                f'</Placemark>'
            )
        parts.append('</Folder>')

    parts += ['</Document>', '</kml>']
    return "\n".join(parts)


def build_kmz_dc():
    log.info("=" * 55)
    log.info("  LightSignal — Build DC KMZ")
    log.info("=" * 55)
    log.info(f"  Output: {OUTPUT_KMZ}")

    # ── Load data ─────────────────────────────────────────────────────────────
    for f in [FILE_DC_POINTS, FILE_ARTICLES_DC]:
        if not f.exists():
            log.error(f"Required file not found: {f}")
            log.error("Run transform scripts first.")
            sys.exit(1)

    df_dc    = pd.read_csv(FILE_DC_POINTS)
    df_arts  = pd.read_csv(FILE_ARTICLES_DC)
    log.info(f"  Loaded {len(df_dc):,} DC points, {len(df_arts):,} article links")

    # Build article lookup by DC ID
    articles_by_dc = defaultdict(list)
    for _, row in df_arts.iterrows():
        dc_id = str(row.get("dc_id", "")).strip()
        if dc_id:
            articles_by_dc[dc_id].append({
                "title":          row.get("title", ""),
                "url":            row.get("url", ""),
                "published_date": row.get("published_date", ""),
                "source":         row.get("source", ""),
            })

    dcs_with_articles = sum(1 for v in articles_by_dc.values() if v)
    log.info(f"  DCs with linked articles: {dcs_with_articles}")

    # ── Build KML ─────────────────────────────────────────────────────────────
    log.info("  Building KML...")
    kml = build_kml(df_dc, articles_by_dc)
    kml_size_mb = len(kml) / 1_000_000
    log.info(f"  KML size: {kml_size_mb:.1f} MB")

    # ── Package as KMZ ────────────────────────────────────────────────────────
    OUTPUT_LATEST_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUTPUT_KMZ, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as kmz:
        kmz.writestr("doc.kml", kml)

    kmz_size_mb = OUTPUT_KMZ.stat().st_size / 1_000_000
    log.info(f"  Written LightSignal_DC.kmz ({kmz_size_mb:.1f} MB) → {OUTPUT_KMZ}")
    log.info("DC KMZ build complete.")

    return OUTPUT_KMZ


if __name__ == "__main__":
    build_kmz_dc()
