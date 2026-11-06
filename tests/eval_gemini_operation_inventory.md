# Gemini operation inventory — active and legacy paths

Audit date: 2026-11-06. This is a source-level inventory plus the limited token evidence in the supplied 6 November live log. Unknown values are intentionally marked unknown; prompt character counts are not token counts.

## 1. Core Option C text conversation

| Operation / AIService method | Active callsite and condition | Request/model settings | Source/runtime bounds | Observed usage |
|---|---|---|---|---|
| `conversation_turn` / `decide_conversation_turn` | `ConversationSearchEngine.handle_message` for ordinary non-shortcut text turns | Fast role; primary `gemini-3.8-flash`, fallback `gemini-3.1-flash-lite`; production request has no explicit generation config | 4s per primary attempt; 8.5s outer turn bound | Two successful 3.8 calls: 1,891 input + 47 candidate + 132 thoughts; 2,369 input + 44 candidate + 188 thoughts. |
| `conversation_selection` / `select_conversation_candidates` | Engine after retrieval and deterministic checks when a non-empty eligible pool remains | Primary 3.8, fallback 3.1 Flash-Lite; JSON MIME + low thinking; up to 70 candidates, selects max 20 | 6.5s primary attempt; 15s outer bound | Prompt 31,862 chars; successful fallback: 12,367 input + 1,391 candidate + 113 thoughts. One primary timeout at 6.509s has unknown provider-side usage. A later response had 11,684 input + 5 candidate tokens, but thought usage was missing, so total output cost is unknown. |
| `no_results_reply` / `compose_no_results_reply` | Engine after successful retrieval ends with no candidates/no eligible candidates; skipped after selector failure/timeout or exhausted turn budget | Fast role; generic JSON/text helper | 3s outer bound | No reliable token usage in supplied logs; a no-results reply timed out in the 6 Nov session. |
| `chat_action` / `decide_chat_action` | Only the short (≤4-word), strategy-less clarification branch after the main interpreter did not return a validated `clarify` action | Fast role; 3.8 primary + configured fallback; no explicit output config | General 20s default, bounded by engine turn budget | One historical pre-label `generate` log on an `engine-clarify` route: 194 input + 20 candidate + 143 thoughts. New attempts are explicitly labeled `chat_action`. |

Local shortcuts (e.g. count/currency follow-ups and narrowing already-shown cards) can avoid `conversation_turn` entirely. Validated `respond` and shown-card comparison routes can exit before AliExpress retrieval and candidate selection.

## 2. ProductAssistant, category and discovery operations

| AIService method / default operation label | Active caller and condition | Prompt/config notes | Usage evidence |
|---|---|---|---|
| `product_search_plan` / `plan_product_search` | ProductAssistant similar/cheaper/better search planning | Search-plan template ~363 chars; real provider search follows | No per-operation token sample. |
| `feed_candidate_selection` / `select_feed_candidates` | SearchService non-raw search results; category browse; merged photo-search pool | Feed selector template; separate from Option C conversation selector | No per-operation token sample. |
| `product_candidate_selection` / `select_candidates` | ProductAssistant after deterministic goal-specific filtering | Selection template ~536 chars | No per-operation token sample. |
| `product_advice` / `analyze_product` | ProductAssistant product advice when AI available | Analysis template ~627 chars | No per-operation token sample. |
| `product_comparison_evidence` / `extract_paired_listing_evidence` | Explicit comparison of two shown products | Comparison template ~1,661 chars; paired evidence, then deterministic rendering | No per-operation token sample. |
| `product_question_answer` / `answer_product_question` | Other product questions only after deterministic evidence cannot answer | Question template ~1,859 chars; product evidence can include multiple shown cards | No per-operation token sample. |

ProductAssistant similar/cheaper/better can make up to three calls: `plan_product_search`, the generic `select_feed_candidates` inside SearchService, then goal-specific `select_candidates`. The latter two are separate selection passes; their marginal value is unmeasured. Do not remove one before a controlled replay compares recall and quality.

Category browse can make one feed-selection call. Ordinary «✨ Находки» feed pagination uses `find_feed_page` and makes zero Gemini calls.

## 3. Photo and legacy operations

