# Gemini cost sensitivity — 20/50/100/500 daily active users

As of 2026-11-06. This is an illustrative sensitivity calculation for the core text-search path only, not a production forecast.

## Observed unit samples

- Mean successful `conversation_turn` on `gemini-3.8-flash`: **$0.002368125** per turn (mean of two observed requests).
- Successful `conversation_selection` on `gemini-3.1-flash-lite`: **$0.00534775** per selection.
- Price-only equivalent selector usage on `gemini-3.8-flash`: **$0.01491525** per selection. This token volume was observed on Flash-Lite; no successful Flash selector sample is available.

Standard API prices used through 2026-12-31:
- Gemini 3.8 Flash: $0.75/M input and $3.75/M output (candidate + thinking).
- Gemini 3.1 Flash-Lite: $0.25/M input and $1.50/M output.
Official source: https://ai.google.dev/gemini-api/docs/pricing

## Explicit daily activity assumptions

| Scenario | Turns per DAU/day | Candidate selections per DAU/day |
|---|---:|---:|
| Low | 2 | 0.5 |
| Base | 3 | 1.5 |
| High | 6 | 3 |

Every DAU is assumed active on all 30 days. “20/50/100/500 users” means daily active users (DAU), not monthly registered users or MAU.

## Current standard rates — through 2026-12-31

| Activity | Selector priced at Flash-Lite: 20 / 50 / 100 / 500 DAU | Selector priced at Flash: 20 / 50 / 100 / 500 DAU |
|---|---|---|
| Low | $4.45 / $11.12 / $22.23 / $111.15 | $7.32 / $18.29 / $36.58 / $182.91 |
| Base | $9.08 / $22.69 / $45.38 / $226.89 | $17.69 / $44.22 / $88.43 / $442.16 |
| High | $18.15 / $45.38 / $90.76 / $453.78 | $35.37 / $88.43 / $176.86 / $884.32 |

## From 2027-01-01 — Gemini 3.8 Flash rates double

The following assumes the same token volumes and activity. Flash-Lite pricing is held unchanged; Flash-priced interpreter/selector requests use the doubled 3.8 tariff.

| Activity | Selector priced at Flash-Lite: 20 / 50 / 100 / 500 DAU | Selector priced at Flash: 20 / 50 / 100 / 500 DAU |
|---|---|---|
| Low | $7.29 / $18.22 / $36.44 / $182.20 | $14.63 / $36.58 / $73.16 / $365.82 |
| Base | $13.34 / $33.35 / $66.69 / $333.46 | $35.37 / $88.43 / $176.86 / $884.32 |
| High | $26.68 / $66.69 / $133.38 / $666.91 | $70.75 / $176.86 / $353.73 / $1,768.63 |

## Formula

For a given DAU and activity scenario:

`monthly_cost = DAU × 30 × (turns_per_day × mean_turn_cost + selectors_per_day × selector_cost)`

## Exclusions and limitations

- Does not include product-menu planning, feed selection, ProductAssistant selection/advice, product questions, photo analysis, category browsing, no-results replies, or other generic `generate` calls.
- Does not include fallback calls, timeouts with unknown provider-side usage, taxes, explicit cache storage, or provider-specific discounts.
- The successful selector sample used Flash-Lite after a 3.8 primary timeout. It is not proof that normal production selection is routed to Flash-Lite.
- The 3.8 selector column prices the same observed token volume at the Flash rate; it is a tariff sensitivity, not an observed successful 3.8 selector request.
- The mean turn cost is based on two observations only. It is not a reliable distribution or p95 estimate.
- Actual activity, selector rate, context length and retry rate must be measured before using this as a budget commitment.
- This calculation is Gemini-only. Alternative provider cost must use each provider's actual tokenization, cache billing and regional tariff.

This model is useful for understanding the sensitivity to activity and the 2027 price change. It must not be presented as the bot's final monthly cost estimate.
