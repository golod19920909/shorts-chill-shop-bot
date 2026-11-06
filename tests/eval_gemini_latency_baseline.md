# Gemini latency baseline — limited 2026-11-06 live evidence

This document records only timings visible in the user-supplied Telegram log and configured source-level bounds. It is not a p50/p95 benchmark.

## Observed successful attempts

| Path | Attempt | Observed duration | Notes |
|---|---|---:|---|
| Core conversation interpretation | `conversation_turn`, 3.8 Flash | 3.738s | Successful. 1,891 input, 47 candidate, 132 thought tokens. |
| Candidate selection | 3.8 Flash primary | 6.509s | Timed out at the 6.5s attempt bound; provider-side usage unknown. |
| Candidate selection fallback | 3.1 Flash-Lite | 4.248s | Successful. 12,367 input, 1,391 candidate, 113 thought tokens. |
| Short clarification helper | generic `generate` on `engine-clarify` route | 3.692s | Preceded by a successful 3.313s conversation turn; combined route duration 7.249s. |
| Later follow-up interpretation | `conversation_turn` | timeout | Deterministic intent fallback used. |
| Later no-eligible path | engine route | 19.911s | Included interpretation timeout, search, selector with almost-empty output and no-results handling timeout; exact per-stage total is not fully attributable. |

## Configured source-level bounds

| Operation | Per-attempt bound | Outer bound |
|---|---:|---:|
| `conversation_turn` | 4.0s | 8.5s |
| `conversation_selection` | 6.5s | 15s |
| `no_results_reply` | not independently confirmed in source beyond helper call; bounded at 3s | 3s |
| Other generic `generate` helpers | AIService default 20s unless caller wraps a tighter deadline | caller-dependent |

A primary timeout followed by fallback is two distinct billable possibilities. The first attempt's cost remains unknown unless provider usage metadata is available. The benchmark runner's first pass intentionally disables retries/fallbacks to compare single-attempt quality and latency; a separate resilience pass must measure the production chain.

## What cannot be concluded

- The supplied sample is too small and scenario-skewed to calculate p50/p95.
- Search-provider/API time is mixed into end-to-end route durations; do not attribute the full 19.9s to Gemini.
- The selector timeout may have incurred provider-side processing/billing even though the client received no usage metadata.
- A short 4s deadline can disadvantage slower models; this is a real bot constraint, but compare both quality at a fixed 4s attempt and quality under the production fallback budget.
- The Telegram rate-limiter warning is a separate dependency issue, not evidence about Gemini model latency.

## Next measurement

Use the blind runner for one-attempt per-model latency at the same 4s primary-attempt bound. Then run a separate resilience replay using the actual 8.5s/15s outer budgets and the production fallback policy. Record provider latency and end-to-end operation latency separately. Do not claim a speed improvement from a single fast response.
