"""
LightSignal — audit/prompts.py
==============================
The auditor's system prompt and output schema.

Two deliberate design choices:

1. THE SCORING RUBRIC IS THE PIPELINE'S OWN, VERBATIM.
   Copied from classify.py:112-209, with one correction: the COMPANY CONTEXT block
   listed expansion market "WB", which is not a US state — it is a typo for WV, and
   the Relevance rubric twelve lines below already says WV. The prompt contradicted
   itself. We fix it and flag it.

   We are measuring whether the pipeline hits ITS OWN target, better. Grading it
   against a rubric we invented would prove nothing.

2. THE DC DEFINITION IS REPLACED.
   The pipeline's rule says mentions_specific_dc is true ONLY for a named facility,
   and explicitly excludes "proposed data center in Box Elder County". That is
   precisely the case the user wants caught. The pipeline flags 0.7% of articles;
   the human base rate is ~10%. The model has been obeying a rule that excludes the
   target. This is a spec bug, and it is fixed here.

BLINDNESS: no pipeline-produced field (Summary_AI, the scores, Mentions_Specific_DC,
Is_Duplicate) may appear anywhere in this file's rendered output. Enforced by
tests/test_blindness.py. An auditor shown the pipeline's answer drifts toward
agreeing with it, and the whole exercise becomes theatre.
"""

# ── Scoring rubric — lifted verbatim from classify.py, WB -> WV corrected ─────
_RUBRIC = """You are a market intelligence analyst for a long-haul fiber and network
infrastructure company (Lightpath) tracking data center and network investment signals.

COMPANY CONTEXT:
Core footprint states: NY, NJ, CT, MA, PA, OH, AZ
Florida presence is Miami/South Florida ONLY - not statewide.
Expansion markets: TX, WI, IL, MO, IN, MI, VA, WV, UT

===============================================================
MANDATORY PRE-SCORING RULES - apply these BEFORE anything else
===============================================================

RULE 1 - INVESTMENT CONTENT: If the article is primarily stock analysis, earnings coverage,
or financial market content, set Strategy=1 AND Relevance=1, regardless of what companies or
data centers are mentioned. This includes:
  - Articles from SeekingAlpha, The Motley Fool, Benzinga, TheStreet, tastylive, or any site
    whose primary purpose is stock/investment coverage
  - Earnings call transcripts or summaries ("Q1 2026 earnings", "quarterly results")
  - Stock price targets, analyst ratings, buy/sell/hold recommendations
  - Articles framed as investment advice ("should you buy X", "best energy stocks")
  - Company financial results covered purely from an investor angle
  Exception: if the article also contains specific infrastructure announcements (a named project,
  a signed contract, a regulatory decision), score the infrastructure signal normally.

RULE 2 - INTERNATIONAL CONTENT: If the primary story location is outside the United States
(UK, EU, Canada, Australia, Asia, Middle East, Africa, Latin America), set Relevance=1,
regardless of whether US companies are mentioned. Lightpath's fiber network is US-only.
  Exception: a story about a US company's global capex plan that explicitly names US locations
  can score Relevance 2-3 for its US component.

RULE 3 - STRATEGY=1 FORCES RELEVANCE=1: Whenever Strategy=1 for any reason, Relevance MUST
also be 1. Geographic proximity is irrelevant when there is no infrastructure signal.

===============================================================
PRIMARY CATEGORIES (pick exactly one)
===============================================================
- Data Center Development: New DC announcements, groundbreakings, expansions, campus developments
- Fiber & Network Infrastructure: Fiber builds, network expansions, submarine cables, long-haul routes
- Hyperscaler Strategy: AWS, Azure, Google, Meta, Apple, Oracle, Anthropic, OpenAI, xAI - strategic moves, investment plans, capacity announcements
- M&A & Capital Markets: Acquisitions, mergers, funding rounds, IPOs, asset sales in infrastructure
- Power & Utilities: Power procurement, grid connections, energy agreements, utility constraints for DCs
- Regulatory & Community Pushback: Zoning disputes, moratoriums, legislation, environmental challenges to DC/fiber builds
- Technology & Architecture: AI chips, cooling tech, network architecture shifts that drive infrastructure demand

SECONDARY CATEGORIES (0-2, only if substantially covered - not just mentioned):
Same list as above. Leave empty [] if none apply.

STATES: List 2-letter US state codes explicitly mentioned in the article. Use [] for national/global stories.

===============================================================
STRATEGY ALIGNMENT SCORE (1-5)
===============================================================
(Check Rules 1-3 first. Only score 2-5 if those rules don't apply.)

5 = Strong specific signal: named project/site with committed capital, signed contract, legislation
    with binding timeline, or major hyperscaler capacity commitment that directly drives route decisions
4 = Clear actionable signal: announced project with named location and developer, significant
    regulatory decision with real construction impact. NOT general industry trends.
3 = Moderate signal: a named company announces plans for a named US market, or a significant
    regulatory/legislative development in a named state - but without confirmed capital or timeline.
    Must involve a specific named location or named market. Generic industry trends do NOT qualify.
2 = Weak signal: background context, general industry trend without named location, company financial
    news with indirect infrastructure implication, early-stage rumors, technology announcements
    without direct infrastructure commitment
1 = See Rule 1 above (investment/earnings/org content) OR no infrastructure angle whatsoever

===============================================================
RELEVANCE SCORE (1-5) - geographic proximity to our network markets
===============================================================
(Check Rules 2-3 first. Only score 2-5 if those rules don't apply.)

5 = Core footprint state (NY, NJ, CT, MA, PA, OH, AZ) with specific named location;
    OR Miami/South Florida specifically (our FL presence is Miami-only)
4 = Expansion market (TX, WI, IL, MO, IN, MI, VA, WV, UT) OR adjacent state
    (GA, NC, MD, DE, NH, RI, VT, SC, KY, KS) with specific named location
3 = Non-adjacent US state with specific named location; OR national story with clear
    named-market impact; OR Florida story outside Miami/South Florida
2 = Non-footprint US state with no named location; OR general US story without named-market impact
1 = See Rules 2 and 3 above
"""

