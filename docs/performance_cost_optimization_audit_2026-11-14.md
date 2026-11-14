# Shorts Chill Shop Bot — performance and cost optimization audit

Audit date: 2026-11-14  
Working branch: `optimize/performance-costs`  
Base: `fix/thematic-collection-retrieval`  
Production branch and deployment: untouched.

## Executive status

The first safe optimizations and measurement improvements are implemented in the isolated branch. No production model routing, search ranking, candidate limits, product validation, AliExpress route count, catalog navigation, «Умный поиск» or «Находки» behavior was changed.

This is an incremental implementation, not a completed acceptance report. GitHub Actions is being used for offline verification. No live Gemini/AliExpress calls were made from this environment.

## 1. Baseline evidence available before this branch

The repository contains historical live evidence, but not a fresh controlled baseline for every current path:

- 2026-11-06 Gemini sample:
  - conversation-turn on Gemini 3.8 Flash: 3.738 s, 1,891 input / 47 candidate / 132 thinking tokens;
  - selector on 3.8 Flash: 6.509 s and timed out at its 6.5 s limit;
  - selector fallback on 3.1 Flash-Lite: 4.248 s, 12,367 input / 1,391 candidate / 113 thinking tokens;
  - one short clarification helper: 3.692 s; combined route duration 7.249 s;
  - a later no-eligible route lasted 19.911 s, but its full duration cannot be attributed to Gemini alone.
- 2026-11-13 thematic retrieval sample: 58.9 s total, 18 first-page routes plus 3 second-page routes and one Gemini selector (~3.4 s). Serial AliExpress retrieval was identified as the dominant delay. The base branch already included bounded thematic route prefetch and a global two-call AliExpress semaphore.
- The samples above are historical observations, not a current p50/p95 benchmark. They must not be presented as current averages or guarantees.

The app already has durable Gemini usage analytics (per-attempt model, operation, duration, real usage metadata and estimated cost) and bounded search/provider latency samples. The AliExpress admin screen previously showed success/error/retry/empty counters but not API p50/p95.

## 2. Changes implemented

### 2.1 Duplicate in-flight AliExpress API calls

`services/marketplaces/aliexpress_client.py` now coalesces identical simultaneous requests for the documented read-only methods:
- Affiliate product query;
- product detail lookup;
- hot product query;
- category lookup.

The coalescing key contains the exact method and canonicalized business parameters. A completed call is immediately removed; a later request performs a fresh API call. There is deliberately no TTL result cache in this first change, so price/stock freshness is not weakened. Affiliate link generation and other non-read operations are never coalesced.

Each waiter receives a deep copy. One cancelled Telegram handler cannot cancel the shared request while another waiter still needs it. The existing global semaphore ceiling remains two.

New counters distinguish handler call attempts from requests actually merged. A bounded 500-sample latency window reports average, p50 and p95 API-stage time, including time waiting for the shared semaphore.

### 2.2 Duplicate identical user text while a search is running

`handlers/search.py` now fingerprints normalized text per Telegram user's existing `context.user_data`. If the same user sends the same normalized text while the first copy is still running, the duplicate is acknowledged and skipped before Gemini or AliExpress work begins. Different queries from the same user are not blocked. The fingerprint is removed in `finally`, so the same query can be deliberately run again after completion or failure.

The thematic «show more» handler already had its own per-session busy guard; it remains unchanged.

### 2.3 Eliminate duplicate clarification interpretation

When the first `conversation_turn` call returned `clarify`, the engine could make a second Gemini call through `decide_chat_action` merely to phrase the same question. The engine now reuses the validated `question` field from the first response. If it is missing or invalid, it uses the deterministic default. This removes a sequential Gemini call on that clarification path without weakening the first context-aware interpretation.

### 2.3 Operational diagnostics

