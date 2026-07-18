"""
LightSignal — audit/checks_structural.py
========================================
Deterministic integrity checks over news_feed.csv. No LLM, no cost.

These findings are *certain*, not probabilistic — which is why they are reported
first and separately from anything a model judged.

The headline defect: dedup.py guards against self-matching in its semantic layer
(`if cached_id == article_id: continue`) but NOT in its two title layers. Re-running
dedup over rows whose titles are already in title_cache.json therefore marks each
article a duplicate of *itself*. It is then suppressed and never summarized or
scored — the article is silently destroyed.
"""

import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from audit.io_utils import load_news_feed, unprocessed, as_bool, as_score  # noqa: E402

# Reuse the pipeline's OWN notion of title identity. If we reimplemented it we'd
# be measuring a different pipeline than the one that ran.
from articles.dedup import _normalize_title  # noqa: E402


@dataclass
class Finding:
    check_id: str
    severity: str            # "data_loss" | "correctness" | "cosmetic"
    summary: str
    article_ids: list = field(default_factory=list)
    evidence: str = ""
    suggested_fix: str = ""

    @property
    def count(self) -> int:
        return len(self.article_ids)


def _dupe_view(df: pd.DataFrame) -> pd.DataFrame:
    v = df.copy()
    v["is_dup"] = as_bool(v["Is_Duplicate"])
    v["target"] = v["Duplicate_Of"].fillna("").astype(str).str.strip()
    return v


# ── The checks ────────────────────────────────────────────────────────────────

def check_self_duplicates(df: pd.DataFrame) -> Finding:
    """An article marked as a duplicate of itself. Suppressed, pointing nowhere."""
    v = _dupe_view(df)
    hits = v[v["is_dup"] & (v["target"] == v["ID"])]
    return Finding(
        check_id="self_duplicate",
        severity="data_loss",
        summary=(
            "Articles flagged as a duplicate of THEMSELVES. Each is suppressed from "
            "the newsletter and never summarized or scored, and points at no canonical."
        ),
        article_ids=hits["ID"].tolist(),
        evidence="; ".join(hits["Title"].head(5).astype(str).str[:70]),
        suggested_fix=(
            "dedup.py: Layers 1a (line ~288) and 1b (line ~296) lack the self-match "
            "guard that Layer 2 (line ~308) already has. Add "
            "`if cached.get('id') == article_id: continue` to both."
        ),
    )


def check_dangling_targets(df: pd.DataFrame) -> Finding:
    """Duplicate_Of points at an ID that does not exist in news_feed.csv."""
    v = _dupe_view(df)
    all_ids = set(df["ID"].dropna())
    hits = v[v["is_dup"] & (v["target"] != "") & (~v["target"].isin(all_ids))]
    return Finding(
        check_id="dangling_duplicate_of",
        severity="data_loss",
        summary=(
            "Duplicate_Of points at an article ID absent from news_feed.csv. The "
            "canonical was never written, so the story exists nowhere."
        ),
        article_ids=hits["ID"].tolist(),
        evidence="; ".join(f"{r.ID}->{r.target}" for r in hits.head(5).itertuples()),
        suggested_fix=(
            "Sequencing bug: dedup (stage 1.5) chooses a canonical BEFORE extract.py "
            "(stage 2) drops BLOCKED_SOURCE_DOMAINS / YouTube rows. The canonical is "
            "deleted out from under its own duplicates. Move the filters ahead of "
            "dedup, or re-point orphans at a surviving cluster member."
        ),
    )


def check_duplicate_cycles(df: pd.DataFrame) -> Finding:
    """
    Follow Duplicate_Of to its terminus. A chain that never reaches a surviving
    (non-duplicate) article means the story is suppressed with nothing to show for it.
    Self-references are the degenerate case and are reported separately.
    """
    v = _dupe_view(df)
    by_id  = {r.ID: r for r in v.itertuples()}
    dup_ids = set(v[v["is_dup"]]["ID"])

    stuck = []
    for row in v[v["is_dup"]].itertuples():
        seen, cur = set(), row.target
        while cur in by_id and cur not in seen:
            seen.add(cur)
            nxt = by_id[cur]
            if not nxt.is_dup:
                break                       # resolves to a real survivor
            cur = str(nxt.target).strip()
        else:
            # fell out of the loop without hitting a survivor: cycle, or dangling
            if cur in dup_ids or cur in seen:
                stuck.append(row.ID)

    return Finding(
        check_id="duplicate_cycle",
        severity="data_loss",
        summary=(
            "Duplicate chains that never terminate at a surviving article (cycles and "
            "self-references). Every member is suppressed; no canonical survives."
        ),
        article_ids=stuck,
        evidence=f"{len(stuck)} rows whose Duplicate_Of chain loops without reaching a survivor",
        suggested_fix=(
            "Fix the self-guard (see self_duplicate), then enforce a canonical-selection "
            "rule so every cluster keeps exactly one survivor by construction."
        ),
    )


