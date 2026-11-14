# Selector evaluation snapshot format

Status: schema/specification only. No product rows are fabricated or included.

The selector benchmark must use authentic AliExpress Affiliate API candidate pools captured from the exact production boundary immediately before `AIService.select_conversation_candidates`. The same immutable snapshot must be replayed to every model.

## Why this is separate

- `tests/eval_b5_results_live.json` contains aggregate pipeline counts and diagnostics, not product titles/descriptions.
- `tests/fixtures_stage44b4_capture_runner.py` captures `parse_query` and `parse_query_intent` text responses only; it does not capture selector candidates.
- The current live Telegram log records candidate counts and token totals, but not the complete candidate payload.
- Do not use the synthetic rows in `eval_b5_runner.py` as selector evidence. They are deliberately generated to test the old search pipeline.

## Required snapshot shape

One JSON object per selector case:

```json
{
  "dataset_version": "selector-snapshot-v1",
  "case_id": "S001",
  "captured_at_utc": "2026-11-06T00:00:00Z",
  "source": {
    "provider": "AliExpress Affiliate Product Query",
    "capture_kind": "real_api_response",
    "query_language": "ru",
    "query_redacted": "generic product request",
    "page_count": 3,
    "candidate_count": 70
  },
  "request": {
    "query": "exact selector query passed by production",
    "wanted": 20,
    "required_features": ["exact active mandatory feature"],
    "identity_review_ids": ["candidate-07"],
    "marketplace_terms": ["seller wording, if supplied"],
    "budget_constraint": "exact normalized budget or null"
  },
  "candidates": [
    {
      "id": "candidate-01",
      "title": "exact AliExpress title",
      "description": "exact description passed to the selector, or empty string",
      "category_path": ["exact", "category", "path"],
      "price": 12.34,
      "currency": "USD",
      "discount_percent": null,
      "rating": 4.8,
      "positive_rate": 98.5,
      "orders_count": 120,
      "has_image": true,
      "availability": "unknown"
    }
  ],
  "gold": {
    "reviewed_by": "human reviewer identifier stored outside the public fixture",
    "eligible_ids": ["candidate-01"],
    "rejected": [
      {
        "id": "candidate-02",
        "reason": "wrong_product_identity"
      }
    ],
    "required_evidence": {
      "candidate-01": {
        "exact active mandatory feature": "verbatim quote from title or description"
      }
    },
    "ranking_tiers": [
      ["candidate-01"],
      ["candidate-04", "candidate-05"]
    ]
  }
}
```

The example above documents the format only; it is not a real product snapshot and must not be added to the benchmark dataset.

## Capture requirements

1. Capture the exact selector payload after deterministic filtering and before the model call. Do not re-run AliExpress later and assume the result set is equivalent.
2. Preserve exact title and description text. Do not translate, paraphrase, shorten, or enrich them.
3. Preserve exact IDs within the snapshot so every model receives identical IDs. IDs can be anonymized consistently if the same mapping is applied to every replay and no source provenance is lost.
4. Do not include affiliate URLs, API credentials, access tokens, Telegram IDs, personal data, or image binaries. The selector prompt itself does not need product URLs.
5. Record the query language, currency, budget, mandatory features, identity-review IDs and seller phrase exactly as sent to the selector.
6. Gold labels must be reviewed independently of the model outputs. Mark eligibility and each hard requirement separately; do not use rating/orders as a substitute for relevance.
7. Include diverse product families, RU/UK/EN queries, cross-language listing titles, accessories vs host products, ambiguous identities, missing descriptions, budget/currency constraints, exclusions, and partial/contradictory evidence.
8. Preserve a minimum of several unrelated product families. No single product type may dominate the dataset.
9. Keep a separate capture manifest with source date and API version. Re-check source terms and permissions before collecting or storing any snapshot.

## Replay invariants

- Identical candidate IDs, fields, query, requirements, budget and review IDs for every provider.
- Identical deterministic pre-filtering and post-validation.
- Same candidate window and output limit (maximum 20 AI-selected; display limit remains 30).
- Same application evidence validator; no provider may bypass it.
- Record provider-native JSON mode, thinking setting, model ID/version, latency, token/cache usage, errors and cost.
- Score exact identity, mandatory feature evidence, compatibility, exclusions, budget/currency, precision, recall, safe abstention and duplicates.
- A model that selects fewer but safe candidates must be distinguished from a model that selects many irrelevant candidates. Report both precision and recall.

## Current blocker

No qualifying full selector candidate snapshots are present in the repository or the supplied live logs. The old B5 aggregate files and parser-only B4 capture records are not substitutes. Do not run or publish a selector quality comparison until authentic snapshots and independently reviewed gold labels exist.

## Opt-in raw capture from the live selector boundary

The production selector now has a privacy-bounded capture hook. It is **off by default** and writes nothing unless `SHORTS_CHILL_SELECTOR_CAPTURE=1` is explicitly set. Use it only in a controlled local test run with real AliExpress Affiliate API results, not on the production bot.

PowerShell:

```powershell
$env:SHORTS_CHILL_SELECTOR_CAPTURE = "1"
$env:SHORTS_CHILL_SELECTOR_CAPTURE_DIR = "reports/selector-captures"
python main.py
```

Every call to `AIService.select_conversation_candidates` writes one JSON file containing the exact query, wanted limit, requirements, identity-review IDs, seller terms, budget and the exact candidate fields passed to the selector. It does not capture affiliate URLs, image URLs, Telegram IDs, user IDs, API keys or signatures. The source query is marked redacted in the source metadata, while the exact selector query is retained in the request section because it is required for faithful replay. Use controlled test queries; do not enable this on ordinary user traffic.

The raw capture uses `capture_kind: "selector_input_capture"`, `gold: null` and `page_count: null` because the method boundary cannot independently prove the upstream source, provide exact page-count provenance or supply human labels. The language field is a script-based hint and must be reviewed. **Raw captures are not benchmark-ready.** Only change `capture_kind` to `real_api_response` after confirming the run used the live AliExpress Affiliate API. Before replay, manually:
1. confirm the source was a real live AliExpress API response;
2. annotate the exact page count if available from the retrieval report;
3. verify the language;
4. independently add the `gold` labels and evidence quotes;
5. remove any test cases that accidentally contain personal data.

Disable capture after the controlled run:

```powershell
Remove-Item Env:SHORTS_CHILL_SELECTOR_CAPTURE -ErrorAction SilentlyContinue
Remove-Item Env:SHORTS_CHILL_SELECTOR_CAPTURE_DIR -ErrorAction SilentlyContinue
```
