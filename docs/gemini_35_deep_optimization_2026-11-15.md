# Gemini 3.5 Flash-Lite deep tuning — audit and implementation log

**Date:** 2026-11-15  
**Repository:** `golod19920909/shorts-chill-shop-bot`  
**Feature branch:** `fix/gemini-35-flash-lite-tuning`  
**Base:** `optimize/performance-costs`  
**Status:** First implementation pass committed; scoped CI and live selector-quality gate pending. No merge or deployment.

## 1. Scope and protected behavior

The work preserves Option C: Gemini interprets dialogue and performs bounded semantic candidate review; Python remains authoritative for retrieval, product identity, hard requirements, compatibility, exclusions, budget, deduplication, ranking, and Telegram delivery.

No search routes, marketplace page depth, candidate-pool limits, candidate payload fields, product-type validation, evidence rules, or protected user-facing sections were changed. AliExpress Affiliate API remains the only marketplace source. No main-branch merge or deployment was performed.

## 2. Audit findings

### 2.1 Live selector trace

The 2026-11-15 gaming-laptop trace showed:

- 498 raw rows retrieved across the existing ten route/page requests;
- 388 unique candidates after fusion;
- 208 candidates remaining after the existing type stage;
- 70 candidates in the bounded Gemini selector window;
- 67 of those 70 marked for semantic identity review;
- one grounded ID accepted by the first selector pass;
- zero additional IDs accepted by the continuation pass;
- one final card.

The line `returned=0 validated=1` was not an accurate description of the two-pass result. `selection_returned_count` was only incremented for the second pass and omitted the first pass. The selector had accepted one ID in the first pass; the second pass added none. The corrected counters now expose first-pass, second-pass, aggregate returned IDs, and engine-validated IDs separately.

The trace alone does not prove that the other 69 candidates were suitable or that the model made a mistake. The model's raw evidence and independently reviewed candidate labels are needed before changing selector semantics or deterministic validation.

### 2.2 Automatic Function Calling warning

The Option C service calls stateless `client.aio.models.generate_content` and does not pass Gemini tools. The warning is consistent with a known google-genai SDK behavior: AFC defaults to enabled even when no tools are supplied. This is not evidence of an application-level agent loop.

Direct generation now explicitly sets `automatic_function_calling.disable=true`. No migration to `AsyncChat.send_message` is justified because the project has no current bounded tool-calling workflow.

### 2.3 Prompt caching

Gemini implicit context caching is enabled automatically for supported models. The selector prompt previously placed the changing user query and IDs before the common selection instructions. The stable selection/evidence rules now lead the prompt; dynamic requirements, query, budget, IDs, seller terms and candidate rows follow them.

This is a cache-eligibility optimization, not a claim that cache hits have already occurred. Attempt logs now expose `cached_content_token_count`, and the existing telemetry/pricing layer already accounts for cached input at its configured rate. Explicit cache objects are not enabled until measured cache-hit frequency and storage/read pricing demonstrate break-even.

### 2.4 Structured output and generation parameters

Conversation interpretation and candidate selection already use JSON response schemas. Their responses now have bounded output budgets:

| Operation | Output cap |
|---|---:|
| `conversation_turn` | 512 tokens |
| `conversation_selection` | 4096 tokens |
| `chat_action` | 128 tokens |
| `no_results_reply` | 256 tokens |

The limits are deliberately conservative but large enough for the documented response contracts; regression tests cover the SDK request config. The active Option C structured operations now include turn interpretation, candidate selection, short chat-action classification, product advice, paired comparison evidence and product-question evidence. Legacy parser/feed/catalog selectors remain unchanged under the protected-path scope. No `temperature`, `top_p`, or `top_k` overrides are added. Google's Gemini 3.5 guidance recommends controlling reasoning through `thinking_level` and avoiding legacy sampling overrides unless a measured need exists.

## 3. Thinking policy and model decision

- `conversation_turn`: explicit `minimal` thinking for the bounded structured dialogue decision.
- `conversation_selection`: configurable `minimal|low|medium|high`; current default remains `low`.
- The selector replay runner supports a controlled `minimal,low,medium` comparison on identical reviewed snapshots.
- `high` is not selected without evidence that its quality improvement justifies extra latency and output cost.

The blind dialogue A/B result favored Gemini 3.5 on acceptance and latency for the measured language operations. It did **not** evaluate product-selection relevance, compatibility, hard-constraint compliance, or final shopping-session quality. Therefore the 3.5 promotion decision remains provisional; selector quality must be evaluated separately against Gemini 3.1.

## 4. Implementation completed in this pass

