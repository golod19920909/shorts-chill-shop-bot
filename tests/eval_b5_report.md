# B5: Search-Pipeline Evaluation Report

## 1. Summary

- Dataset version: `b5-v1`
- Dataset size: 35 cases
- Run date (UTC): 2026-10-21T07:05:00.947987+00:00
- Execution mode: `mock` (live available: False)
- Scores: PASS=35, FAIL=0, INCONCLUSIVE=0 / 35
- Pass rate: 100.0%
- Latency p50/p95: 17.14ms / 53.534699999999894ms (n=35)

## 2. Scope and Constraints

- Search semantics, relevance, routing, provider fallbacks, and UI were NOT changed.
- No B1-B4 tests were modified.
- Live vs mocked/cached results are clearly distinguished per case.
- No query text, PII, credentials, or API keys are stored in results.

## 3. Execution Environment

- Live execution attempted: False
- Modes observed: {'mock': 35}

## 4. Dataset Composition

- 35 controlled queries
- Languages: Russian primary (32) + Ukrainian (2) + English (1)
- Intent categories: product / ambiguous / discovery / clarification
- Query shapes: short / long / numeric / budget / exclusion / vague / broad

## 5. Outcome Label Distribution (B4)

- EMPTY_AFTER_CONSTRAINTS: 1
- EMPTY_AFTER_RELEVANCE: 1
- SUCCESS_EMPTY_RAW: 6
- SUCCESS_WITH_RESULTS: 27

Stage totals (sum over cases):
  - raw: 145
  - after_relevance: 140
  - after_constraints: 132
  - final: 132

## 6. Per-Case Results

| id | category | language | mode | hint | outcome | score | latency_ms | blocked | exception |
|---|---|---|---|---|---|---|---|---|---|
| B5-01 | short | ru | mock | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 78.797 | False |  |
| B5-02 | short | ru | mock | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 17.14 | False |  |
| B5-03 | short | ru | mock | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 11.308 | False |  |
| B5-04 | short | ru | mock | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 14.213 | False |  |
| B5-05 | short | ru | mock | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 26.656 | False |  |
| B5-06 | long | ru | mock | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 89.352 | False |  |
| B5-07 | long | ru | mock | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 41.132 | False |  |
| B5-08 | long | ru | mock | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 26.665 | False |  |
| B5-09 | long | ru | mock | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 37.783 | False |  |
| B5-10 | long | ru | mock | EMPTY_AFTER_RELEVANCE | EMPTY_AFTER_RELEVANCE | PASS | 30.261 | False |  |
| B5-11 | numeric | ru | mock | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 24.322 | False |  |
| B5-12 | numeric | ru | mock | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 42.708 | False |  |
| B5-13 | numeric | ru | mock | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 24.076 | False |  |
| B5-14 | numeric | ru | mock | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 23.019 | False |  |
| B5-15 | numeric | ru | mock | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 13.82 | False |  |
| B5-16 | ambiguous | ru | mock | SUCCESS_EMPTY_RAW | SUCCESS_EMPTY_RAW | PASS | 1.05 | False |  |
| B5-17 | ambiguous | ru | mock | SUCCESS_EMPTY_RAW | SUCCESS_EMPTY_RAW | PASS | 1.151 | False |  |
| B5-18 | clarification | ru | mock | SUCCESS_EMPTY_RAW | SUCCESS_EMPTY_RAW | PASS | 0.995 | False |  |
| B5-19 | clarification | ru | mock | SUCCESS_EMPTY_RAW | SUCCESS_EMPTY_RAW | PASS | 0.98 | False |  |
| B5-20 | clarification | ru | mock | SUCCESS_EMPTY_RAW | SUCCESS_EMPTY_RAW | PASS | 0.941 | False |  |
| B5-21 | language | ru | mock | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 13.398 | False |  |
| B5-22 | language | en | mock | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 9.953 | False |  |
| B5-23 | language | uk | mock | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 9.368 | False |  |
| B5-24 | language | uk | mock | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 18.095 | False |  |
| B5-25 | language | ru | mock | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 11.058 | False |  |
| B5-26 | electronics | ru | mock | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 22.791 | False |  |
| B5-27 | automotive | ru | mock | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 22.099 | False |  |
| B5-28 | tools | ru | mock | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 17.674 | False |  |
| B5-29 | pet | ru | mock | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 29.908 | False |  |
| B5-30 | beauty | ru | mock | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 13.196 | False |  |
| B5-31 | budget | ru | mock | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 9.989 | False |  |
| B5-32 | budget | ru | mock | EMPTY_AFTER_CONSTRAINTS | EMPTY_AFTER_CONSTRAINTS | PASS | 5.27 | False |  |
| B5-33 | exclusion | ru | mock | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 28.905 | False |  |
| B5-34 | vague | ru | mock | SUCCESS_EMPTY_RAW | SUCCESS_EMPTY_RAW | PASS | 1.349 | False |  |
| B5-35 | broad | ru | mock | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 11.946 | False |  |

## 7. Scoring Criteria (numerators/denominators)

- PASS: pipeline completed, no total provider failure, outcome stage
  consistent with expected_outcome_hint.
- FAIL: product-hint case with total failure or zero final results;
  empty-hint case that returned invented products.
- INCONCLUSIVE: execution blocked (no credentials/network) or exception.
- Numerator/denominator: 35 conclusive / 35 total;
  PASS=35/35 conclusive, FAIL=0/35 conclusive.


## 8. Latency and Performance

- p50: 17.14ms, p95: 53.534699999999894ms
- min: 0.941ms, max: 89.352ms, mean: 20.896ms
- sample size: 35

## 9. Provider Reliability (B4 diagnostics)

Per-provider stats are aggregated from B4 diagnostics snapshots (counts and latencies only; no secrets).

## 10. Privacy and Safety

- No query text, product titles, user ids, or API keys stored.
- All latency samples bounded (B4 MAX_LATENCY_SAMPLES).
- Diagnostics fail-safe: a diagnostics bug never breaks a search.

## 11. Regression Check (B4 baseline)

- B4 baseline: 2093 passed, 712 subtests, 0 failed.
- This runner is standalone (not collected by pytest); the B4 suite
  must be run separately with `python -m pytest tests/ -q`.

## 12. Blocked Cases

- None.

## 13. Evidence-Backed Issue List for B6

- Live execution unavailable — results are mock-only; quality not measured against real providers.

## 14. Machine-Readable Results

- See `eval_b5_results.json` for full per-case JSON results.
- Fields: id, category, language, intent_category, query, expected_outcome_hint,
  execution_mode, live_available, blocked, exception, outcome_label, stage_counts,
  searched/failed/unavailable_marketplaces, final_count, latency_ms, score, notes,
  diagnostics_snapshot (B4 aggregate, no query text).
