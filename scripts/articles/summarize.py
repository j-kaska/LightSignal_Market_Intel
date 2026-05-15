"""
LightSignal — summarize.py
============================
Stage 3 of the article pipeline.

For each article with summarize_status = "pending" and a non-empty
article_text or rss_description in staged_articles.csv:
  - Sends the article text to the configured LLM
  - Writes a 2-4 sentence Summary_AI
  - Updates summarize_status = "success" or "failed"

Provider is controlled by LIGHTSIGNAL_PROVIDER env var (default: gemini).
Set to "anthropic" to use Claude instead.
If Gemini quota is exhausted mid-run, automatically falls back to Anthropic.

Run directly:
  python scripts/articles/summarize.py

Or called by:
  python scripts/articles/run_articles.py
"""

import csv
import httpx
import logging
import os
import re
import sys
import time
from pathlib import Path

from openai import OpenAI

# ── Path setup ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from utils.config import (
    FILE_STAGED, ARTICLES_MODEL, GEMINI_BASE_URL,
    API_PROVIDER, ANTHROPIC_MODEL,
)

# ── Logging ───────────────────────────────────────────────────────────────────
log = logging.getLogger(__name__)

# ── Quota sentinel ────────────────────────────────────────────────────────────
QUOTA_EXHAUSTED = object()

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
    snippet = text[:3000].strip()
    return f"Article title: {title}\n\nArticle text:\n{snippet}\n\nWrite a 2-4 sentence summary."


# ── Provider-agnostic client + call ──────────────────────────────────────────

def _make_client(provider=None):
    p = provider or API_PROVIDER
    if p == "anthropic":
        import anthropic
        return anthropic.Anthropic(http_client=_http_client)
    return OpenAI(
        base_url=GEMINI_BASE_URL,
        api_key=os.environ.get("GEMINI_API_KEY", ""),
        http_client=_http_client,
    )


def _is_rate_limit(e: Exception) -> bool:
    return getattr(e, "status_code", None) == 429 or "RateLimitError" in type(e).__name__


def _call_llm(client, user_content: str, max_tokens: int, provider=None) -> str:
    p = provider or API_PROVIDER
    if p == "anthropic":
        import anthropic
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        return response.content[0].text.strip()
    response = client.chat.completions.create(
        model=ARTICLES_MODEL,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_content},
        ],
    )
    return response.choices[0].message.content.strip()


def summarize_article(client, title: str, text: str, provider=None):
    """
    Call configured LLM to summarize one article.
    Returns summary string, None on non-quota failure, or QUOTA_EXHAUSTED sentinel
    if all retries failed with rate-limit/billing errors.
    """
    rate_limit_count = 0
    for attempt in range(3):
        try:
            raw = _call_llm(client, build_prompt(title, text), max_tokens=300, provider=provider)
            raw = re.sub(r'^```[a-z]*\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw).strip()
            return raw
        except Exception as e:
            if _is_rate_limit(e):
                rate_limit_count += 1
                wait = 60 * (attempt + 1)   # 60 / 120 / 180s
                log.warning(f"    Rate limited (attempt {attempt + 1}): waiting {wait}s")
            else:
                wait = 10 * (attempt + 1)
                log.warning(f"    Summarize error (attempt {attempt + 1}): {str(e)[:80]} — waiting {wait}s")
            time.sleep(wait)
    if rate_limit_count == 3:
        return QUOTA_EXHAUSTED
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
    log.info(f"  Provider: {API_PROVIDER}")
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

    active_provider    = API_PROVIDER
    active_client      = _make_client()
    fallback_triggered = False
    row_by_id          = {r["article_id"]: r for r in rows}

    success_count     = 0
    failed_count      = 0
    consecutive_fails = 0
    MAX_CONSECUTIVE   = 5

    for i, article in enumerate(pending, 1):
        article_id = article["article_id"]
        title      = article.get("title", "")
        text       = article.get("article_text") or article.get("rss_description", "")

        log.info(f"  [{i:3}/{len(pending)}] {title[:65]}")

        summary = summarize_article(active_client, title, text, provider=active_provider)

        # Gemini quota exhausted — switch to Anthropic and retry this article
        if summary is QUOTA_EXHAUSTED and not fallback_triggered and active_provider == "gemini":
            log.warning("  ⚠  Gemini quota exhausted — switching to Anthropic fallback for this run")
            active_client      = _make_client("anthropic")
            active_provider    = "anthropic"
            fallback_triggered = True
            summary = summarize_article(active_client, title, text, provider="anthropic")

        if summary and summary is not QUOTA_EXHAUSTED:
            row_by_id[article_id]["summary_ai"]       = summary
            row_by_id[article_id]["summarize_status"] = "success"
            log.info(f"         ✓  {summary[:80]}...")
            success_count    += 1
            consecutive_fails = 0
        else:
            row_by_id[article_id]["summarize_status"] = "failed"
            log.warning(f"         ✗  Summarization failed")
            failed_count      += 1
            consecutive_fails += 1
            if consecutive_fails >= MAX_CONSECUTIVE:
                log.error(f"  {MAX_CONSECUTIVE} consecutive failures — quota likely exhausted. Stopping.")
                break

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
