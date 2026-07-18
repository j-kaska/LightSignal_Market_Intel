"""
LightSignal — audit/metrics.py
==============================
Scoring functions, and the honesty rails around them.

The one thing this module exists to prevent:

    The `Mentions_DC_Actual` column in news_feed_test_feedback.xlsx contains ONLY
    True values (203 of them). The user corrected False->True and never True->False.
    Under the imputation policy, every AI "true" that was not corrected therefore
    becomes gold-true BY CONSTRUCTION — so DC precision computes to exactly 1.000
    no matter how many false positives the model actually produces.

    That number is an artifact, not a measurement. A harness that prints it as if it
    were real would be lying, and would look most impressive precisely where it knows
    least. `measurability()` detects the condition and the reporters refuse to print
    a precision figure when it holds.

Recall is unaffected and is a real measurement: the 203 corrections are genuine
false-negatives the pipeline produced.
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class BinaryMetrics:
    n: int
    tp: int
    fp: int
    fn: int
    tn: int
    precision_measurable: bool = True
    note: str = ""

    @property
    def precision(self) -> float | None:
        if not self.precision_measurable:
            return None
        d = self.tp + self.fp
        return self.tp / d if d else None

    @property
    def recall(self) -> float | None:
        d = self.tp + self.fn
        return self.tp / d if d else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None or (p + r) == 0:
            return None
        return 2 * p * r / (p + r)

    def render(self, label: str) -> str:
        p = "unmeasurable" if self.precision is None else f"{self.precision:.3f}"
        r = "  n/a " if self.recall is None else f"{self.recall:.3f}"
        line = (f"{label:22s} n={self.n:5d}  precision={p:>12s}  recall={r}   "
                f"TP={self.tp} FP={self.fp} FN={self.fn}")
        if self.note:
            line += f"\n{'':22s}   ! {self.note}"
        return line


@dataclass
class ScoreMetrics:
    n: int
    mae: float
    bias: float
    exact: float
    within1: float

    def render(self, label: str) -> str:
        return (f"{label:22s} n={self.n:5d}  MAE={self.mae:.3f}  bias={self.bias:+.3f}  "
                f"exact={self.exact:5.1%}  within1={self.within1:5.1%}")


def measurability(pred: pd.Series, gold: pd.Series, corrections_present: pd.Series) -> tuple[bool, str]:
    """
    Can precision be measured from this label set?

    Precision needs false positives — cases where the model said True and the human
    said False. If the human never once corrected a True down to a False, then no
    such case can exist in the labels, and any precision figure is circular.
    """
    contradicted_a_true = bool(((pred) & (corrections_present) & (~gold)).any())
    if contradicted_a_true:
        return True, ""
    if not pred.any():
        return False, "model produced no positives — precision undefined"
    return False, (
        "the label set contains no True->False corrections, so a false positive is "
        "unrepresentable and precision would compute to 1.000 by construction. "
        "Reporting it would be circular. Recall below IS a real measurement."
    )


def binary(pred: pd.Series, gold: pd.Series,
           corrections_present: pd.Series | None = None) -> BinaryMetrics:
    pred = pred.astype(bool)
    gold = gold.astype(bool)

    if corrections_present is None:
        ok, note = True, ""
    else:
        ok, note = measurability(pred, gold, corrections_present.astype(bool))

    return BinaryMetrics(
        n=len(pred),
        tp=int((pred & gold).sum()),
        fp=int((pred & ~gold).sum()),
        fn=int((~pred & gold).sum()),
        tn=int((~pred & ~gold).sum()),
        precision_measurable=ok,
        note=note,
    )


def numeric(pred: pd.Series, gold: pd.Series) -> ScoreMetrics:
    err = (pd.to_numeric(pred, errors="coerce") - pd.to_numeric(gold, errors="coerce")).dropna()
    if err.empty:
        return ScoreMetrics(0, float("nan"), float("nan"), float("nan"), float("nan"))
    return ScoreMetrics(
        n=len(err),
        mae=float(err.abs().mean()),
        bias=float(err.mean()),
        exact=float((err == 0).mean()),
        within1=float((err.abs() <= 1).mean()),
    )


def newsletter_selection(df: pd.DataFrame, strategy_col: str, relevance_col: str,
                         min_combined: int = 7, top_n: int = 10) -> set:
    """
    Reproduce generate_newsletter.py's selection: combined score >= 7, take top N.

    This is the metric that actually matters. An MAE of 0.3 is irrelevant if it never
    changes which articles reach the newsletter; a bias that pushes three articles over
    the >=7 line every week is a real defect even at low MAE.
    """
    s = pd.to_numeric(df[strategy_col], errors="coerce")
    r = pd.to_numeric(df[relevance_col], errors="coerce")
    combined = (s + r).rename("combined")
    d = df.assign(combined=combined)
    d = d[d["combined"] >= min_combined]
    d = d.sort_values("combined", ascending=False).head(top_n)
    return set(d["ID"])


def selection_agreement(pred_sel: set, gold_sel: set) -> dict:
    inter = pred_sel & gold_sel
    union = pred_sel | gold_sel
    return {
        "precision_at_n": len(inter) / len(pred_sel) if pred_sel else float("nan"),
        "recall_at_n":    len(inter) / len(gold_sel) if gold_sel else float("nan"),
        "jaccard":        len(inter) / len(union) if union else float("nan"),
        "n_pred": len(pred_sel),
        "n_gold": len(gold_sel),
    }
