# Gemini operation inventory — active and legacy paths

Audit date: 2026-11-06. This is a source-level inventory plus the limited token evidence in the supplied 6 November live log. Unknown values are intentionally marked unknown; prompt character counts are not token counts.

## 1. Core Option C text conversation

| Operation / AIService method | Active callsite and condition | Request/model settings | Source/runtime bounds | Observed usage |
|---|---|---|---|---|
| `conversation_turn` / `decide_conversation_turn` | `ConversationSearchEngine.handle_message` for ordinary non-shortcut text turns | Fast role; primary `gemini-3.8-flash`, fallback `gemini-3.1-flash-lite`; production request has no explicit generation config | 4s per primary attempt; 8.5s outer turn bound | Two successful 3.8 calls: 1,891 input + 47 candidate + 132 thoughts; 2,369 input + 44 candidate + 188 thoughts. |
| `conversation_selection` / `select_conversation_candidates` | Engine after retrieval and deterministic checks when a non-empty eligible pool remains | Primary 3.8, fallback 3.1 Flash-Lite; JSON MIME + low thinking; up to 70 candidates, selects max 20 | 6.5s primary attempt; 15s outer bound | Prompt 31,862 chars; successful fallback: 12,367 input + 1,391 candidate + 113 thoughts. One primary timeout at 6.509s has unknown provider-side usage. A later response had 11,684 input + 5 candidate tokens, but thought usage was missing, so total output cost is unknown. |
| `no_results_reply` / `compose_no_results_reply` | Engine after successful retrieval ends with no candidates/no eligible candidates; skipped after selector failure/timeout or exhausted turn budget | Fast role; generic JSON/text helper | 3s outer bound | No reliable token usage in supplied logs; a no-results reply timed out in the 6 Nov session. |
| `generate` / `decide_chat_action` | Only the short (≤4-word), strategy-less clarification branch after the main interpreter did not return a validated `clarify` action | Fast role; 3.8 primary + configured fallback; no explicit output config | General 20s default, bounded by engine turn budget | One generic `generate` log on an `engine-clarify` route: 194 input + 20 candidate + 143 thoughts. The log labels the operation only as `generate`, so exact method attribution is not guaranteed. |

Local shortcuts (e.g. count/currency follow-ups and narrowing already-shown cards) can avoid `conversation_turn` entirely. Validated `respond` and shown-card comparison routes can exit before AliExpress retrieval and candidate selection.

## 2. ProductAssistant, category and discovery operations

| AIService method / default operation label | Active caller and condition | Prompt/config notes | Usage evidence |
|---|---|---|---|
| `plan_product_search` / `generate` | ProductAssistant similar/cheaper/better search planning | Search-plan template ~363 chars; real provider search follows | No per-operation token sample. |
| `select_feed_candidates` / `generate` | SearchService non-raw search results; category browse; merged photo-search pool | Feed selector template; separate from Option C conversation selector | No per-operation token sample. |
| `select_candidates` / `generate` | ProductAssistant after deterministic goal-specific filtering | Selection template ~536 chars | No per-operation token sample. |
| `analyze_product` / `generate` | ProductAssistant product advice when AI available | Analysis template ~627 chars | No per-operation token sample. |
| `extract_paired_listing_evidence` / `product_question` | Explicit comparison of two shown products | Comparison template ~1,661 chars; paired evidence, then deterministic rendering | No per-operation token sample. |
| `answer_product_question` / `product_question` | Other product questions only after deterministic evidence cannot answer | Question template ~1,859 chars; product evidence can include multiple shown cards | No per-operation token sample. |

ProductAssistant similar/cheaper/better can make up to three calls: `plan_product_search`, the generic `select_feed_candidates` inside SearchService, then goal-specific `select_candidates`. The latter two are separate selection passes; their marginal value is unmeasured. Do not remove one before a controlled replay compares recall and quality.

Category browse can make one feed-selection call. Ordinary «✨ Находки» feed pagination uses `find_feed_page` and makes zero Gemini calls.

## 3. Photo and legacy operations

| Method / label | Caller and condition | Prompt notes | Usage evidence |
|---|---|---|---|
| `analyze_photo_with_vision` / `generate` | Active photo-message handler's first vision stage | Image-analysis prompt ~1,304 chars plus image payload | No per-operation token sample. |
| `analyze_image` / `generate` | Legacy photo fallback if first vision stage yields no usable query | Separate image analysis prompt; can create a second sequential request for one photo | No per-operation token sample. |
| `describe_image_query` / `generate` | Older image-description helper; no active caller established in inspected handler graph | Image-query prompt ~360 chars | Not counted as active photo traffic without a caller. |
| `parse_query` / `generate` | Legacy `SearchService.find_products_from_text`; no caller from authoritative `handlers.search.search_message` path | Query-parsing prompt ~2,311 chars; can be followed by `parse_query_intent` in the legacy path | Exclude from normal Option C conversation forecast until production reachability is proven. |
| `rank_candidates` / `generate` | `SearchService.find_discovery`; no active Telegram caller established | Ranking prompt ~728 chars | Treat as unverified/legacy traffic. |

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
