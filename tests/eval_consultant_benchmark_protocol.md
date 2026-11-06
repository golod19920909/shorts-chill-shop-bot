# Consultant model benchmark protocol

Status: design ready; no provider calls have been made.
Corpus: `tests/eval_consultant_dialogue_dataset.py`, version `consultant-dialogue-v1`.

## 1. Scope

Benchmark three current Option C language operations:
- conversation-turn interpretation: exact user message + bounded context → structured decision, validated by the production `AIService.decide_conversation_turn` path;
- short clarification helper: short message → `search` or one clarification question, validated by `AIService.decide_chat_action`;
- no-results wording: user request + active constraints + grounded search outcome → one short plain-text question, validated by `AIService.compose_no_results_reply`.
The auxiliary helper corpora are artificial language/contract fixtures, not marketplace product evidence. No marketplace retrieval, product fabrication, ranking changes, or Telegram side effects occur in this benchmark.

The selector is a separate benchmark. It must not use synthetic product rows. Capture anonymized, permitted real AliExpress API candidate pools first, including the exact title, description, category, price, currency, rating/order fields passed to the selector. Every candidate model must receive the identical saved pool and requirement set.

## 2. Candidate model roster

Pin exact IDs and provider endpoints in the run manifest. Initial candidates to verify at run time:
- Google: `gemini-3.8-flash` (current primary), `gemini-3.1-flash-lite` (current fallback).
- OpenAI: `gpt-6-luna`.
- Anthropic: `claude-haiku-5-5`.
- DeepSeek: `deepseek-flash` (DeepSeek-V4.1-Flash-1017).
- Alibaba Model Studio: `qwen3.5-flash` as the default JSON-Object candidate; optionally test `qwen3.7-flash` / `qwen3.8-flash` for documented JSON-Schema support when the exact workspace endpoint exposes them.

Do not silently substitute a model alias if a pinned ID is unavailable. Record exact returned model/version and region/endpoint. Do not use a gateway in the first quality comparison: it adds another variable. Gateways can be benchmarked after direct-provider baselines.

## 3. Fair request construction

- Reuse the exact production `CONVERSATION_TURN_PROMPT` and exact corpus context; do not rewrite or translate prompts for individual vendors.
- Use the same user message, context, output contract, temperature policy (where supported), timeout, and maximum output budget.
- Provider-native JSON/JSON-Schema output may be enabled only when documented for that exact model/API. Record the mode per provider. Do not count a plain JSON-object guarantee as strict JSON-Schema compliance.
- The runner's output modes are recorded in the separate blind map: Gemini production prompt-only JSON; OpenAI strict JSON Schema; Anthropic JSON Schema; DeepSeek JSON Object; Qwen 3.5/3.6 Flash JSON Object with thinking disabled; Qwen 3.7/3.8 families may use JSON Schema where the exact model supports it. OpenAI and Anthropic effort are set to medium; Gemini and DeepSeek use provider defaults. This is not a claim that reasoning configurations are identical; the exact settings are part of the run manifest/map and must be considered when interpreting results.
- Preserve the current application validator as the common final gate. A valid JSON response that fails the bot's semantic/grounding validation is a rejected decision.
- Do not include product URLs, credentials, user identifiers, or unneeded PII in benchmark payloads.

## 4. Blind execution

- Before scoring, map provider/model names to random run-local aliases (A, B, C, ...).
- Store the alias-to-model map in a separate file that is not shown to reviewers until scoring is locked.
- Randomize request order per model and case using a recorded seed. Avoid running all requests for one provider first.
- Run at least three repetitions for latency-sensitive cases; preserve all raw outputs. Report median and p95, not only averages.
- First run one request per model/case without application retries to measure base capability. Then run a separate resilience pass using the production timeout and fallback policy.
- Keep the run manifest with timestamp, region, endpoint, model ID/version, API mode, thinking/effort settings, timeouts, seed, and prompt/corpus hashes.

## 5. Interpreter scoring

For every turn, score separately:
1. action exact match (new_search/refine/correction/alternatives/compare/clarify/respond);
2. product identity: correct core product head, no relation-target or accessory substitution;
3. slot operations: kind, add/replace/remove, exact active target, current-turn-grounded value;
4. retrieval decision: search only when appropriate; no retrieval for clarify/respond/compare;
5. clarification quality: one focused question, in the user's language, when required;
6. schema/validator acceptance: valid parse plus accepted by the real application validator.

Report per-scenario and per-language scores. A high aggregate score cannot hide a critical regression in product switching, budget changes, exclusions, compatibility, or comparison grounding.

