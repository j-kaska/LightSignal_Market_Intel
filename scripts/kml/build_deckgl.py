"""
LightSignal — build_deckgl.py  (dashboard edition)
====================================================
Full interactive dashboard with:
  - 4 live KPI cards filtered by state/status/type
  - Sidebar: layer toggles, H3 selector, R3/R4/R5, DC status, DC company type, PP status, state list
  - H3 layers: static national view, dims non-selected states
  - Marker layers: fully filtered
  - Articles slide-out drawer with date range toggle
  - Map flies to selected state
  - Clear Filters button in header

Output: output/latest/LightSignal/index.html + lib.js
"""

import sys, json, logging, urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd

SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from utils.config import (
    FILE_DC_POINTS, FILE_PP_POINTS,
    FILE_ARTICLES_DC, FILE_ARTICLES_STATE,
    H3_FILES, H3_RESOLUTIONS,
    OUTPUT_LATEST_DIR,
    H3_COL_ID,
    H3_COL_DC_OPERATIONAL, H3_COL_DC_PIPELINE, H3_COL_DC_TOTAL,
    H3_COL_PP_OPERATIONAL, H3_COL_PP_PLANNED,  H3_COL_PP_TOTAL,
    H3_COL_BC,
    H3_COL_DC_OPERATIONAL_PCT, H3_COL_DC_PIPELINE_PCT, H3_COL_DC_TOTAL_PCT,
    H3_COL_PP_OPERATIONAL_PCT, H3_COL_PP_PLANNED_PCT,  H3_COL_PP_TOTAL_PCT,
    H3_COL_BC_PCT,
    H3_COL_IS_ZERO,
    DC_STATUS_COLORS, PP_STATUS_COLORS,
    H3_COLOR_BUCKETS, COLOR_ZERO_HEX,
    PP_FIELD_STATUS_LABEL,
    DC_STATUS_OPERATIONAL, DC_STATUS_UNDER_CONSTRUCTION,
    DC_STATUS_PLANNED, DC_STATUS_WITHDRAWN,
    DC_STATUS_LAND_BANK, DC_STATUS_UNKNOWN, DC_STATUS_CLOSED,
    OUTPUT_NEWSLETTERS_DIR,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  [%(levelname)s]  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

OUTPUT_DIR   = OUTPUT_LATEST_DIR / "LightSignal"
OUTPUT_HTML  = OUTPUT_DIR / "index.html"
BUNDLE_CACHE = SCRIPT_DIR / "deckgl_bundle.js"

JS_URLS = [
    ("h3-js",        "https://unpkg.com/h3-js@3.7.2/dist/h3-js.umd.js"),
    ("supercluster", "https://unpkg.com/supercluster@8.0.1/dist/supercluster.min.js"),
    ("deck.gl",      "https://unpkg.com/deck.gl@8.9.35/dist.min.js"),
]

def get_bundle():
    if BUNDLE_CACHE.exists():
        size = BUNDLE_CACHE.stat().st_size / 1_000_000
        log.info(f"  Using cached JS bundle ({size:.1f} MB)")
        return BUNDLE_CACHE.read_text(encoding="utf-8")
    log.info("  First run: downloading JS bundle (one-time, then cached)...")
    parts = []
    for name, url in JS_URLS:
        log.info(f"    Downloading {name}...")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=90) as r:
                content = r.read().decode("utf-8", errors="replace")
            parts.append(f"/* === {name} === */\n{content}\n")
            log.info(f"    {name}: {len(content)/1_000_000:.1f} MB")
        except Exception as e:
            log.error(f"    Failed to download {name}: {e}")
            sys.exit(1)
    bundle = "\n".join(parts)
    BUNDLE_CACHE.write_text(bundle, encoding="utf-8")
    log.info(f"  Bundle cached ({len(bundle)/1_000_000:.1f} MB)")
    return bundle

def hex_to_rgb(h):
    h = h.lstrip("#")
    return [int(h[0:2],16), int(h[2:4],16), int(h[4:6],16)]

YLORD_RGB     = [hex_to_rgb(c) for _,_,c in H3_COLOR_BUCKETS]
ZERO_RGB      = hex_to_rgb(COLOR_ZERO_HEX)
DC_STATUS_RGB = {s: hex_to_rgb(c) for s,c in DC_STATUS_COLORS.items()}
PP_TECH_RGB   = {
    "Natural Gas":   hex_to_rgb("#E74C3C"),
    "Solar":         hex_to_rgb("#F1C40F"),
    "Wind":          hex_to_rgb("#3498DB"),
    "Coal":          hex_to_rgb("#7F8C8D"),
    "Nuclear":       hex_to_rgb("#9B59B6"),
    "Other/Storage": hex_to_rgb("#1ABC9C"),
}

STATE_BOUNDS = {
    "AL":[-88.47,30.22,-84.89,35.01],"AK":[-179.15,51.21,-129.98,71.35],
    "AZ":[-114.82,31.33,-109.04,37.00],"AR":[-94.62,33.00,-89.64,36.50],
    "CA":[-124.41,32.53,-114.13,42.01],"CO":[-109.06,36.99,-102.04,41.00],
    "CT":[-73.73,40.98,-71.79,42.05],"DE":[-75.79,38.45,-75.05,39.84],
    "FL":[-87.63,24.54,-80.03,31.00],"GA":[-85.61,30.36,-80.84,35.00],
    "HI":[-160.25,18.92,-154.81,22.24],"ID":[-117.24,41.99,-111.04,49.00],
    "IL":[-91.51,36.97,-87.50,42.51],"IN":[-88.10,37.77,-84.78,41.76],
    "IA":[-96.64,40.38,-90.14,43.50],"KS":[-102.05,36.99,-94.59,40.00],
    "KY":[-89.57,36.50,-81.96,39.15],"LA":[-94.04,28.93,-88.82,33.02],
    "ME":[-71.08,43.06,-66.95,47.46],"MD":[-79.49,37.91,-75.05,39.72],
    "MA":[-73.51,41.24,-69.93,42.89],"MI":[-90.42,41.70,-82.41,48.19],
    "MN":[-97.24,43.50,-89.49,49.38],"MS":[-91.65,30.17,-88.10,35.01],
    "MO":[-95.77,35.99,-89.10,40.61],"MT":[-116.05,44.36,-104.04,49.00],
    "NE":[-104.05,39.99,-95.31,43.00],"NV":[-120.00,35.00,-114.04,42.00],
    "NH":[-72.56,42.70,-70.70,45.31],"NJ":[-75.56,38.93,-73.89,41.36],
    "NM":[-109.05,31.33,-103.00,37.00],"NY":[-79.76,40.49,-71.86,45.02],
    "NC":[-84.32,33.84,-75.46,36.59],"ND":[-104.05,45.94,-96.55,49.00],
    "OH":[-84.82,38.40,-80.52,41.98],"OK":[-103.00,33.62,-94.43,37.00],
    "OR":[-124.57,41.99,-116.46,46.24],"PA":[-80.52,39.72,-74.69,42.27],
    "RI":[-71.86,41.15,-71.12,42.02],"SC":[-83.35,32.05,-78.55,35.22],
    "SD":[-104.06,42.48,-96.44,45.95],"TN":[-90.31,34.98,-81.65,36.68],
    "TX":[-106.65,25.84,-93.51,36.50],"UT":[-114.05,37.00,-109.04,42.00],
    "VT":[-73.44,42.73,-71.46,45.02],"VA":[-83.68,36.54,-75.24,39.47],
    "WA":[-124.73,45.54,-116.92,49.00],"WV":[-82.64,37.20,-77.72,40.64],
    "WI":[-92.89,42.49,-86.25,47.08],"WY":[-111.06,40.99,-104.05,45.01],
    "DC":[-77.12,38.79,-76.91,38.99],
}

def _float(v):
    try:
        f = float(v)
        return 0.0 if f != f else f
    except: return 0.0

def load_dc(articles_by_dc):
    df = pd.read_csv(FILE_DC_POINTS)
    out = []
    for _, row in df.iterrows():
        aid  = str(row.get("asset_id",""))
        arts = articles_by_dc.get(aid, [])
        out.append({
            "id":           aid,
            "name":         str(row.get("name","") or ""),
            "status":       str(row.get("status","") or ""),
            "company_type": str(row.get("company_type","") or ""),
            "operator":     str(row.get("operator","") or ""),
            "estimated_mw": _float(row.get("estimated_mw")),
            "state":        str(row.get("state","") or ""),
            "city":         str(row.get("city","") or ""),
            "lat":          float(row["lat"]),
            "lon":          float(row["lon"]),
            "color":        DC_STATUS_RGB.get(str(row.get("status","")), [149,165,166]),
            "article_count":len(arts),
            "articles":     arts[:5],
        })
    log.info(f"  DC: {len(out):,}")
    return out

def load_pp():
    df = pd.read_csv(FILE_PP_POINTS)
    out = []
    for _, row in df.iterrows():
        tech = str(row.get("tech_group","") or "Other/Storage")
        mw   = _float(row.get("nameplate_mw"))
        out.append({
            "name":      str(row.get("plant_name","") or ""),
            "pp_status": str(row.get(PP_FIELD_STATUS_LABEL,"") or ""),
            "tech_group":tech,
            "mw":        mw,
            "state":     str(row.get("state","") or ""),
            "county":    str(row.get("county","") or ""),
            "lat":       float(row["lat"]),
            "lon":       float(row["lon"]),
            "color":     PP_TECH_RGB.get(tech, [26,188,156]),
            "radius":    max(3, min(20, int((mw or 0)**0.4))),
        })
    log.info(f"  PP: {len(out):,}")
    return out

