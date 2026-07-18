"""
Tests for gold-label derivation and the metric honesty rails.

The measurability tests are the important ones. They encode the fact that this label
set CANNOT measure DC precision, and they fail loudly if someone later "fixes" the
harness into reporting a 1.000 that isn't real.

    python -m pytest scripts/audit/tests/test_labels.py -v
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from audit import metrics as M                                   # noqa: E402
from audit.io_utils import as_bool, as_nullable_bool             # noqa: E402
from audit.labels import build_gold                              # noqa: E402


# ── boolean coercion ──────────────────────────────────────────────────────────
# The xlsx round-trip turns booleans into floats. A coercion that only understands
# the string "true" silently returns all-False, which reads as "no positives" and
# flatters every metric. This is a real bug that this suite caught.

@pytest.mark.parametrize("raw,expected", [
    (["TRUE", "FALSE"],   [True, False]),      # news_feed.csv, old writer
    (["True", "False"],   [True, False]),      # news_feed.csv, gemini-era writer
    ([True, False],       [True, False]),      # xlsx bool column
    ([1.0, 0.0],          [True, False]),      # xlsx float round-trip  <-- the bug
    ([1, 0],              [True, False]),
])
def test_as_bool_handles_every_shape_the_data_actually_takes(raw, expected):
    assert as_bool(pd.Series(raw)).tolist() == expected


def test_as_bool_treats_missing_as_false_never_nan():
    assert as_bool(pd.Series([None, pd.NA, ""])).tolist() == [False, False, False]


def test_as_nullable_bool_preserves_not_stated():
    """Blank in a correction column means 'I didn't correct this', NOT 'I said false'."""
    out = as_nullable_bool(pd.Series([1.0, 0.0, None]))
    assert out.iloc[0] is True
    assert out.iloc[1] is False
    assert pd.isna(out.iloc[2])


# ── the measurability guard ───────────────────────────────────────────────────

def test_precision_is_unmeasurable_without_true_to_false_corrections():
    """
    This is the shape of the real label file: the human only ever corrected
    False->True. No false positive can be expressed, so precision must be refused.
    """
    pred = pd.Series([True,  True,  False, False])
    gold = pd.Series([True,  True,  True,  False])   # no gold-False where pred is True
    corr = pd.Series([False, False, True,  False])   # corrections only on pred=False rows

    m = M.binary(pred, gold, corrections_present=corr)
    assert m.precision is None, "precision must be refused, not reported as 1.000"
    assert "circular" in m.note
    assert m.recall == pytest.approx(2 / 3)          # recall IS real
    assert "unmeasurable" in m.render("dc")


def test_precision_is_measurable_when_a_true_was_corrected_down():
    pred = pd.Series([True,  True,  False])
    gold = pd.Series([True,  False, True])           # row 1: model said True, human said False
    corr = pd.Series([False, True,  True])

    m = M.binary(pred, gold, corrections_present=corr)
    assert m.precision == pytest.approx(0.5)
    assert m.note == ""


def test_binary_without_corrections_reports_precision_normally():
    """Dedup labels DO contain both directions, so precision is fair game there."""
    m = M.binary(pd.Series([True, True, False]), pd.Series([True, False, True]))
    assert m.precision == pytest.approx(0.5)
    assert m.recall == pytest.approx(0.5)


# ── imputation policy ─────────────────────────────────────────────────────────

def _label_frame(rows):
    cols = [
        "ID", "Title", "Summary_AI", "Mentions_Specific_DC", "Is_Duplicate",
        "Duplicate_Of", "Strategy_Alignment_Score", "Relevance_Score",
        "Accept", "Dupe Error", "Dupe ID", "Mentions_DC_Actual",
        "Strategy Score_Actual", "Relevance_Score_Actual", "Notes",
    ]
    df = pd.DataFrame(rows)
    for c in cols:
        if c not in df.columns:
            df[c] = pd.NA
    return df[cols]


def test_rejected_but_uncorrected_score_is_excluded_not_assumed_correct():
    """
    The core of the policy. A row you rejected without saying what the right score was
    tells us the pipeline is wrong but not what right looks like. Counting it as
    'pipeline was correct' would silently inflate the incumbent's score.
    """
    df = _label_frame([
        {"ID": "A", "Accept": "NO",  "Strategy_Alignment_Score": 4},                              # no correction -> OUT
        {"ID": "B", "Accept": "NO",  "Strategy_Alignment_Score": 4, "Strategy Score_Actual": 2},  # corrected -> IN
        {"ID": "C", "Accept": "YES", "Strategy_Alignment_Score": 3},                              # ratified -> IN
    ])
    g = build_gold(df)
    in_scope = set(g.scope("strategy")["ID"])
    assert in_scope == {"B", "C"}
    assert "A" not in in_scope

    row_b = g.df.set_index("ID").loc["B"]
    assert row_b["gold_strategy"] == 2      # your correction wins
    row_c = g.df.set_index("ID").loc["C"]
    assert row_c["gold_strategy"] == 3      # ratified -> pipeline value is gold


def test_duplicate_rows_are_out_of_scope_for_the_dc_metric():
    """Dupes are short-circuited before classification, so no DC call was ever made."""
    df = _label_frame([
        {"ID": "A", "Accept": "YES", "Is_Duplicate": True,  "Mentions_Specific_DC": None},
        {"ID": "B", "Accept": "YES", "Is_Duplicate": False, "Mentions_Specific_DC": 0.0},
    ])
    g = build_gold(df)
    assert set(g.scope("dc")["ID"]) == {"B"}


def test_missed_dupe_label_becomes_a_recall_target():
    df = _label_frame([
        {"ID": "A", "Accept": "NO", "Is_Duplicate": False, "Dupe Error": "Missed Dupe", "Dupe ID": "Z"},
        {"ID": "B", "Accept": "NO", "Is_Duplicate": True,  "Dupe Error": "Dupe Error"},
    ])
    g = build_gold(df).df.set_index("ID")
    assert g.loc["A", "gold_is_dup"] == True    # you say it IS a dupe; pipeline missed it
    assert g.loc["B", "gold_is_dup"] == False   # you say it is NOT; pipeline over-suppressed
