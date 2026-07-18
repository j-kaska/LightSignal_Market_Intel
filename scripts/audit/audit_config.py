"""
LightSignal — audit/audit_config.py
===================================
Audit-only constants. Imports shared paths from utils.config but never mutates it.

The live pipeline imports utils.config; adding constants there is a pipeline fix,
not an audit concern. Keep the two separate.
"""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from utils.config import (  # noqa: E402
    FILE_NEWS_FEED, FILE_STAGED, FILE_DC,
    ARTICLES_DIR,
    CORE_FOOTPRINT, EXPANSION_MARKETS,
)

# ── Audit output tree (the only places this package writes) ───────────────────
AUDIT_DIR          = ROOT / "data" / "audit"
CACHE_DIR          = AUDIT_DIR / "cache"
LLM_CACHE_DIR      = CACHE_DIR / "llm"
EMBED_CACHE_DIR    = CACHE_DIR / "embeddings"
REPORTS_DIR        = ROOT / "output" / "audit"

FILE_LABELS_XLSX     = ARTICLES_DIR / "test" / "news_feed_test_feedback.xlsx"
FILE_LABELS_SNAPSHOT = CACHE_DIR / "labels_snapshot.pkl"
FILE_METRICS_HISTORY = REPORTS_DIR / "metrics_history.jsonl"

for _d in (AUDIT_DIR, CACHE_DIR, LLM_CACHE_DIR, EMBED_CACHE_DIR, REPORTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ── Files the audit must never modify ────────────────────────────────────────
READ_ONLY_FILES = (FILE_NEWS_FEED, FILE_DC, FILE_STAGED)

# ── Auditor model ─────────────────────────────────────────────────────────────
# Deliberately a different family AND a different input than the pipeline
# (gemini-2.5-flash-lite over Summary_AI only). An auditor correlated with the
# thing it audits is worthless.
AUDIT_MODEL   = "claude-opus-4-8"
AUDIT_EFFORT  = "high"
PROMPT_VERSION = "v1"

# Opus 4.8 rejects temperature/top_p/top_k with a 400 (removed in the 4.7/4.8
# family). Reproducibility comes from the disk cache keyed by
# sha256(model + PROMPT_VERSION + article_id + text_hash), not from temperature=0.

# ── Embedding (local, free) ───────────────────────────────────────────────────
# The pipeline uses all-MiniLM-L6-v2 (384-dim) over `title + rss_description`.
# We use a stronger encoder over `title + full article text` — the whole point is
# to see what the pipeline's weaker signal misses.
AUDIT_EMBED_MODEL = "BAAI/bge-base-en-v1.5"

# Dedup cut heights are FITTED on DEV against the human dupe labels (labels.py),
# never guessed. These are only fallbacks if fitting hasn't run yet.
FALLBACK_SAME_STORY_SIM = 0.90   # tight  -> true duplicate, suppress
FALLBACK_SAME_TOPIC_SIM = 0.78   # loose  -> "adjacent article, same topic", group

# ── Newsletter selection contract (scripts/generate_newsletter.py) ───────────
# Drives the impact ranking: a score disagreement only matters if it moves an
# article across this threshold.
NEWSLETTER_MIN_COMBINED = 7
NEWSLETTER_TOP_N        = 10

# ── Text sourcing ─────────────────────────────────────────────────────────────
# news_feed.csv truncates Article_Text to 2000 chars; staged_articles.csv keeps
# up to 8000. Prefer staging, fall back to the feed.
MAX_ARTICLE_CHARS = 8000
MIN_ARTICLE_CHARS = 120   # below this there is nothing to judge — flag, don't score

# ── Fields the pipeline produced. These must NEVER appear in an auditor prompt. ─
# Enforced by tests/test_blindness.py. Without this the auditor drifts toward
# agreeing with the thing it is supposed to be checking.
PIPELINE_FIELDS = (
    "Summary_AI",
    "Primary_Category",
    "Secondary_Categories",
    "Strategy_Alignment_Score",
    "Relevance_Score",
    "Mentions_Specific_DC",
    "Is_Duplicate",
    "Duplicate_Of",
    "DC_ID",
)