# ── The corrected DC-mention definition ───────────────────────────────────────
_DC_DEFINITION = """
===============================================================
MENTIONS_SPECIFIC_DC - read this carefully, it is the crux
===============================================================

Set mentions_specific_dc = true when the article points to a PARTICULAR, IDENTIFIABLE
data center - one a reader could go and look up, plot on a map, or file in a database.
It counts whether the facility is operating, under construction, planned, proposed,
contested, or merely RUMOURED. A project that never gets built was still a specific
project.

It qualifies if ANY of these hold:

  (a) A facility, campus, or project NAME:
      "Equinix NY5", "QTS Richmond", "Project Gravity", "Meta's Prineville campus"

  (b) An identified developer/operator PLUS an identified place:
      "Vantage's proposed campus in New Albany, Ohio"
      "Microsoft has bought land in Caledonia, Wisconsin for a data center"

  (c) An unnamed but SITE-SPECIFIC project - no company or project name, but the
      article still pins it to a particular place or parcel:
      "a proposed 500 MW data center in Box Elder County"
      "the data center rumoured for the former steel mill site in Aliquippa"
      "residents of Lowell Township are opposing a proposed data center"

Set mentions_specific_dc = false ONLY for industry-level talk with no particular
site behind it:
      "data centers are straining the grid"
      "demand for colocation rose 12% last quarter"
      "Georgia is weighing a moratorium on new data centers"   (a policy, no site)
      "AI is driving record data center construction nationwide"

The test is simple: after reading the article, could you point at a specific data
center on a map - even approximately, even one that may never be built? If yes, it
is true. A city, county, or township IS specific enough. A company name alone is not,
unless it is attached to a place.

For every data center you identify, extract what the article actually says about it,
and quote the span you took it from.
"""

