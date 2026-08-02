"""
LightSignal — utils/dates.py
============================
Shared PublishedDate parsing for the news feed.

The feed is written by the article pipeline as "%m/%d/%Y %I:%M %p", but it is
also hand-scrubbed in Excel. Excel rewrites the column on save: most cells come
back as "M/D/YYYY H:MM", and any cell it treats as General comes back as a raw
serial number (e.g. 46230.61319 for 2026-07-27 02:43 PM).

Serial cells parse to NaT, and the newsletter drops NaT rows — so an Excel
round-trip could silently empty a whole week. These helpers accept every form
the column is known to arrive in.
"""

import re
from datetime import datetime, timedelta

import pandas as pd

# Excel's day 1 is 1900-01-01, but its leap-year bug means the usable epoch is
# 1899-12-30 for every date after 1900-02-28 — all feed dates qualify.
EXCEL_EPOCH = datetime(1899, 12, 30)

_SERIAL_RE = re.compile(r"^\d+(\.\d+)?$")

# Formats the column has been observed in, most common first.
_STRING_FORMATS = (
    "%m/%d/%Y %I:%M %p",   # pipeline canonical
    "%m/%d/%Y %H:%M",      # Excel short date + 24h time
    "%m/%d/%Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)


def _from_excel_serial(value: float) -> datetime:
    """Convert an Excel date serial to a datetime, rounded to the minute.

    Serials carry float noise (…59.615999688); the feed's own resolution is
    one minute, so rounding there keeps repaired values stable.
    """
    dt = EXCEL_EPOCH + timedelta(days=value)
    return (dt + timedelta(seconds=30)).replace(second=0, microsecond=0)


def parse_feed_date(value):
    """Parse one PublishedDate cell. Returns a datetime, or None if unparseable."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value

    raw = str(value).strip()
    if not raw or raw.lower() in ("nan", "nat", "none"):
        return None

    if _SERIAL_RE.match(raw):
        try:
            return _from_excel_serial(float(raw))
        except (ValueError, OverflowError):
            return None

    for fmt in _STRING_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue

    # Last resort — let pandas try anything the explicit formats missed.
    parsed = pd.to_datetime(raw, errors="coerce")
    return None if pd.isna(parsed) else parsed.to_pydatetime()


def parse_feed_dates(series: pd.Series) -> pd.Series:
    """Vectorized-ish parse of a PublishedDate column to datetime64, NaT on failure."""
    return pd.to_datetime(series.apply(parse_feed_date), errors="coerce")
