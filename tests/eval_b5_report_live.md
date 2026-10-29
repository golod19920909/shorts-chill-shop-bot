# B5: Search-Pipeline Evaluation Report

## 1. Summary

- Dataset version: `b5-v1`
- Dataset size: 35 cases
- Run date (UTC): 2026-10-21T07:09:02.817560+00:00
- Execution mode: `live` (live available: True)
- Scores: PASS=24, FAIL=11, INCONCLUSIVE=0 / 35
- Pass rate: 68.6%
- Latency p50/p95: 5904.788ms / 11306.0476ms (n=35)

## 2. Scope and Constraints

- Search semantics, relevance, routing, provider fallbacks, and UI were NOT changed.
- No B1-B4 tests were modified.
- Live vs mocked/cached results are clearly distinguished per case.
- No query text, PII, credentials, or API keys are stored in results.

## 3. Execution Environment

- Live execution attempted: True
- Modes observed: {'live': 35}

## 4. Dataset Composition

- 35 controlled queries
- Languages: Russian primary (32) + Ukrainian (2) + English (1)
- Intent categories: product / ambiguous / discovery / clarification
- Query shapes: short / long / numeric / budget / exclusion / vague / broad

## 5. Outcome Label Distribution (B4)

- EMPTY_AFTER_CONSTRAINTS: 2
- SUCCESS_EMPTY_RAW: 4
- SUCCESS_WITH_RESULTS: 29

Stage totals (sum over cases):
  - raw: 4280
  - after_relevance: 2260
  - after_constraints: 1772
  - final: 731

## 6. Per-Case Results

| id | category | language | mode | hint | outcome | score | latency_ms | blocked | exception |
|---|---|---|---|---|---|---|---|---|---|
| B5-01 | short | ru | live | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 5677.715 | False |  |
| B5-02 | short | ru | live | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 5250.035 | False |  |
| B5-03 | short | ru | live | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 3913.777 | False |  |
| B5-04 | short | ru | live | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 3756.088 | False |  |
| B5-05 | short | ru | live | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 4972.349 | False |  |
| B5-06 | long | ru | live | SUCCESS_WITH_RESULTS | EMPTY_AFTER_CONSTRAINTS | FAIL | 4332.528 | False |  |
| B5-07 | long | ru | live | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 11160.502 | False |  |
| B5-08 | long | ru | live | SUCCESS_WITH_RESULTS | EMPTY_AFTER_CONSTRAINTS | FAIL | 7282.379 | False |  |
| B5-09 | long | ru | live | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 9447.417 | False |  |
| B5-10 | long | ru | live | EMPTY_AFTER_RELEVANCE | SUCCESS_EMPTY_RAW | FAIL | 2478.942 | False |  |
| B5-11 | numeric | ru | live | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 10559.083 | False |  |
| B5-12 | numeric | ru | live | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 4860.233 | False |  |
| B5-13 | numeric | ru | live | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 10190.21 | False |  |
| B5-14 | numeric | ru | live | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 9956.316 | False |  |
| B5-15 | numeric | ru | live | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 10796.449 | False |  |
| B5-16 | ambiguous | ru | live | SUCCESS_EMPTY_RAW | SUCCESS_WITH_RESULTS | FAIL | 6311.695 | False |  |
| B5-17 | ambiguous | ru | live | SUCCESS_EMPTY_RAW | SUCCESS_EMPTY_RAW | PASS | 1800.732 | False |  |
| B5-18 | clarification | ru | live | SUCCESS_EMPTY_RAW | SUCCESS_WITH_RESULTS | FAIL | 8106.737 | False |  |
| B5-19 | clarification | ru | live | SUCCESS_EMPTY_RAW | SUCCESS_WITH_RESULTS | FAIL | 4721.399 | False |  |
| B5-20 | clarification | ru | live | SUCCESS_EMPTY_RAW | SUCCESS_WITH_RESULTS | FAIL | 5904.788 | False |  |
| B5-21 | language | ru | live | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 9124.537 | False |  |
| B5-22 | language | en | live | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 4451.313 | False |  |
| B5-23 | language | uk | live | SUCCESS_WITH_RESULTS | SUCCESS_EMPTY_RAW | FAIL | 2102.938 | False |  |
| B5-24 | language | uk | live | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 7110.282 | False |  |
| B5-25 | language | ru | live | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 12353.913 | False |  |
| B5-26 | electronics | ru | live | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 7847.79 | False |  |
| B5-27 | automotive | ru | live | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 7816.407 | False |  |
| B5-28 | tools | ru | live | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 7987.303 | False |  |
| B5-29 | pet | ru | live | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 7346.108 | False |  |
| B5-30 | beauty | ru | live | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 5520.002 | False |  |
| B5-31 | budget | ru | live | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 5190.705 | False |  |
| B5-32 | budget | ru | live | EMPTY_AFTER_CONSTRAINTS | SUCCESS_WITH_RESULTS | FAIL | 4728.395 | False |  |
| B5-33 | exclusion | ru | live | SUCCESS_WITH_RESULTS | SUCCESS_EMPTY_RAW | FAIL | 1384.634 | False |  |
| B5-34 | vague | ru | live | SUCCESS_EMPTY_RAW | SUCCESS_WITH_RESULTS | FAIL | 5398.549 | False |  |
| B5-35 | broad | ru | live | SUCCESS_WITH_RESULTS | SUCCESS_WITH_RESULTS | PASS | 11645.654 | False |  |