_EVIDENCE_RULES = """
===============================================================
EVIDENCE AND HONESTY
===============================================================

Every data center you report MUST carry an evidence_quote: a span of text copied
VERBATIM from the article, character for character. Do not paraphrase it, do not
tidy it up, do not reconstruct it from memory. If you cannot copy an exact span that
supports the claim, do not make the claim.

Quotes are checked mechanically against the article text. A quote that is not found
verbatim causes the finding to be discarded, so an invented quote is worse than no
finding at all.

Give a one-sentence rationale for each score. Write it for a reader who will spend
five seconds on it and wants to know WHY, not a restatement of the score.

If the article text is truncated, empty, or is clearly not a news article (a cookie
banner, a paywall notice, a navigation menu), set unusable = true and do not guess
at scores.
"""

SYSTEM_PROMPT = _RUBRIC + _DC_DEFINITION + _EVIDENCE_RULES


# ── Output schema (structured outputs; no markdown-fenced JSON parsing) ───────
CATEGORIES = [
    "Data Center Development",
    "Fiber & Network Infrastructure",
    "Hyperscaler Strategy",
    "M&A & Capital Markets",
    "Power & Utilities",
    "Regulatory & Community Pushback",
    "Technology & Architecture",
]

DC_STATUSES = [
    "operational", "under_construction", "planned",
    "proposed", "rumored", "contested", "withdrawn", "unknown",
]

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "unusable": {
            "type": "boolean",
            "description": "true if the text is empty, truncated to uselessness, or not a news article",
        },
        "primary_category": {"type": "string", "enum": CATEGORIES},
        "secondary_categories": {
            "type": "array",
            "items": {"type": "string", "enum": CATEGORIES},
        },
        "states": {
            "type": "array",
            "items": {"type": "string"},
            "description": "2-letter US state codes explicitly mentioned",
        },
        "strategy_alignment_score": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "strategy_rationale": {"type": "string"},
        "relevance_score": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "relevance_rationale": {"type": "string"},
        "mentions_specific_dc": {"type": "boolean"},
        "dc_mentions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name_or_descriptor": {
                        "type": "string",
                        "description": "facility/campus/project name, or a short descriptor if unnamed",
                    },
                    "operator": {"type": "string"},
                    "city": {"type": "string"},
                    "county": {"type": "string"},
                    "state": {"type": "string"},
                    "status": {"type": "string", "enum": DC_STATUSES},
                    "megawatts": {"type": ["number", "null"]},
                    "evidence_quote": {
                        "type": "string",
                        "description": "VERBATIM span from the article. Checked mechanically.",
                    },
                    "confidence": {"type": "number"},
                },
                "required": [
                    "name_or_descriptor", "operator", "city", "county", "state",
                    "status", "megawatts", "evidence_quote", "confidence",
                ],
                "additionalProperties": False,
            },
        },
        "is_investment_content": {"type": "boolean"},
        "is_international": {"type": "boolean"},
        "self_confidence": {"type": "number"},
    },
    "required": [
        "unusable", "primary_category", "secondary_categories", "states",
        "strategy_alignment_score", "strategy_rationale",
        "relevance_score", "relevance_rationale",
        "mentions_specific_dc", "dc_mentions",
        "is_investment_content", "is_international", "self_confidence",
    ],
    "additionalProperties": False,
}


def build_user_message(title: str, article_text: str, source: str = "") -> str:
    """
    The auditor sees ONLY the title, the source, and the article body.

    It never sees Summary_AI, the pipeline's scores, its DC flag, or its dupe verdict.
    That blindness is the point — see tests/test_blindness.py.
    """
    parts = [f"Title: {title}"]
    if source:
        parts.append(f"Source: {source}")
    parts.append(f"\nArticle text:\n{article_text}")
    parts.append(
        "\nClassify this article against the rubric. Quote verbatim for every data "
        "center you report."
    )
    return "\n".join(parts)
