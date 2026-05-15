"""
LightSignal — notify.py
=========================
Sends the daily article pipeline summary email via Outlook (win32com).
No credentials required — uses your logged-in Outlook desktop session.

Called automatically at the end of run_articles.py.
Can also be run standalone to re-send the last summary:
  python scripts/articles/notify.py

Summary includes:
  - Articles fetched from RSS today
  - Extraction failures (articles with no text — nothing downstream ran)
  - Summarization failures
  - Classification results (categories, scores distribution)
  - Duplicate detection count
  - Articles with specific DC mentions
  - Articles in core footprint states
  - Any pipeline errors
"""

import csv
import logging
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from utils.config import (
    FILE_STAGED, FILE_NEWS_FEED,
    SUMMARY_EMAIL_TO, SUMMARY_EMAIL_FROM,
    CORE_FOOTPRINT, EXPANSION_MARKETS,
)

log = logging.getLogger(__name__)


# ── Data collection ───────────────────────────────────────────────────────────

def load_todays_staged(path: Path, run_date: str) -> list:
    """Load articles staged today from staged_articles.csv."""
    if not path.exists():
        return []
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            staged_at = row.get("staged_at", "")
            if staged_at.startswith(run_date):
                rows.append(row)
    return rows


def load_todays_news_feed(path: Path, run_date: str) -> list:
    """Load articles written to news_feed.csv today."""
    if not path.exists():
        return []
    rows = []

    # Convert run_date (YYYY-MM-DD) to match MM/DD/YYYY format in news_feed
    try:
        dt = datetime.strptime(run_date, "%Y-%m-%d")
        date_prefix = dt.strftime("%-m/%-d/%Y")   # e.g. 2/23/2026
    except Exception:
        date_prefix = ""

    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pub = row.get("PublishedDate", "")
            if date_prefix and pub.startswith(date_prefix):
                rows.append(row)
            elif not date_prefix:
                rows.append(row)
    return rows


def build_summary(staged: list, news_feed: list, pipeline_errors: list) -> dict:
    """Compile all metrics into a summary dict."""
    now = datetime.now(timezone.utc)

    # Staged metrics
    total_staged    = len(staged)
    extract_success = sum(1 for r in staged if r.get("extraction_status") == "success")
    extract_fallback= sum(1 for r in staged if r.get("extraction_status") == "fallback")
    extract_failed  = sum(1 for r in staged if r.get("extraction_status") == "failed")
    extract_pending = sum(1 for r in staged if r.get("extraction_status") == "pending")
    summarize_fail  = sum(1 for r in staged if r.get("summarize_status") == "failed")
    classify_fail   = sum(1 for r in staged if r.get("classify_status") == "failed")

    # Articles that got NO text — nothing downstream ran for them
    no_text = [r for r in staged if r.get("extraction_status") == "failed"]

    # News feed metrics
    total_written   = len(news_feed)
    duplicates      = sum(1 for r in news_feed if r.get("Is_Duplicate", "").lower() == "true")
    classified      = total_written - duplicates
    dc_mentions     = sum(1 for r in news_feed
                         if r.get("Mentions_Specific_DC", "").lower() == "true")

    # Category distribution
    categories = Counter(
        r.get("Primary_Category", "")
        for r in news_feed
        if r.get("Primary_Category") and r.get("Is_Duplicate", "").lower() != "true"
    )

    # State distribution — articles in footprint
    footprint_articles = []
    expansion_articles = []
    for r in news_feed:
        if r.get("Is_Duplicate", "").lower() == "true":
            continue
        states_raw = r.get("States", "[]")
        try:
            import json
            states = json.loads(states_raw) if states_raw else []
        except Exception:
            states = []
        if any(s in CORE_FOOTPRINT for s in states):
            footprint_articles.append(r)
        elif any(s in EXPANSION_MARKETS for s in states):
            expansion_articles.append(r)

    # High-signal articles (strategy >= 4)
    high_signal = [
        r for r in news_feed
        if r.get("Is_Duplicate", "").lower() != "true"
        and _safe_float(r.get("Strategy_Alignment_Score")) >= 4
    ]

    return {
        "timestamp"         : now.strftime("%Y-%m-%d %H:%M UTC"),
        "run_date"          : now.strftime("%B %d, %Y"),
        "total_staged"      : total_staged,
        "extract_success"   : extract_success,
        "extract_fallback"  : extract_fallback,
        "extract_failed"    : extract_failed,
        "extract_pending"   : extract_pending,
        "summarize_fail"    : summarize_fail,
        "classify_fail"     : classify_fail,
        "no_text_articles"  : no_text,
        "total_written"     : total_written,
        "duplicates"        : duplicates,
        "classified"        : classified,
        "dc_mentions"       : dc_mentions,
        "categories"        : categories,
        "footprint_articles": footprint_articles,
        "expansion_articles": expansion_articles,
        "high_signal"       : high_signal,
        "pipeline_errors"   : pipeline_errors,
    }


