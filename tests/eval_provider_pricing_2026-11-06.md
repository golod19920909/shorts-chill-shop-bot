# Provider pricing and Ukraine availability — 2026-11-06

All token prices are USD per 1 million tokens (MTok). Prices are public list prices checked on 2026-11-06; taxes, negotiated discounts, account-specific quotas and workspace restrictions may differ. This is a tariff reference, not a quality recommendation or a measured bot-cost forecast.

## Direct API candidates

| Provider / exact model | Input | Output | Cache / special billing | Notes for this project |
|---|---:|---:|---|---|
| Google Gemini `gemini-3.8-flash` | $0.75 through 2026-12-31; $1.50 from 2027-01-01 | $3.75 through 2026-12-31; $7.50 from 2027-01-01 | Cached input $0.075 then $0.15; storage $0.50 then $1.00 per MTok-hour. Output price includes thinking tokens. | Current primary model. The announced 2027 increase doubles the standard token rates. |
| Google Gemini `gemini-3.1-flash-lite` | $0.25 | $1.50 | Cached input $0.025; storage $1.00 per MTok-hour. | Current fallback model. Do not assume it can replace the primary interpreter without the blind benchmark. |
| OpenAI `gpt-6-luna` | $0.10 | $0.50 | Cached input $0.01; cache writes $0.125 (30m) / $0.25 (12h). Flex/Batch list rates are $0.05 / $0.25. | Standard Responses API request is billed at Standard rates; batch is not a real-time Telegram option. |
| Anthropic `claude-haiku-5-5` | $0.10 up to 100K prompt tokens; $0.50 above 100K | $0.50 up to 100K; $2.50 above 100K | Cache write $0.125 (5m) / $0.20 (1h); cache read $0.01 per MTok up to 100K. Batch input/output 50% off. | The current benchmark prompts are far below the 100K threshold. Newer tokenizer produces about 30% more tokens for the same text than Haiku 4.5. |
| DeepSeek `deepseek-flash` (DeepSeek-V4.1-Flash-1017) | Cache miss: $0.15 off-peak / $0.30 peak; cache hit: $0.003 / $0.006 | $0.60 off-peak / $1.20 peak | Off-peak is half price. Peak: 01:00–04:00 and 06:00–10:00 UTC, Mon–Fri, excluding Chinese public holidays. | 1M context; supports thinking and non-thinking, JSON output and tool calls. Effective rate depends on UTC time and cache-hit ratio. |
| Alibaba `qwen3.5-flash` (International / EU scope) | $0.10 | $0.40 | Pricing page lists no free quota for International scope. | Use JSON Object in non-thinking mode for structured responses; not strict JSON Schema. |
| Alibaba `qwen3.5-flash` (Global scope, Frankfurt deployment) | $0.029 up to 128K; $0.115 for 128K–256K; $0.172 for 256K–1M | $0.287 / $1.147 / $1.72 for the same tiers | Exact scope and endpoint determine the tariff. | Do not use this lower Global rate unless the actual workspace endpoint is confirmed to be Global scope. |
| Alibaba `qwen3.7-flash` (Global scope) | $0.028 up to 32K; $0.083 for 32K–256K; $0.165 for 256K–1M | $0.110 / $0.330 / $0.660 for the same tiers | Batch inference may have a 50% discount where supported. | Strict JSON Schema is documented for this family. Good low-price candidate to test, not yet proven equal in quality. |
| Alibaba `qwen3.8-flash` (EU scope) | $0.15 | $0.47 | Context caching is listed; exact discount depends on endpoint. | Strict JSON Schema is documented for this family. |
| Alibaba `qwen3.8-flash` (Global scope) | $0.113 | $0.382 | Context caching is listed; exact discount depends on endpoint. | Global and EU prices differ; verify the actual workspace scope before calculating cost. |

## API availability from Ukraine

| Provider | Officially documented status | What remains to verify |
|---|---|---|
| Google Gemini API | Ukraine appears in the official Gemini API / AI Studio available-regions list. | Account eligibility, billing profile and quota for the selected model. |
| OpenAI API | Ukraine appears in the official supported-country list, with certain exceptions. | Account billing, API project limits and model access. |
| Anthropic Claude API | Ukraine appears in Anthropic's supported-regions list, except Crimea, Donetsk and Luhansk regions. | Account verification, payment method and exact model availability. |
| DeepSeek API | Official API docs publish a global API endpoint but no equivalent supported-country list was found in the checked documentation. | Verify account registration, payment and successful API call from the actual bot environment; do not infer availability from the endpoint URL. |
| Alibaba Model Studio | Official model pages list EU (Frankfurt) and Global deployment scopes for relevant Qwen models. | Confirm the user's workspace, base URL, region scope and billing eligibility. No Ukraine-specific access guarantee is inferred. |

## Billing and comparison cautions

- Gemini output pricing includes thinking tokens. Keep candidate output and thinking-token counts separate in logs, but do not double-count them in estimated cost.
- DeepSeek pricing varies by peak/off-peak UTC window and cache hit/miss. A single flat rate is misleading.
- Claude Haiku 5.5's 100K threshold is calculated per prompt and includes cache reads/writes; requests above it use materially higher rates.
- Qwen pricing depends on model family, deployment scope, region and prompt-length tier. The same model ID may have different EU and Global rates.
- Tokenization differs by provider. A price-only calculation using Gemini token counts is a sensitivity estimate, not a measured cost comparison.
- The benchmark runner's private rate file must be keyed by the exact returned billing model ID and the applicable region/tier. If the actual model ID or tariff cannot be resolved, cost must remain unknown rather than being guessed.
- Do not move production traffic based on this table alone. Require blind quality, latency, reliability and cost measurements on identical user scenarios first.

## Official sources

- Gemini pricing: https://ai.google.dev/gemini-api/docs/pricing
- Gemini available regions: https://ai.google.dev/gemini-api/docs/available-regions
- OpenAI GPT-6 Luna model: https://developers.openai.com/api/docs/models/gpt-6-luna
- OpenAI API pricing: https://developers.openai.com/api/docs/pricing
- OpenAI supported countries: https://help.openai.com/en/articles/5347006-openai-api-supported-countries-and-territories
- Claude Haiku 5.5 model and pricing: https://platform.claude.com/docs/en/models/haiku-5-5/overview
- Claude pricing: https://platform.claude.com/docs/en/about-claude/pricing
- Anthropic supported regions: https://platform.claude.com/docs/en/api/supported-regions
- DeepSeek model and pricing: https://api-docs.deepseek.com/quick_start/pricing/
- DeepSeek model updates: https://api-docs.deepseek.com/updates/
- Qwen model pricing: https://www.alibabacloud.com/help/en/model-studio/model-pricing
- Qwen 3.5 Flash capabilities: https://docs.modelstudio.console.alibabacloud.com/en/model-studio/qwen3-5-flash
- Qwen structured output modes: https://docs.modelstudio.console.alibabacloud.com/en/model-studio/qwen-structured-output