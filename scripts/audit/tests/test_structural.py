"""
Unit tests for the deterministic structural checks.

These matter more than they look. The audit reports structural findings as
*certain* rather than probabilistic; that claim is only honest if the detectors
are themselves tested against frames with known, hand-planted defects.

    python -m pytest scripts/audit/tests/test_structural.py -v
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from audit import checks_structural as cs  # noqa: E402

COLUMNS = [
    "ID", "Title", "CleanURL", "Source", "PublishedDate", "Summary_AI",
    "Primary_Category", "Secondary_Categories", "States", "DC_ID",
    "Is_Duplicate", "Duplicate_Of", "Strategy_Alignment_Score",
    "Relevance_Score", "Mentions_Specific_DC", "Article_Text", "Processed",
]


def frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    for c in COLUMNS:
        if c not in df.columns:
            df[c] = pd.NA
    return df[COLUMNS]


def row(id, title="A story about a data center", dup=False, dup_of="",
        summary="s", strategy="3", relevance="3", dc="FALSE", pub="6/8/2026 17:33"):
    return {
        "ID": id, "Title": title, "PublishedDate": pub,
        "Summary_AI": summary if summary else pd.NA,
        "Is_Duplicate": "TRUE" if dup else "FALSE",
        "Duplicate_Of": dup_of,
        "Strategy_Alignment_Score": strategy,
        "Relevance_Score": relevance,
        "Mentions_Specific_DC": dc,
    }


# ── self-duplicate ────────────────────────────────────────────────────────────

def test_detects_self_duplicate():
    df = frame([
        row("A", dup=True, dup_of="A", summary=""),      # planted defect
        row("B", dup=True, dup_of="C"),
        row("C"),
    ])
    f = cs.check_self_duplicates(df)
    assert f.article_ids == ["A"]
    assert f.severity == "data_loss"


def test_no_false_positive_on_healthy_duplicate():
    df = frame([row("A", dup=True, dup_of="B"), row("B")])
    assert cs.check_self_duplicates(df).count == 0


# ── dangling target ───────────────────────────────────────────────────────────

def test_detects_dangling_target():
    df = frame([
        row("A", dup=True, dup_of="GHOST"),   # GHOST is not in the frame
        row("B", dup=True, dup_of="C"),
        row("C"),
    ])
    f = cs.check_dangling_targets(df)
    assert f.article_ids == ["A"]


# ── cycles ────────────────────────────────────────────────────────────────────

def test_detects_two_node_cycle():
    df = frame([
        row("A", dup=True, dup_of="B"),
        row("B", dup=True, dup_of="A"),   # A <-> B, no survivor
        row("C"),
    ])
    f = cs.check_duplicate_cycles(df)
    assert set(f.article_ids) == {"A", "B"}


def test_chain_reaching_survivor_is_not_a_cycle():
    df = frame([
        row("A", dup=True, dup_of="B"),
        row("B", dup=True, dup_of="C"),
        row("C"),                          # survivor — chain terminates cleanly
    ])
    assert cs.check_duplicate_cycles(df).count == 0


def test_self_reference_counts_as_stuck_chain():
    df = frame([row("A", dup=True, dup_of="A")])
    assert cs.check_duplicate_cycles(df).article_ids == ["A"]


# ── fully suppressed clusters ─────────────────────────────────────────────────

def test_detects_cluster_with_no_survivor():
    t = "Massive data center plan dropped in SC - The State"
    df = frame([
        row("A", title=t, dup=True, dup_of="X"),
        row("B", title=t, dup=True, dup_of="X"),   # both suppressed -> story lost
        row("C", title="Unrelated story"),
    ])
    f = cs.check_fully_suppressed_clusters(df)
    assert set(f.article_ids) == {"A", "B"}


def test_cluster_with_a_survivor_is_fine():
    t = "Massive data center plan dropped in SC - The State"
    df = frame([
        row("A", title=t, dup=True, dup_of="B"),
        row("B", title=t),                          # survivor present
    ])
    assert cs.check_fully_suppressed_clusters(df).count == 0


def test_cluster_matching_uses_pipelines_own_title_normalization():
    """En-dash vs hyphen must collapse to the same cluster — that was a real June bug."""
    df = frame([
        row("A", title="Data center plan – The State", dup=True, dup_of="X"),
        row("B", title="Data center plan - The State",      dup=True, dup_of="X"),
    ])
    assert cs.check_fully_suppressed_clusters(df).count == 2


# ── score rules ───────────────────────────────────────────────────────────────

def test_detects_rule3_violation():
    """The prompt's own RULE 3: Strategy=1 must force Relevance=1."""
    df = frame([
        row("A", strategy="1", relevance="4"),   # violation
        row("B", strategy="1", relevance="1"),   # compliant
        row("C", strategy="3", relevance="5"),
    ])
    assert cs.check_score_rule_violations(df).article_ids == ["A"]


def test_detects_out_of_range_score():
    df = frame([row("A", strategy="22", relevance="3")])
    assert "A" in cs.check_score_rule_violations(df).article_ids


# ── batch timestamps ──────────────────────────────────────────────────────────

def test_detects_batch_timestamp():
    rows = [row(f"R{i}", pub="07/08/2026 02:56 PM") for i in range(25)]
    rows.append(row("SOLO", pub="6/8/2026 17:33"))
    f = cs.check_batch_timestamps(frame(rows))
    assert len(f.article_ids) == 25
    assert "SOLO" not in f.article_ids


def test_normal_dates_are_not_flagged():
    rows = [row(f"R{i}", pub=f"6/{i+1}/2026 10:00") for i in range(20)]
    assert cs.check_batch_timestamps(frame(rows)).count == 0


# ── casing ────────────────────────────────────────────────────────────────────

def test_detects_casing_drift():
    df = frame([row("A", dc="TRUE"), row("B", dc="True")])
    f = cs.check_boolean_casing(df)
    assert f.severity == "cosmetic"          # not a live bug — consumers lowercase
    assert "True" in f.evidence