def check_fully_suppressed_clusters(df: pd.DataFrame) -> Finding:
    """
    A title-cluster in which EVERY member is flagged duplicate. The story vanishes
    from the newsletter entirely — the worst outcome, and invisible without this check.
    """
    v = _dupe_view(df)
    v["norm_title"] = v["Title"].fillna("").astype(str).map(_normalize_title)

    lost_ids, lost_clusters = [], 0
    for norm, grp in v.groupby("norm_title"):
        if not norm or len(grp) < 2:
            continue
        if grp["is_dup"].all():
            lost_clusters += 1
            lost_ids.extend(grp["ID"].tolist())

    return Finding(
        check_id="fully_suppressed_cluster",
        severity="data_loss",
        summary=(
            f"Title-clusters where every single member is flagged duplicate "
            f"({lost_clusters} clusters). The story reaches the newsletter zero times."
        ),
        article_ids=lost_ids,
        evidence=f"{lost_clusters} clusters, {len(lost_ids)} articles lost outright",
        suggested_fix=(
            "Canonical-selection rule: within each cluster keep exactly one survivor "
            "(prefer non-empty Summary_AI > CleanURL > longest text > earliest ID)."
        ),
    )


def check_suppressed_but_unscored(df: pd.DataFrame) -> Finding:
    """Non-duplicate rows that nevertheless never got a summary — classify silently skipped them."""
    v = _dupe_view(df)
    hits = v[(~v["is_dup"]) & (v["Summary_AI"].isna())]
    return Finding(
        check_id="unscored_non_duplicate",
        severity="correctness",
        summary="Rows that are NOT duplicates yet have no Summary_AI — classification never ran.",
        article_ids=hits["ID"].tolist(),
        evidence=f"{len(hits)} rows",
        suggested_fix="Inspect classify_status in staged_articles.csv for these IDs.",
    )


def check_score_rule_violations(df: pd.DataFrame) -> Finding:
    """
    The classify prompt's own RULE 3: 'Whenever Strategy=1 for any reason, Relevance
    MUST also be 1.' Rows violating it are cases where the model ignored its own rubric.
    """
    v   = df.copy()
    s   = as_score(v["Strategy_Alignment_Score"])
    r   = as_score(v["Relevance_Score"])
    bad_range = v[((s < 1) | (s > 5) | (r < 1) | (r > 5)).fillna(False)]
    rule3     = v[((s == 1) & (r > 1)).fillna(False)]
    ids = sorted(set(bad_range["ID"]) | set(rule3["ID"]))
    return Finding(
        check_id="score_rule_violation",
        severity="correctness",
        summary=(
            "Scores outside 1-5, or violating the prompt's own RULE 3 "
            "(Strategy=1 must force Relevance=1)."
        ),
        article_ids=ids,
        evidence=f"{len(bad_range)} out-of-range, {len(rule3)} RULE 3 violations",
        suggested_fix="Validate the model's JSON against the rubric before writing the row.",
    )


def check_batch_timestamps(df: pd.DataFrame) -> Finding:
    """
    PublishedDate carrying a pipeline RUN timestamp instead of the article's real
    publish date. Symptom: many rows sharing one timestamp to the minute.

    This is systemic, not incidental: the unprocessed backlog has 979 articles across
    only 8 distinct PublishedDate values — one per pipeline run. The column records
    when we FETCHED the article, not when it was published. A three-month-old story
    pulled today is indistinguishable from today's news, and every date-window
    behaviour keys off this field (dedup's 14-day window, newsletter recency).
    """
    counts  = df["PublishedDate"].fillna("").value_counts()
    suspect = counts[counts >= 20]
    ids     = df[df["PublishedDate"].isin(suspect.index)]["ID"].tolist()
    n_distinct = df["PublishedDate"].nunique()

    return Finding(
        check_id="batch_timestamp",
        severity="correctness",
        summary=(
            f"PublishedDate is a fetch timestamp, not a publish date: {len(df)} articles "
            f"share only {n_distinct} distinct values ({len(ids)} of them in run-sized "
            f"clumps of 20+). Article age is unrecoverable from this column."
        ),
        article_ids=ids,
        evidence="; ".join(f"{k!r} x{v}" for k, v in suspect.head(6).items()),
        suggested_fix=(
            "fetch_rss.py: persist the RSS entry's own published date (feedparser's "
            "entry.published_parsed) rather than the run clock. Until then, dedup's "
            "14-day window and any newsletter recency logic are keyed off the wrong field."
        ),
    )


