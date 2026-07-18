"""
LightSignal — audit/run_audit.py
================================
CLI entry point for the audit harness.

    python scripts/audit/run_audit.py integrity              # Phase 0, free, no LLM
    python scripts/audit/run_audit.py integrity --all        # whole file, not just unprocessed
    python scripts/audit/run_audit.py snapshot               # freeze the label workbook

Every subcommand runs inside read_only_guard(): the live CSVs are hashed on entry
and re-hashed on exit, and any change is a hard failure.
"""

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from audit import checks_structural as cs          # noqa: E402
from audit.audit_config import REPORTS_DIR         # noqa: E402
from audit.io_utils import (                       # noqa: E402
    load_news_feed, unprocessed, read_only_guard, snapshot_labels,
)


def cmd_integrity(args) -> int:
    df    = load_news_feed()
    scope = "entire news_feed.csv" if args.all else "unprocessed rows (Processed != TRUE)"
    target = df if args.all else unprocessed(df)

    print(f"Scanning {len(target)} rows ({scope})...\n")
    findings = cs.run_all(target)

    md  = cs.to_markdown(findings, scope=f"{scope} — {len(target)} rows")
    out_md  = REPORTS_DIR / "integrity_report.md"
    out_csv = REPORTS_DIR / "integrity_issues.csv"
    out_md.write_text(md, encoding="utf-8")
    cs.to_dataframe(findings).to_csv(out_csv, index=False, encoding="utf-8")

    # Some checks are signals rather than per-article defects (base rates, casing);
    # they carry no article IDs. Showing those as "ok" would bury the point.
    SIGNAL_CHECKS = {"dc_flag_base_rate", "boolean_casing_drift"}

    for f in sorted(findings, key=lambda f: (cs.SEVERITY_ORDER[f.severity], -f.count)):
        if f.check_id in SIGNAL_CHECKS:
            marker = " info "
        elif f.severity == "data_loss" and f.count:
            marker = " LOSS "
        elif f.count:
            marker = " warn "
        else:
            marker = "  ok  "
        count = "" if f.check_id in SIGNAL_CHECKS else f"{f.count:5d}"
        print(f"[{marker}] {f.check_id:28s} {count:>5s}  {f.summary[:78]}")

    lost_ids = set()
    for f in findings:
        if f.severity == "data_loss":
            lost_ids |= set(f.article_ids)
    print(
        f"\n{len(lost_ids)} distinct articles are affected by a data-loss defect "
        f"— suppressed from the newsletter with no surviving canonical."
    )
    print(f"\nWrote {out_md}\n      {out_csv}")
    return 0


def cmd_snapshot(args) -> int:
    df = snapshot_labels(force=True)
    print(f"Snapshotted {len(df)} labeled rows.")
    print(f"Columns: {list(df.columns)}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="run_audit", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("integrity", help="Phase 0 — deterministic structural checks (free)")
    pi.add_argument("--all", action="store_true",
                    help="scan the whole file instead of just unprocessed rows")
    pi.set_defaults(func=cmd_integrity)

    ps = sub.add_parser("snapshot", help="freeze news_feed_test_feedback.xlsx to parquet")
    ps.set_defaults(func=cmd_snapshot)

    args = p.parse_args()
    with read_only_guard():
        return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