## 7. Scoring Criteria (numerators/denominators)

- PASS: pipeline completed, no total provider failure, outcome stage
  consistent with expected_outcome_hint.
- FAIL: product-hint case with total failure or zero final results;
  empty-hint case that returned invented products.
- INCONCLUSIVE: execution blocked (no credentials/network) or exception.
- Numerator/denominator: 35 conclusive / 35 total;
  PASS=24/35 conclusive, FAIL=11/35 conclusive.


## 8. Latency and Performance

- p50: 5904.788ms, p95: 11306.0476ms
- min: 1384.634ms, max: 12353.913ms, mean: 6613.94ms
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

- 1 case(s) [empty_expected_got_results]: B5-10
- 4 case(s) [results_expected_got_empty]: B5-06, B5-08, B5-23, B5-33
- 6 case(s) [stage_mismatch]: B5-16, B5-18, B5-19, B5-20, B5-32, B5-34
- p95 latency 11306.0476ms exceeds 5s — consider provider timeouts.

### 13.1 Failing cases in detail

| id | hint | outcome | final_count | kind | query |
|---|---|---|---|---|---|
| B5-06 | SUCCESS_WITH_RESULTS | EMPTY_AFTER_CONSTRAINTS | 0 | results_expected_got_empty | беспроводная мышь для ноутбука тихая с хорошей батареей |
| B5-08 | SUCCESS_WITH_RESULTS | EMPTY_AFTER_CONSTRAINTS | 0 | results_expected_got_empty | робот-пылесос с fonctions влажной уборки |
| B5-10 | EMPTY_AFTER_RELEVANCE | SUCCESS_EMPTY_RAW | 0 | empty_expected_got_results | ищу наушники без проводов и без шумоподавления |
| B5-16 | SUCCESS_EMPTY_RAW | SUCCESS_WITH_RESULTS | 30 | stage_mismatch | хороший подарок |
| B5-18 | SUCCESS_EMPTY_RAW | SUCCESS_WITH_RESULTS | 30 | stage_mismatch | устройство |
| B5-19 | SUCCESS_EMPTY_RAW | SUCCESS_WITH_RESULTS | 30 | stage_mismatch | лучшее |
| B5-20 | SUCCESS_EMPTY_RAW | SUCCESS_WITH_RESULTS | 7 | stage_mismatch | оно |
| B5-23 | SUCCESS_WITH_RESULTS | SUCCESS_EMPTY_RAW | 0 | results_expected_got_empty | безпроводні навушники |
| B5-32 | EMPTY_AFTER_CONSTRAINTS | SUCCESS_WITH_RESULTS | 23 | stage_mismatch | настольная лампа до 500 грн |
| B5-33 | SUCCESS_WITH_RESULTS | SUCCESS_EMPTY_RAW | 0 | results_expected_got_empty | микроволновка без гриля и без функции разморозки |
| B5-34 | SUCCESS_EMPTY_RAW | SUCCESS_WITH_RESULTS | 30 | stage_mismatch | стильная одежда |

## 14. Machine-Readable Results

- See `eval_b5_results_live.json` for full per-case JSON results.
- Fields: id, category, language, intent_category, query, expected_outcome_hint,
  execution_mode, live_available, blocked, exception, outcome_label, stage_counts,
  searched/failed/unavailable_marketplaces, final_count, latency_ms, score, notes,
  diagnostics_snapshot (B4 aggregate, no query text).