def load_articles_state():
    df = pd.read_csv(FILE_ARTICLES_STATE)
    
    # Sort by published_date descending (most recent first)
    if 'published_date' in df.columns:
        df['published_date'] = pd.to_datetime(df['published_date'], errors='coerce')
        df = df.sort_values('published_date', ascending=False)
    
    # Group by state and collect article data (up to 5 most recent)
    g  = df.groupby(["state","state_lat","state_lon"]).agg(
             count=("title","count"),
             articles=("title", lambda x: list(df.loc[x.index, ['title', 'url', 'published_date']].to_dict('records'))[:5])
         ).reset_index()
    out = []
    for _, row in g.iterrows():
        out.append({
            "state": str(row["state"]),
            "lat": float(row["state_lat"]),
            "lon": float(row["state_lon"]),
            "count": int(row["count"]),
            "articles": row["articles"]  # Now contains full article objects with title, url, date
        })
    log.info(f"  State articles: {len(out):,}")
    return out

def load_articles_full():
    df1 = pd.read_csv(FILE_ARTICLES_DC)
    df2 = pd.read_csv(FILE_ARTICLES_STATE)
    out, seen = [], set()
    for df, src in [(df1,"dc"),(df2,"state")]:
        for _, row in df.iterrows():
            t = str(row.get("title","") or "")
            if t in seen: continue
            seen.add(t)
            out.append({
                "title":  t,
                "url":    str(row.get("url","") or ""),
                "date":   str(row.get("published_date","") or ""),
                "source": str(row.get("source","") or ""),
                "dc_id":  str(row.get("dc_id","") or "") if src=="dc" else "",
                "state":  str(row.get("state","") or ""),
            })
    log.info(f"  Articles full: {len(out):,}")
    return out


def load_articles_all():
    """All articles where Primary_Category is not blank, with scores."""
    import csv as _csv
    filepath = ROOT / "data" / "raw" / "inputs" / "news_feed.csv"
    if not filepath.exists():
        log.warning(f"  news_feed.csv not found at {filepath}")
        return []
    # Try encodings
    for enc in ["utf-8-sig", "utf-8", "cp1252"]:
        try:
            df = pd.read_csv(filepath, encoding=enc, dtype=str)
            break
        except Exception:
            continue
    else:
        log.warning("  Could not read news_feed.csv")
        return []

    # Normalize column names
    df.columns = [c.strip().lstrip("\ufeff").strip('"') for c in df.columns]

    # Filter: Primary_Category not blank
    if "Primary_Category" in df.columns:
        df = df[df["Primary_Category"].notna() & (df["Primary_Category"].str.strip() != "")]
    else:
        log.warning("  Primary_Category column not found")

    out = []
    for _, row in df.iterrows():
        try:
            sa = float(row.get("Strategy_Alignment_Score") or 0)
        except: sa = 0.0
        try:
            rs = float(row.get("Relevance_Score") or 0)
        except: rs = 0.0
        out.append({
            "id":       str(row.get("ID","") or ""),
            "title":    str(row.get("Title","") or ""),
            "url":      str(row.get("Article_URL","") or ""),
            "date":     str(row.get("PublishedDate","") or ""),
            "source":   str(row.get("Source","") or ""),
            "summary":  str(row.get("Summary_AI","") or row.get("Summary","") or ""),
            "category": str(row.get("Primary_Category","") or ""),
            "state":    str(row.get("States","") or ""),
            "dc_id":    str(row.get("DC_ID","") or ""),
            "priority": round(sa + rs, 2),
            "sa_score": round(sa, 2),
            "rel_score":round(rs, 2),
        })

    # Sort by priority descending
    out.sort(key=lambda x: x["priority"], reverse=True)
    log.info(f"  All articles (Primary_Category not blank): {len(out):,}")
    return out

def load_h3(res):
    df = pd.read_csv(H3_FILES[res], dtype={H3_COL_ID: str})
    df = df[df[H3_COL_ID].notna() & (df[H3_COL_ID] != "")].copy()
    for col in [H3_COL_DC_OPERATIONAL_PCT, H3_COL_DC_PIPELINE_PCT, H3_COL_DC_TOTAL_PCT,
                H3_COL_PP_OPERATIONAL_PCT, H3_COL_PP_PLANNED_PCT, H3_COL_PP_TOTAL_PCT,
                H3_COL_BC_PCT]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    if H3_COL_IS_ZERO in df.columns:
        df[H3_COL_IS_ZERO] = df[H3_COL_IS_ZERO].astype(str).str.lower() == "true"
    out = []
    for _, row in df.iterrows():
        out.append({
            "h3_id":        str(row[H3_COL_ID]),
            "dc_op_mw":     _float(row.get(H3_COL_DC_OPERATIONAL)),
            "dc_pipe_mw":   _float(row.get(H3_COL_DC_PIPELINE)),
            "dc_total_mw":  _float(row.get(H3_COL_DC_TOTAL)),
            "pp_op_mw":     _float(row.get(H3_COL_PP_OPERATIONAL)),
            "pp_plan_mw":   _float(row.get(H3_COL_PP_PLANNED)),
            "pp_total_mw":  _float(row.get(H3_COL_PP_TOTAL)),
            "bc_unit_cost_ft": _float(row.get(H3_COL_BC)),
            "dc_op_pct":    _float(row.get(H3_COL_DC_OPERATIONAL_PCT)),
            "dc_pipe_pct":  _float(row.get(H3_COL_DC_PIPELINE_PCT)),
            "dc_total_pct": _float(row.get(H3_COL_DC_TOTAL_PCT)),
            "pp_op_pct":    _float(row.get(H3_COL_PP_OPERATIONAL_PCT)),
            "pp_plan_pct":  _float(row.get(H3_COL_PP_PLANNED_PCT)),
            "pp_total_pct": _float(row.get(H3_COL_PP_TOTAL_PCT)),
            "bc_pct":       _float(row.get(H3_COL_BC_PCT)),
            "is_zero":      bool(row.get(H3_COL_IS_ZERO, False)),
        })
    log.info(f"  H3 r{res}: {len(out):,}")
    return out

def build_deckgl():
    log.info("="*55)
    log.info("  LightSignal — Build Deck.gl Dashboard")
    log.info("="*55)

    required = [FILE_DC_POINTS, FILE_PP_POINTS, FILE_ARTICLES_DC, FILE_ARTICLES_STATE] + list(H3_FILES.values())
    missing  = [f for f in required if not f.exists()]
    if missing:
        for f in missing: log.error(f"  Missing: {f}")
        sys.exit(1)

    js_bundle = get_bundle()

    df_arts = pd.read_csv(FILE_ARTICLES_DC)
    articles_by_dc = defaultdict(list)
    for _, row in df_arts.iterrows():
        dc_id = str(row.get("dc_id","")).strip()
        if dc_id:
            articles_by_dc[dc_id].append({
                "title": str(row.get("title","") or ""),
                "url":   str(row.get("url","") or ""),
                "date":  str(row.get("published_date","") or ""),
                "src":   str(row.get("source","") or ""),
            })

    log.info("  Loading datasets...")
    dc_data   = load_dc(articles_by_dc)
    pp_data   = load_pp()
    art_state = load_articles_state()
    art_full  = load_articles_full()
    art_all   = load_articles_all()
    h3_data   = {res: load_h3(res) for res in H3_RESOLUTIONS}

    log.info("  Serializing...")
    data_js = (
        f"const DC_DATA={json.dumps(dc_data,separators=(',',':'),default=str)};\n"
        f"const PP_DATA={json.dumps(pp_data,separators=(',',':'),default=str)};\n"
        f"const ART_STATE={json.dumps(art_state,separators=(',',':'),default=str)};\n"
        f"const ART_FULL={json.dumps(art_full,separators=(',',':'),default=str)};\n"
        f"const ART_ALL={json.dumps(art_all,separators=(',',':'),default=str)};\n"
        f"const H3_DATA={json.dumps({str(k):v for k,v in h3_data.items()},separators=(',',':'),default=str)};\n"
        "const YLORD_COLORS=[[255,255,255],[224,255,179],[186,242,112],[134,219,72],[78,188,39],[34,153,15],[15,119,6],[5,85,2],[1,50,0]];\n"
        f"const ZERO_COLOR={json.dumps(ZERO_RGB,separators=(',',':'))};\n"
        f"const DC_STATUS_RGB_MAP={json.dumps(DC_STATUS_RGB,separators=(',',':'))};\n"
        f"const DC_STATUS_COLORS_MAP={json.dumps(DC_STATUS_COLORS,separators=(',',':'))};\n"
        f"const STATE_BOUNDS={json.dumps(STATE_BOUNDS,separators=(',',':'))};\n"
        f"const DC_STATUSES={json.dumps([DC_STATUS_OPERATIONAL,DC_STATUS_UNDER_CONSTRUCTION,DC_STATUS_PLANNED,DC_STATUS_WITHDRAWN,DC_STATUS_LAND_BANK,DC_STATUS_UNKNOWN,DC_STATUS_CLOSED],separators=(',',':'))};\n"
    )
    # Scan for published newsletters (newest first)
    newsletters = []
    if OUTPUT_NEWSLETTERS_DIR.exists():
        for f in sorted(OUTPUT_NEWSLETTERS_DIR.glob("LightSignal_Weekly_*.html"), reverse=True):
            date_str = f.stem.replace("LightSignal_Weekly_", "")
            try:
                end_dt   = datetime.strptime(date_str, "%Y-%m-%d")
                start_dt = end_dt - timedelta(days=6)
                label    = (f"{start_dt.strftime('%b ')+str(int(start_dt.strftime('%d')))}"
                            f" \u2013 {end_dt.strftime('%b ')+str(int(end_dt.strftime('%d')))+end_dt.strftime(', %Y')}")
            except ValueError:
                label = date_str
            newsletters.append({"label": label, "path": f"../../newsletters/{f.name}"})
    log.info(f"  Newsletters found: {len(newsletters)}")

    data_js += f"const NEWSLETTERS={json.dumps(newsletters,separators=(',',':'))};\n"
    log.info(f"  Data JSON: {len(data_js)/1_000_000:.1f} MB")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    bundle_out = OUTPUT_DIR / "lib.js"
    bundle_out.write_text(js_bundle, encoding="utf-8")
    OUTPUT_HTML.write_text(_html(data_js), encoding="utf-8")
    log.info(f"  Written index.html ({OUTPUT_HTML.stat().st_size/1_000_000:.1f} MB)")
    log.info(f"  Written lib.js    ({bundle_out.stat().st_size/1_000_000:.1f} MB)")
    log.info("Dashboard build complete.")
    log.info(f"  Open: {OUTPUT_HTML}")
    return OUTPUT_HTML

