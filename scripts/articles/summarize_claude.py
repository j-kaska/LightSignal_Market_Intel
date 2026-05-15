"""
LEGACY ROLLBACK — summarize_claude.py
=======================================
Original Claude-based summarization. Kept as rollback only.
To reactivate: in run_articles.py, import from articles.summarize_claude
instead of articles.summarize.

Original: LightSignal — summarize.py
============================
Stage 3 of the article pipeline.

For each article with summarize_status = "pending" and a non-empty
article_text or rss_description in staged_articles.csv:
  - Sends the article text to Claude
  - Writes a 2-4 sentence Summary_AI
  - Updates summarize_status = "success" or "failed"

Uses the same SSL bypass and JSON-fence stripping as classify.py.

Run directly:
  python scripts/articles/summarize.py

Or called by:
  python scripts/articles/run_articles.py
"""

import csv
import httpx
import logging
import re
import sys
import time
from pathlib import Path

import anthropic

# ── Path setup ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from utils.config import FILE_STAGED, ARTICLES_MODEL

# ── Logging ───────────────────────────────────────────────────────────────────
log = logging.getLogger(__name__)

# ── SSL bypass (corporate network) ───────────────────────────────────────────
_http_client = httpx.Client(verify=False)

# ── Summarization prompt ──────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a market intelligence analyst summarizing news articles for 
a long-haul fiber and network infrastructure company. Write concise, factual summaries 
that capture the key market signal in the article.

Rules:
- 2 to 4 sentences only
- Focus on the infrastructure, data center, or network angle
- Include specific details: company names, locations, MW capacity, dollar amounts if mentioned
- Avoid generic statements — be specific to what's actually in the article
- Do not editorialize or add your own analysis
- Write in third person, past or present tense
- Return only the summary text — no labels, no bullet points, no markdown"""


def build_prompt(title: str, text: str) -> str:
    # Use first 3000 chars of article text — enough for a good summary
    snippet = text[:3000].strip()
    return f"Article title: {title}\n\nArticle text:\n{snippet}\n\nWrite a 2-4 sentence summary."


def summarize_article(client: anthropic.Anthropic, title: str, text: str) -> str | None:
    """Call Claude to summarize one article. Returns summary string or None on failure."""
    for attempt in range(3):
        try:
            response = client.messages.create(
                model=ARTICLES_MODEL,
                max_tokens=300,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": build_prompt(title, text)}],
            )
            summary = response.content[0].text.strip()
            # Strip any accidental markdown fences
            summary = re.sub(r'^```[a-z]*\s*', '', summary)
            summary = re.sub(r'\s*```$', '', summary).strip()
            return summary
        except Exception as e:
            wait = 10 * (attempt + 1)
            log.warning(f"    Summarize error (attempt {attempt + 1}): {str(e)[:80]} — waiting {wait}s")
            time.sleep(wait)
    return None


# ── Staging helpers ───────────────────────────────────────────────────────────

def load_staging(path: Path) -> list:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_staging(path: Path, rows: list) -> None:
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


# ── Main ──────────────────────────────────────────────────────────────────────

def summarize_articles() -> tuple:
    """
    Summarize all pending articles in the staging file.
    Returns (success_count, failed_count).
    """
    log.info("=" * 60)
    log.info("  Stage 3: Summarize Articles")
    log.info("=" * 60)

    rows    = load_staging(FILE_STAGED)
    pending = [
        r for r in rows
        if r.get("summarize_status") == "pending"
        and r.get("extraction_status") in ("success", "fallback")
        and (r.get("article_text") or r.get("rss_description"))
    ]

    log.info(f"  Pending summarization: {len(pending)}")

    if not pending:
        log.info("  Nothing to summarize.")
        return 0, 0

    client    = anthropic.Anthropic(http_client=_http_client)
    row_by_id = {r["article_id"]: r for r in rows}

    success_count = 0
    failed_count  = 0

    for i, article in enumerate(pending, 1):
        article_id = article["article_id"]
        title      = article.get("title", "")
        text       = article.get("article_text") or article.get("rss_description", "")

        log.info(f"  [{i:3}/{len(pending)}] {title[:65]}")

        summary = summarize_article(client, title, text)

        if summary:
            row_by_id[article_id]["summary_ai"]        = summary
            row_by_id[article_id]["summarize_status"]  = "success"
            log.info(f"         ✓  {summary[:80]}...")
            success_count += 1
        else:
            row_by_id[article_id]["summarize_status"] = "failed"
            log.warning(f"         ✗  Summarization failed")
            failed_count += 1

    save_staging(FILE_STAGED, list(row_by_id.values()))

    log.info(f"  Summarized: {success_count}")
    log.info(f"  Failed:     {failed_count}")

    return success_count, failed_count


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  [%(levelname)s]  %(message)s",
        datefmt="%H:%M:%S",
    )
    summarize_articles()