Hard safety gates:
- no invented product identity;
- no stale constraints carried across an explicit product switch;
- no mandatory requirement silently dropped;
- no unsupported product claim from a shown card;
- no search triggered for a compare/respond/clarify decision;
- malformed or ungrounded output must fail closed.

## 6. Selector scoring (blocked until authentic pools exist)

Use identical real AliExpress candidate snapshots for every model. Score:
- precision and recall against reviewed gold candidate IDs;
- exact product-family/identity correctness;
- each mandatory feature independently;
- compatibility and exclusions;
- budget and currency compliance;
- quote evidence validity against the exact title/description field;
- duplicate rate and safe abstention.

Do not score popularity or price rank as a substitute for relevance. Keep deterministic pre-filters, post-validation and candidate-window size identical across models.

## 7. Cost and latency capture

Per request capture:
- provider/model/version, operation, case/turn ID, timestamp;
- wall-clock duration and outcome (success, timeout, HTTP/API error, invalid JSON, validator rejection);
- provider-reported input, output, cached-input and reasoning/thinking tokens separately;
- cache-write/storage usage where billed;
- retry/fallback attempts separately;
- calculated cost using the tariff valid at request time.

Do not estimate failed-request cost as zero when usage is unavailable. Mark it unknown and show a range if the provider may have processed the request before client timeout.

Report p50/p95 latency, valid-JSON rate, application-acceptance rate, exact-action accuracy, critical-error rate, token distributions and cost per accepted correct decision.

## 8. Selection rule

Choose by the agreed order: quality first, then speed, reliability, cost, maintainability. A lower-cost model is not eligible to replace the current model if it fails any hard safety gate or materially regresses the RU/UK dialogue corpus. A cheaper model may be used for a narrow operation only after that operation passes its own blind benchmark.

## 9. Current blockers

- The 13-scenario dialogue corpus exists, but gold labels need a review pass before model selection.
- No permitted real AliExpress selector candidate snapshots have been added yet.
- A provider-neutral blind runner exists for the three language operations above; no provider API calls have been made yet.
- No model API keys have been added to CI or repository settings; never add them. Benchmark credentials must be supplied locally through environment variables, not committed or printed.
- No model comparison results exist. The runner has not been executed against real provider APIs. Do not describe any candidate as better, faster or cheaper in actual bot use until it has been measured on this protocol.

## 10. Official API references and pricing checked 2026-11-06

- Consolidated tariff and Ukraine availability matrix: `tests/eval_provider_pricing_2026-11-06.md`.

- OpenAI GPT-6 Luna model and pricing: https://developers.openai.com/api/docs/models/gpt-6-luna and https://developers.openai.com/api/docs/pricing
- OpenAI Responses structured outputs: https://developers.openai.com/api/docs/guides/structured-outputs and https://developers.openai.com/api/reference/resources/responses/methods/create
- Anthropic Messages API / JSON schema output: https://platform.claude.com/docs/en/api/http/messages
- Anthropic Haiku 5.5 model: https://platform.claude.com/docs/en/models/haiku-5-5/overview
- DeepSeek model/pricing and OpenAI-compatible endpoint: https://api-docs.deepseek.com/quick_start/pricing/ and https://api-docs.deepseek.com/api/create-chat-completion/
- DeepSeek JSON mode: https://api-docs.deepseek.com/guides/json_mode/
- Alibaba Qwen 3.5 Flash model/regions/structured output: https://docs.modelstudio.console.alibabacloud.com/en/model-studio/qwen3-5-flash
- Alibaba current Model Studio pricing (updated 2026-11-05): https://www.alibabacloud.com/help/en/model-studio/model-pricing
- Alibaba structured output modes and Qwen3.5 non-thinking restriction: https://docs.modelstudio.console.alibabacloud.com/en/model-studio/qwen-structured-output
- Alibaba OpenAI-compatible API: https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-responses


## 11. Human blind review and scope boundaries

The current runner measures three bounded language operations: the conversation-turn interpreter, the short chat-action helper, and the no-results reply helper. The auxiliary chat-action and no-results cases are synthetic contract fixtures; no product evidence is fabricated or used for selector scoring. It still:
- does not call the live AliExpress provider;
- does not benchmark candidate selection (blocked until authentic candidate snapshots exist);
- does not call product advice, paired comparison evidence, factual product-question helpers, or photo vision;
- does not score the final Telegram answer or real product relevance.

The no-results helper is exercised on a separate artificial auxiliary corpus and scored for its production format/validation contract; its naturalness still requires blind human review and eventual anonymized real-outcome replay. D12's provider-failure behavior remains downstream and is not exercised by the no-results helper corpus.

