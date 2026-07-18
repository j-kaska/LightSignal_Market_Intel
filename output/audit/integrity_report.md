# Structural integrity report

Scope: **entire news_feed.csv — 4011 rows**

These checks are deterministic — no model judged anything here. The findings are certain.

| Severity | Check | Articles affected | Summary |
|---|---|---:|---|
| data_loss | `duplicate_cycle` | 126 | Duplicate chains that never terminate at a surviving article (cycles and self-references). Every member is suppressed; no canonical survives. |
| data_loss | `self_duplicate` | 121 | Articles flagged as a duplicate of THEMSELVES. Each is suppressed from the newsletter and never summarized or scored, and points at no canonical. |
| data_loss | `dangling_duplicate_of` | 43 | Duplicate_Of points at an article ID absent from news_feed.csv. The canonical was never written, so the story exists nowhere. |
| data_loss | `fully_suppressed_cluster` | 28 | Title-clusters where every single member is flagged duplicate (12 clusters). The story reaches the newsletter zero times. |
| correctness | `batch_timestamp` | 3223 | PublishedDate is a fetch timestamp, not a publish date: 4011 articles share only 445 distinct values (3223 of them in run-sized clumps of 20+). Article age is unrecoverable from this column. |
| correctness | `score_rule_violation` | 19 | Scores outside 1-5, or violating the prompt's own RULE 3 (Strategy=1 must force Relevance=1). |
| correctness | `unscored_non_duplicate` | 2 | Rows that are NOT duplicates yet have no Summary_AI — classification never ran. |
| correctness | `dc_flag_base_rate` | 0 | Mentions_Specific_DC is true on 548/3731 scored rows (14.7%). Human-labeled base rate is ~4.3%. |
| cosmetic | `boolean_casing_drift` | 0 | Mixed boolean casing (TRUE/FALSE vs True/False) from two different writers. |

## `duplicate_cycle` — 126 articles

Duplicate chains that never terminate at a surviving article (cycles and self-references). Every member is suppressed; no canonical survives.

**Evidence:** 126 rows whose Duplicate_Of chain loops without reaching a survivor

**Fix:** Fix the self-guard (see self_duplicate), then enforce a canonical-selection rule so every cluster keeps exactly one survivor by construction.

## `self_duplicate` — 121 articles

Articles flagged as a duplicate of THEMSELVES. Each is suppressed from the newsletter and never summarized or scored, and points at no canonical.

**Evidence:** Blackstone-Backed Data-Center Operator Taps Investors Ahead of Over $1; After a 160% AI-Fueled Rally, Can Caterpillar Stock Keep Climbing? | M; Who would benefit from a U.S. wealth fund's investment in AI? - Market; Contentious crowd shouts boos during Lowell Township discussion about ; Wall Street Is Rushing to Fund the AI Bonanza in Every Conceivable Way

**Fix:** dedup.py: Layers 1a (line ~288) and 1b (line ~296) lack the self-match guard that Layer 2 (line ~308) already has. Add `if cached.get('id') == article_id: continue` to both.

## `dangling_duplicate_of` — 43 articles

Duplicate_Of points at an article ID absent from news_feed.csv. The canonical was never written, so the story exists nowhere.

**Evidence:** ART-20260302155640311961->ART-20260227152455943167; ART-20260326181437469390->ART-20260326140929273501; ART-20260611202519743209->ART-20260604233946032040; ART-20260611202521410023->ART-20260604233946032040; ART-20260612181816273212->ART-20260612181815597376

**Fix:** Sequencing bug: dedup (stage 1.5) chooses a canonical BEFORE extract.py (stage 2) drops BLOCKED_SOURCE_DOMAINS / YouTube rows. The canonical is deleted out from under its own duplicates. Move the filters ahead of dedup, or re-point orphans at a surviving cluster member.

## `fully_suppressed_cluster` — 28 articles

Title-clusters where every single member is flagged duplicate (12 clusters). The story reaches the newsletter zero times.

**Evidence:** 12 clusters, 28 articles lost outright

**Fix:** Canonical-selection rule: within each cluster keep exactly one survivor (prefer non-empty Summary_AI > CleanURL > longest text > earliest ID).

## `batch_timestamp` — 3223 articles

PublishedDate is a fetch timestamp, not a publish date: 4011 articles share only 445 distinct values (3223 of them in run-sized clumps of 20+). Article age is unrecoverable from this column.

**Evidence:** '6/8/2026 17:33' x157; '6/9/2026 17:52' x153; '07/09/2026 07:29 PM' x151; '6/10/2026 17:11' x148; '6/11/2026 20:25' x146; '07/08/2026 02:56 PM' x141

**Fix:** fetch_rss.py: persist the RSS entry's own published date (feedparser's entry.published_parsed) rather than the run clock. Until then, dedup's 14-day window and any newsletter recency logic are keyed off the wrong field.

## `score_rule_violation` — 19 articles

Scores outside 1-5, or violating the prompt's own RULE 3 (Strategy=1 must force Relevance=1).

**Evidence:** 1 out-of-range, 18 RULE 3 violations

**Fix:** Validate the model's JSON against the rubric before writing the row.

## `unscored_non_duplicate` — 2 articles

Rows that are NOT duplicates yet have no Summary_AI — classification never ran.

**Evidence:** 2 rows

**Fix:** Inspect classify_status in staged_articles.csv for these IDs.

## `dc_flag_base_rate` — 0 articles

Mentions_Specific_DC is true on 548/3731 scored rows (14.7%). Human-labeled base rate is ~4.3%.

**Evidence:** 14.69% vs ~4.3% expected

**Fix:** The classify prompt defines a specific-DC mention as a NAMED FACILITY only, and explicitly excludes 'a proposed data center in Box Elder County'. That excludes exactly the rumored/proposed sites the user wants. Spec bug, not a model failure.