The existing admin AliExpress API screen now shows:
- successful and failed API calls;
- requests received from handlers;
- identical simultaneous requests coalesced;
- transient retries and empty results;
- average / p50 / p95 API-stage latency.

No query text, user ID, product payload, signature or credential is stored in these counters.

### 2.4 Gemini 3.5 Flash-Lite experiment preparation

Production defaults remain unchanged:
- primary: `gemini-3.1-flash-lite`;
- fallback: `gemini-3.8-flash`.

Added the official 3.5 Flash-Lite model ID to the cost estimator and dated benchmark rate file. Added 61 unique product-intent cases across RU/UK/EN. Together with the existing dialogue corpus and auxiliary chat-action/no-results cases, the blind runner now covers 100 unique scenarios. The default comparison uses three repetitions and the 5.5 s production primary attempt cap.

The runner accepts both model IDs through environment variables; it reads no `.env` file and the model identities remain in a separate private map. The first pass compares one attempt per model without fallback, so quality and latency are not masked by fallback behavior. A separate resilience pass is still required.

## 3. Current Standard paid-tier rates (USD per 1M tokens)

| Model | Input | Output incl. thinking | Cached input |
|---|---:|---:|---:|
| Gemini 3.1 Flash-Lite | 0.25 | 1.50 | 0.025 |
| Gemini 3.5 Flash-Lite | 0.30 | 2.50 | 0.03 |

Gemini 3.5 Flash-Lite is 20% more expensive on input, 66.7% on output and 20% on cached input. Total request cost must be decided from the actual input/output/thinking token distribution and cache-hit ratio, not the tariff alone. The cache storage rate listed by Google is $1 per 1M cached tokens per hour.

Sources:
- https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite
- https://ai.google.dev/gemini-api/docs/pricing
- https://deepmind.google/models/model-cards/gemini-3-5-flash-lite/

## 4. Decisions and deferred work

### Caching

- Implemented now: in-flight single-flight coalescing for identical simultaneous read-only API calls.
- Not implemented yet: time-based cross-request cache for product query results, product details, popular queries or thematic shelves.
- Reason: current listing price/stock data is volatile, and complete thematic results are affected by user-specific shown history, exclusions, budget, currency and other constraints. A shared shelf/result cache must store only a safe pre-personalization pool and re-run all per-user hard filters; volatile price/stock must be refreshed before delivery. No TTL or freshness contract has yet been proven safe.
- Existing per-user shown-result/session caches continue to support follow-up questions and browsing; they are not a global product cache.

### Gemini Context Caching

No explicit cache object was created. The technical capability exists: both 3.1 Flash-Lite and 3.5 Flash-Lite model pages list caching as supported, and the app uses `generateContent`, which is the API path required for explicit cache objects. However:

- Google's implicit-caching guide says caching is enabled by default for Gemini 2.5+ but its published per-model minimum-token table does not list either Flash-Lite model. It therefore does not provide a reliable minimum threshold for this exact comparison.
- Google says implicit cache hits are not guaranteed; only the actual `cached_content_token_count` returned for each request proves that a hit occurred.
- Explicit caching bills cache creation input at the standard input rate plus storage for token-count × TTL; subsequent reads use the lower cached-input rate.
- The current app builds each operation as a complete prompt string. Using an explicit cache would require separating a stable system prefix from per-user context and message content. That is a prompt-structure change and must be regression-tested.

Illustrative break-even only: for a 3,000-token stable prefix held for one hour, assuming cache creation at the standard input rate and storage at $1 / 1M token-hours:
- 3.1 Flash-Lite: creation ~$0.00075 + storage ~$0.003; each subsequent cache read saves ~$0.000675; about **6 reads within the hour** are needed to break even.
- 3.5 Flash-Lite: creation ~$0.00090 + storage ~$0.003; each subsequent cache read saves ~$0.00081; about **5 reads within the hour** are needed to break even.

