"""
LightSignal — audit/llm.py
==========================
The Opus 4.8 client: structured outputs, prompt caching, a disk cache, and batching.

Notes that matter:

* Opus 4.8 REJECTS temperature/top_p/top_k with a 400 — the sampling params were
  removed in the 4.7/4.8 family. Reproducibility therefore comes from the disk cache,
  keyed by sha256(model + prompt_version + article_id + text_hash), not temperature=0.

* SSL verification is disabled to match the rest of the repo, which runs behind a
  corporate proxy that MITMs TLS. classify.py and summarize.py already do this.

* The system prompt (~2.5k tokens) is cached; every article after the first reads it
  at 0.1x. That is most of the input cost.
"""

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import anthropic
import httpx
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from audit.audit_config import (          # noqa: E402
    AUDIT_MODEL, AUDIT_EFFORT, PROMPT_VERSION, LLM_CACHE_DIR,
)

load_dotenv(ROOT / ".env")

_client: anthropic.Anthropic | None = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set (looked in .env and the environment)")
        _client = anthropic.Anthropic(
            api_key=key,
            http_client=httpx.Client(verify=False, timeout=180.0),
            max_retries=4,
        )
    return _client


# ── Usage accounting ──────────────────────────────────────────────────────────

@dataclass
class Usage:
    calls: int = 0
    cached_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0

    # Opus 4.8: $5 / 1M input, $25 / 1M output, cache reads at 0.1x, writes at 1.25x
    @property
    def cost_usd(self) -> float:
        return (
            self.input_tokens  * 5.00 / 1e6
            + self.output_tokens * 25.00 / 1e6
            + self.cache_read    * 0.50 / 1e6
            + self.cache_write   * 6.25 / 1e6
        )

    def render(self) -> str:
        return (
            f"{self.calls} calls ({self.cached_calls} served from disk cache) | "
            f"in={self.input_tokens:,} out={self.output_tokens:,} "
            f"cache_read={self.cache_read:,} | ${self.cost_usd:.2f}"
        )


USAGE = Usage()


# ── Disk cache ────────────────────────────────────────────────────────────────

def cache_key(article_id: str, text: str, model: str = AUDIT_MODEL,
              version: str = PROMPT_VERSION) -> str:
    h = hashlib.sha256()
    h.update(model.encode())
    h.update(version.encode())
    h.update(article_id.encode())
    h.update(text.encode("utf-8", "replace"))
    return h.hexdigest()[:32]


def cache_get(key: str) -> dict | None:
    p = LLM_CACHE_DIR / f"{key}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            p.unlink(missing_ok=True)
    return None


def cache_put(key: str, value: dict) -> None:
    (LLM_CACHE_DIR / f"{key}.json").write_text(
        json.dumps(value, ensure_ascii=False), encoding="utf-8"
    )


# ── The call ──────────────────────────────────────────────────────────────────

class FatalAPIError(RuntimeError):
    """
    An error that will hit every single row, so retrying the rest is pointless:
    no credit, bad key, revoked permissions.

    These must ABORT the run, not degrade into per-article failures. A run that
    quietly marks 1,050 articles 'audit_ok=False' because the account is out of
    credit looks, from the report, exactly like a run that found nothing to say —
    and that is precisely the kind of silent failure this harness exists to catch.
    """


_FATAL_MARKERS = (
    "credit balance is too low",
    "invalid x-api-key",
    "authentication_error",
    "permission_error",
)


def _raise_if_fatal(exc: Exception) -> None:
    msg = str(exc).lower()
    if any(m in msg for m in _FATAL_MARKERS):
        raise FatalAPIError(str(exc)) from exc


def classify(system_prompt: str, user_message: str, schema: dict,
             article_id: str, cache_text: str,
             effort: str = AUDIT_EFFORT, max_tokens: int = 4000) -> dict:
    """
    One structured-output call. Returns the parsed object.

    Cached on disk by (model, prompt version, article id, text hash) — so a re-run
    after a crash costs nothing, and bumping PROMPT_VERSION invalidates cleanly.
    """
    key = cache_key(article_id, cache_text)
    hit = cache_get(key)
    if hit is not None:
        USAGE.cached_calls += 1
        return hit

    try:
        resp = client().messages.create(
            model=AUDIT_MODEL,
            max_tokens=max_tokens,
            system=[{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},   # ~2.5k tokens, read at 0.1x
            }],
            messages=[{"role": "user", "content": user_message}],
            thinking={"type": "adaptive"},
            output_config={
                "format": {"type": "json_schema", "schema": schema},
                "effort": effort,
            },
        )
    except Exception as e:
        _raise_if_fatal(e)   # out of credit / bad key -> abort the run, don't limp on
        raise

    USAGE.calls         += 1
    USAGE.input_tokens  += resp.usage.input_tokens
    USAGE.output_tokens += resp.usage.output_tokens
    USAGE.cache_read    += getattr(resp.usage, "cache_read_input_tokens", 0) or 0
    USAGE.cache_write   += getattr(resp.usage, "cache_creation_input_tokens", 0) or 0

    if resp.stop_reason == "refusal":
        raise RuntimeError(f"model refused on {article_id}")

    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        raise RuntimeError(f"no text block returned for {article_id}")

    parsed = json.loads(text)
    cache_put(key, parsed)
    return parsed


# ── Evidence verification — the anti-hallucination rail ───────────────────────

def _normalize_for_match(s: str) -> str:
    """
    Quote matching has to tolerate the mojibake in Article_Text (cp1252 -> utf8 damage
    left '�' where smart quotes and em-dashes were) without tolerating invention.
    Collapse whitespace, fold the punctuation the corruption touched, lowercase.
    """
    import re
    import unicodedata

    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        if cat == "Pd":                          # any dash variant -> hyphen
            out.append("-")
        elif ch in "'‘’“”´`\"�":
            # Apostrophes, quotes, and the U+FFFD replacement char are all DELETED
            # rather than normalized. The cp1252->utf8 damage in Article_Text turned
            # "centers'" into "centers�", so the corrupted text has no apostrophe
            # at all while the model's quote does. Deleting both sides is the only
            # fold that makes them agree.
            continue
        elif cat.startswith("Z"):
            out.append(" ")
        else:
            out.append(ch)

    s = "".join(out).lower()
    s = re.sub(r"[^\w\s-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def verify_quote(quote: str, article_text: str) -> bool:
    """
    Is the quote a real, verbatim span of the article?

    This is the strongest anti-hallucination lever available and it is cheap. A DC
    finding whose quote is not in the text is discarded — an invented quote is worse
    than no finding, because it is the one thing a reviewer would take on trust.
    """
    if not quote or not quote.strip():
        return False
    return _normalize_for_match(quote) in _normalize_for_match(article_text)
