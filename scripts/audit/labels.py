"""
LightSignal — audit/labels.py
=============================
Turns news_feed_test_feedback.xlsx into gold labels.

This is the module where an audit harness usually cheats, so the policy is written
down explicitly and tested.

THE IMPUTATION POLICY, applied per field, independently:

    Accept == YES                       -> gold = the pipeline's value (you ratified it)
    Accept == NO  & correction present  -> gold = your correction
    Accept == NO  & correction blank    -> EXCLUDED from that metric

That last line is the important one. A rejected row with no correction tells us the
pipeline was wrong but not what right looks like. Treating it as "pipeline was correct"
would silently inflate every score. It is dropped from that field's metric instead.

A row can be in-scope for the DC metric and out-of-scope for the scoring metric —
the columns are graded independently, because you filled them in independently.

THE HARD SUBSET:
Every metric is also computed on `Accept == NO` rows only. That is the honest number.
The auditor's whole job is catching the 1,511 rows you rejected; a headline score
buoyed up by the 3,185 easy ratified rows means nothing.
"""

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from audit.io_utils import snapshot_labels, as_bool, as_nullable_bool, as_score  # noqa: E402

RANDOM_SEED = 20260711
DEV_FRACTION = 0.60


@dataclass
class GoldSet:
    """Per-field gold labels. `mask` marks rows in scope for that field."""
    df: pd.DataFrame          # one row per article, with gold_* and pipe_* columns
    split: pd.Series          # "dev" | "test"

    def scope(self, field: str, split: str | None = None, hard_only: bool = False) -> pd.DataFrame:
        d = self.df[self.df[f"in_scope_{field}"]]
        if split:
            d = d[self.split.reindex(d.index) == split]
        if hard_only:
            d = d[d["accept"] == "NO"]
        return d


def _norm_accept(s: pd.Series) -> pd.Series:
    return s.fillna("").astype(str).str.strip().str.upper()


def _has_dc_call(df: pd.DataFrame) -> pd.Series:
    """
    Did the pipeline actually make a DC call on this row?

    Duplicate rows are short-circuited before classification, so Mentions_Specific_DC
    is blank on all 1,048 of them. Grading the pipeline on a call it never made is
    meaningless, so those rows are out of scope for the DC metric.
    """
    return df["Mentions_Specific_DC"].notna()


def build_gold(df: pd.DataFrame | None = None) -> GoldSet:
    if df is None:
        df = snapshot_labels()

    g = pd.DataFrame(index=df.index)
    g["ID"]     = df["ID"]
    g["Title"]  = df["Title"]
    g["accept"] = _norm_accept(df["Accept"])

    # ── pipeline's own outputs (what we're grading) ───────────────────────────
    g["pipe_dc"]        = as_bool(df["Mentions_Specific_DC"])
    g["pipe_strategy"]  = as_score(df["Strategy_Alignment_Score"])
    g["pipe_relevance"] = as_score(df["Relevance_Score"])
    g["pipe_is_dup"]    = as_bool(df["Is_Duplicate"])

    # ── your corrections ──────────────────────────────────────────────────────
    corr_dc        = as_nullable_bool(df["Mentions_DC_Actual"])
    corr_strategy  = as_score(df["Strategy Score_Actual"])
    corr_relevance = as_score(df["Relevance_Score_Actual"])
    dupe_error     = df["Dupe Error"].fillna("").astype(str).str.strip().str.lower()

    accepted = g["accept"] == "YES"
    rejected = g["accept"] == "NO"

    # ── DC mention ────────────────────────────────────────────────────────────
    # Correction present -> use it. Otherwise the pipeline's call stands: you either
    # ratified the row, or you rejected it for a different reason (a score, a dupe)
    # and left the DC call uncontested.
    # Rows the pipeline never classified (duplicates) are out of scope entirely.
    has_dc_corr  = corr_dc.notna()
    g["gold_dc"] = np.where(has_dc_corr, corr_dc.fillna(False).astype(bool), g["pipe_dc"])
    g["in_scope_dc"] = _has_dc_call(df) | has_dc_corr

    # ── scores ────────────────────────────────────────────────────────────────
    # Here the blank-correction case is genuinely ambiguous, so we exclude it:
    # a rejected row whose score you did NOT correct might have been rejected for
    # a dupe or DC reason, and assuming the score was fine would flatter everyone.
    for field, corr, pipe in (
        ("strategy",  corr_strategy,  g["pipe_strategy"]),
        ("relevance", corr_relevance, g["pipe_relevance"]),
    ):
        has_corr = corr.notna()
        g[f"gold_{field}"] = np.where(has_corr, corr, pipe)
        g[f"in_scope_{field}"] = accepted | has_corr      # rejected-but-uncorrected: OUT

    # ── dupes ─────────────────────────────────────────────────────────────────
    # "Missed Dupe" (310): pipeline said unique, you say duplicate -> recall target.
    # "Dupe Error"  (84):  pipeline said duplicate, you say unique -> precision target.
    missed_dupe = dupe_error.str.contains("missed")
    false_dupe  = dupe_error.eq("dupe error")
    g["gold_is_dup"] = np.where(
        missed_dupe, True,
        np.where(false_dupe, False, g["pipe_is_dup"]),
    )
    g["gold_dup_of"]  = df["Dupe ID"]
    g["in_scope_dup"] = accepted | missed_dupe | false_dupe

    split = _stratified_split(g)
    return GoldSet(df=g, split=split)


def _stratified_split(g: pd.DataFrame) -> pd.Series:
    """
    Stratify on the things that are rare and that we care about, so DEV and TEST
    both contain enough hard rows to mean anything.
    """
    strata = (
        g["accept"].astype(str)
        + "|" + g["gold_dc"].astype(str)
        + "|" + g["gold_is_dup"].astype(str)
    )
    rng   = np.random.default_rng(RANDOM_SEED)
    split = pd.Series("test", index=g.index, dtype=object)
    for _, idx in g.groupby(strata).groups.items():
        idx = np.array(list(idx))
        rng.shuffle(idx)
        n_dev = int(round(len(idx) * DEV_FRACTION))
        split.loc[idx[:n_dev]] = "dev"
    return split


def summary(gold: GoldSet) -> str:
    g = gold.df
    lines = [
        f"Labeled rows            : {len(g)}",
        f"  Accept=YES (ratified) : {(g['accept'] == 'YES').sum()}",
        f"  Accept=NO  (rejected) : {(g['accept'] == 'NO').sum()}",
        "",
        "In-scope rows per metric (dev / test):",
    ]
    for field in ("dc", "strategy", "relevance", "dup"):
        dev  = len(gold.scope(field, "dev"))
        test = len(gold.scope(field, "test"))
        hard = len(gold.scope(field, hard_only=True))
        lines.append(f"  {field:10s} {dev:5d} / {test:5d}   (hard subset, Accept=NO: {hard})")
    lines += [
        "",
        "Gold positives:",
        f"  mentions a specific DC : {int(g['gold_dc'].sum())} "
        f"({g['gold_dc'].mean():.1%} base rate)",
        f"  is a duplicate         : {int(g['gold_is_dup'].sum())}",
    ]
    return "\n".join(lines)
