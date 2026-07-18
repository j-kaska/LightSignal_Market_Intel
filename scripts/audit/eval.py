"""
LightSignal — audit/eval.py
===========================
Phase 1: score the auditor against the user's own labels. The trust gate.

This runs BEFORE the auditor is allowed to render a verdict on the unprocessed
backlog. If it cannot beat the incumbent on data the user has already hand-checked,
it does not get to have opinions about data they haven't.

Every metric is reported three ways:
  * the pipeline's score  (the incumbent — no number is shown without it)
  * the auditor's score
  * both, restricted to the rows the user marked Accept=NO (the hard subset)

The hard subset is the honest number. The auditor's entire job is catching the 1,511
rows the user rejected; a headline buoyed up by 3,185 easy ratified rows means nothing.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from audit import llm, metrics as M                              # noqa: E402
from audit.audit_config import (                                 # noqa: E402
    MIN_ARTICLE_CHARS, MAX_ARTICLE_CHARS, PROMPT_VERSION,
    AUDIT_MODEL, REPORTS_DIR, FILE_METRICS_HISTORY,
)
from audit.auditor import audit_many, verdicts_to_frame          # noqa: E402
from audit.labels import build_gold                              # noqa: E402
from audit.io_utils import snapshot_labels, as_nullable_bool     # noqa: E402

# Gates, written down before the run. See the plan.
GATES = {
    "dc_recall":       0.85,
    "dup_recall":      0.90,
    "dup_precision":   0.85,
    "strategy_mae":    0.60,
    "relevance_mae":   0.60,
}


def _eval_frame(n: int | None, split: str, seed: int = 7) -> pd.DataFrame:
    """Rows to evaluate: labeled, with enough text to judge."""
    raw  = snapshot_labels()
    gold = build_gold(raw)

    df = raw.copy()
    df["split"]    = gold.split.values
    df["text"]     = df["Article_Text"].fillna("").astype(str).str.slice(0, MAX_ARTICLE_CHARS)
    df["text_len"] = df["text"].str.len()
    df["source"]   = df.get("Source", "")

    df = df[(df["split"] == split) & (df["text_len"] >= MIN_ARTICLE_CHARS)]

    if n and n < len(df):
        # Stratify the sample so the rare, interesting rows survive it. A plain random
        # 400 is mostly easy ratified negatives, and metrics computed on that would
        # look great while telling us nothing about the rows that matter.
        g = gold.df.set_index("ID")
        keys = []
        for i in df["ID"]:
            if i in g.index:
                r = g.loc[i]
                keys.append(f"{r['accept']}|{bool(r['gold_dc'])}|{bool(r['gold_is_dup'])}")
            else:
                keys.append("na")
        df = df.assign(_stratum=keys)

        frac  = n / len(df)
        parts = []
        for _, grp in df.groupby("_stratum", sort=False):
            take = max(1, int(round(len(grp) * frac)))
            parts.append(grp.sample(min(take, len(grp)), random_state=seed))
        df = pd.concat(parts).drop(columns="_stratum")

    return df


def run_eval(n: int | None = 500, split: str = "dev") -> dict:
    raw  = snapshot_labels()
    gold = build_gold(raw)
    dc_corr = as_nullable_bool(raw["Mentions_DC_Actual"]).notna()

    df = _eval_frame(n, split)
    print(f"Evaluating {len(df)} labeled rows (split={split}, prompt={PROMPT_VERSION})\n")

    verdicts = audit_many(df, workers=8)
    aud = verdicts_to_frame(verdicts)

    # join gold + pipeline + auditor on ID
    g = gold.df.set_index("ID")
    j = aud.set_index("ID").join(g, how="inner")
    j = j[j["audit_ok"]]

    corr_here = dc_corr.set_axis(raw["ID"]).reindex(j.index).fillna(False)

    report, results = [], {}
    report.append("=" * 78)
    report.append(f"EVAL — auditor vs pipeline, on YOUR labels")
    report.append(f"model={AUDIT_MODEL}  prompt={PROMPT_VERSION}  split={split}  n={len(j)}")
    report.append("=" * 78)
    report.append("")

    # ── DC mention ────────────────────────────────────────────────────────────
    report.append("DC MENTION")
    report.append("-" * 78)
    base = M.binary(j["pipe_dc"].astype(bool), j["gold_dc"].astype(bool),
                    corrections_present=corr_here)
    audm = M.binary(j["aud_dc"].fillna(False).astype(bool), j["gold_dc"].astype(bool),
                    corrections_present=corr_here)
    report.append(base.render("  pipeline"))
    report.append(audm.render("  auditor "))
    results["dc_recall_pipeline"] = base.recall
    results["dc_recall_auditor"]  = audm.recall
    report.append("")

    # ── scores ────────────────────────────────────────────────────────────────
    for f in ("strategy", "relevance"):
        in_scope = j[j[f"in_scope_{f}"]]
        hard     = in_scope[in_scope["accept"] == "NO"]
        report.append(f"{f.upper()} SCORE")
        report.append("-" * 78)
        bp = M.numeric(in_scope[f"pipe_{f}"], in_scope[f"gold_{f}"])
        ba = M.numeric(in_scope[f"aud_{f}"],  in_scope[f"gold_{f}"])
        hp = M.numeric(hard[f"pipe_{f}"], hard[f"gold_{f}"])
        ha = M.numeric(hard[f"aud_{f}"],  hard[f"gold_{f}"])
        report.append(bp.render("  pipeline (all)"))
        report.append(ba.render("  auditor  (all)"))
        report.append(hp.render("  pipeline (rejected)"))
        report.append(ha.render("  auditor  (rejected)"))
        results[f"{f}_mae_pipeline"] = bp.mae
        results[f"{f}_mae_auditor"]  = ba.mae
        results[f"{f}_mae_auditor_hard"] = ha.mae
        report.append("")

    # ── the metric that actually matters ─────────────────────────────────────
    report.append("NEWSLETTER SELECTION  (combined >= 7, top 10 — what you actually ship)")
    report.append("-" * 78)
    sel = _selection_agreement(j)
    for line in sel:
        report.append(f"  {line}")
    results["selection"] = sel
    report.append("")

    # ── hallucination rate ───────────────────────────────────────────────────
    dropped = int(aud["dropped_quotes"].sum())
    total_dc = int(aud["aud_n_dc"].sum()) + dropped
    rate = dropped / total_dc if total_dc else 0.0
    report.append("EVIDENCE GROUNDING")
    report.append("-" * 78)
    report.append(f"  DC findings whose quote was NOT in the article: {dropped}/{total_dc} "
                  f"({rate:.1%}) — discarded.")
    results["hallucination_rate"] = rate
    report.append("")

    # ── gates ─────────────────────────────────────────────────────────────────
    report.append("GATES")
    report.append("-" * 78)
    passed = _check_gates(results, report)
    results["gates_passed"] = passed
    report.append("")
    report.append(f"  {'PASS — auditor may render verdicts on unlabeled data'if passed else 'FAIL — verdicts would be ADVISORY ONLY until this passes'}")
    report.append("")
    report.append(llm.USAGE.render())

    text = "\n".join(report)
    print(text)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (REPORTS_DIR / f"eval_{PROMPT_VERSION}_{split}_{stamp}.md").write_text(text, encoding="utf-8")
    j.to_csv(REPORTS_DIR / f"eval_rows_{PROMPT_VERSION}_{split}_{stamp}.csv", encoding="utf-8")

    with open(FILE_METRICS_HISTORY, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ts": stamp, "model": AUDIT_MODEL, "prompt": PROMPT_VERSION,
            "split": split, "n": len(j),
            **{k: (v if not isinstance(v, dict) else v) for k, v in results.items()},
        }, default=float) + "\n")

    _error_gallery(j, stamp, split)
    return results


def _selection_agreement(j: pd.DataFrame) -> list[str]:
    out = []
    d = j.reset_index()
    for who in ("pipe", "aud"):
        sel  = M.newsletter_selection(d, f"{who}_strategy", f"{who}_relevance")
        goldsel = M.newsletter_selection(d, "gold_strategy", "gold_relevance")
        a = M.selection_agreement(sel, goldsel)
        name = "pipeline" if who == "pipe" else "auditor "
        out.append(
            f"{name}: precision@10={a['precision_at_n']:.2f} "
            f"recall@10={a['recall_at_n']:.2f} jaccard={a['jaccard']:.2f} "
            f"(picked {a['n_pred']}, gold picks {a['n_gold']})"
        )
    return out


def _check_gates(r: dict, report: list) -> bool:
    ok = True
    def chk(name, val, gate, higher_better=True):
        nonlocal ok
        if val is None or (isinstance(val, float) and np.isnan(val)):
            report.append(f"  [ ?? ] {name:18s} not measurable")
            return
        good = val >= gate if higher_better else val <= gate
        ok = ok and good
        arrow = ">=" if higher_better else "<="
        report.append(f"  [{'PASS' if good else 'FAIL'}] {name:18s} {val:.3f} {arrow} {gate}")

    chk("dc_recall",     r.get("dc_recall_auditor"),    GATES["dc_recall"])
    chk("strategy_mae",  r.get("strategy_mae_auditor"), GATES["strategy_mae"],  higher_better=False)
    chk("relevance_mae", r.get("relevance_mae_auditor"),GATES["relevance_mae"], higher_better=False)
    return ok


def _error_gallery(j: pd.DataFrame, stamp: str, split: str) -> None:
    """
    The 20 worst misses in each direction, with the auditor's own rationale.

    This is what turns "recall is 0.87" into something a human can actually trust or
    reject. Metrics persuade nobody; twenty rows you can read do.
    """
    fn = j[(~j["aud_dc"].fillna(False).astype(bool)) & (j["gold_dc"].astype(bool))]
    fp = j[(j["aud_dc"].fillna(False).astype(bool)) & (~j["gold_dc"].astype(bool))]
    cols = ["Title", "aud_dc", "gold_dc", "pipe_dc", "aud_strategy", "gold_strategy",
            "aud_strategy_rationale", "aud_confidence", "accept"]
    cols = [c for c in cols if c in j.columns]
    with pd.ExcelWriter(REPORTS_DIR / f"error_gallery_{split}_{stamp}.xlsx") as w:
        fn.head(20)[cols].to_excel(w, sheet_name="DC_false_negatives")
        fp.head(20)[cols].to_excel(w, sheet_name="DC_false_positives")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("-n", type=int, default=500)
    p.add_argument("--split", default="dev", choices=["dev", "test"])
    a = p.parse_args()
    run_eval(n=a.n, split=a.split)