| Method / label | Caller and condition | Prompt notes | Usage evidence |
|---|---|---|---|
| `photo_vision_search` / `analyze_photo_with_vision` | Active photo-message handler's first vision stage | Image-analysis prompt ~1,304 chars plus image payload | No per-operation token sample. |
| `photo_image_analysis` / `analyze_image` | Legacy photo fallback if first vision stage yields no usable query | Separate image analysis prompt; can create a second sequential request for one photo | No per-operation token sample. |
| `photo_query_description` / `describe_image_query` | Older image-description helper; no active caller established in inspected handler graph | Image-query prompt ~360 chars | Not counted as active photo traffic without a caller. |
| `legacy_query_parse` / `parse_query` | Legacy `SearchService.find_products_from_text`; no caller from authoritative `handlers.search.search_message` path | Query-parsing prompt ~2,311 chars; can be followed by `parse_query_intent` in the legacy path | Exclude from normal Option C conversation forecast until production reachability is proven. |
| `discovery_ranking` / `rank_candidates` | `SearchService.find_discovery`; no active Telegram caller established | Ranking prompt ~728 chars | Treat as unverified/legacy traffic. |

The active photo handler can call the first vision operation and then a second legacy `analyze_image` request if the first result is unusable. This is a genuine sequential fallback path and should be measured separately.

## 4. Other prompt template sizes

These are source template character counts, not provider token counts:
- Conversation-turn static template: ~7,444 chars; context snapshot bounded to 6,000 chars and latest user message to 500 chars.
- Query parsing: ~2,311 chars.
- Product question: ~1,859 chars.
- Product comparison: ~1,661 chars.
- Image analysis: ~1,304 chars.
- Candidate ranking: ~728 chars.
- Short chat action: ~698 chars.
- Product analysis: ~627 chars.
- Product selection: ~536 chars.
- Search planning: ~363 chars.
- Image query: ~360 chars.
- Conversation-selection prompt with 70-candidate payload in the live sample: 31,862 chars.

## 5. Known cost samples (Google Standard API rates valid through 2026-12-31)

Billable Gemini output includes candidate + thinking tokens. The following costs use 3.8 Flash at $0.75/M input and $3.75/M output; 3.1 Flash-Lite at $0.25/M input and $1.50/M output.

| Live request | Model | Input | Candidate output | Thoughts | Calculated request cost |
|---|---|---:|---:|---:|---:|
| Conversation turn #1 | 3.8 Flash | 1,891 | 47 | 132 | $0.00208950 |
| Conversation turn #2 | 3.8 Flash | 2,369 | 44 | 188 | $0.00264675 |
| Successful selector fallback | 3.1 Flash-Lite | 12,367 | 1,391 | 113 | $0.00534775 |
| Generic helper on clarification route | 3.8 Flash | 194 | 20 | 143 | $0.00075675 |

Known subtotal for the first successful turn + selector fallback is **$0.00743725**. It excludes the timed-out primary selector attempt, whose provider-side token usage is unknown. It is not the total cost of the complete search scenario.

A later selector response with 11,684 input and 5 candidate tokens had no recorded thought count. Its input-plus-candidate minimum at the 3.8 rate is $0.00878175, but exact output cost is unknown.

## 6. Required telemetry gaps

- No dependable production turn-scope aggregation is wired into active handlers.
- Most helper operations use the generic `generate` label; the current logs cannot reliably separate product advice, photo analysis, search planning and feed selection.
- No persistent cost ledger, cost-per-scenario aggregation, or daily/weekly/monthly spend report exists.
- The supplied logs show successful token usage, but failed/time-out attempts may have unknown provider-side billing.
- Gemini implicit caching is on by default for the relevant model generation, but no confirmed cache hit was visible in the supplied sample. Cached-token usage must be kept separate.
- The benchmark runner now records normalized provider usage and optional tariff-based estimates for the interpreter path; production telemetry still needs a separately approved instrumentation design.

## 7. Audit disposition

The highest-confidence cost leads are:
1. The 70-candidate selector payload is the largest measured prompt.
2. ProductAssistant can perform two candidate-selection calls after search planning.
3. Photo failure can trigger a second vision request.
4. Most non-core helper calls lack operation-specific token telemetry.
5. Conversation-turn reasoning tokens are material relative to visible candidate output.

None of these observations alone authorizes deleting a call, shrinking the candidate window, lowering reasoning, or changing model routing. Each requires the blinded quality/latency benchmark and the TЗ's quality-first decision rule.

## 8. Call-count matrix from the current handler graph

Counts below are logical operation calls, not HTTP attempts. A single _generate call can issue the primary model and then the configured fallback. The fallback policy is described separately in §9.

