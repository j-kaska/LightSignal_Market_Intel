"""
LightSignal — audit/auditor.py
==============================
Runs the auditor over a set of articles and returns verdicts.

Text sourcing is the quiet win here. classify.py shows the model only Title +
Summary_AI — it never sees the article. news_feed.csv keeps 2,000 chars of body text
(515 of the 979 unprocessed rows sit exactly at that cap), while staged_articles.csv
keeps up to 8,000. So we join to staging and prefer its copy: for 362 of the
unprocessed rows there is text on disk that the scorer has never looked at.
"""

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from audit import llm                                            # noqa: E402
from audit.audit_config import (                                 # noqa: E402
    MAX_ARTICLE_CHARS, MIN_ARTICLE_CHARS,
)
from audit.prompts import SYSTEM_PROMPT, OUTPUT_SCHEMA, build_user_message  # noqa: E402
from audit.io_utils import load_staged                           # noqa: E402


@dataclass
class Verdict:
    article_id: str
    ok: bool
    reason: str = ""
    data: dict = field(default_factory=dict)
    text_source: str = ""
    text_len: int = 0
    dropped_quotes: int = 0


# ── Text sourcing ─────────────────────────────────────────────────────────────

def build_text_table(feed: pd.DataFrame) -> pd.DataFrame:
    """
    Attach the best available body text to each article, and say where it came from.

    Staging keeps 8,000 chars; news_feed truncates to 2,000. Staging rows are pruned
    after 30 days, so the older backlog falls back to the feed's shorter copy.
    """
    staged = load_staged()[["article_id", "article_text", "clean_url", "source"]]
    staged = staged.rename(columns={"article_id": "ID", "article_text": "staged_text"})

    df = feed.merge(staged, on="ID", how="left")

    staged_len = df["staged_text"].fillna("").str.len()
    feed_len   = df["Article_Text"].fillna("").str.len()

    use_staged = staged_len > feed_len
    df["text"] = df["Article_Text"].fillna("")
    df.loc[use_staged, "text"] = df.loc[use_staged, "staged_text"].fillna("")
    df["text"] = df["text"].str.slice(0, MAX_ARTICLE_CHARS)

    df["text_source"] = "news_feed(2k cap)"
    df.loc[use_staged, "text_source"] = "staging(8k)"
    df.loc[df["text"].str.len() < MIN_ARTICLE_CHARS, "text_source"] = "INSUFFICIENT"
    df["text_len"] = df["text"].str.len()
    return df


# ── One article ───────────────────────────────────────────────────────────────

def audit_one(row: pd.Series) -> Verdict:
    aid  = str(row["ID"])
    text = str(row.get("text") or "")

    if len(text) < MIN_ARTICLE_CHARS:
        return Verdict(
            aid, ok=False,
            reason=f"insufficient text ({len(text)} chars) — flagged, not scored",
            text_source=row.get("text_source", ""), text_len=len(text),
        )

    msg = build_user_message(
        title=str(row.get("Title") or ""),
        article_text=text,
        source=str(row.get("source") or ""),
    )

    try:
        data = llm.classify(
            system_prompt=SYSTEM_PROMPT,
            user_message=msg,
            schema=OUTPUT_SCHEMA,
            article_id=aid,
            cache_text=text,
        )
    except llm.FatalAPIError:
        # Out of credit, bad key. Every remaining row would fail identically.
        # Let this propagate and kill the run — a report built from 1,000 silently
        # failed rows is worse than no report.
        raise
    except Exception as e:
        return Verdict(aid, ok=False, reason=f"{type(e).__name__}: {e}",
                       text_source=row.get("text_source", ""), text_len=len(text))

    if data.get("unusable"):
        return Verdict(aid, ok=False, reason="model judged the text unusable",
                       data=data, text_source=row.get("text_source", ""), text_len=len(text))

    # ── the evidence rail ────────────────────────────────────────────────────
    # Any DC finding whose quote is not literally in the article is discarded. If
    # every finding is discarded, mentions_specific_dc collapses to false: we do not
    # let an unsupported claim through just because the model asserted it.
    kept, dropped = [], 0
    for m in data.get("dc_mentions", []):
        if llm.verify_quote(m.get("evidence_quote", ""), text):
            kept.append(m)
        else:
            dropped += 1
    data["dc_mentions"] = kept
    if dropped and not kept:
        data["mentions_specific_dc"] = False

    return Verdict(
        aid, ok=True, data=data,
        text_source=row.get("text_source", ""), text_len=len(text),
        dropped_quotes=dropped,
    )


# ── Many articles ─────────────────────────────────────────────────────────────

def audit_many(df: pd.DataFrame, workers: int = 8, progress: bool = True) -> list[Verdict]:
    rows = [r for _, r in df.iterrows()]
    out: list[Verdict] = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(audit_one, r): r["ID"] for r in rows}
        try:
            for i, fut in enumerate(as_completed(futures), 1):
                out.append(fut.result())
                if progress and (i % 25 == 0 or i == len(rows)):
                    print(f"  {i}/{len(rows)}  {llm.USAGE.render()}", flush=True)
        except llm.FatalAPIError as e:
            for f in futures:
                f.cancel()
            raise SystemExit(
                f"\nAUDIT ABORTED — the API is refusing every request:\n\n  {e}\n\n"
                f"{len(out)} articles completed before the failure and are cached on "
                f"disk; re-running after you fix this will resume from there and will "
                f"not re-pay for them.\n"
            ) from e

    return out


def verdicts_to_frame(verdicts: list[Verdict]) -> pd.DataFrame:
    rows = []
    for v in verdicts:
        d = v.data or {}
        rows.append({
            "ID": v.article_id,
            "audit_ok": v.ok,
            "audit_reason": v.reason,
            "text_source": v.text_source,
            "text_len": v.text_len,
            "dropped_quotes": v.dropped_quotes,
            "aud_strategy":   d.get("strategy_alignment_score"),
            "aud_relevance":  d.get("relevance_score"),
            "aud_dc":         d.get("mentions_specific_dc"),
            "aud_category":   d.get("primary_category"),
            "aud_states":     ",".join(d.get("states") or []),
            "aud_strategy_rationale":  d.get("strategy_rationale"),
            "aud_relevance_rationale": d.get("relevance_rationale"),
            "aud_confidence": d.get("self_confidence"),
            "aud_investment": d.get("is_investment_content"),
            "aud_international": d.get("is_international"),
            "aud_dc_mentions": d.get("dc_mentions") or [],
            "aud_n_dc": len(d.get("dc_mentions") or []),
        })
    return pd.DataFrame(rows)