def check_boolean_casing(df: pd.DataFrame) -> Finding:
    """
    Cosmetic. Two writers produced TRUE/FALSE and True/False. Every Python consumer
    in the repo already lowercases, so this is NOT a live bug — reported so it isn't
    mistaken for one, and so a future non-lowercasing consumer doesn't get bitten.
    """
    vals = set()
    for col in ("Is_Duplicate", "Mentions_Specific_DC"):
        vals |= set(df[col].dropna().unique())
    mixed = len({v.lower() for v in vals}) < len(vals)
    return Finding(
        check_id="boolean_casing_drift",
        severity="cosmetic",
        summary="Mixed boolean casing (TRUE/FALSE vs True/False) from two different writers.",
        article_ids=[],
        evidence=f"distinct values: {sorted(vals)}" if mixed else "consistent",
        suggested_fix="Normalize on write. Cosmetic only — all Python consumers lowercase.",
    )


def check_dc_flag_base_rate(df: pd.DataFrame) -> Finding:
    """
    Not a defect per se — a signal. The pipeline flags a specific-DC mention on ~0.7%
    of scored articles. The human-labeled base rate is ~4.3%. A 6x gap is prima facie
    under-detection, and it traces to the prompt's definition, not the model.
    """
    scored = df[df["Summary_AI"].notna()]
    flagged = as_bool(scored["Mentions_Specific_DC"]).sum()
    rate = flagged / len(scored) if len(scored) else 0.0
    return Finding(
        check_id="dc_flag_base_rate",
        severity="correctness",
        summary=(
            f"Mentions_Specific_DC is true on {flagged}/{len(scored)} scored rows "
            f"({rate:.1%}). Human-labeled base rate is ~4.3%."
        ),
        article_ids=[],
        evidence=f"{rate:.2%} vs ~4.3% expected",
        suggested_fix=(
            "The classify prompt defines a specific-DC mention as a NAMED FACILITY only, "
            "and explicitly excludes 'a proposed data center in Box Elder County'. That "
            "excludes exactly the rumored/proposed sites the user wants. Spec bug, not "
            "a model failure."
        ),
    )


ALL_CHECKS = (
    check_self_duplicates,
    check_dangling_targets,
    check_duplicate_cycles,
    check_fully_suppressed_clusters,
    check_suppressed_but_unscored,
    check_score_rule_violations,
    check_batch_timestamps,
    check_boolean_casing,
    check_dc_flag_base_rate,
)


def run_all(df: pd.DataFrame) -> list[Finding]:
    return [check(df) for check in ALL_CHECKS]


# ── Reporting ─────────────────────────────────────────────────────────────────

SEVERITY_ORDER = {"data_loss": 0, "correctness": 1, "cosmetic": 2}


def to_markdown(findings: list[Finding], scope: str) -> str:
    findings = sorted(findings, key=lambda f: (SEVERITY_ORDER[f.severity], -f.count))
    lines = [
        "# Structural integrity report",
        "",
        f"Scope: **{scope}**",
        "",
        "These checks are deterministic — no model judged anything here. "
        "The findings are certain.",
        "",
        "| Severity | Check | Articles affected | Summary |",
        "|---|---|---:|---|",
    ]
    for f in findings:
        lines.append(
            f"| {f.severity} | `{f.check_id}` | {f.count} | {f.summary} |"
        )
    lines.append("")
    for f in findings:
        if f.severity == "cosmetic" and not f.count:
            continue
        lines += [
            f"## `{f.check_id}` — {f.count} articles",
            "",
            f.summary,
            "",
            f"**Evidence:** {f.evidence}",
            "",
            f"**Fix:** {f.suggested_fix}",
            "",
        ]
    return "\n".join(lines)


def to_dataframe(findings: list[Finding]) -> pd.DataFrame:
    rows = []
    for f in findings:
        for aid in f.article_ids:
            rows.append({
                "check_id": f.check_id,
                "severity": f.severity,
                "ID": aid,
                "summary": f.summary,
                "suggested_fix": f.suggested_fix,
            })
    return pd.DataFrame(rows, columns=["check_id", "severity", "ID", "summary", "suggested_fix"])