This excludes non-cached prompt tail, output tokens, cache misses and cache invalidation/recreation. The calculation is not evidence that the bot currently reaches those hit rates. The next step is to inspect actual persisted `cached_content_token_count` distributions and exact stable-prefix token counts, then run a small TTL experiment. Do not enable explicit caching before that measurement.

Official references:
- https://ai.google.dev/gemini-api/docs/caching
- https://ai.google.dev/gemini-api/docs/generate-content/caching
- https://ai.google.dev/gemini-api/docs/pricing
- https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite
- https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite

### Selector quality benchmark

The 100-scenario corpus measures conversation interpretation and auxiliary chat-action/no-results operations. It does **not** prove candidate-selection parity. An opt-in capture hook is implemented at the exact `AIService.select_conversation_candidates` boundary. It is disabled by default and, when explicitly enabled during a controlled local live-AliExpress test, writes the exact selector query and candidate payload without affiliate URLs, image URLs, Telegram/user IDs or credentials. Raw captures are marked `selector_input_capture`, have `gold: null` and unknown `page_count`; they are not benchmark-ready until source provenance is checked and independent human gold labels are added. A blind replay runner is now implemented: it rejects unreviewed captures, reconstructs Products and verifies the exact `_rank_payload`, then sends identical frozen pools to both models and records precision/recall/F1, false positives/negatives, ranking tiers, latency, tokens/cache and cost. It performs no AliExpress calls and does not write evaluation telemetry to the bot database. Instructions are in `tests/eval_selector_capture_schema.md`. Synthetic product rows remain prohibited.

### Live benchmark and VPS staging

No paid Gemini benchmark, AliExpress live test, production bot test or VPS staging deployment was performed from this environment. The local PowerShell command is documented in `tests/eval_consultant_benchmark_protocol.md`. Production/staging separation is a later VPS task and requires separate bot token, database and settings.

## 5. Verification status

Offline regression coverage added or extended:
- `tests/test_aliexpress_retry.py`: single-flight, deep-copy isolation, cancellation safety, fresh later requests, two-call concurrency ceiling and latency summary;
- `tests/test_search_dedup.py`: duplicate suppression, different-query concurrency and cleanup after exceptions;
- `tests/test_gemini_pricing.py`: 3.5 standard and cached-input rate calculations;
- `tests/test_eval_consultant_optimization_dataset.py`: 100-scenario corpus structure;
- existing Gemini, conversation-engine, search-service and benchmark-runner regression suites are included in the new workflow.

The first CI attempt found a syntax error in the admin screen; it was fixed. The first unfiltered regression run then reported 242 passed and five unrelated stale legacy failures (two tests import the removed `services.agent_loop`, one expects a single Gemini attempt despite the configured fallback, one expects an older type-gate reason, and one expects all thematic routes despite the current early-stop planner). The focused workflow explicitly deselects those five known failures rather than hiding them in a full-suite claim. Latest workflow run **#72** succeeded: compileall passed, both benchmark CLI entrypoints passed, and the focused regression suite reported **253 passed, 5 deselected, 54 subtests passed**. The five deselections remain known legacy failures; this is not a full-suite pass. No local pytest execution was performed in this environment.

## 6. Files changed in this workstream

- `services/marketplaces/aliexpress_client.py`
- `handlers/admin.py`
- `handlers/search.py`
- `services/convsearch/engine.py`
- `services/ai_service.py`
- `tests/test_selector_snapshot_capture.py`
- `tests/eval_selector_replay_runner.py`
- `tests/test_selector_replay_runner.py`
- `tests/eval_selector_capture_schema.md`
- `.gitignore`
- `.github/workflows/optimization-regression.yml`
- `tests/test_aliexpress_retry.py`
- `tests/test_search_dedup.py`
- `tests/test_convsearch_clarification_reuse.py`
- `services/gemini_pricing.py`
- `tests/test_gemini_pricing.py`
- `tests/eval_gemini_rates_2026-11-14.json`
- `tests/eval_consultant_optimization_dataset.py`
- `tests/test_eval_consultant_optimization_dataset.py`
- `tests/eval_consultant_benchmark_runner.py`
- `tests/eval_consultant_benchmark_protocol.md`

