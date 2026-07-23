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
Set LIGHTSIGNAL_RETRY_FAILED_SUMMARIES=1 to also retry rows with summarize_status="failed".

Run directly:
  python scripts/articles/summarize.py

Or called by:
  python scripts/articles/run_articles.py
"""

import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from articles._llm import (
    QUOTA_EXHAUSTED, PROVIDER_UNAVAILABLE,
    call_with_retries, make_client as _make_client,
)
from articles._staging import load_staging, save_staging, staged_on_or_after
from utils.config import (
    FILE_STAGED, API_PROVIDER,
    LLM_MAX_WORKERS, SUMMARIZE_MAX_ATTEMPTS,
)

# ── Logging ───────────────────────────────────────────────────────────────────
log = logging.getLogger(__name__)

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


def summarize_article(client, title: str, text: str, provider=None):
    """
    Call configured LLM to summarize one article.
    Returns summary string, None on non-quota failure, or QUOTA_EXHAUSTED sentinel
    if all retries failed with rate-limit/billing errors.
    """
    return call_with_retries(
        client,
        SYSTEM_PROMPT,
        build_prompt(title, text),
        max_tokens=300,
        provider=provider,
        label="Summarize",
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def summarize_articles(since: str = "") -> tuple:
    """
    Summarize all pending articles in the staging file.

    `since` (ISO YYYY-MM-DD) restricts the run to articles staged on or after
    that date; older rows keep their status and are picked up by a later
    unscoped run.

    Returns (success_count, failed_count).
    """
    log.info("=" * 60)
    log.info("  Stage 3: Summarize Articles")
    log.info(f"  Provider: {API_PROVIDER}")
    if since:
        log.info(f"  Scope: articles staged on or after {since}")

    # Failed rows are retried automatically until they hit SUMMARIZE_MAX_ATTEMPTS.
    # The env var forces a retry of everything, ignoring the attempt cap.
    force_retry = os.environ.get("LIGHTSIGNAL_RETRY_FAILED_SUMMARIES", "0") == "1"
    if force_retry:
        log.info("  Retry mode: forced (ignoring attempt cap)")

    log.info("=" * 60)

    def _attempts(r) -> int:
        try:
            return int(r.get("summarize_attempts") or 0)
        except (TypeError, ValueError):
            return 0

    def _eligible(r) -> bool:
        status = r.get("summarize_status")
        if status == "pending":
            return True
        if status == "failed":
            return force_retry or _attempts(r) < SUMMARIZE_MAX_ATTEMPTS
        return False

    rows = load_staging(FILE_STAGED)

    # Backfill the retry counter on staging files written before it existed, so
    # every row carries the column and DictWriter sees a consistent schema.
    for r in rows:
        r.setdefault("summarize_attempts", "0")
        if r.get("summarize_attempts") is None:
            r["summarize_attempts"] = "0"

    def _ready(r) -> bool:
        return (
            _eligible(r)
            and r.get("extraction_status") in ("success", "fallback")
            and (r.get("article_text") or r.get("rss_description"))
        )

    eligible = [r for r in rows if _ready(r)]
    pending  = [r for r in staged_on_or_after(rows, since) if _ready(r)]

    deferred = len(eligible) - len(pending)
    retrying = sum(1 for r in pending if r.get("summarize_status") == "failed")
    log.info(f"  Pending summarization: {len(pending)}"
             + (f"  ({retrying} retries of earlier failures)" if retrying else ""))
    if deferred:
        log.info(f"  Deferred by --since: {deferred} older articles left for a later run")

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

    # Articles are independent, so they are summarized concurrently in batches.
    # Batching (rather than one big submit) keeps the quota-fallback switch and the
    # consecutive-failure circuit breaker meaningful: each batch sees the provider
    # chosen by the previous one, and results are applied in submission order.
    workers = max(1, min(LLM_MAX_WORKERS, len(pending)))
    log.info(f"  Concurrency: {workers} workers")

    def _article_text(a):
        return a.get("article_text") or a.get("rss_description", "")

    stop = False
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for batch_start in range(0, len(pending), workers):
            if stop:
                break
            batch = pending[batch_start:batch_start + workers]

            client, provider = active_client, active_provider
            results = list(pool.map(
                lambda a: summarize_article(client, a.get("title", ""), _article_text(a),
                                            provider=provider),
                batch,
            ))

            # Gemini quota exhausted — switch provider once, then redo the affected rows
            if (not fallback_triggered and active_provider == "gemini"
                    and any(r is QUOTA_EXHAUSTED for r in results)):
                log.warning("  ⚠  Gemini quota exhausted — switching to Anthropic fallback for this run")
                active_client      = _make_client("anthropic")
                active_provider    = "anthropic"
                fallback_triggered = True
                redo = [j for j, r in enumerate(results) if r is QUOTA_EXHAUSTED]
                fb_client = active_client
                retried = list(pool.map(
                    lambda j: summarize_article(fb_client, batch[j].get("title", ""),
                                                _article_text(batch[j]), provider="anthropic"),
                    redo,
                ))
                for j, r in zip(redo, retried):
                    results[j] = r

            for offset, (article, summary) in enumerate(zip(batch, results)):
                i          = batch_start + offset + 1
                article_id = article["article_id"]
                log.info(f"  [{i:3}/{len(pending)}] {article.get('title', '')[:65]}")

                provider_down = summary is QUOTA_EXHAUSTED or summary is PROVIDER_UNAVAILABLE

                if summary and not provider_down:
                    row_by_id[article_id]["summary_ai"]       = summary
                    row_by_id[article_id]["summarize_status"] = "success"
                    log.info(f"         ✓  {summary[:80]}...")
                    success_count    += 1
                    consecutive_fails = 0
                elif provider_down:
                    # The provider was unavailable, not the article. Leave the row
                    # 'pending' and spend no attempt, so it retries on a later run
                    # no matter how long the outage lasts. The circuit breaker
                    # below still stops this run once the provider looks down.
                    row_by_id[article_id]["summarize_status"] = "pending"
                    log.warning("         …  Provider unavailable — left pending, will retry (no attempt used)")
                    consecutive_fails += 1
                    if consecutive_fails >= MAX_CONSECUTIVE:
                        log.error(f"  {MAX_CONSECUTIVE} consecutive provider failures — likely down. Stopping.")
                        stop = True
                        break
                else:
                    # Genuine content failure — spend an attempt and give up at the cap.
                    attempts = _attempts(row_by_id[article_id]) + 1
                    row_by_id[article_id]["summarize_status"]   = "failed"
                    row_by_id[article_id]["summarize_attempts"] = str(attempts)
                    remaining = SUMMARIZE_MAX_ATTEMPTS - attempts
                    log.warning(
                        f"         ✗  Summarization failed "
                        f"({'will retry' if remaining > 0 else 'giving up'}; "
                        f"attempt {attempts}/{SUMMARIZE_MAX_ATTEMPTS})"
                    )
                    failed_count      += 1
                    consecutive_fails += 1
                    if consecutive_fails >= MAX_CONSECUTIVE:
                        log.error(f"  {MAX_CONSECUTIVE} consecutive failures — quota likely exhausted. Stopping.")
                        stop = True
                        break

            # Checkpoint after each batch so a crash doesn't lose completed work
            save_staging(FILE_STAGED, list(row_by_id.values()))

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
