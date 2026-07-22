"""
LightSignal — _llm.py
=======================
Shared provider-agnostic LLM client and retry policy for the article pipeline.

summarize.py (Stage 3) and classify.py (Stage 4) previously carried byte-identical
copies of the client factory, the error classifiers, the call wrapper and the
retry/backoff loop. Every tuning change had to be made twice — the SDK-retry and
backoff fixes landed as four near-duplicate edits. One copy now.

Provider is chosen by LIGHTSIGNAL_PROVIDER (default: gemini); "anthropic" uses
Claude instead. Callers pass an explicit provider to override per-call, which is
how the Gemini-quota-exhausted fallback switches mid-run.
"""

import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import httpx
from openai import OpenAI

SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from utils.config import (
    ARTICLES_MODEL, GEMINI_BASE_URL,
    API_PROVIDER, ANTHROPIC_MODEL,
    LLM_MAX_WORKERS, LLM_SDK_RETRIES,
)

log = logging.getLogger(__name__)
# Reduce noisy transport-level request logs; keep stage-level logs visible.
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Returned when every retry failed with a rate-limit/billing error. Compared with
# `is`, so it must be this one shared object — do not redefine it per module.
QUOTA_EXHAUSTED = object()

# ── SSL bypass (corporate network) ───────────────────────────────────────────
# Pool sized for LLM_MAX_WORKERS so concurrent calls don't queue on connections.
_http_client = httpx.Client(
    verify=False,
    limits=httpx.Limits(
        max_connections=max(10, LLM_MAX_WORKERS * 2),
        max_keepalive_connections=max(10, LLM_MAX_WORKERS),
    ),
)


def make_client(provider=None):
    """Build a client for the given provider (defaults to API_PROVIDER)."""
    p = provider or API_PROVIDER
    if p == "anthropic":
        import anthropic
        return anthropic.Anthropic(http_client=_http_client)
    return OpenAI(
        base_url=GEMINI_BASE_URL,
        api_key=os.environ.get("GEMINI_API_KEY", ""),
        http_client=_http_client,
        max_retries=LLM_SDK_RETRIES,
        timeout=60,
    )


def is_rate_limit(e: Exception) -> bool:
    return getattr(e, "status_code", None) == 429 or "RateLimitError" in type(e).__name__


def is_transient_http_error(e: Exception) -> bool:
    status_code = getattr(e, "status_code", None)
    if status_code in {408, 425, 429, 500, 502, 503, 504}:
        return True

    msg = str(e).lower()
    transient_markers = (
        "timed out",
        "timeout",
        "temporarily unavailable",
        "service unavailable",
        "connection reset",
        "connection aborted",
        "remote protocol error",
    )
    return any(marker in msg for marker in transient_markers)


def call(client, system_prompt: str, user_content: str, max_tokens: int, provider=None) -> str:
    """One LLM call. Returns the response text, stripped."""
    p = provider or API_PROVIDER
    if p == "anthropic":
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        return response.content[0].text.strip()
    response = client.chat.completions.create(
        model=ARTICLES_MODEL,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_content},
        ],
    )
    return response.choices[0].message.content.strip()


def strip_code_fence(raw: str) -> str:
    """Remove a leading ```/```json fence and a trailing ``` if present."""
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    return re.sub(r'\s*```$', '', raw).strip()


def call_with_retries(
    client,
    system_prompt: str,
    user_content: str,
    max_tokens: int,
    provider=None,
    parse=None,
    label: str = "LLM",
    attempts: int = 3,
):
    """
    Call the LLM with the pipeline's retry policy.

    `parse` is applied to the (fence-stripped) response text; pass json.loads for
    structured stages. A parse failure is retried like a transient error.

    Returns the parsed result, None if every attempt failed for a non-quota
    reason, or QUOTA_EXHAUSTED if every attempt hit a rate-limit/billing error.
    """
    rate_limit_count = 0
    for attempt in range(attempts):
        try:
            raw = strip_code_fence(call(client, system_prompt, user_content, max_tokens, provider))
            return parse(raw) if parse else raw
        except json.JSONDecodeError as e:
            log.warning(f"    {label}: JSON parse error (attempt {attempt + 1}): {e}")
            time.sleep(5)
        except Exception as e:
            if is_rate_limit(e):
                rate_limit_count += 1
                wait = 60 * (attempt + 1)   # 60 / 120 / 180s
                log.warning(f"    {label}: rate limited (attempt {attempt + 1}): waiting {wait}s")
            elif is_transient_http_error(e):
                # The SDK already burned LLM_SDK_RETRIES fast retries before this,
                # so keep the stage-level backoff short.
                wait = 2 * (attempt + 1)    # 2 / 4 / 6s
                log.info(f"    {label}: transient HTTP error (attempt {attempt + 1}): retrying in {wait}s")
            else:
                wait = 10 * (attempt + 1)
                log.warning(f"    {label} error (attempt {attempt + 1}): {str(e)[:80]} — waiting {wait}s")
            time.sleep(wait)
    if rate_limit_count == attempts:
        return QUOTA_EXHAUSTED
    return None