After automated scoring, reviewers must score the raw outputs under the random aliases without opening the alias map:
- language naturalness and clarity (1–5);
- whether the model asks exactly one useful clarification when required (1–5; N/A otherwise);
- whether the answer preserves the user's explicit constraints and context (pass/fail);
- whether it introduces an unsupported fact, requirement, product identity or compatibility claim (hard fail);
- whether marketplace terms are useful, cross-language where needed, and do not change the product identity (1–5; N/A if none);
- concise reviewer rationale.

Use two independent reviewers for any case that may change provider routing. Resolve disagreements before revealing provider names. Keep the automated decision score and human-language score separate; do not average away a hard failure.

## 12. Required follow-up replay sets

Create and review separate gold sets before testing these operations across providers:
1. No-results replies: real outcomes classified as no candidates, no eligible products, selector empty, and provider failure (provider failure must not be presented as an empty catalogue).
2. Product questions: anonymized real card fields, questions whose answer is present, absent, ambiguous, and compatibility-related. Score exact quote grounding and safe abstention.
3. Product comparisons: paired real card snapshots; score each field difference against its exact source and whether the final deterministic renderer says only what is supported.
4. ProductAssistant planning/selection: anonymized real AliExpress candidate pools with gold relevance/goal labels; do not reuse synthetic rows as marketplace evidence.
5. Photo analysis: consented/anonymized image set with image-type, visible text, uncertainty and search-hypothesis gold labels. No user images or identifiers in public repository fixtures.

These replay sets are separate from the 13-scenario interpreter corpus. The interpreter results alone cannot authorize a model switch for product selection, factual answers, comparisons or vision.


### Production-baseline output mode correction

The Gemini interpreter baseline now sends the exact production request body: prompt-only JSON instructions, with no `responseMimeType` or `responseSchema`. The current live `conversation_turn` path does not enable Gemini structured-output mode. Other providers use their documented native structured-output modes where available; these modes are recorded in the blind map. This is an intentional capability comparison, not an identical transport constraint. The same production application parser/validator remains the final gate for every model.


### Provider-native output and reasoning controls

The current Gemini production interpreter has no explicit output-token cap or thinking-level setting; the baseline runner preserves that behavior. OpenAI and DeepSeek are also left without an explicit output cap. Anthropic's Messages API requires `max_tokens`, so the runner uses 2048 there. Qwen structured-output requests omit `max_tokens` as required by its documented request contract. The exact limit is recorded per blind alias.

Reasoning settings are likewise recorded rather than falsely treated as equivalent: Gemini and DeepSeek use provider defaults, OpenAI and Anthropic use medium effort; Qwen 3.5/3.6 Flash disables thinking for JSON Object mode, while Qwen 3.7/3.8 structured-schema candidates use the selected provider-supported reasoning mode. This is a practical provider-capability comparison, not a mathematically identical inference configuration. If one model is a finalist, run a second effort/latency sweep before selecting production settings.


### Strict separation of blind review from performance data

The runner writes two artifacts:
- blind results: alias, scenario/language, raw output, application-validated decision, gold label and quality score only;
- private map/performance artifact: alias-to-provider/model mapping, resolved model versions, seed, tariff table, per-request token/latency/error/cost records and per-alias summaries.

Do not give the private artifact to reviewers until all quality scores and disagreements are locked. Token counts, latency, cost and HTTP/provider errors are intentionally excluded from the blind quality artifact so they cannot reveal a likely provider or bias human scoring.

## Same-provider model comparison

The benchmark accepts two distinct provider/model combinations, not only two provider families. This allows the first real run to compare Gemini 3.8 Flash with Gemini 3.1 Flash-Lite using the existing Gemini API key. The manifest marks such a run as `same_provider_model_comparison`; runs spanning multiple provider families are marked `cross_provider`. Duplicate identical provider/model pairs are rejected. This first Gemini-only run is useful for Stage IV Option A and must not be misrepresented as a cross-provider comparison.

## Gemini-only first-run command

The runner can compare `gemini-3.8-flash` and `gemini-3.1-flash-lite` using the same `GEMINI_API_KEY`; the manifest marks this as `same_provider_model_comparison`. A dated rate file is available at `tests/eval_gemini_rates_2026-11-06.json` (valid for the published rates through 2026-12-31). The runner itself does not read `.env`; the user may run it through the installed `python-dotenv` CLI so keys are injected into the child process without printing them. Example from the repository root: `python -m dotenv run -- .venv/Scripts/python.exe tests/eval_consultant_benchmark_runner.py --repeat 3 --rates-file tests/eval_gemini_rates_2026-11-06.json`.