| User flow | Logical AI operations in current source | Calls per normal flow | Conditions that add/avoid calls |
|---|---|---:|---|
| New text search with eligible results | conversation_turn → conversation_selection | 2 | Each operation can have a primary + fallback attempt. |
| Budget / multi-feature / exclusion refinement | conversation_turn → conversation_selection | 2 | Same as new search when the turn reaches retrieval and eligible candidates remain. Local count/currency/narrowing shortcuts can bypass the interpreter and/or retrieval. |
| Explicit category change | conversation_turn → conversation_selection | 2 | The engine owns the category switch; retrieval/ranking stays deterministic before semantic selection. |
| Successful search with no safe candidates | conversation_turn → optional conversation_selection → no_results_reply | 2–3 | Selection is absent if there is no eligible pool. The no-results composer is skipped after selector timeout/error/unavailable or exhausted turn budget. |
| Search returns a few products | conversation_turn → conversation_selection | 2 | There is no separate “few results” helper. A short valid selection is not padded. |
| Compare two shown products | conversation_turn → product_question (extract_paired_listing_evidence) | usually 2 | The comparison evidence call runs for explicit comparisons with at least two shown products; the final comparison text is deterministic and grounded. |
| Factual question about a shown product | conversation_turn → optional product_question (answer_product_question) | 1–2 | Deterministic title/field evidence answers first; Gemini is called only when deterministic evidence cannot answer. |
| Product-card “worth buying?” action | analyze_product | 1 | Deterministic advice is the fallback. |
| Product-card similar/cheaper/better action | plan_product_search → select_feed_candidates → select_candidates | up to 3 | Planning runs before retrieval; generic feed selection runs on the broad SearchService pool; goal-specific selection runs after ProductAssistant's deterministic goal filter. Calls may be skipped when no candidates remain. |
| Category browse | select_feed_candidates | 1 | SearchService runs the normal category retrieval and then one feed-selection pass when the pool is non-empty. |
| Ordinary «✨ Находки» pagination | none | 0 | find_feed_page uses AliExpress' own order and does not call Gemini. |
| Active photo search, first vision result usable | analyze_photo_with_vision | 1 | The active handler then runs one or two bounded photo searches with broaden=False; this primary path does not call the AI feed selector. |
| Active photo search, first vision result unusable | analyze_photo_with_vision → analyze_image → optional feed selection | 2–3 | The fallback photo pipeline can make one second vision request, compose several grounded hypotheses, and call apply_feed_selection once on the merged pool. |
| Short ambiguous text (≤4 words) without a validated interpreter clarification | conversation_turn → decide_chat_action | 1–2 | The second call is a narrow optional clarification helper; it is not run if the main interpreter already returned a validated clarify action. |

These are source-derived flow counts, not measured averages. They do not include provider-side attempts hidden by client timeouts, and they do not imply that every flow reaches every listed operation.

## 9. Retry/fallback policy by operation

| Operation family | Primary model | Fallback behavior | Timeout behavior |
|---|---|---|---|
| conversation_turn | fast role, normally Gemini 3.8 Flash | One fallback model (normally 3.1 Flash-Lite) on model-unavailable errors and client timeout | 4s per attempt; engine/AIService outer limit 8.5s |
| conversation_selection | fast role, normally Gemini 3.8 Flash | One fallback on model-unavailable errors and client timeout | 6.5s per attempt; engine outer limit 15s |
| no_results_reply | fast role | Fallback only on model-unavailable errors; quota stops; timeout does not trigger a second attempt | AIService limit 3s, engine wrapper 3.2s |
| product_question | smart role, normally Gemini 3.8 Flash | Fallback on model-unavailable errors; timeout is constrained by the product-question outer deadline and does not get a full second attempt | Caller uses PRODUCT_QUESTION_TIMEOUT_SECONDS; exact wall time depends on that constant |
| Generic generate helpers | role-specific fast/smart/vision model | Fallback on model-unavailable errors; quota stops | Normally AIService's 20s timeout unless a caller applies a shorter wrapper (feed selector has an 8s wall bound) |

The current code does not retry quota/rate-limit failures: the key/account quota is shared, so trying another Gemini model would not help. Generic helper timeouts do not automatically trigger fallback. This distinction matters when counting billable attempts: a client timeout has unknown provider-side usage even when no successful response/usage metadata is returned.

## 10. Confirmed repeated or potentially redundant AI work

1. **ProductAssistant has two sequential candidate-selection passes after planning.** The broad SearchService.find_products path can run select_feed_candidates; then ProductAssistant._select applies goal-specific deterministic filters and calls select_candidates again. These are not identical criteria, but the first pass may discard a listing useful for “cheaper/better/similar” before the goal-specific decision. Candidate for a controlled A/B replay; do not remove either pass without measuring recall and final suitability.
2. **Explicit comparison always calls Gemini before deterministic rendering.** ProductAssistant.answer calls extract_paired_listing_evidence for an explicit comparison, then deterministic_comparison renders prices, ratings, orders, delivery and verified quotes. When structured facts already give a useful comparison, the marginal value of the evidence call may be low. Candidate for evidence-coverage analysis; do not bypass it yet.
3. **Photo fallback may analyze the same image twice.** The active photo handler first calls analyze_photo_with_vision; if it cannot produce a usable hypothesis, it delegates to the legacy fallback, which calls AIService.analyze_image. The second prompt is richer and is a fallback, not a parallel duplicate. Measure failure rate and marginal recovery before changing.
4. **Conversation interpreter and candidate selector are not duplicates.** The first decides intent/action and the second reviews real retrieved listings against identity/requirements. Removing either would collapse distinct safety/quality stages and is outside safe optimization absent benchmark proof.
5. **The short clarification helper is conditional, not an unconditional duplicate.** It is reached only when the interpreter did not return a validated clarify action and the deterministic path still needs a question.

