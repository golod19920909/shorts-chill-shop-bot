# Shorts Chill Shop Bot — Monetization Architecture

Status: implementation baseline on `audit/aliexpress-portals-smoke-test` (2026-11-08).

## 1. Measurement model

### Product affiliate link requests
- Product-card Buy action is a bot callback. The callback validates the cached product and exact API-provided affiliate URL, writes `affiliate_link_requested` to the existing `analytics_events` table, then sends that exact URL as a Telegram URL button.
- This measures an explicit request for a partner link. Telegram does not send the bot a callback when the user opens a URL button, so this is not a confirmed browser click, visit, order or purchase.
- Do not combine legacy `affiliate_clicked` rows with the new event. They are historical/unproduced event names.
- A future owned HTTPS redirect can measure actual redirect requests only after a stable project domain, TLS, hosting, retention policy and privacy disclosure are approved. It still cannot prove a purchase.

### Orders and commissions
- Use the existing `AliExpressClient` and the documented `aliexpress.affiliate.order.list` method, never scrape the Portals website.
- Manual `/admin → Монетизация → Синхронизировать за 30 дней` sync uses a bounded 30-day PST window, four known statuses, page size 50 and at most two pages per status per run.
- Store one row per `(order_id, sub_order_id)` in `affiliate_orders`, upserting status changes. Store only allowlisted order/product/status/amount/currency/timestamp fields. Never store buyer details, custom parameters or raw API responses.
- Preserve `estimated_paid_commission` and `estimated_finished_commission` verbatim as estimates. Do not convert currencies or call them paid commissions. Status `Completed Settlement` is the platform's settled status; the commission amount fields remain named/labelled as estimated unless a separate authoritative final payout field is returned.
- The API method is documented in legacy/deprecated references and has not yet returned a non-empty row for this app key. The corrected PST diagnostic must be run before claiming live order ingestion works. Empty results are valid empty responses, not proof of a zero-sales month.

## 2. Coupons and discounts

- Per-product `promo_code_info` from a real product listing may be shown on that product's card.
- The standalone `🎟 Промокоды` section remains the configured partner/referral landing page until a supported live coupon feed is verified.
- `🔥 Горячие скидки` remains four fixed partner destinations. It is not a verified per-product discount feed.
- Do not synthesize coupon codes, discount percentages, minimum spends, expiry dates or historical prices.

## 3. Campaign calendar and sale landing pages

- Reuse `config.sales`, `handlers.sales`, the existing `bot_config` table and `/admin → Акции`; do not create a second sales system or admin panel.
- `SaleEvent` holds the known campaign identity and optional default dates. Runtime start/end dates and destination URL live in `bot_config` and can be updated by the existing `manage_sales.py` utility.
- The current Black Friday calendar entry has the known start date `2026-11-19`; no end date is assumed.
- The user-facing sale page is a partner landing page. It may show campaign products only after `featuredpromo.products.get` returns records for the exact campaign name and exact PST start/end values. The campaign list method currently returns a successful envelope with zero rows, so the product probe is gated.
- If campaign product data is unavailable, keep the partner landing link; do not imply that ordinary search products participate in the campaign.

## 4. Consent-based cross-sell

- Future cross-sell must be opt-in: ask whether the user wants complementary items before running a second search.
- Reuse the existing conversational search, strict product identity/evidence checks, AliExpress provider and existing result-card delivery. No separate recommendation engine or hidden automatic product injection.
- Do not send repeated unsolicited promotions. The user must be able to decline and continue the original flow unchanged.
- This block is not yet implemented; it is deliberately not represented as complete.

## 5. Admin

- Extend the existing `/admin` only. Current sections include statistics, discounts, sales, AliExpress API, database and the new `Монетизация` section.
- The new section reports link requests, imported order rows by status, latest sync time and raw API commission estimates for recent rows.
- API diagnostics distinguish callable methods with empty data from permission/transport failures.

## 6. Database and server operations

- `analytics_events` remains the source for bot-side events. `affiliate_orders` is the separate, idempotent ledger for API-returned order line items.
- `scripts.database_maintenance` uses SQLite's online backup API, integrity checks, retention and guarded offline restore with a pre-restore copy.
- `deploy/shopbot-backup.service` and `.timer` schedule a daily backup at 03:15 server-local time after installation.
- Production server installation, restore drill and automated code-update/rollback deployment are not yet verified. Do not claim the systemd timer is active until installed on the actual server.

## 7. Explicit exclusions

- No Premium subscription, Telegram Stars, paid limits, payment flows or restrictions on free users.
- No fake orders, commissions, discounts, campaign membership or click-through totals.
- No changes to the Gemini model architecture or PR #10 implementation as part of this monetization branch.

## 8. Release gates

1. Run corrected Portals diagnostics with the live app key; inspect aggregate output only.
2. Review the draft branch CI results and focused tests.
3. Install the backup timer on the production server and perform a backup + restore drill before enabling automated deployments.
4. Complete opt-in cross-sell only after the user-facing flow is tested.
5. Keep this branch separate until PR #10's integration base is settled; do not merge to `main` from this branch.

## 9. AliExpress API capability boundary (2026-11-09)

- The separate capability matrix is maintained at `docs/ALIEXPRESS_PORTALS_CAPABILITY_MATRIX.md`.
- Affiliate Promotion Creatives documents featured-promotion list/products methods; the last app-specific list probe returned success with zero rows. Product lookup remains gated on an exact returned campaign name and exact dates.
- The public API catalog also lists seller Marketing Management coupon/store-promotion methods. These are a separate API category and are not evidence that the Portals affiliate app has seller authorization. They are not wired into this bot.
- Product-level `promo_code_info` remains the only coupon-like product data currently surfaced from the affiliate product response. No standalone coupon-code feed is assumed.
- Seller marketing endpoints must not be called with Portals credentials as an undocumented workaround.


## 10. Affiliate link requests from favorites

- Favorites now expose the same Buy action only when the persisted product snapshot contains a valid affiliate URL.
- The callback identifies the item by marketplace + product id, re-reads the current user's favorites, validates the stored URL and records `affiliate_link_requested` before returning the exact URL.
- The metric remains a request for a partner link; Telegram does not confirm that the URL button was opened. This does not measure actual click-through or purchase.