# ══════════════════════════════════════════════════════════════════════
# HTML TEMPLATE
# ══════════════════════════════════════════════════════════════════════
def _html(data_js):
    return """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/><title>LightSignal Market Intelligence</title>
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<style>
*{margin:0;padding:0;box-sizing:border-box;}
body{background:#0d0d1a;font-family:'Segoe UI',Arial,sans-serif;color:#eee;overflow:hidden;}

/* HEADER */
#header{position:absolute;top:0;left:0;right:0;height:90px;z-index:200;
  background:linear-gradient(90deg,#0d0d1a,#1a1a2e);border-bottom:1px solid #2d2d4e;
  display:flex;align-items:center;padding:0 18px;gap:14px;}
#logo{display:flex;align-items:center;gap:8px;flex-shrink:0;}
#logo-icon{font-size:30px;color:#E0FF00;}
#logo-text{font-size:26px;font-weight:800;letter-spacing:1.5px;color:#E0FF00;}
#logo-sub{font-size:11px;color:#999;letter-spacing:.5px;margin-top:2px;}
#kpi-bar{display:flex;gap:10px;flex:1;min-width:0;}
.kpi{background:#161628;border:1px solid #252540;border-radius:7px;
  padding:10px 18px;flex:1;min-width:0;}
.kpi-lbl{font-size:10px;color:#777;text-transform:uppercase;letter-spacing:.8px;margin-bottom:2px;}
.kpi-val{font-size:26px;font-weight:700;color:#fff;line-height:1.15;}
.kpi-sub{font-size:10px;color:#444;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
#hdr-btns{display:flex;gap:6px;flex-shrink:0;}
.hbtn{background:#161628;border:1px solid #252540;border-radius:5px;
  color:#888;font-size:12px;padding:6px 12px;cursor:pointer;white-space:nowrap;transition:all .15s;}
.hbtn:hover{background:#1e1e35;color:#eee;border-color:#E0FF0066;}
.hbtn.active{background:#E0FF0022;border-color:#E0FF00;color:#E0FF00;}
#btn-news,#btn-nl{background:linear-gradient(135deg,#1e1e10,#141408);border:1.5px solid #E0FF00;
  color:#E0FF00;font-size:13px;font-weight:600;padding:10px 18px;border-radius:7px;
  letter-spacing:.5px;box-shadow:0 0 12px rgba(224,255,0,.15);}
#btn-news:hover,#btn-nl:hover{background:linear-gradient(135deg,#252510,#18180a);border-color:#E0FF00;
  color:#E0FF00;box-shadow:0 0 18px rgba(224,255,0,.3);}
#btn-news.active,#btn-nl.active{background:#E0FF0022;border-color:#E0FF00;color:#E0FF00;}

/* SIDEBAR */
#sidebar{position:absolute;top:90px;left:0;bottom:0;width:252px;
  background:#141424;border-right:1px solid #1e1e35;
  display:flex;flex-direction:column;z-index:100;transform:translateX(0);}
#sidebar.animated{transition:transform .3s ease;}
#sidebar.collapsed{transform:translateX(-252px);}
#sb-toggle{position:absolute;top:50%;left:252px;transform:translateY(-50%);
  background:#141424;border:1px solid #1e1e35;border-left:none;
  width:14px;height:42px;cursor:pointer;z-index:101;
  display:flex;align-items:center;justify-content:center;
  border-radius:0 4px 4px 0;color:#555;font-size:8px;}
#sb-scroll{flex:1;overflow-y:auto;padding:6px 0 12px;}
#sb-scroll::-webkit-scrollbar{width:3px;}
#sb-scroll::-webkit-scrollbar-thumb{background:#2a2a3a;border-radius:2px;}

.sec-hdr{padding:7px 12px 3px;font-size:9px;font-weight:700;color:#444;
  text-transform:uppercase;letter-spacing:1px;cursor:pointer;
  display:flex;align-items:center;justify-content:space-between;}
.sec-hdr:hover .sh-txt{color:#777;}
.sh-arr{font-size:7px;color:#333;transition:transform .2s;}
.sh-arr.open{transform:rotate(90deg);}
.sec-body{overflow:hidden;}

/* Layer buttons — now with optional expand arrow */
.lbtn{display:flex;align-items:center;gap:7px;width:100%;padding:6px 12px 6px 18px;
  background:none;border:none;color:#888;font-size:12px;cursor:pointer;text-align:left;transition:background .15s;}
.lbtn:hover{background:#1c1c2e;color:#ccc;}
.lbtn.active{color:#eee;background:#191930;}
.ldot{width:8px;height:8px;border-radius:50%;flex-shrink:0;border:1px solid rgba(255,255,255,.1);}
.lbtn.active .ldot{border-color:rgba(255,255,255,.45);}
.lbtn-expand{margin-left:auto;font-size:9px;color:#444;transition:transform .2s;padding:0 2px;}
.lbtn-expand.open{transform:rotate(90deg);}

/* Sub-filters under a layer button */
.layer-filters{background:#0f0f1f;border-top:1px solid #1a1a2a;padding:4px 0 6px;}
.frow{padding:3px 12px 3px 24px;display:flex;align-items:center;gap:7px;cursor:pointer;}
.frow:hover{background:#181828;}
.frow input[type=checkbox]{accent-color:#E0FF00;cursor:pointer;flex-shrink:0;}
.frow label{font-size:11px;color:#888;cursor:pointer;flex:1;}
.frow label:hover{color:#ccc;}
.fbadge{font-size:9px;padding:1px 4px;border-radius:6px;background:#1e1e35;color:#555;flex-shrink:0;}
.fsub-hdr{padding:5px 12px 2px 24px;font-size:9px;color:#444;text-transform:uppercase;letter-spacing:.8px;}

/* State list */
#state-search{margin:3px 12px 5px 18px;padding:3px 7px;
  background:#0d0d1a;border:1px solid #252535;border-radius:4px;
  color:#ddd;font-size:11px;width:calc(100% - 30px);}
#state-search::placeholder{color:#444;}
.sbtn{display:flex;align-items:center;padding:4px 12px 4px 18px;
  background:none;border:none;color:#777;font-size:11px;cursor:pointer;width:100%;text-align:left;}
.sbtn:hover{background:#181828;color:#ddd;}
.sbtn.active{color:#E0FF00;background:#1a1a2a;}
.scode{font-weight:700;width:26px;flex-shrink:0;}
.scnt{font-size:10px;color:#444;margin-left:auto;}
.sbtn.active .scnt{color:#E0FF0066;}

.res-tabs{display:flex;gap:4px;padding:4px 12px 7px 18px;}
.rtab{padding:2px 10px;border-radius:9px;border:1px solid #2a2a3a;
  background:none;color:#666;font-size:11px;cursor:pointer;transition:all .15s;}
.rtab.active{background:#E0FF00;border-color:#E0FF00;color:#0d0d1a;font-weight:700;}

#legend{padding:8px 12px;border-top:1px solid #161626;}
#leg-title{color:#444;font-size:9px;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;}
#leg-bar{display:flex;height:7px;border-radius:3px;overflow:hidden;margin-bottom:3px;}
.lseg{flex:1;}
#leg-lbl{display:flex;justify-content:space-between;color:#444;font-size:9px;}

/* DRAWER */
#drw-overlay{position:absolute;inset:0;background:rgba(0,0,0,.45);z-index:300;display:none;opacity:0;transition:opacity .3s;}
#drw-overlay.open{display:block;opacity:1;}
#drawer{position:absolute;top:90px;right:0;bottom:0;width:400px;
  background:#141424;border-left:1px solid #1e1e35;z-index:400;
  display:flex;flex-direction:column;transform:translateX(100%);transition:transform .3s ease;}
#drawer.open{transform:translateX(0);}
#drw-hdr{padding:12px 14px;border-bottom:1px solid #1e1e35;display:flex;align-items:center;justify-content:space-between;}
#drw-hdr h3{font-size:13px;color:#ddd;}
#drw-close{background:none;border:none;color:#555;font-size:17px;cursor:pointer;padding:2px 5px;}
#drw-close:hover{color:#eee;}
#drw-filters{padding:8px 14px;border-bottom:1px solid #111;display:flex;gap:5px;flex-wrap:wrap;align-items:center;}
.dbtn{padding:3px 10px;border-radius:9px;border:1px solid #2a2a3a;background:none;color:#666;font-size:11px;cursor:pointer;transition:all .15s;}
.dbtn.active{background:#E0FF00;border-color:#E0FF00;color:#0d0d1a;font-weight:700;}
#drw-cats{padding:6px 14px 0;display:flex;gap:5px;flex-wrap:wrap;border-bottom:1px solid #111;padding-bottom:8px;}
.catbtn{padding:2px 8px;border-radius:9px;border:1px solid #2a2a3a;background:none;color:#666;font-size:10px;cursor:pointer;transition:all .15s;white-space:nowrap;}
.catbtn.active{background:#5dade222;border-color:#5dade2;color:#5dade2;}
#drw-cnt{font-size:10px;color:#555;}
#drw-list{flex:1;overflow-y:auto;padding:4px 0;}
#drw-list::-webkit-scrollbar{width:3px;}
#drw-list::-webkit-scrollbar-thumb{background:#2a2a3a;}
.aitem{padding:9px 14px;border-bottom:1px solid #0f0f1f;transition:background .15s;}
.aitem:hover{background:#1a1a2a;}
.atitle{font-size:12px;color:#5dade2;margin-bottom:3px;line-height:1.4;}
.atitle a{color:inherit;text-decoration:none;}
.atitle a:hover{text-decoration:underline;}
.ameta{font-size:10px;color:#444;display:flex;gap:7px;flex-wrap:wrap;align-items:center;}
.ameta .acat{background:#5dade211;color:#5dade2;border-radius:4px;padding:1px 5px;font-size:9px;}
.ameta .apri{color:#E0FF0088;font-weight:700;}
.asummary{font-size:10px;color:#555;margin-top:4px;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}

/* NEWSLETTER PANEL */
#nl-overlay{position:absolute;inset:0;background:rgba(0,0,0,.45);z-index:300;display:none;opacity:0;transition:opacity .3s;}
#nl-overlay.open{display:block;opacity:1;}
#nl-panel{position:absolute;top:90px;right:0;bottom:0;width:320px;
  background:#141424;border-left:1px solid #1e1e35;z-index:400;
  display:flex;flex-direction:column;transform:translateX(100%);transition:transform .3s ease;}
#nl-panel.open{transform:translateX(0);}
#nl-hdr{padding:12px 16px;border-bottom:1px solid #1e1e35;display:flex;align-items:center;justify-content:space-between;gap:8px;}
#nl-hdr h3{font-size:13px;color:#E0FF00;letter-spacing:.3px;}
#nl-close{background:none;border:none;color:#555;font-size:17px;cursor:pointer;padding:2px 5px;}
#nl-close:hover{color:#eee;}
#nl-note{padding:10px 16px 6px;font-size:10px;color:#444;border-bottom:1px solid #0f0f1f;}
#nl-list{flex:1;overflow-y:auto;padding:4px 0;}
#nl-list::-webkit-scrollbar{width:3px;}
#nl-list::-webkit-scrollbar-thumb{background:#2a2a3a;}
.nl-item{display:flex;align-items:center;padding:12px 16px;border-bottom:1px solid #0f0f1f;
  text-decoration:none;color:#bbb;font-size:12px;gap:10px;transition:background .15s;}
.nl-item:hover{background:#1a1a2a;color:#E0FF00;}
.nl-item::before{content:'\u25a6';color:#E0FF00;opacity:.5;font-size:14px;flex-shrink:0;}
.nl-empty{padding:24px 16px;font-size:12px;color:#444;font-style:italic;}

/* TOOLTIP */
.tt-base{position:absolute;z-index:500;border-radius:8px;
  padding:11px 14px;max-width:320px;font-size:12px;
  box-shadow:0 6px 24px rgba(0,0,0,.5);}
#tooltip{pointer-events:none;background:rgba(255,255,255,.10);
  border:1px solid rgba(255,255,255,.22);
  backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);display:none;}
#pinned{pointer-events:auto;background:rgba(255,255,255,.13);
  border:1px solid rgba(255,255,255,.28);
  backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);display:none;
  min-width:220px;}
.tt-base h4{font-size:13px;margin-bottom:5px;color:#fff;font-weight:600;}
.tbadge{display:inline-block;padding:1px 7px;border-radius:7px;font-size:10px;font-weight:700;margin-bottom:4px;}
.trow{display:flex;gap:7px;margin:2px 0;}
.tlbl{color:rgba(255,255,255,.45);min-width:85px;font-size:11px;}
.tval{color:rgba(255,255,255,.9);}
.tscore{font-size:19px;font-weight:700;color:#E0FF00;margin:3px 0 1px;}
.tarts{margin-top:7px;border-top:1px solid rgba(255,255,255,.12);padding-top:5px;}
.tart{color:#7dd3fc;font-size:11px;margin:2px 0;line-height:1.4;}
.tart a{color:#7dd3fc;text-decoration:none;}
.tart a:hover{text-decoration:underline;color:#bae6fd;}
#pin-close{position:absolute;top:6px;right:8px;background:none;border:none;
  color:rgba(255,255,255,.4);font-size:14px;cursor:pointer;padding:2px 4px;line-height:1;}
#pin-close:hover{color:#fff;}
#pin-hint{font-size:9px;color:rgba(255,255,255,.3);margin-top:6px;text-align:right;}

#infobar{position:absolute;bottom:10px;right:10px;z-index:100;
  background:#141424dd;border:1px solid #1e1e35;border-radius:4px;
  padding:4px 10px;font-size:10px;color:#444;}

/* Score builder */
.sb-var{display:flex;align-items:center;gap:6px;padding:3px 12px 3px 18px;}
.sb-var input[type=checkbox]{accent-color:#E0FF00;flex-shrink:0;cursor:pointer;}
.sb-var label{font-size:11px;color:#888;flex:1;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.sb-wt{width:44px;padding:2px 4px;background:#0d0d1a;border:1px solid #2a2a3a;
  border-radius:3px;color:#E0FF00;font-size:11px;text-align:right;-moz-appearance:textfield;}
.sb-wt::-webkit-inner-spin-button{display:none;}
.sb-wt:focus{outline:none;border-color:#E0FF0066;}
.sb-pct{font-size:10px;color:#444;width:16px;flex-shrink:0;}
.sb-total{display:flex;justify-content:space-between;padding:5px 12px 4px 18px;
  font-size:10px;border-top:1px solid #1a1a2a;margin-top:3px;}
.sb-total-lbl{color:#444;}
.sb-total-val{color:#E0FF00;font-weight:700;}
.sb-total-val.over{color:#ef4444;}
.preset-btns{display:flex;gap:4px;padding:4px 12px 6px 18px;flex-wrap:wrap;}
.pbtn{padding:2px 8px;border-radius:9px;border:1px solid #2a2a3a;background:none;
  color:#666;font-size:10px;cursor:pointer;transition:all .15s;white-space:nowrap;}
.pbtn:hover{border-color:#E0FF0055;color:#E0FF00;}
</style>
</head>
<body>

<div id="header">
  <div id="logo">
    
    <div><div id="logo-text">LightSignal</div><div id="logo-sub">Lightpath Market Intel</div></div>
  </div>
  <div id="kpi-bar">
    <div class="kpi"><div class="kpi-lbl">Data Centers</div><div class="kpi-val" id="k-dc">—</div><div class="kpi-sub" id="k-dc-s">all states</div></div>
    <div class="kpi"><div class="kpi-lbl">DC Est. MW</div><div class="kpi-val" id="k-dmw">—</div><div class="kpi-sub">estimated MW</div></div>
    <div class="kpi"><div class="kpi-lbl">Power Plants</div><div class="kpi-val" id="k-pp">—</div><div class="kpi-sub" id="k-pp-s">all states</div></div>
    <div class="kpi"><div class="kpi-lbl">Plant Nameplate MW</div><div class="kpi-val" id="k-pmw">—</div><div class="kpi-sub">nameplate MW</div></div>
  </div>
  <div id="hdr-btns">
    <button class="hbtn" onclick="openDrawer()" id="btn-news">Industry Intel</button>
    <button class="hbtn" onclick="openNL()" id="btn-nl">Newsletter</button>
    <button class="hbtn" id="btn-clear" onclick="clearFilters()" style="display:none">&#10005; Clear</button>
  </div>
</div>

<div id="map" style="position:absolute;top:90px;left:0;right:0;bottom:0;"></div>

<div id="sidebar">
  <div id="sb-scroll">

    <!-- LAYERS -->
    <div class="sec-hdr" onclick="toggleSec('s-layers')"><span class="sh-txt">Layers</span><span class="sh-arr open" id="arr-s-layers">&#9658;</span></div>
    <div class="sec-body" id="s-layers">

      <!-- Data Centers -->
      <div style="border-bottom:1px solid #0f0f1f">
        <div style="display:flex;align-items:center;">
          <div class="frow" style="padding-left:18px;flex:0 0 auto;">
            <input type="checkbox" id="chk-dc-all" checked onchange="toggleDCLayer(this.checked)">
          </div>
          <button class="lbtn" style="flex:1;padding-left:4px;" onclick="toggleDCFilters()" id="btn-dc">
            <div class="ldot" style="background:#2ECC71"></div>Data Centers
            <span class="lbtn-expand" id="arr-dc-filters">&#9658;</span>
          </button>
        </div>
        <div class="layer-filters" id="dc-filters" style="display:none">
          <div class="fsub-hdr">Status</div>
          <div id="dc-st-checks"></div>
          <div class="fsub-hdr" style="margin-top:4px">Company Type</div>
          <div id="dc-tp-checks"></div>
        </div>
      </div>

      <!-- Power Plants -->
      <div style="border-bottom:1px solid #0f0f1f">
        <div style="display:flex;align-items:center;">
          <div class="frow" style="padding-left:18px;flex:0 0 auto;">
            <input type="checkbox" id="chk-pp-all" onchange="togglePPLayer(this.checked)">
          </div>
          <button class="lbtn" style="flex:1;padding-left:4px;" onclick="togglePPFilters()" id="btn-pp">
            <div class="ldot" style="background:#27AE60"></div>Power Plants
            <span class="lbtn-expand" id="arr-pp-filters">&#9658;</span>
          </button>
        </div>
        <div class="layer-filters" id="pp-filters" style="display:none">
          <div class="fsub-hdr">Status</div>
          <div class="frow"><input type="checkbox" id="pp-op" checked onchange="togglePPSt('Operational',this.checked)"><label for="pp-op">Operational</label></div>
          <div class="frow"><input type="checkbox" id="pp-pl" checked onchange="togglePPSt('Planned',this.checked)"><label for="pp-pl">Planned</label></div>
          <div class="fsub-hdr" style="margin-top:4px">Technology</div>
          <div id="pp-tech-checks"></div>
        </div>
      </div>

      <!-- News Articles -->
      <div style="display:flex;align-items:center;">
        <div class="frow" style="padding-left:18px;flex:0 0 auto;">
          <input type="checkbox" id="chk-arts-all" onchange="toggleArtsLayer(this.checked)">
        </div>
        <button class="lbtn" style="flex:1;padding-left:4px;" id="btn-articles">
          <div class="ldot" style="background:#FFAA00"></div>News Articles
        </button>
      </div>

    </div>

    <div class="sec-hdr" onclick="toggleSec('s-score')"><span class="sh-txt">Score Builder</span><span class="sh-arr open" id="arr-s-score">&#9658;</span></div>
    <div class="sec-body" id="s-score">
      <div class="preset-btns">
        <button class="pbtn" onclick="loadPreset('full')">Full Picture</button>
        <button class="pbtn" onclick="loadPreset('today')">Today</button>
        <button class="pbtn" onclick="loadPreset('growth')">Growth</button>
        <button class="pbtn" onclick="loadPreset('clear')">Clear</button>
      </div>
      <div id="sb-vars"></div>
      <div class="sb-total">
        <span class="sb-total-lbl">Total weight</span>
        <span class="sb-total-val" id="sb-total-val">0%</span>
      </div>
    </div>

    <div class="sec-hdr" style="cursor:default;"><span class="sh-txt">H3 Resolution</span></div>
    <div class="res-tabs">
      <button class="rtab active" onclick="setRes(3)" id="res-3">R3</button>
      <button class="rtab" onclick="setRes(4)" id="res-4">R4</button>
      <button class="rtab" onclick="setRes(5)" id="res-5">R5</button>
    </div>

    <div style="height:1px;background:#111;margin:5px 0;"></div>

    <!-- STATE FILTER -->
    <div class="sec-hdr" onclick="toggleSec('s-states')"><span class="sh-txt">State Filter</span><span class="sh-arr open" id="arr-s-states">&#9658;</span></div>
    <div class="sec-body" id="s-states">
      <input type="text" id="state-search" placeholder="Search states…" oninput="filterStates()">
      <div id="state-list"></div>
    </div>

  </div>
  <div id="legend"><div id="leg-title">H3 Score Percentile</div><div id="leg-bar"></div><div id="leg-lbl"><span>Low</span><span>High</span></div></div>
</div>
<div id="sb-toggle" onclick="toggleSB()">&#9664;</div>

<!-- ARTICLES DRAWER -->
<div id="drw-overlay" onclick="closeDrawer()"></div>
<div id="drawer">
  <div id="drw-hdr"><h3>Industry Intel</h3><button id="drw-close" onclick="closeDrawer()">&#10005;</button></div>
  <div id="drw-filters">
    <button class="dbtn active" onclick="setDays(7)"  id="d-7">7d</button>
    <button class="dbtn" onclick="setDays(30)" id="d-30">30d</button>
    <button class="dbtn" onclick="setDays(90)" id="d-90">90d</button>
    <button class="dbtn" onclick="setDays(0)"  id="d-0">All</button>
    <span id="drw-cnt" style="margin-left:auto;font-size:10px;color:#555;"></span>
  </div>
  <div id="drw-cats"></div>
  <div id="drw-list"></div>
</div>

<!-- NEWSLETTER PANEL -->
<div id="nl-overlay" onclick="closeNL()"></div>
<div id="nl-panel">
  <div id="nl-hdr"><h3>&#128240; Weekly Newsletter</h3><button id="nl-close" onclick="closeNL()">&#10005;</button></div>
  <div id="nl-note">Click any issue to open it in a new tab.</div>
  <div id="nl-list"></div>
</div>

<div id="tooltip" class="tt-base"></div>
<div id="pinned" class="tt-base"><button id="pin-close" onclick="closePin()">&#10005;</button><div id="pin-body"></div></div>
<div id="infobar">LightSignal &nbsp;|&nbsp; Scroll to zoom &nbsp; Drag to pan &nbsp; Hover to inspect</div>

<script src="lib.js"></script>
<script>
""" + data_js + r"""
const {DeckGL,H3HexagonLayer,ScatterplotLayer,TileLayer,BitmapLayer} = deck;
const TextLayer = deck.TextLayer || null; // graceful fallback

// ── Score builder config ────────────────────────────────────────────
const SB_VARS = [
  { id:'dc_total_pct',       label:'DC Total MW'       },
  { id:'dc_op_pct',          label:'DC Operational MW' },
  { id:'dc_pipe_pct',        label:'DC Pipeline MW'    },
  { id:'pp_total_pct',       label:'PP Total MW'       },
  { id:'pp_op_pct',          label:'PP Operational MW' },
  { id:'pp_plan_pct',        label:'PP Planned MW'     },
  { id:'bc_pct',             label:'Build Cost (inv.)' },
];
const PRESETS = {
  full:   { dc_total_pct:50, pp_total_pct:30, bc_pct:20 },
  today:  { dc_op_pct:60,    pp_op_pct:30,   bc_pct:10  },
  growth: { dc_pipe_pct:60,  pp_plan_pct:30, bc_pct:10  },
  clear:  {},
};

// ── State ──────────────────────────────────────────────────────────
const S = {
  showDC:true, showPP:false, showArts:false,
  h3On:true, h3Res:3,
  sbWeights: { dc_total_pct:50, pp_total_pct:30, bc_pct:20 }, // h3On driven by sbWeights
  sbOpen:true, drawerOpen:false, drawerDays:7,
  drawerCat: '',
  dcStatuses:   new Set(DC_STATUSES),
  dcTypes:      new Set(DC_DATA.map(d=>d.company_type).filter(Boolean)),
  ppStatuses:   new Set(['Operational','Planned']),
  ppTechs:      new Set(PP_DATA.map(d=>d.tech_group).filter(Boolean)),
  selStates:    new Set(),
};

let fDC=[], fPP=[];
const CELL_CACHE={};

function fmtMW(mw) {
  if (!mw) return '';
  if (mw >= 1000) return (mw/1000).toFixed(1)+'GW';
  return Math.round(mw)+'MW';
}

// DC company type colors — visually distinct palette
const DC_TYPE_COLORS = {
  'Hyperscale':               [99,  102, 241, 230],  // indigo
  'Carrier Neutral/Real Estate':[245,158, 11, 220],  // amber
  'Carrier/Mobile/MSO':       [236, 72,  153, 215],  // pink
  'Miner':                    [234,179,  8,  215],   // yellow
  'Enterprise/Other':         [20, 184, 166, 215],   // teal
  'Unclassified':             [148,163, 184, 180],   // slate
};
function dcTypeColor(type) {
  return DC_TYPE_COLORS[type] || [148,163,184,180];
}
function fmtDate(s) {
  if (!s) return '';
  const d = new Date(s);
  if (isNaN(d)) return s.substring(0,10);
  return d.toLocaleDateString('en-US', {month:'short', day:'numeric', year:'numeric'});
}

// ── Filters ────────────────────────────────────────────────────────
function applyFilters(){
  const ss=S.selStates;
  fDC=DC_DATA.filter(d=>
    S.dcStatuses.has(d.status)&&
    S.dcTypes.has(d.company_type)&&
    (ss.size===0||ss.has(d.state))
  );
  fPP=PP_DATA.filter(d=>
    S.ppStatuses.has(d.pp_status)&&
    S.ppTechs.has(d.tech_group)&&
    (ss.size===0||ss.has(d.state))
  );
  updateKPIs(); updateClearBtn();
  render();
  if(S.drawerOpen) renderDrawer();
}

// ── KPIs ───────────────────────────────────────────────────────────
function fmt(n){
  if(n>=1e9) return (n/1e9).toFixed(1)+'B';
  if(n>=1e6) return (n/1e6).toFixed(1)+'M';
  if(n>=1e3) return (n/1e3).toFixed(1)+'K';
  return Math.round(n).toLocaleString();
}
function updateKPIs(){
  document.getElementById('k-dc').textContent  = fmt(fDC.length);
  document.getElementById('k-dmw').textContent = fmt(fDC.reduce((s,d)=>s+(d.estimated_mw||0),0));
  document.getElementById('k-pp').textContent  = fmt(fPP.length);
  document.getElementById('k-pmw').textContent = fmt(fPP.reduce((s,d)=>s+(d.mw||0),0));
  const lbl = S.selStates.size ? [...S.selStates].filter(s=>s&&s!=='nan').join(', ') : 'all states';
  document.getElementById('k-dc-s').textContent = lbl;
  document.getElementById('k-pp-s').textContent = lbl;
}

// ── Colors ─────────────────────────────────────────────────────────
function computeScore(row){
  const w=S.sbWeights;
  const keys=Object.keys(w);
  if(!keys.length) return 0;
  let score=0;
  keys.forEach(k=>{
    const v=row[k];
    if(v!=null&&!isNaN(v)) score+=(w[k]/100)*v;
  });
  return Math.min(1,Math.max(0,score));
}

function pctToColor(pct,isZero,dim){
  if(isZero||pct==null||pct===0) return [...ZERO_COLOR, dim?25:60];
  const i=Math.min(8,Math.floor(pct*9));
  return [...YLORD_COLORS[i], dim?20:70];
}

function cellInStates(h3id){
  if(!CELL_CACHE[h3id]){
    try{ const c=h3.h3ToGeo(h3id); CELL_CACHE[h3id]={lat:c[0],lon:c[1]}; }
    catch(e){ return false; }
  }
  const {lat,lon}=CELL_CACHE[h3id];
  for(const st of S.selStates){
    const b=STATE_BOUNDS[st];
    if(b&&lon>=b[0]&&lat>=b[1]&&lon<=b[2]&&lat<=b[3]) return true;
  }
  return false;
}

// ── Deck.gl ────────────────────────────────────────────────────────
const deckgl=new DeckGL({
  container: document.getElementById('map'),
  initialViewState:{longitude:-98.35,latitude:39.5,zoom:4,pitch:0,bearing:0},
  controller:true,
  onHover:onHover,
  onClick:onClick,
});

function basemap(){
  return new TileLayer({
    id:'base',
    data:['a','b','c','d'].map(s=>`https://${s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png`),
    tileSize:256,
    renderSubLayers:p=>{
      const{bbox:{west,south,east,north}}=p.tile;
      return new BitmapLayer(p,{data:null,image:p.data,bounds:[west,south,east,north]});
    },
  });
}

function getLayers(){
  const L=[basemap()];

  // H3
  if(S.h3On){
    const rows=H3_DATA[String(S.h3Res)]||[];
    const dim=S.selStates.size>0;
    L.push(new H3HexagonLayer({
      id:'h3base', data:rows, visible:true, opacity:1, filled:true,
      extruded:false, coverage:1, highPrecision:false,
      getHexagon:d=>d.h3_id,
      getFillColor:d=>pctToColor(computeScore(d),d.is_zero,dim),
      getLineColor:[0,0,0,20], lineWidthMinPixels:0.3, pickable:true,
      updateTriggers:{getFillColor:[JSON.stringify(S.sbWeights),S.h3Res,S.selStates.size]},
      onError:e=>console.error('H3:',e),
    }));
    if(dim){
      L.push(new H3HexagonLayer({
        id:'h3sel',
        data:rows.filter(d=>cellInStates(d.h3_id)),
        visible:true, opacity:1, filled:true, extruded:false, coverage:1, highPrecision:false,
        getHexagon:d=>d.h3_id,
        getFillColor:d=>pctToColor(computeScore(d),d.is_zero,false),
        getLineColor:[0,0,0,40], lineWidthMinPixels:0.5, pickable:false,
        updateTriggers:{data:[S.selStates.size,[...S.selStates].sort().join(','),S.h3Res],getFillColor:[JSON.stringify(S.sbWeights)]},
      }));
    }
  }

  // DC markers — color by company type, outline ring for Hyperscale
  if(S.showDC && fDC.length){
    // Base fill layer
    L.push(new ScatterplotLayer({
      id:'dc', data:fDC,
      getPosition:d=>[d.lon,d.lat],
      getFillColor:d=>dcTypeColor(d.company_type),
      getLineColor:d=>d.company_type==='Hyperscale'?[255,255,255,200]:[0,0,0,120],
      getRadius:3000, radiusUnits:'meters',
      radiusMinPixels:3, radiusMaxPixels:9,
      lineWidthMinPixels:d=>d.company_type==='Hyperscale'?2:1,
      stroked:true, pickable:true,
    }));
  }

  // PP markers — color by tech group, sized by MW
  if(S.showPP && fPP.length){
    L.push(new ScatterplotLayer({
      id:'pp', data:fPP,
      getPosition:d=>[d.lon,d.lat],
      getFillColor:d=>[...d.color,190],
      getLineColor:[0,0,0,100],
      getRadius:3500, radiusUnits:'meters',
      radiusMinPixels:3, radiusMaxPixels:8,
      lineWidthMinPixels:1, stroked:true, pickable:true,
    }));
  }

  // Articles
  if(S.showArts){
    const ad=S.selStates.size?ART_STATE.filter(d=>S.selStates.has(d.state)):ART_STATE;
    L.push(new ScatterplotLayer({
      id:'arts', data:ad,
      getPosition:d=>[d.lon,d.lat],
      getFillColor:[255,170,0,210],
      getLineColor:[255,255,255,160],
      getRadius:d=>Math.max(20000,d.count*15000), radiusUnits:'meters',
      radiusMinPixels:6, radiusMaxPixels:22,
      lineWidthMinPixels:1.5, stroked:true, pickable:true,
    }));
  }

  return L;
}

function render(){
  try {
    deckgl.setProps({layers:getLayers()});
  } catch(e) {
    console.error('render error:', e);
    // Fallback: render just basemap
    deckgl.setProps({layers:[basemap()]});
  }
}

// ── Tooltips ───────────────────────────────────────────────────────
let pinnedLayer=null, pinnedObject=null;

function tipContent(object, layerId) {
  if(layerId==='dc')     return dcTip(object);
  if(layerId==='pp')     return ppTip(object);
  if(layerId==='arts')   return artTip(object);
  if(layerId==='h3base') return h3Tip(object);
  return '';
}

function onHover({object,layer,x,y}){
  const el=document.getElementById('tooltip');
  // Don't show hover tooltip if this same object is already pinned
  if(!object||!layer){el.style.display='none';return;}
  const h=tipContent(object,layer.id);
  if(!h){el.style.display='none';return;}
  el.innerHTML=h; el.style.display='block';
  const p=14;
  el.style.left=Math.max(p,Math.min(x+14,window.innerWidth-el.offsetWidth-p))+'px';
  el.style.top=Math.max(90+p,Math.min(y+14,window.innerHeight-el.offsetHeight-p))+'px';
}

function onClick({object,layer,x,y}){
  if(!object||!layer) return;
  // Pin tooltips for DC, PP, H3 cells, and state article dots
  const validLayers=['dc','pp','h3base','arts'];
  if(!validLayers.includes(layer.id)) return;
  
  const h=tipContent(object,layer.id);
  if(!h) return;
  
  const pin=document.getElementById('pinned');
  document.getElementById('pin-body').innerHTML=h+'<div id="pin-hint">Click ✕ to close</div>';
  pin.style.display='block';
  
  // Hide hover tooltip immediately
  document.getElementById('tooltip').style.display='none';
  
  // Position: prefer right of click, but keep on screen
  const pw=Math.max(pin.offsetWidth,240), ph=pin.offsetHeight||200;
  const p=14, mx=window.innerWidth, my=window.innerHeight;
  let lx=x+18, ly=y-20;
  if(lx+pw>mx-p) lx=x-pw-10;
  if(ly+ph>my-p) ly=my-ph-p;
  if(ly<90+p) ly=90+p;
  pin.style.left=Math.max(p,lx)+'px';
  pin.style.top=ly+'px';
}

function closePin(){
  document.getElementById('pinned').style.display='none';
}
function clusterTip(d,type){
  const n=d.properties.point_count, mw=d.properties.mw||0;
  return `<h4>${type} Cluster</h4>
    <div class="trow"><span class="tlbl">Sites</span><span class="tval">${n.toLocaleString()}</span></div>
    <div class="trow"><span class="tlbl">Total MW</span><span class="tval">${fmtMW(mw)}</span></div>
    <div style="color:#555;font-size:10px;margin-top:6px">Zoom in to expand</div>`;
}
function dcTip(d){
  const sc=DC_STATUS_COLORS_MAP[d.status]||'#95a5a6';
  const tc=dcTypeColor(d.company_type);
  const tcHex='#'+tc.slice(0,3).map(v=>v.toString(16).padStart(2,'0')).join('');
  let h=`<h4>${d.name||'Unknown'}</h4>`;
  h+=`<span class="tbadge" style="background:${sc}">${d.status}</span> `;
  h+=`<span class="tbadge" style="background:${tcHex}22;border:1px solid ${tcHex};color:${tcHex}">${d.company_type||'—'}</span><br/>`;
  [[d.operator,'Operator'],[d.estimated_mw>0?d.estimated_mw.toLocaleString()+' MW':null,'Est. MW'],[d.city&&d.state&&d.city!=='nan'?d.city+', '+d.state:d.state,'Location']].forEach(([v,l])=>{if(v&&v!=='nan')h+=`<div class="trow"><span class="tlbl">${l}</span><span class="tval">${v}</span></div>`;});
  if(d.articles?.length){h+='<div class="tarts">';d.articles.forEach(a=>{h+=a.url?`<div class="tart"><a href="${a.url}" target="_blank">${a.title.substring(0,55)}${a.title.length>55?'...':''}</a></div>`:`<div class="tart">${a.title.substring(0,55)}</div>`;});h+='</div>';}
  return h;
}
function ppTip(d){
  return `<h4>${d.name||'Unknown'}</h4>`+
  `<div class="trow"><span class="tlbl">Status</span><span class="tval">${d.pp_status}</span></div>`+
  `<div class="trow"><span class="tlbl">Technology</span><span class="tval">${d.tech_group}</span></div>`+
  `<div class="trow"><span class="tlbl">Nameplate MW</span><span class="tval">${d.mw.toLocaleString(undefined,{maximumFractionDigits:1})} MW</span></div>`+
  `<div class="trow"><span class="tlbl">Location</span><span class="tval">${d.county}, ${d.state}</span></div>`;
}
function artTip(d){
  let h=`<h4>${d.state} — Regional News</h4><div class="trow"><span class="tlbl">Articles</span><span class="tval">${d.count}</span></div>`;
  if(d.articles && d.articles.length){
    d.articles.forEach(art=>{
      const title=art.title||'';
      const shortTitle=title.substring(0,60)+(title.length>60?'...':'');
      const dateStr=art.published_date?new Date(art.published_date).toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'}):'';
      if(art.url){
        h+=`<div class="tart"><a href="${art.url}" target="_blank" rel="noopener">${shortTitle}</a>`;
        if(dateStr) h+=` <span style="color:rgba(255,255,255,.4);font-size:10px">${dateStr}</span>`;
        h+=`</div>`;
      }else{
        h+=`<div class="tart">${shortTitle}`;
        if(dateStr) h+=` <span style="color:rgba(255,255,255,.4);font-size:10px">${dateStr}</span>`;
        h+=`</div>`;
      }
    });
  }
  return h;
}
// H3L lookup removed — labels now come from SB_VARS
function h3Tip(d){
  const sc=computeScore(d);
  const activeLabels=Object.entries(S.sbWeights).filter(([k,v])=>v>0).map(([k,v])=>{
    const def=SB_VARS.find(x=>x.id===k); return (def?def.label:k)+' ('+v+'%)';
  }).join(', ')||'No weights set';
  return `<h4>H3 Cell — R${S.h3Res}</h4><div class="tscore">${sc.toFixed(3)}</div>`+
  `<div style="color:rgba(255,255,255,.35);font-size:10px;margin-bottom:7px">${activeLabels}</div>`+
  `<div class="trow"><span class="tlbl">DC Total MW</span><span class="tval">${(d.dc_total_mw||0).toLocaleString(undefined,{maximumFractionDigits:0})}</span></div>`+
  `<div class="trow"><span class="tlbl">DC Oper. MW</span><span class="tval">${(d.dc_op_mw||0).toLocaleString(undefined,{maximumFractionDigits:0})}</span></div>`+
  `<div class="trow"><span class="tlbl">PP Total MW</span><span class="tval">${(d.pp_total_mw||0).toLocaleString(undefined,{maximumFractionDigits:0})}</span></div>`+
  `<div class="trow"><span class="tlbl">Bore Cost</span><span class="tval">${d.bc_unit_cost_ft?'$'+d.bc_unit_cost_ft.toFixed(2)+'/ft':'—'}</span></div>`;
}

// ── Layer toggles ──────────────────────────────────────────────────
function toggleDCLayer(on){
  S.showDC=on;
  document.getElementById('btn-dc').classList.toggle('active',on);
  if(on) render(); else render();
}
function togglePPLayer(on){
  S.showPP=on;
  document.getElementById('btn-pp').classList.toggle('active',on);
  if(on) render(); else render();
}
function toggleArtsLayer(on){
  S.showArts=on;
  document.getElementById('btn-articles').classList.toggle('active',on);
  render();
}
function toggleDCFilters(){
  const f=document.getElementById('dc-filters');
  const a=document.getElementById('arr-dc-filters');
  const open=f.style.display!=='none';
  f.style.display=open?'none':'block';
  a.classList.toggle('open',!open);
}
function togglePPFilters(){
  const f=document.getElementById('pp-filters');
  const a=document.getElementById('arr-pp-filters');
  const open=f.style.display!=='none';
  f.style.display=open?'none':'block';
  a.classList.toggle('open',!open);
}
// ── Score Builder ──────────────────────────────────────────────────
function buildScoreUI(){
  const c=document.getElementById('sb-vars');
  c.innerHTML='';
  SB_VARS.forEach(v=>{
    const row=document.createElement('div'); row.className='sb-var';
    const active=S.sbWeights[v.id]>0;
    row.innerHTML=
      `<input type="checkbox" id="sbv-${v.id}" ${active?'checked':''}
         onchange="toggleSBVar('${v.id}',this.checked)">` +
      `<label for="sbv-${v.id}">${v.label}</label>` +
      `<input type="number" class="sb-wt" id="sbw-${v.id}"
         value="${S.sbWeights[v.id]||0}" min="0" max="100"
         ${active?'':'disabled'}
         oninput="updateSBWeight('${v.id}',this.value)">` +
      `<span class="sb-pct">%</span>`;
    c.appendChild(row);
  });
  updateSBTotal();
}

function toggleSBVar(id, on){
  if(on){
    // Default to 10 when enabling a new variable
    S.sbWeights[id]=10;
  } else {
    delete S.sbWeights[id];
  }
  const inp=document.getElementById('sbw-'+id);
  if(inp){ inp.disabled=!on; inp.value=S.sbWeights[id]||0; }
  S.h3On=true;
  updateSBTotal();
  render();
}

function updateSBWeight(id, val){
  const n=Math.max(0,Math.min(100,parseInt(val)||0));
  if(n===0){
    delete S.sbWeights[id];
    const cb=document.getElementById('sbv-'+id);
    if(cb) cb.checked=false;
    const inp=document.getElementById('sbw-'+id);
    if(inp){ inp.disabled=true; inp.value=0; }
  } else {
    S.sbWeights[id]=n;
  }
  S.h3On=Object.keys(S.sbWeights).length>0;
  updateSBTotal();
  render();
}

function updateSBTotal(){
  const total=Object.values(S.sbWeights).reduce((s,v)=>s+(v||0),0);
  const el=document.getElementById('sb-total-val');
  if(el){
    el.textContent=total+'%';
    el.className='sb-total-val'+(total>100?' over':'');
  }
}

function loadPreset(name){
  const p=PRESETS[name]||{};
  S.sbWeights={};
  SB_VARS.forEach(v=>{ if(p[v.id]>0) S.sbWeights[v.id]=p[v.id]; });
  S.h3On=Object.keys(S.sbWeights).length>0;
  buildScoreUI();
  render();
}
function setRes(r){
  [3,4,5].forEach(n=>document.getElementById('res-'+n).classList.toggle('active',n===r));
  S.h3Res=r; S.h3On=true;
  Object.keys(CELL_CACHE).forEach(k=>delete CELL_CACHE[k]);
  render();
}
function toggleSB(){
  S.sbOpen=!S.sbOpen;
  const sb=document.getElementById('sidebar');
  sb.classList.add('animated');
  sb.classList.toggle('collapsed',!S.sbOpen);
  const t=document.getElementById('sb-toggle');
  t.innerHTML=S.sbOpen?'&#9664;':'&#9658;';
  t.style.left=S.sbOpen?'252px':'0';
}
function toggleSec(id){
  const b=document.getElementById(id);
  const a=document.getElementById('arr-'+id);
  const open=b.style.display!=='none';
  b.style.display=open?'none':'block';
  if(a) a.classList.toggle('open',!open);
}

// ── Filter builders ────────────────────────────────────────────────
function buildDCStatusChecks(){
  const c=document.getElementById('dc-st-checks');
  DC_STATUSES.forEach(st=>{
    const n=DC_DATA.filter(d=>d.status===st).length;
    if(!n) return;
    const col=DC_STATUS_COLORS_MAP[st]||'#95a5a6';
    const id='dcs-'+st.replace(/[^a-z0-9]/gi,'_');
    S.dcStatuses.add(st);
    const div=document.createElement('div'); div.className='frow';
    div.innerHTML=`<input type="checkbox" id="${id}" checked onchange="toggleDCSt('${st}',this.checked)"><label for="${id}" style="display:flex;align-items:center;gap:5px;"><span style="width:7px;height:7px;border-radius:50%;background:${col};flex-shrink:0;display:inline-block;"></span>${st}</label><span class="fbadge">${n}</span>`;
    c.appendChild(div);
  });
}
function toggleDCSt(st,on){
  on?S.dcStatuses.add(st):S.dcStatuses.delete(st);
  // Update master checkbox state
  const allOn=DC_STATUSES.filter(s=>DC_DATA.some(d=>d.status===s)).every(s=>S.dcStatuses.has(s));
  document.getElementById('chk-dc-all').indeterminate=!allOn&&S.dcStatuses.size>0;
  document.getElementById('chk-dc-all').checked=allOn;
  applyFilters();
}

function buildDCTypeChecks(){
  const types=[...new Set(DC_DATA.map(d=>d.company_type))].sort();
  types.forEach(t=>S.dcTypes.add(t));
  const c=document.getElementById('dc-tp-checks');
  types.forEach(tp=>{
    const n=DC_DATA.filter(d=>d.company_type===tp).length;
    const id='dct-'+tp.replace(/[^a-z0-9]/gi,'_');
    const div=document.createElement('div'); div.className='frow';
    div.innerHTML=`<input type="checkbox" id="${id}" checked onchange="toggleDCTp('${tp}',this.checked)"><label for="${id}">${tp}</label><span class="fbadge">${n}</span>`;
    c.appendChild(div);
  });
}
function toggleDCTp(tp,on){ on?S.dcTypes.add(tp):S.dcTypes.delete(tp); applyFilters(); }

function buildPPTechChecks(){
  const techs=[...new Set(PP_DATA.map(d=>d.tech_group))].sort();
  techs.forEach(t=>S.ppTechs.add(t));
  const c=document.getElementById('pp-tech-checks');
  const techColors={'Natural Gas':'#E74C3C','Solar':'#F1C40F','Wind':'#3498DB','Coal':'#7F8C8D','Nuclear':'#9B59B6','Other/Storage':'#1ABC9C'};
  techs.forEach(tech=>{
    const n=PP_DATA.filter(d=>d.tech_group===tech).length;
    const col=techColors[tech]||'#aaa';
    const id='ppt-'+tech.replace(/[^a-z0-9]/gi,'_');
    const div=document.createElement('div'); div.className='frow';
    div.innerHTML=`<input type="checkbox" id="${id}" checked onchange="togglePPTech('${tech}',this.checked)"><label for="${id}" style="display:flex;align-items:center;gap:5px;"><span style="width:7px;height:7px;border-radius:50%;background:${col};flex-shrink:0;display:inline-block;"></span>${tech}</label><span class="fbadge">${n}</span>`;
    c.appendChild(div);
  });
}
function togglePPTech(tech,on){ on?S.ppTechs.add(tech):S.ppTechs.delete(tech); applyFilters(); }
function togglePPSt(st,on){ on?S.ppStatuses.add(st):S.ppStatuses.delete(st); applyFilters(); }

// ── State filter ───────────────────────────────────────────────────
function buildStateList(){
  const counts={};
  DC_DATA.forEach(d=>{
    if(!d.state||d.state==='nan'||d.state==='null'||d.state.trim()==='') return;
    counts[d.state]=(counts[d.state]||0)+1;
  });
  const states=Object.entries(counts).sort((a,b)=>b[1]-a[1]);
  const c=document.getElementById('state-list');
  states.forEach(([code,cnt])=>{
    const btn=document.createElement('button');
    btn.className='sbtn'; btn.id='sb-'+code; btn.dataset.state=code;
    btn.innerHTML=`<span class="scode">${code}</span><span style="flex:1;text-align:left;">${SN[code]||code}</span><span class="scnt">${cnt}</span>`;
    btn.onclick=()=>toggleState(code);
    c.appendChild(btn);
  });
}
function filterStates(){
  const q=document.getElementById('state-search').value.toLowerCase();
  document.querySelectorAll('.sbtn').forEach(b=>{
    const st=b.dataset.state.toLowerCase();
    b.style.display=(!q||st.includes(q)||(SN[b.dataset.state]||'').toLowerCase().includes(q))?'':'none';
  });
}
function toggleState(code){
  if(S.selStates.has(code)){
    S.selStates.delete(code);
    document.getElementById('sb-'+code)?.classList.remove('active');
  } else {
    S.selStates.add(code);
    document.getElementById('sb-'+code)?.classList.add('active');
    flyTo(code);
  }
  applyFilters();
}
function flyTo(code){
  const b=STATE_BOUNDS[code]; if(!b) return;
  const lon=(b[0]+b[2])/2, lat=(b[1]+b[3])/2;
  const zoom=Math.min(7,Math.max(4,7-Math.log2(Math.max(b[2]-b[0],b[3]-b[1]))));
  deckgl.setProps({initialViewState:{longitude:lon,latitude:lat,zoom,transitionDuration:700}});
}
function clearFilters(){
  S.selStates.clear();
  S.dcStatuses=new Set(DC_STATUSES);
  S.ppStatuses=new Set(['Operational','Planned']);
  S.dcTypes=new Set([...new Set(DC_DATA.map(d=>d.company_type))]);
  S.ppTechs=new Set([...new Set(PP_DATA.map(d=>d.tech_group))]);
  document.querySelectorAll('#dc-st-checks input,#dc-tp-checks input,#pp-tech-checks input').forEach(cb=>cb.checked=true);
  document.getElementById('pp-op').checked=true;
  document.getElementById('pp-pl').checked=true;
  document.getElementById('chk-dc-all').checked=true;
  document.getElementById('chk-dc-all').indeterminate=false;
  document.querySelectorAll('.sbtn').forEach(b=>b.classList.remove('active'));
  applyFilters();
  deckgl.setProps({initialViewState:{longitude:-98.35,latitude:39.5,zoom:4,transitionDuration:600}});
}
function updateClearBtn(){
  const on=S.selStates.size>0||S.dcStatuses.size<DC_STATUSES.length||S.ppStatuses.size<2;
  document.getElementById('btn-clear').style.display=on?'inline-block':'none';
}

// ── Articles drawer ────────────────────────────────────────────────
function openDrawer(){
  S.drawerOpen=true;
  document.getElementById('drawer').classList.add('open');
  document.getElementById('drw-overlay').classList.add('open');
  document.getElementById('btn-news').classList.add('active');
  buildCatButtons();
  renderDrawer();
}
function closeDrawer(){
  S.drawerOpen=false;
  document.getElementById('drawer').classList.remove('open');
  document.getElementById('drw-overlay').classList.remove('open');
  document.getElementById('btn-news').classList.remove('active');
}
function setDays(d){
  S.drawerDays=d;
  [7,30,90,0].forEach(n=>document.getElementById('d-'+n).classList.toggle('active',n===d));
  renderDrawer();
}
function buildCatButtons(){
  const cats=[...new Set(ART_ALL.map(a=>a.category).filter(Boolean))].sort();
  const c=document.getElementById('drw-cats');
  c.innerHTML='';
  const allBtn=document.createElement('button');
  allBtn.className='catbtn'+(S.drawerCat===''?' active':'');
  allBtn.textContent='All'; allBtn.onclick=()=>setCat('');
  c.appendChild(allBtn);
  cats.forEach(cat=>{
    const btn=document.createElement('button');
    btn.className='catbtn'+(S.drawerCat===cat?' active':'');
    btn.textContent=cat; btn.onclick=()=>setCat(cat);
    c.appendChild(btn);
  });
}
function setCat(cat){
  S.drawerCat=cat;
  document.querySelectorAll('.catbtn').forEach(b=>b.classList.toggle('active',b.textContent===(cat||'All')));
  renderDrawer();
}
function renderDrawer(){
  const now=new Date();
  let arts=ART_ALL;
  
  // Filter by selected states
  if(S.selStates.size>0){
    arts=arts.filter(a=>a.state && S.selStates.has(a.state));
  }
  
  // Filter by category
  if(S.drawerCat) arts=arts.filter(a=>a.category===S.drawerCat);
  
  // Filter by date range
  if(S.drawerDays>0){
    const cut=new Date(now-S.drawerDays*86400000);
    arts=arts.filter(a=>{const d=new Date(a.date);return !isNaN(d)&&d>=cut;});
  }
  
  // Already sorted by priority from Python, but re-sort in case of filter
  arts=[...arts].sort((a,b)=>b.priority-a.priority);
  document.getElementById('drw-cnt').textContent=`${arts.length} article${arts.length!==1?'s':''}`;
  const list=document.getElementById('drw-list');
  if(!arts.length){
    list.innerHTML='<div style="padding:18px 14px;color:#444;font-size:12px;">No articles match current filters.</div>';
    return;
  }
  list.innerHTML=arts.map(a=>`
    <div class="aitem">
      <div class="atitle">${a.url?`<a href="${a.url}" target="_blank">${a.title||'Untitled'}</a>`:(a.title||'Untitled')}</div>
      <div class="ameta">
        ${a.category?`<span class="acat">${a.category}</span>`:''}
        ${a.priority>0?`<span class="apri">&#9733; ${a.priority.toFixed(1)}</span>`:''}
        ${a.source?`<span>${a.source}</span>`:''}
        ${a.date?`<span>${fmtDate(a.date)}</span>`:''}
        ${a.state?`<span style="color:#E0FF0066;">${a.state}</span>`:''}
      </div>
      ${a.summary?`<div class="asummary">${a.summary}</div>`:''}
    </div>`).join('');
}

// ── Legend ─────────────────────────────────────────────────────────
(function(){
  const bar=document.getElementById('leg-bar');
  YLORD_COLORS.forEach(rgb=>{const s=document.createElement('div');s.className='lseg';s.style.background=`rgb(${rgb.join(',')})`;bar.appendChild(s);});
})();

const SN={AL:'Alabama',AK:'Alaska',AZ:'Arizona',AR:'Arkansas',CA:'California',CO:'Colorado',CT:'Connecticut',DE:'Delaware',FL:'Florida',GA:'Georgia',HI:'Hawaii',ID:'Idaho',IL:'Illinois',IN:'Indiana',IA:'Iowa',KS:'Kansas',KY:'Kentucky',LA:'Louisiana',ME:'Maine',MD:'Maryland',MA:'Massachusetts',MI:'Michigan',MN:'Minnesota',MS:'Mississippi',MO:'Missouri',MT:'Montana',NE:'Nebraska',NV:'Nevada',NH:'New Hampshire',NJ:'New Jersey',NM:'New Mexico',NY:'New York',NC:'North Carolina',ND:'North Dakota',OH:'Ohio',OK:'Oklahoma',OR:'Oregon',PA:'Pennsylvania',RI:'Rhode Island',SC:'South Carolina',SD:'South Dakota',TN:'Tennessee',TX:'Texas',UT:'Utah',VT:'Vermont',VA:'Virginia',WA:'Washington',WV:'West Virginia',WI:'Wisconsin',WY:'Wyoming',DC:'Washington DC'};

// ── Newsletter panel ───────────────────────────────────────────────
function openNL(){
  document.getElementById('nl-panel').classList.add('open');
  document.getElementById('nl-overlay').classList.add('open');
  document.getElementById('btn-nl').classList.add('active');
  const list=document.getElementById('nl-list');
  if(!NEWSLETTERS||!NEWSLETTERS.length){
    list.innerHTML='<div class="nl-empty">No newsletters published yet.</div>';
    return;
  }
  list.innerHTML=NEWSLETTERS.map(n=>`<a class="nl-item" href="${n.path}" target="_blank" rel="noopener">${n.label}</a>`).join('');
}
function closeNL(){
  document.getElementById('nl-panel').classList.remove('open');
  document.getElementById('nl-overlay').classList.remove('open');
  document.getElementById('btn-nl').classList.remove('active');
}

window.addEventListener('resize',()=>deckgl.setProps({width:window.innerWidth,height:window.innerHeight}));

// ── Init ───────────────────────────────────────────────────────────
buildDCStatusChecks();
buildDCTypeChecks();
buildPPTechChecks();
buildStateList();
buildScoreUI();
applyFilters();
render();
</script>
</body>
</html>"""


if __name__ == "__main__":
    build_deckgl()
