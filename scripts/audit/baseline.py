"""
LightSignal — audit/baseline.py
===============================
Score the EXISTING pipeline against the human labels.

This runs before any model is called and costs nothing. It produces the number the
auditor has to beat, and it goes at the top of every eval report. No absolute metric
is ever shown without it — "recall 0.85" means nothing until you know the incumbent
scores 0.60.
"""

import sys
from pathlib import Path

import pandas as pd

SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from audit import metrics as M                      # noqa: E402
from audit.labels import GoldSet, build_gold        # noqa: E402
from audit.io_utils import snapshot_labels, as_nullable_bool  # noqa: E402


def pipeline_baseline(gold: GoldSet | None = None, split: str | None = None) -> str:
    if gold is None:
        gold = build_gold()

    raw = snapshot_labels()
    dc_corrections = as_nullable_bool(raw["Mentions_DC_Actual"]).notna()

    out = []
    out.append("PIPELINE BASELINE — the incumbent's score on your own labels")
    out.append("=" * 78)
    if split:
        out.append(f"split: {split}")
    out.append("")

    # ── DC mention ────────────────────────────────────────────────────────────
    d = gold.scope("dc", split=split)
    bm = M.binary(d["pipe_dc"], d["gold_dc"],
                  corrections_present=dc_corrections.reindex(d.index).fillna(False))
    out.append(bm.render("DC mention"))
    out.append("")

    # ── scores: overall and on the rows you rejected ──────────────────────────
    for f in ("strategy", "relevance"):
        s = gold.scope(f, split=split)
        h = gold.scope(f, split=split, hard_only=True)
        out.append(M.numeric(s[f"pipe_{f}"], s[f"gold_{f}"]).render(f"{f} (all)"))
        out.append(M.numeric(h[f"pipe_{f}"], h[f"gold_{f}"]).render(f"{f} (rejected only)"))
    out.append("")
    out.append("  The 'rejected only' rows are the honest number: they are the ones you")
    out.append("  marked Accept=NO. A positive bias there means the model over-scores")
    out.append("  precisely where it is already wrong.")
    out.append("")

    # ── dedup ─────────────────────────────────────────────────────────────────
    u  = gold.scope("dup", split=split)
    bm = M.binary(u["pipe_is_dup"], u["gold_is_dup"])
    out.append(bm.render("dedup"))
    out.append("")
    out.append("  Dedup precision IS measurable — you logged 84 'Dupe Error' rows where")
    out.append("  the pipeline suppressed a story that was not a duplicate.")

    return "\n".join(out)


if __name__ == "__main__":
    print(pipeline_baseline())
