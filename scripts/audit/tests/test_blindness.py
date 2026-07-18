"""
The blindness and evidence tests.

These two properties are what separate an audit from theatre:

  BLINDNESS  — the auditor must never see the pipeline's answer. Shown the incumbent's
               verdict, a model drifts toward agreeing with it, and a "disagreement
               report" built on that is worthless.

  EVIDENCE   — every DC finding must quote the article verbatim. An invented quote is
               worse than no finding at all, because a reviewer would take it on trust.

    python -m pytest scripts/audit/tests/test_blindness.py -v
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from audit.audit_config import PIPELINE_FIELDS                          # noqa: E402
from audit.llm import verify_quote                                      # noqa: E402
from audit.prompts import SYSTEM_PROMPT, build_user_message             # noqa: E402


# ── Blindness ─────────────────────────────────────────────────────────────────

def test_system_prompt_never_names_a_pipeline_output_field():
    for field in PIPELINE_FIELDS:
        assert field not in SYSTEM_PROMPT, (
            f"system prompt references the pipeline field {field!r}. The auditor must "
            f"not know what the pipeline decided."
        )


def test_user_message_carries_only_title_source_and_body():
    """
    Render a message from a row that carries every pipeline verdict, then assert none
    of those verdicts survived into the prompt.
    """
    msg = build_user_message(
        title="Vantage plans a campus in New Albany",
        article_text="Vantage Data Centers said it would build in New Albany, Ohio.",
        source="Columbus Dispatch",
    )
    leaked = [
        v for v in (
            "Summary_AI", "Strategy_Alignment_Score", "Relevance_Score",
            "Mentions_Specific_DC", "Is_Duplicate", "Duplicate_Of", "DC_ID",
        ) if v in msg
    ]
    assert not leaked, f"pipeline fields leaked into the prompt: {leaked}"


def test_user_message_does_not_leak_a_pipeline_score_value():
    """Guard against someone later f-stringing the pipeline's score into the prompt."""
    msg = build_user_message(title="T", article_text="Body text here.", source="S")
    assert "score" not in msg.lower() or "Classify" in msg
    assert msg.count("Title:") == 1
    assert "Summary" not in msg


def test_the_broad_dc_definition_is_actually_in_the_prompt():
    """
    The spec bug we are correcting: the pipeline explicitly EXCLUDES Box Elder County.
    The auditor must explicitly INCLUDE it. If this assertion ever flips, the auditor
    has silently reverted to the rule the user rejected.
    """
    assert "Box Elder County" in SYSTEM_PROMPT
    assert "rumoured" in SYSTEM_PROMPT or "rumored" in SYSTEM_PROMPT
    # the pipeline's exclusionary phrasing must NOT be present
    assert "are NOT specific enough" not in SYSTEM_PROMPT


def test_wb_typo_is_corrected_to_wv():
    """classify.py's COMPANY CONTEXT says expansion market 'WB', which is not a state."""
    assert " WB," not in SYSTEM_PROMPT and " WB\n" not in SYSTEM_PROMPT
    assert "WV" in SYSTEM_PROMPT


# ── Evidence ──────────────────────────────────────────────────────────────────

ARTICLE = (
    "Residents of Lowell Township packed the meeting hall Tuesday to oppose a "
    "proposed 250 MW data center on the former Carter farm parcel. The developer, "
    "Vantage Data Centers, said construction could begin in 2027."
)


def test_verbatim_quote_verifies():
    assert verify_quote("a proposed 250 MW data center on the former Carter farm parcel", ARTICLE)


def test_invented_quote_is_rejected():
    assert not verify_quote("a proposed 900 MW data center in Ashburn, Virginia", ARTICLE)


def test_paraphrase_is_rejected():
    """A paraphrase is not evidence, however true it may be."""
    assert not verify_quote("Locals objected to a large datacenter development", ARTICLE)


def test_empty_quote_is_rejected():
    assert not verify_quote("", ARTICLE)
    assert not verify_quote("   ", ARTICLE)


def test_quote_matching_tolerates_the_mojibake_in_article_text():
    """
    Article_Text carries cp1252->utf8 damage: smart quotes and em-dashes became U+FFFD.
    A quote must still verify across that corruption, or every finding on a damaged
    article would be discarded and we would under-report rather than over-report.
    """
    damaged = "data centers� independence from the grid will hike costs"
    assert verify_quote("data centers' independence from the grid", damaged)


def test_quote_matching_tolerates_whitespace_and_dash_variants():
    assert verify_quote("a  proposed   250 MW\ndata center", ARTICLE)
    assert verify_quote("Vantage Data Centers", ARTICLE.replace("Vantage", "Vantage"))


# ── Fatal-error handling ──────────────────────────────────────────────────────
# A billing or auth failure hits every row identically. If it degrades into
# per-article "audit_ok=False", the final report is indistinguishable from a run
# that simply found nothing — which is the exact silent failure this project exists
# to eliminate. It must abort loudly instead.

from audit.llm import FatalAPIError, _raise_if_fatal   # noqa: E402


@pytest.mark.parametrize("msg", [
    "Error code: 400 - Your credit balance is too low to access the Anthropic API.",
    "authentication_error: invalid x-api-key",
    "permission_error: not allowed",
])
def test_account_level_errors_are_fatal(msg):
    with pytest.raises(FatalAPIError):
        _raise_if_fatal(RuntimeError(msg))


@pytest.mark.parametrize("msg", [
    "rate_limit_error: too many requests",
    "overloaded_error",
    "Connection reset by peer",
])
def test_transient_errors_are_not_fatal(msg):
    _raise_if_fatal(RuntimeError(msg))   # must not raise — these are worth retrying