def _safe_float(val) -> float:
    try:
        return float(val or 0)
    except (ValueError, TypeError):
        return 0.0


# ── Email rendering ───────────────────────────────────────────────────────────

def render_html(s: dict) -> str:
    """Render the summary as an HTML email body."""

    def section(title):
        return f'<h3 style="color:#1a1a2e;border-bottom:2px solid #E0FF00;padding-bottom:4px;margin-top:24px;">{title}</h3>'

    def kpi(label, value, color="#1a1a2e"):
        return (
            f'<div style="display:inline-block;background:#f5f5f5;border-radius:8px;'
            f'padding:12px 20px;margin:6px;text-align:center;min-width:100px;">'
            f'<div style="font-size:28px;font-weight:700;color:{color};">{value}</div>'
            f'<div style="font-size:12px;color:#666;margin-top:2px;">{label}</div>'
            f'</div>'
        )

    def article_row(r):
        title  = r.get("Title", "")[:80]
        url    = r.get("CleanURL", "")
        cat    = r.get("Primary_Category", "")
        states = r.get("States", "[]")
        sa     = r.get("Strategy_Alignment_Score", "")
        rel    = r.get("Relevance_Score", "")
        link   = f'<a href="{url}" style="color:#2980b9;">{title}</a>' if url else title
        return (
            f'<tr style="border-bottom:1px solid #eee;">'
            f'<td style="padding:6px 8px;">{link}</td>'
            f'<td style="padding:6px 8px;color:#666;font-size:12px;">{cat}</td>'
            f'<td style="padding:6px 8px;text-align:center;">{sa}</td>'
            f'<td style="padding:6px 8px;text-align:center;">{rel}</td>'
            f'<td style="padding:6px 8px;color:#666;font-size:12px;">{states}</td>'
            f'</tr>'
        )

    def table_header():
        return (
            f'<table style="width:100%;border-collapse:collapse;font-size:13px;">'
            f'<tr style="background:#f0f0f0;">'
            f'<th style="padding:6px 8px;text-align:left;">Title</th>'
            f'<th style="padding:6px 8px;text-align:left;">Category</th>'
            f'<th style="padding:6px 8px;">Strategy</th>'
            f'<th style="padding:6px 8px;">Relevance</th>'
            f'<th style="padding:6px 8px;text-align:left;">States</th>'
            f'</tr>'
        )

    # Health color
    has_issues = s["extract_failed"] > 0 or s["pipeline_errors"] or s["classify_fail"] > 0
    status_color = "#e74c3c" if has_issues else "#27ae60"
    status_text  = "⚠ Issues Detected" if has_issues else "✓ Clean Run"

    html = f"""
<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#333;max-width:900px;margin:0 auto;padding:20px;">

<div style="background:#1a1a2e;color:#E0FF00;padding:16px 24px;border-radius:8px;margin-bottom:20px;">
  <div style="font-size:22px;font-weight:700;">LightSignal Market Intel</div>
  <div style="font-size:14px;opacity:0.8;margin-top:4px;">Daily Pipeline Summary — {s['run_date']}</div>
  <div style="margin-top:8px;font-size:13px;background:rgba(255,255,255,0.1);
              display:inline-block;padding:4px 12px;border-radius:4px;color:{'#ff6b6b' if has_issues else '#E0FF00'};">
    {status_text}
  </div>
</div>

{section("Pipeline Overview")}
<div>
  {kpi("Fetched from RSS", s['total_staged'])}
  {kpi("Extracted", s['extract_success'], "#27ae60")}
  {kpi("Fallback only", s['extract_fallback'], "#f39c12")}
  {kpi("Extraction Failed", s['extract_failed'], "#e74c3c" if s['extract_failed'] else "#999")}
  {kpi("Classified", s['classified'], "#2980b9")}
  {kpi("Duplicates", s['duplicates'], "#8e44ad")}
  {kpi("DC Mentions", s['dc_mentions'], "#16a085")}
</div>
"""

    # Extraction failures — these are the most important to call out
    if s["no_text_articles"]:
        html += f"""
{section(f"⚠ Extraction Failures — {len(s['no_text_articles'])} Articles Got No Text")}
<p style="color:#666;font-size:13px;">These articles were fetched but text extraction failed.
No summarization, classification, or duplicate detection ran for them.
They will be retried on the next pipeline run.</p>
<table style="width:100%;border-collapse:collapse;font-size:13px;">
<tr style="background:#fdf2f2;"><th style="padding:6px 8px;text-align:left;">Title</th>
<th style="padding:6px 8px;text-align:left;">Raw URL</th></tr>
"""
        for r in s["no_text_articles"]:
            title = r.get("title", "")[:80]
            url   = r.get("raw_url", "")[:80]
            html += f'<tr><td style="padding:6px 8px;">{title}</td><td style="padding:6px 8px;color:#999;font-size:11px;">{url}</td></tr>'
        html += "</table>"

    # High signal articles
    if s["high_signal"]:
        html += f"""
{section(f"🎯 High Signal Articles (Strategy ≥ 4) — {len(s['high_signal'])} today")}
{table_header()}
"""
        for r in s["high_signal"][:20]:
            html += article_row(r)
        html += "</table>"
        if len(s["high_signal"]) > 20:
            html += f'<p style="color:#999;font-size:12px;">+ {len(s["high_signal"]) - 20} more</p>'

    # Footprint articles
    if s["footprint_articles"]:
        html += f"""
{section(f"📍 Core Footprint Articles — {len(s['footprint_articles'])} today")}
<p style="color:#666;font-size:12px;">NY, NJ, CT, MA, PA, OH, FL, AZ</p>
{table_header()}
"""
        for r in s["footprint_articles"][:15]:
            html += article_row(r)
        html += "</table>"

    # Expansion market articles
    if s["expansion_articles"]:
        html += f"""
{section(f"📈 Expansion Market Articles — {len(s['expansion_articles'])} today")}
<p style="color:#666;font-size:12px;">TX, WI, IL, MO, IN, MI, VA, WV, UT</p>
{table_header()}
"""
        for r in s["expansion_articles"][:10]:
            html += article_row(r)
        html += "</table>"

    # Category breakdown
    if s["categories"]:
        html += f"{section('Category Distribution')}<table style='width:100%;border-collapse:collapse;font-size:13px;'>"
        for cat, count in s["categories"].most_common():
            pct = round(count / max(s["classified"], 1) * 100)
            bar = "█" * (pct // 5)
            html += (
                f'<tr><td style="padding:4px 8px;width:280px;">{cat}</td>'
                f'<td style="padding:4px 8px;color:#2980b9;font-weight:700;">{count}</td>'
                f'<td style="padding:4px 8px;color:#bbb;font-size:12px;">{bar} {pct}%</td></tr>'
            )
        html += "</table>"

    # Pipeline errors
    if s["pipeline_errors"]:
        html += f"""
{section(f"⚠ Pipeline Errors — {len(s['pipeline_errors'])}")}
<pre style="background:#fdf2f2;padding:12px;border-radius:4px;font-size:12px;overflow:auto;">
{''.join(s['pipeline_errors'][:20])}
</pre>
"""

    # Classify/summarize failures
    if s["summarize_fail"] or s["classify_fail"]:
        html += f"""
{section("Processing Failures")}
<p>Summarization failures: <strong>{s['summarize_fail']}</strong> &nbsp;&nbsp;
Classification failures: <strong>{s['classify_fail']}</strong></p>
<p style="color:#999;font-size:12px;">These will be retried on the next run.</p>
"""

    html += f"""
<div style="margin-top:32px;padding-top:12px;border-top:1px solid #eee;
            color:#999;font-size:11px;text-align:center;">
  LightSignal Market Intel · Generated {s['timestamp']} ·
  Powered by Claude + Voyage AI
</div>
</body></html>
"""
    return html


def render_plain(s: dict) -> str:
    """Plain text version of the summary."""
    lines = [
        f"LightSignal Market Intel — Daily Summary",
        f"{s['run_date']}  |  {s['timestamp']}",
        "=" * 60,
        "",
        "PIPELINE OVERVIEW",
        f"  Fetched from RSS:      {s['total_staged']}",
        f"  Extracted successfully: {s['extract_success']}",
        f"  Fallback (RSS text):   {s['extract_fallback']}",
        f"  Extraction failures:   {s['extract_failed']}",
        f"  Classified:            {s['classified']}",
        f"  Duplicates detected:   {s['duplicates']}",
        f"  DC mentions:           {s['dc_mentions']}",
        "",
    ]

    if s["no_text_articles"]:
        lines += [
            f"⚠ EXTRACTION FAILURES ({len(s['no_text_articles'])} articles — no downstream processing)",
        ]
        for r in s["no_text_articles"]:
            lines.append(f"  - {r.get('title', '')[:70]}")
        lines.append("")

    if s["high_signal"]:
        lines += [f"HIGH SIGNAL ARTICLES (Strategy ≥ 4) — {len(s['high_signal'])}"]
        for r in s["high_signal"][:10]:
            lines.append(
                f"  [{r.get('Strategy_Alignment_Score','')}/{r.get('Relevance_Score','')}] "
                f"{r.get('Title','')[:65]}"
            )
        lines.append("")

    if s["categories"]:
        lines += ["CATEGORY BREAKDOWN"]
        for cat, count in s["categories"].most_common():
            lines.append(f"  {cat:<40} {count}")
        lines.append("")

    if s["pipeline_errors"]:
        lines += [f"PIPELINE ERRORS ({len(s['pipeline_errors'])})"]
        lines += s["pipeline_errors"][:10]

    return "\n".join(lines)


# ── Send email ────────────────────────────────────────────────────────────────

def send_email(subject: str, html_body: str, plain_body: str) -> bool:
    """Send email via Outlook desktop using win32com."""
    try:
        import win32com.client
        outlook = win32com.client.Dispatch("Outlook.Application")
        mail    = outlook.CreateItem(0)  # 0 = olMailItem

        mail.To      = SUMMARY_EMAIL_TO
        mail.Subject = subject
        mail.HTMLBody = html_body
        mail.Body    = plain_body

        if SUMMARY_EMAIL_FROM:
            # Send from a specific account if configured
            for account in outlook.Session.Accounts:
                if account.SmtpAddress.lower() == SUMMARY_EMAIL_FROM.lower():
                    mail._oleobj_.Invoke(
                        *(64209, 0, 8, 0, account)  # SendUsingAccount
                    )
                    break

        mail.Send()
        log.info(f"  Summary email sent to: {SUMMARY_EMAIL_TO}")
        return True

    except ImportError:
        log.warning("  win32com not available — email not sent. Install: pip install pywin32")
        return False
    except Exception as e:
        log.error(f"  Failed to send email: {e}")
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def send_daily_summary(pipeline_errors: list = None) -> bool:
    """
    Build and send the daily summary email.
    Called by run_articles.py at the end of each run.

    Args:
        pipeline_errors: List of error strings collected during the run.
    """
    if not SUMMARY_EMAIL_TO:
        log.info("  SUMMARY_EMAIL_TO not configured — skipping email.")
        return False

    pipeline_errors = pipeline_errors or []
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    staged    = load_todays_staged(FILE_STAGED, run_date)
    news_feed = load_todays_news_feed(FILE_NEWS_FEED, run_date)
    summary   = build_summary(staged, news_feed, pipeline_errors)

    has_issues = summary["extract_failed"] > 0 or pipeline_errors
    status     = "⚠ Issues" if has_issues else "✓ Clean"
    subject    = (
        f"[LightSignal] {status} — "
        f"{summary['classified']} articles, "
        f"{summary['dc_mentions']} DC mentions — "
        f"{summary['run_date']}"
    )

    html_body  = render_html(summary)
    plain_body = render_plain(summary)

    return send_email(subject, html_body, plain_body)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  [%(levelname)s]  %(message)s",
        datefmt="%H:%M:%S",
    )
    send_daily_summary()