## 11. Reachability and file-name correction

The current branch does not contain services/consultant.py or services/consultant_answers.py. The active Option C implementation is in services/convsearch/engine.py, services/convsearch/state.py, services/convsearch/retrieval.py, and services/ai_service.py; product-specific advice and question handling are in services/product_assistant.py.

The following methods exist but are not on the current authoritative Telegram text-search path:
- SearchService.find_products_from_text / AIService.parse_query / parse_query_intent — legacy text parsing path; handlers.search.search_message binds the Option C engine instead.
- SearchService.find_discovery / AIService.rank_candidates — no current Telegram handler caller was found in the inspected registration/call graph.
- AIService.describe_image_query — the active photo handler uses analyze_photo_with_vision; its fallback uses analyze_image.
- services.conversation.resolve_request — legacy merge helper; no active caller was found in the current text handler graph.

Do not include these unverified/legacy paths in normal user-scenario cost totals unless a live caller is established.

## 12. Audit conclusions updated

The highest-confidence opportunities to evaluate next are:
- determine whether ProductAssistant can safely bypass the generic feed selector for its goal-specific candidate search, retaining the broad raw pool for the single goal-specific selection;
- determine when explicit comparison evidence adds useful listing facts beyond deterministic product fields;
- attribute every generic generate request to its actual helper so future usage logs can separate plan/select/advice/vision calls;
- measure no-results and photo-fallback invocation rates before changing their prompts or model roles.

No production call, retrieval breadth, ranking, filtering, validation, product evidence, or Telegram delivery behavior was changed by this audit.


## 13. Prompt-prefix and cache-readiness audit

Source-level findings:
- `AIService._generate` sends `model` and `contents`; it does not create a Gemini cached-content resource or set a cache identifier. Candidate selection alone adds JSON MIME and low thinking. The telemetry reader can observe cached-token metadata if the provider returns it, but this is not evidence that explicit caching is configured.
- No confirmed cache-token hit appeared in the supplied 6 November live log.
- The conversation-turn prompt places the variable JSON context near the beginning of the template, before most of the stable instruction text. Only the short pre-context portion is a stable request prefix; the large instruction tail cannot be assumed to hit an automatic prefix cache.
- The conversation-selection prompt starts with variable query, identity IDs and seller terms before its stable JSON contract and lengthy fixed selection rules. Its largest static section is therefore not at the beginning of the request.
- The candidate payload itself is dynamic and must remain present for grounded selection. It is not safe to cache or omit product evidence as though it were constant.

Potential Stage V experiment, not yet implemented: move only immutable system rules to the beginning and place current-turn context / requirements / candidate rows after them, then compare exact output quality, cache-hit tokens, billed input, latency and total cost. Keep prompt hashes and blind regression results. Do not introduce cache state that can reuse old candidate rows, prices, budgets, region or conversation constraints.

## 2026-11-06 telemetry operation labels

Added explicit operation labels to every inspected AIService._generate call site and the two direct helper callers. The per-attempt `gemini_attempt op=...` log can now distinguish:
- `conversation_turn`, `conversation_selection`, `chat_action`, `no_results_reply`;
- `product_search_plan`, `feed_candidate_selection`, `product_candidate_selection`, `product_advice`;
- `product_comparison_evidence`, `product_question_answer`;
- `photo_vision_search`, `photo_image_analysis`, `photo_query_description`;
- `legacy_query_parse`, `legacy_query_intent`, `discovery_ranking`.

This changes telemetry labels only; it does not change prompts, model selection, retry/fallback rules, timeouts, search, validation, ranking, or Telegram output. The historical `generate` log sample for the clarification route predates the new label and is retained as historical evidence.

Important remaining measurement gap: `services/gemini_telemetry.py` defines `turn_scope`, but no active Telegram handler currently opens it. Per-attempt logs are attributable by operation, but per-user-message aggregated token totals are not yet emitted by the production request flow. Do not claim per-dialogue accounting is complete until handler scopes and a privacy-safe persistence/aggregation path are implemented.