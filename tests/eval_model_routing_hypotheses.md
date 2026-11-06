# Model-routing hypotheses — quality-first, not approved changes

Audit date: 2026-11-06. This is a decision framework for the later blind benchmark. It does not change production model routing.

## Decision order

Quality and hard safety gates > latency > reliability/fallback behavior > total cost > maintainability.

A model is eligible for a cheaper route only if it preserves the current application validator and passes the relevant RU/UK/EN scenarios. Price per million tokens alone is not a decision metric.

## Operation-by-operation assessment

| Operation | Current role | Candidate for cheaper model? | Risk / evidence needed |
|---|---|---|---|
| `conversation_turn` | Main intent/action/slot interpreter on ordinary text turns | Low priority; retain current high-quality baseline until later | Highest conversational impact: wrong action, stale slots, product switch, missed exclusions or false retrieval. Must pass all 13 scenarios and long-context corrections. |
| `conversation_selection` | Semantic review of up to 70 candidates, returns up to 20 | Medium priority only after selector benchmark | High product-quality risk. Must preserve exact identity, mandatory features, compatibility, budget, exclusions, evidence quotes, precision and recall. No authentic candidate snapshots yet. |
| `no_results_reply` | User-facing wording after successful empty/no-eligible search | High-priority candidate | No product records are supplied; must not invent catalogue facts or claim an exhaustive search. Could also compare a deterministic template against a low-cost model. |
| `decide_chat_action` | Short-message search-vs-clarify decision on one conditional branch | High-priority candidate | Narrow contract, but false search/clarify decisions can harm UX. Compare deterministic heuristic, current Gemini and low-cost models on ambiguous RU/UK/EN messages. |
| `plan_product_search` | Creates a search phrase for similar/cheaper/better item | High-priority candidate | Search quality depends on preserving product identity and user goal. Must evaluate candidate recall after real AliExpress retrieval, not just string similarity. |
| `analyze_product` | Product advice from a shown product | Medium priority | Risk of unsupported factual advice. Requires evidence-grounded product cases and safe abstention. |
| `extract_paired_listing_evidence` | Extracts evidence for explicit two-product comparison | Medium priority | Small payload and bounded task, but incorrect evidence can invert a recommendation. Keep deterministic comparison rendering and validate every quote. |
| `answer_product_question` | Answers product questions when deterministic evidence cannot | Low/medium priority | Called only after deterministic evidence fails; this is exactly where factual inference risk is highest. Do not downshift until a grounded QA set exists. |
| `select_feed_candidates` | Generic selection in non-raw/category/ProductAssistant paths | Medium priority | It can be followed by a second ProductAssistant selector; test whether the first pass adds measurable quality before deciding whether to downshift or remove it. |
| `select_candidates` | Goal-specific ProductAssistant selection | Medium priority | Same product-quality guardrails as selector; test with identical captured candidate pools. |
| Photo vision analysis | Describes photo / builds a search query | Separate multimodal benchmark | Text-only cheap models are not substitutes. Test vision capability, image token cost and actual search recall; also measure whether the second legacy vision call recovers failures. |
| Legacy `parse_query`, `rank_candidates`, `describe_image_query` | Old or unverified paths | Do not route until production reachability is proven | Exclude from active cost forecast; avoid paying to optimize unused code. |

## First benchmark candidates

1. Compare a deterministic no-results template with the current model and one low-cost text model. Measure user-language correctness, honesty and latency.
2. Compare current `decide_chat_action` with deterministic short-message rules and a low-cost model. Measure false-search and false-clarification rates.
3. Compare `plan_product_search` models on identical shown-product/goal examples, then run the exact same AliExpress search and score relevant-candidate recall.
4. Only then evaluate helper selectors and product advice against reviewed evidence datasets.
5. Leave `conversation_turn` and `conversation_selection` as the quality baseline until all lower-risk tests are complete.

## Non-negotiable constraints

- No provider may alter the deterministic AliExpress retrieval, product validation, hard constraints, ranking safety gates, affiliate URL handling or Telegram delivery.
- No model may invent product facts, prices, specs, stock, compatibility or search outcomes.
- Preserve current fallback/timeout behavior until a separate reliability pass proves an alternative.
- No model route is changed by this document. All items are hypotheses, not implementation approvals.

## Gateway decision update — 2026-11-06

Detailed current comparison: `tests/eval_gateway_comparison_2026-11-06.md`.

Preliminary result remains: do not add a gateway during the current benchmark. Direct provider adapters have the fewest dependencies for the current small shortlist. LiteLLM is the strongest later self-hosted control plane if multiple production providers, shared spend controls and failover justify its infrastructure. OpenRouter is useful for temporary model discovery but adds credit-purchase/BYOK fees and requires pinned routing. Vercel AI Gateway has no token markup and useful failover/observability, but introduces Vercel credits and another production dependency. No latency claim is accepted without a same-host measurement.