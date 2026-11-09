# AliExpress Portals capability matrix

Status: research snapshot for the monetization audit branch, 2026-11-09.

This document separates **documented API capability** from **permission and live-response evidence for this specific app**. A method appearing in public documentation does not prove that the configured Portals app can call it successfully.

## Affiliate / Portals API

| Capability | Documented method or field | Current project evidence | Safe product decision |
|---|---|---|---|
| Affiliate product search | `aliexpress.affiliate.product.query` | Already used by the existing provider | Keep the current provider and its allowlisted product mapping |
| Product-level coupon | `promo_code_info` and related fields in product results | Existing parser/card supports fields returned by the API | Show only when the actual product response includes the value; never synthesize a code or expiry |
| Featured promotion list | `aliexpress.affiliate.featuredpromo.get` | Prior live diagnostic returned HTTP/API success but zero campaign rows | Do not display invented campaign entries; repeat with live credentials before relying on it |
| Products in a named promotion | `aliexpress.affiliate.featuredpromo.products.get` | Requires an exact campaign name and exact start/end values; current list probe returned no campaign to use | Keep the product probe gated until all exact campaign inputs are known |
| Affiliate order report | `aliexpress.affiliate.order.list` | Documented in legacy/deprecated Affiliate API references; previous probe's timezone window was corrected, but the corrected live probe has not yet been run | Keep bounded manual sync and mark API-returned commission fields as estimates; do not claim revenue is confirmed |
| Affiliate link generation | `aliexpress.affiliate.link.generate` | Existing provider supplies affiliate URLs for products; live link-generation capability is not separately audited in this pass | Preserve exact provider-returned links; no URL reconstruction |

Official documentation references:
- [Promotion Creatives API category](https://developer.alibaba.com/docs/api.htm?apiId=48597)
- [Featured promotion list method](https://developer.alibaba.com/docs/api.htm?apiId=48596)
- [Featured promotion products method](https://developer.alibaba.com/docs/api.htm?apiId=48597)
- [Affiliate API reference marked deprecated](https://jaq-doc.alibaba.com/docs/doc.htm?articleId=48596&docType=2&treeId=674)

## Seller marketing API — not a Portals substitute

The public AliExpress API catalog also lists seller marketing methods such as:
- `aliexpress.marketing.redefining.getactlist` — coupon activity query;
- `aliexpress.marketing.redefining.findsellercouponactivity` — seller coupon activity details;
- `aliexpress.marketing.storepromotions.querybyproduct` — seller promotions for a product;
- `aliexpress.marketing.storepromotion.products.query` — products in a seller promotion.

These appear under the separate **AliExpress Marketing Management** category, not the Affiliate / Promotion Creatives category. Their existence does not establish that a Portals affiliate app has seller authorization or may call them. They are therefore **not wired into the bot**, and no attempt should be made to call them using affiliate credentials as a workaround.

Reference: [AliExpress Marketing Management API category](https://developer.alibaba.com/docs/api.htm?apiId=33119&scopeId=12785).

## Current limits and next verification

1. Run the corrected 30-day order diagnostic with the actual app credentials; inspect aggregate counts and per-status errors only.
2. If a campaign appears, record its exact name and exact start/end timestamps before requesting campaign products.
3. Do not turn partner landing pages into claims that their contents are campaign-verified products.
4. Do not present seller coupon APIs as available unless the project has separate seller authorization and a reviewed implementation.
5. Keep the product-level `promo_code_info` path independent from the standalone promo landing page.