## 7. Gemini 3.1 vs 3.5 blind benchmark — user-run results (2026-11-14)

The user ran the documented Gemini-only benchmark locally with the same API key, frozen prompts/contexts, three repetitions, a 5.5-second conversation-turn cap, no application retries and no fallback. The run made **624 direct Gemini `generateContent` calls**: 312 per model. It did not call AliExpress, Telegram or the production database. The JSON result and private alias map remain local to the user's machine and were not uploaded to this repository; the figures below are recorded from the user's supplied run report.

The request-count arithmetic is consistent with the committed runner: 13 dialogue scenarios contain 17 conversation turns; plus 61 additional product-intent turns gives 78 conversation-turn tasks per repetition, or 234 per model over three repetitions. The auxiliary sets add 14 × 3 = 42 `chat_action` calls and 12 × 3 = 36 `no_results_reply` calls. Total: 234 + 42 + 36 = **312 per model**. Thus 100 unique scenarios does not mean 100 API requests; multi-turn dialogue scenarios expand into individual turn requests.

| Metric | Gemini 3.5 Flash-Lite | Gemini 3.1 Flash-Lite |
|---|---:|---:|
| Accepted by application validator | 284/312 (91.0%) | 258/312 (82.7%) |
| `conversation_turn` acceptance | 88.0% | 78.6% |
| `no_results_reply` acceptance | 88.9% | 100% |
| Russian acceptance | 95.2% | 81.0% |
| Accepted-request latency p50 / p95 | ~1,007 / ~1,718 ms | ~1,139 / ~1,951 ms |
| Best-effort estimated total cost | ~$0.1863 | ~$0.1403 |
| Best-effort estimated cost per accepted request | ~$0.0007 | ~$0.0005 |

The private alias map identifies A as 3.5 and B as 3.1. The raw result's `estimated_cost_usd` field was `null` for every request because complete billable usage (including reasoning/thinking and cached usage) was unavailable. The cost figures above are therefore a best-effort recomputation from input and candidate-output tokens only, not authoritative billed costs; on that basis 3.5 was approximately 33% more expensive for this corpus. `chat_action` is diagnostic-only and is no longer part of the active Option C clarification path. `no_results_reply` also did not favor 3.5. Aggregate acceptance is application-validator acceptance, not independently human-scored semantic correctness.

**Decision:** do not change production model routing from this benchmark alone. 3.5 showed higher aggregate/application acceptance and modestly lower latency, especially on the Russian subset, but at a higher estimated cost; the test does not establish selector quality parity, exact semantic correctness on authentic products, or end-to-end production cost per successful search. Keep 3.1 as the production primary and existing fallback unchanged until the authentic selector replay and live telemetry gates are satisfied. A narrow 3.5 experiment may be reconsidered after the resilience pass and better usage-cost evidence.

## 8. Acceptance gates still open

1. **Completed:** latest offline CI run #72 succeeded (253 passed, 5 deselected, 54 subtests passed; see the explicit legacy-failure caveat above).
2. **Completed:** the 100-unique-scenario blind Gemini 3.1 vs 3.5 comparison was run by the user; results and limitations are recorded above. Raw JSON/private-map artifacts remain local.
3. Capture and independently label authentic selector snapshots; then run the same candidate pools through both models.
4. Use real production telemetry to compare Gemini costs per successful search and AliExpress API p50/p95 before/after.
5. Evaluate safe short-lived product/query/shelf caching only after a price/stock freshness and personalization design is testable.
6. After VPS migration, add isolated staging with a separate bot token/database/config.

No production model change, merge to `main`, PR merge or deployment has been performed.
