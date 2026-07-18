"""
LightSignal — audit/eval_hard.py
================================
The honest trust gate.

WHY THIS FILE EXISTS
--------------------
eval.py grades against `gold`, and the imputation policy sets gold = the pipeline's
own value on every row the user did not explicitly correct. That is ~90% of rows. On
those, we are not measuring accuracy at all — we are measuring AGREEMENT WITH THE
PIPELINE, and every place the auditor legitimately disagrees is scored against it.

The tell: eval.py reports the pipeline's DC recall as 1.000 on the dev sample, when
the true figure over the whole label set is 0.604. A metric that flatters the
incumbent to perfection is not measuring the incumbent.

So this module throws away the imputed rows and evaluates ONLY where the user's own
hand is on the label:

  * DC       — the 203 rows where they wrote Mentions_DC_Actual (all True: "the AI
               missed a real data center here"). Pipeline recall on these is 0 BY
               DEFINITION. The auditor's recall on them is a real, uncontaminated
               measurement of whether it finds what the pipeline misses.

  * SCORES   — the ~978 / ~930 rows where they wrote a corrected score. Both systems
               are graded against the human number. Nobody gets credit for agreeing
               with the pipeline.

  * DUPES    — the 310 "Missed Dupe" and 84 "Dupe Error" rows.

These are small, adversarial, and the only numbers worth anything.
"""

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from audit import llm, metrics as M                              # noqa: E402
from audit.audit_config import (                                 # noqa: E402
    MIN_ARTICLE_CHARS, MAX_ARTICLE_CHARS, PROMPT_VERSION, AUDIT_MODEL, REPORTS_DIR,
)
from audit.auditor import audit_many, verdicts_to_frame          # noqa: E402
from audit.io_utils import snapshot_labels, as_bool, as_nullable_bool, as_score  # noqa: E402


def _prep(raw: pd.DataFrame) -> pd.DataFrame:
    df = raw.copy()
    df["text"]     = df["Article_Text"].fillna("").astype(str).str.slice(0, MAX_ARTICLE_CHARS)
    df["text_len"] = df["text"].str.len()
    df["source"]   = df.get("Source", "")
    return df[df["text_len"] >= MIN_ARTICLE_CHARS]


def run(max_per_task: int | None = None) -> None:
    raw = snapshot_labels()
    df  = _prep(raw)

    df["dc_corr"]        = as_nullable_bool(df["Mentions_DC_Actual"])
    df["strategy_corr"]  = as_score(df["Strategy Score_Actual"])
    df["relevance_corr"] = as_score(df["Relevance_Score_Actual"])
    df["pipe_dc"]        = as_bool(df["Mentions_Specific_DC"])
    df["pipe_strategy"]  = as_score(df["Strategy_Alignment_Score"])
    df["pipe_relevance"] = as_score(df["Relevance_Score"])

    corrected = df[
        df["dc_corr"].notna()
        | df["strategy_corr"].notna()
        | df["relevance_corr"].notna()
    ]
    if max_per_task:
        corrected = corrected.head(max_per_task)

    print(f"Rows where YOUR hand is on the label: {len(corrected)}")
    print("(these are the only rows where 'gold' is not just the pipeline's own answer)\n")

    verdicts = audit_many(corrected, workers=8)
    aud = verdicts_to_frame(verdicts)
    j = corrected.merge(aud, on="ID", how="inner")
    j = j[j["audit_ok"]]

    out = []
    out.append("=" * 78)
    out.append("HARD EVAL — only rows the user actually corrected")
    out.append(f"model={AUDIT_MODEL}  prompt={PROMPT_VERSION}  n={len(j)}")
    out.append("=" * 78)
    out.append("")
    out.append("Rows where the user did not correct anything are EXCLUDED: on those,")
    out.append("'gold' is the pipeline's own output, so grading against it measures")
    out.append("agreement-with-the-pipeline, not accuracy.")
    out.append("")

    # ── DC: the rows where the user said "you missed one" ────────────────────
    dc = j[j["dc_corr"].notna()]
    if len(dc):
        gold = dc["dc_corr"].astype(bool)
        pipe = dc["pipe_dc"].astype(bool)
        auditor = dc["aud_dc"].fillna(False).astype(bool)
        out.append("DC MENTION — the data centers YOU said the pipeline missed")
        out.append("-" * 78)
        out.append(f"  n = {len(dc)}   (all are gold-positive: you corrected False -> True)")
        out.append(f"  pipeline found : {int((pipe & gold).sum()):3d}/{len(dc)}   recall = {(pipe & gold).sum()/max(gold.sum(),1):.3f}")
        out.append(f"  auditor  found : {int((auditor & gold).sum()):3d}/{len(dc)}   recall = {(auditor & gold).sum()/max(gold.sum(),1):.3f}")
        out.append("")
        out.append("  This is the uncontaminated DC number. The pipeline scores ~0 here by")
        out.append("  construction — these rows are DEFINED as its misses. The question is")
        out.append("  purely whether the auditor recovers them.")
        out.append("")

    # ── scores: both systems graded against the human number ─────────────────
    for f in ("strategy", "relevance"):
        s = j[j[f"{f}_corr"].notna()]
        if not len(s):
            continue
        gold = s[f"{f}_corr"]
        bp = M.numeric(s[f"pipe_{f}"], gold)
        ba = M.numeric(s[f"aud_{f}"],  gold)
        out.append(f"{f.upper()} — graded against YOUR corrected score")
        out.append("-" * 78)
        out.append(bp.render("  pipeline"))
        out.append(ba.render("  auditor "))
        delta = bp.mae - ba.mae
        out.append(f"  -> auditor reduces error by {delta:+.3f} MAE "
                   f"({delta/bp.mae*100:+.0f}%)" if bp.mae else "")
        out.append(f"  -> inflation (bias): pipeline {bp.bias:+.3f}  auditor {ba.bias:+.3f}")
        out.append("")

    # ── evidence grounding ───────────────────────────────────────────────────
    dropped  = int(aud["dropped_quotes"].sum())
    total_dc = int(aud["aud_n_dc"].sum()) + dropped
    rate = dropped / total_dc if total_dc else 0.0
    out.append("EVIDENCE GROUNDING")
    out.append("-" * 78)
    out.append(f"  DC findings discarded for an unverifiable quote: {dropped}/{total_dc} ({rate:.1%})")
    out.append("  (a quote that is not a literal span of the article is treated as a")
    out.append("   hallucination and the finding is dropped)")
    out.append("")
    out.append(llm.USAGE.render())

    text = "\n".join(out)
    print(text)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (REPORTS_DIR / f"eval_hard_{PROMPT_VERSION}_{stamp}.md").write_text(text, encoding="utf-8")
    j.to_csv(REPORTS_DIR / f"eval_hard_rows_{PROMPT_VERSION}_{stamp}.csv", index=False, encoding="utf-8")
    print(f"\nWrote eval_hard_{PROMPT_VERSION}_{stamp}.md + rows CSV")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("-n", type=int, default=None, help="cap rows (for a cheap dry run)")
    a = p.parse_args()
    run(max_per_task=a.n)
