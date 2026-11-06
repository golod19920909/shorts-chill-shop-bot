# LLM gateway comparison — 2026-11-06

Audit date: 2026-11-06. This is research only. No gateway is installed or connected to production.

## Comparison

| Option | Token markup / platform fee | What it provides | Cost and operational trade-offs |
|---|---|---|---|
| Direct provider APIs + current adapter | No gateway fee | Full control over model IDs, provider-native request formats, usage metadata, timeouts and fallbacks. | Lowest dependency count and likely lowest total cost for 2–3 providers. Requires maintaining adapters, per-provider error handling, rate tables and observability. This is the current architecture. |
| LiteLLM self-hosted Proxy | Open-source core; no per-token gateway markup. Infrastructure is paid by us. | OpenAI-compatible gateway for 100+ providers; virtual keys, user/team budgets, routing, retries/fallbacks, per-request cost logs, UI, Prometheus/OpenTelemetry integrations. | Requires a deployed service and database for spend tracking; Redis/cache may be needed for high availability/rate limiting. Adds a network hop and another production component to patch/monitor. Model-cost maps must be kept current; unknown pricing can silently appear as $0 unless alerts are enabled. |
| OpenRouter Standard | 5.5% platform fee on credit purchases; current pricing page also lists a $25K/month BYOK list-price allowance with no fee, then 5% after. | One API for 600+ models/80+ providers, preferred-provider controls, auto-routing, budgets and activity logs. | Fee is charged on credit purchases, not each inference request. The current pricing page is authoritative for the live plan; an older/updated blog post discusses a possible future BYOK fee change, so recheck before adopting. Free tier is 50 requests/day and is not a production allowance. EU in-region routing is Business/Enterprise, not Standard. |
| Vercel AI Gateway | No inference markup; provider list price is passed through, including BYOK. Payment processing fees may apply. | Unified API, model catalog, observability, usage/spend, fallbacks to another provider for the same model, OpenAI Chat Completions/Responses and Anthropic Messages compatibility. | Team accounts receive $5/month in free credits until the account purchases credits; after moving to paid credits, the monthly free credit no longer applies. Adds Vercel account/credit dependency. Some optional capabilities may be metered separately. |

## Relevance to Shorts Chill Shop Bot

### Direct APIs

Best current choice while the project is still measuring Gemini and validating only a small provider shortlist. Existing Gemini-specific adapter already handles provider-native usage metadata and timeouts. Adding one direct OpenAI-compatible adapter for DeepSeek/Qwen plus a separate Anthropic adapter is manageable and avoids paying for a gateway before its value is proven.

### LiteLLM

Strongest gateway candidate if the bot later needs several providers, per-user/team budgets, shared cost dashboards, provider health routing and centralized spend control. Its per-request spend tracking can use token usage and provider-specific pricing metadata. However, the bot already has an application-level usage collector; LiteLLM would duplicate part of this unless it becomes the single source of truth. Do not add both independent cost ledgers without reconciliation.

### OpenRouter

Useful for rapid model discovery and short-lived experiments because one endpoint exposes many models. Not automatically the cheapest production option: credit-purchase fees apply, BYOK limits/fees apply, and provider/region selection must be pinned for repeatable latency and pricing. Avoid its free model tier for production: request limits and model availability are not guaranteed.

### Vercel AI Gateway

Attractive if a unified dashboard and failover are more valuable than owning the provider adapter. It has no per-token markup, but it introduces Vercel as a production dependency and uses pre-purchased credits by default. The bot is a Python Telegram application, so migration must be tested against its existing provider-native JSON modes and usage metadata; a base-URL swap must not be assumed to preserve all request semantics.

## Preliminary decision

1. Do not install a gateway during the current audit. Continue direct provider benchmarking first.
2. If one alternative provider is selected, add it directly behind the existing AIService adapter and preserve the current deterministic AliExpress core.
3. Revisit LiteLLM only if at least three production providers, shared budget controls, or operationally complex failover justify running another service.
4. Use OpenRouter for experiments only if its model catalog materially speeds up the blind benchmark; pin exact provider/model and record the fee.
5. Consider Vercel AI Gateway only if its no-markup spend/failover/observability bundle proves useful enough to justify Vercel credits and dependency.

## Latency and reliability caveat

None of the providers publishes a universal gateway-overhead guarantee applicable to this bot's deployment region. The extra hop must be measured from the same host and with the same prompts. Compare p50/p95 end-to-end time, time-to-first-byte, failure rate and fallback time. Do not infer latency from marketing claims.

## Official sources

- LiteLLM self-hosted gateway: https://docs.litellm.ai/docs/simple_proxy
- LiteLLM spend tracking: https://docs.litellm.ai/docs/proxy/cost_tracking
- LiteLLM logging: https://docs.litellm.ai/docs/proxy/logging
- OpenRouter pricing and plans: https://openrouter.ai/pricing
- OpenRouter platform fee update: https://openrouter.ai/blog/simplifying-our-platform-fee/
- Vercel AI Gateway pricing: https://vercel.com/docs/ai-gateway/pricing
- Vercel AI Gateway overview: https://vercel.com/ai-gateway