- Corrected selector first-/second-pass return counters.
- Added privacy-safe aggregate selector diagnostics: raw response item count, evidence-grounded IDs, and rejected ungrounded decisions. No prompt, product title, user text, or product ID is logged.
- Reordered the selector prompt to put stable instructions before dynamic context for implicit prefix caching.
- Explicitly disabled SDK AFC on direct stateless generation requests.
- Added output-token caps for bounded structured and short-text operations.
- Added cached-input token count to per-attempt logs.
- The selector's dynamic JSON schema caps `selections` at the requested limit, disallows unexpected root/item/evidence properties, and describes the evidence fields. Python still verifies every returned ID and exact quote.
- Updated offline tests for request configuration, stable prompt ordering, strict selector schema shape/maximum item count, selector counter attribution, cached-token persistence, and benchmark/production output-cap parity.
- Updated `AGENTS.md` and `PROJECT_CONTEXT.md`.
- GitHub Actions optimization regression workflow #129 passed on code head `5567514b69ff86895e78dd672fc1a0a7153ea4a1`: **297 passed, 60 subtests passed, 5 deselected, 1 warning**.
- Added strict response schemas for active Option C product advice, paired comparison evidence, product-question evidence and short chat-action classification, with per-operation output caps. GitHub Actions run #145 passed on code head `f384c070c8b6338d2d5b324902d9a7ae49db218d`: **302 passed, 64 subtests passed, 5 deselected, 1 warning**. Compileall and both benchmark CLI checks passed. These are scoped optimization regressions, not an unfiltered full-repository test run.

## 5. Google capability audit

| Capability | Finding | Decision |
|---|---|---|
| Thinking levels | Gemini 3.5 Flash-Lite supports minimal/low/medium/high | Keep turn minimal; selector level configurable and benchmarked |
| Structured output | Supported; schema still requires application-level semantic validation | Retain schemas and exact evidence validation |
| Function calling | Supported by model, but unused in Option C | Disable SDK AFC for direct calls; no agent loop |
| Implicit context caching | Automatic on supported models | Reorder stable prefix and measure cached-token counts |
| Explicit context caching | Supported, but requires cache lifecycle and storage-cost analysis | Not enabled before measured break-even |
| Batch API | Useful for offline evaluation, not synchronous Telegram search | Keep out of interactive search |
| Temperature/top-p/top-k | Not recommended as routine Gemini 3.5 tuning knobs | Do not set without controlled evidence |

Official references:
- [Gemini 3.5 Flash-Lite model capabilities](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite)
- [Thinking levels and Gemini 3.5 guidance](https://ai.google.dev/gemini-api/docs/thinking)
- [Structured output](https://ai.google.dev/gemini-api/docs/structured-output)
- [Context caching](https://ai.google.dev/gemini-api/docs/caching)
- [Gemini Developer API pricing](https://ai.google.dev/gemini-api/docs/pricing)
- [Google Gen AI Python SDK](https://googleapis.github.io/python-genai/)

## 6. Cost model from the available live trace

The user's 2026-11-15 live search trace reported the following estimated Gemini 3.5 costs (Standard paid-tier rate table in `services/gemini_pricing.py`, input $0.30 / 1M tokens and output $2.50 / 1M tokens):

| Operation | Estimated cost |
|---|---:|
| `conversation_turn` | $0.00086360 |
| First `conversation_selection` | $0.00393790 |
| Continuation `conversation_selection` | $0.00375910 |
| **Total for this search attempt** | **$0.00856060** |

This trace used 26,927 input tokens and 193 candidate-output tokens across three requests. Cached-token usage was not visible in the old log. The search returned one card, so this is a cost-per-attempt observation, **not** a validated cost-per-successful-20-card search and not a representative average.

For scale only, assuming 30 searches per user per month and every search costing as much as this one trace:

| Active users | Searches / month | Gemini estimate / month |
|---:|---:|---:|
| 20 | 600 | $5.14 |
| 50 | 1,500 | $12.84 |
| 100 | 3,000 | $25.68 |
| 500 | 15,000 | $128.41 |

These are scenario estimates, not invoices; they exclude AliExpress API, hosting, Telegram and all other infrastructure. Actual cost must be recomputed from the new cached-token telemetry and a larger sample of successful/empty/clarification sessions.

The earlier blind language-operation A/B report estimated $0.1863 for 312 requests on 3.5 versus $0.1403 for 312 requests on 3.1 (about 33% more for 3.5). That corpus did not include product selection and must not be used as a full-search cost comparison.

## 7. Remaining acceptance work

1. Run the scoped GitHub Actions optimization regression workflow on the latest feature-branch commit. Report the exact run and result; do not describe the filtered workflow as a full-suite pass.
2. Capture real selector inputs from AliExpress using the existing opt-in `SHORTS_CHILL_SELECTOR_CAPTURE=1` mechanism. These raw captures are not gold-labeled and must not be used as quality truth until independently reviewed.
3. Build a reviewed set with eligible/rejected IDs, exact identity/requirement evidence, ranking tiers, and critical-error labels.
4. Run blind 3.1 vs 3.5 selector replay on identical snapshots at minimal/low/medium, with repeated runs and no reserve retry.
5. Evaluate the user's requested weighted quality criteria and critical errors separately from JSON acceptance. Compare p50/p95, prompt/output/thinking/cached tokens, and cost per successful search.
6. Only after those gates decide whether 3.5 should remain primary for candidate selection. If it does not match or exceed 3.1 quality without unacceptable latency/cost, keep 3.1 primary for that operation.

**No live Gemini, AliExpress, or Telegram call was made by this implementation pass. No local pytest result is claimed here.**
