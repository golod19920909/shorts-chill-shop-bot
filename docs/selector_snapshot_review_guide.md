# Selector snapshot human-review guide

The blind selector replay must use identical frozen AliExpress Affiliate API candidate pools for Gemini 3.1 and 3.5. Raw captures are not gold labels. A reviewer must independently label every candidate before the replay runner will accept a snapshot.

## 0. Capture a live selector input

From the project directory in PowerShell, start one controlled run with the opt-in capture enabled:

```powershell
$env:SHORTS_CHILL_SELECTOR_CAPTURE = "1"
$env:SHORTS_CHILL_SELECTOR_CAPTURE_DIR = "reports\selector-captures"
python main.py
```

Run one real search (prefer the known test query "нужен игровой ноутбук"), then stop the bot with Ctrl+C. The JSON capture is written under reports/selector-captures. The capture contains the selector query and candidate fields, but no product URLs or user IDs. It is intentionally marked as an unreviewed selector input; it is not benchmark-ready yet.

Remove the temporary process variables after the run:

```powershell
Remove-Item Env:SHORTS_CHILL_SELECTOR_CAPTURE
Remove-Item Env:SHORTS_CHILL_SELECTOR_CAPTURE_DIR
```

No .env edit is required.

## 1. Preserve source provenance

Keep the captured request and candidate payload unchanged. Confirm from the corresponding bot log:

- the source is AliExpress Affiliate API;
- the actual number of completed provider pages;
- the candidate count exactly matches the captured list;
- the query, required features, budget, identity-review IDs and seller terms match the actual search.

Only after verifying those facts set source.capture_kind to real_api_response and source.page_count to the actual completed page count. Never infer or fabricate page count. Set source.candidate_count to the exact number of candidate rows.

## 2. Label every candidate

Every candidate ID must appear in exactly one of:

- gold.eligible_ids: the listing is genuinely suitable for the user's request and satisfies all mandatory constraints using its own title/description evidence;
- gold.rejected: the listing is not suitable. Each row must include an ID, a reason and a boolean critical.

Suggested rejection reasons:

- wrong_product_type
- wrong_product_identity
- accessory_or_part
- mandatory_feature_missing
- compatibility_mismatch
- budget_violation
- explicit_exclusion
- unsupported_evidence
- duplicate_or_near_duplicate
- low_relevance
- other

Set critical=true for a wrong primary product type, explicit incompatibility, missing mandatory feature, budget violation, explicit exclusion or unsupported evidence that would make a recommendation unsafe/misleading. Use critical=false for non-critical relevance or diversity weaknesses. Be consistent across all snapshots.

## 3. Ground all evidence

For every eligible ID:

- required_evidence[id] must contain one exact quote from the candidate's title or description for every item in request.required_features;
- if the ID appears in request.identity_review_ids, identity_evidence[id] must contain an exact quote proving the principal product identity;
- quotes must be present in the actual candidate fields; paraphrases and category-path guesses are not evidence.

The runner verifies these quotes against the frozen listing text before making any model call.

## 4. Score eligible products

Every eligible ID must have one gold.quality_scores[id] object. Score each dimension from 0 to 5:

| Field | Weight | What to judge |
|---|---:|---|
| query_match | 30 | How closely the actual product fulfils the user's intent |
| product_type | 20 | Whether the principal item is exactly the requested type |
| required_features | 15 | Whether every required feature is explicitly evidenced |
| compatibility | 15 | Compatibility and compliance with exclusions/budget |
| practical_appeal | 10 | Real usefulness/value, supported by listing quality signals |
| diversity_group | 10 | A concise family label used to measure variety across the selected set |

The five numeric scores must be integers from 0 to 5. A score of 0 means the candidate fails that criterion; if it fails a hard requirement, it should be rejected rather than marked eligible.

diversity_group is a consistent label such as compact_power_bank, high_capacity_power_bank, or solar_power_bank. Use the same label for near-identical variants so the evaluator does not mistake duplicates for variety.

## 5. Rank eligible products

gold.ranking_tiers is an ordered list of ID groups from best to worst. IDs in the same tier are considered equivalent in overall recommendation quality. Every eligible ID must appear exactly once; rejected IDs must never appear.

## 6. Metrics produced by the runner

The replay reports, separately:

- precision, recall and F1;
- weighted recommendation quality (30/20/15/15/10 for the five per-item criteria, plus 10 points for coverage of eligible diversity groups);
- weighted quality coverage (how much of the total gold quality was actually retrieved);
- critical false-positive count and reasons;
- ranking-pair accuracy;
- missing required/identity evidence;
- latency, token usage and estimated cost.

A high weighted quality score cannot hide low recall or critical errors. The model comparison must consider all metrics and the critical-error list together.


The selector replay is not a full AliExpress search. Its cost report therefore distinguishes the cost of any schema-accepted decision (including a valid empty/no-match decision) from the cost of a non-empty selection with no critical false positives and complete gold evidence. Do not call either metric full-search cost; use production turn telemetry for that.
