"""
LightSignal — text_utils.py
=============================
Shared text-processing utilities used across the article pipeline.

Functions:
  extract_sentences(text, n, fallback)   — first-N-sentences extraction
  normalize_title(title)                 — lowercase, strip punctuation for dedup
  canonicalize_url(url)                  — strip tracking params for URL dedup
"""

import hashlib
import re
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse


# ── Sentence extraction ───────────────────────────────────────────────────────

def extract_sentences(text: str, n: int = 3, fallback: str = "") -> str:
    """
    Extract the first n complete sentences from text.
    Falls back to `fallback` if text is empty or unparseable.

    Designed for news article bodies. The first N sentences contain the
    journalistic lede — the most information-dense part of the article.
    """
    if not text or not isinstance(text, str):
        return fallback

    text = text.strip()
    if not text:
        return fallback

    # Split on sentence-ending punctuation followed by whitespace.
    # Lookbehind keeps the terminal punctuation on the sentence.
    parts = re.split(r'(?<=[.!?])\s+', text)

    # Filter out very short fragments (abbreviations, initials, etc.)
    sentences = [s.strip() for s in parts if len(s.strip()) > 25]

    if not sentences:
        # Fallback: return first 500 chars if no clean sentence break found
        return text[:500]

    return " ".join(sentences[:n])


# ── Title normalization ───────────────────────────────────────────────────────

_PUNCT_RE = re.compile(r"[^\w\s]")
_SPACE_RE = re.compile(r"\s+")

def normalize_title(title: str) -> str:
    """
    Lowercase, remove punctuation, collapse whitespace.
    Used to produce a stable fingerprint for duplicate detection.
    """
    if not title:
        return ""
    t = title.lower()
    t = _PUNCT_RE.sub(" ", t)
    t = _SPACE_RE.sub(" ", t).strip()
    return t


def title_fingerprint(title: str) -> str:
    """SHA-256 of the normalized title. Identical titles → same fingerprint."""
    return hashlib.sha256(normalize_title(title).encode()).hexdigest()


# ── URL canonicalization ──────────────────────────────────────────────────────

# Query params that are purely tracking and carry no URL identity
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "referrer", "source", "cid", "gclid", "fbclid", "msclkid",
    "mc_cid", "mc_eid", "_ga", "igshid",
}


def canonicalize_url(url: str) -> str:
    """
    Strip tracking query parameters and fragments from a URL so that
    the same article at different tracking endpoints compares as equal.
    """
    if not url:
        return url
    try:
        parsed = urlparse(url.strip())
        # Remove fragment
        clean = parsed._replace(fragment="")
        # Strip tracking params from query string
        qs = parse_qs(parsed.query, keep_blank_values=False)
        qs_clean = {k: v for k, v in qs.items() if k.lower() not in _TRACKING_PARAMS}
        clean = clean._replace(query=urlencode(qs_clean, doseq=True))
        return urlunparse(clean).lower().rstrip("/")
    except Exception:
        return url.lower().strip()


# ── State extraction ──────────────────────────────────────────────────────────

# Full state names → 2-letter codes, including common geographic aliases
STATE_NAMES_TO_CODE: dict[str, str] = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT",
    "Delaware": "DE", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN",
    "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA",
    "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA",
    "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM",
    "New York": "NY", "North Carolina": "NC", "North Dakota": "ND",
    "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD",
    "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT",
    "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY",
    # Washington DC variants
    "Washington D.C.": "DC", "Washington DC": "DC",
    "District of Columbia": "DC",
    # Common geographic aliases used in DC/tech press
    "Northern Virginia": "VA", "North Virginia": "VA",
    "Silicon Valley": "CA", "Bay Area": "CA",
    "Research Triangle": "NC", "Triangle Park": "NC",
    "Chicagoland": "IL",
}

# Pre-build lowercase lookup for fast matching
_STATE_LOWER: dict[str, str] = {k.lower(): v for k, v in STATE_NAMES_TO_CODE.items()}
_ALL_CODES: frozenset[str] = frozenset(STATE_NAMES_TO_CODE.values())
# Compiled pattern for standalone state abbreviations
_ABBREV_RE = re.compile(r'\b(' + '|'.join(sorted(_ALL_CODES, key=len, reverse=True)) + r')\b')


def extract_states(text: str) -> list[str]:
    """
    Extract US state codes from free text.
    Returns a sorted, deduplicated list of 2-letter state codes.
    """
    if not text:
        return []

    found: set[str] = set()
    text_lower = text.lower()

    # 1. Full state names (case-insensitive)
    for name_lower, code in _STATE_LOWER.items():
        if name_lower in text_lower:
            found.add(code)

    # 2. Standalone 2-letter abbreviations in original-case text
    for m in _ABBREV_RE.finditer(text):
        found.add(m.group(1))

    return sorted(found)
