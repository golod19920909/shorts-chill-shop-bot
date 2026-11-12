# AGENTS.md

Guidance for AI agents and maintainers working in this repository.

## Environment

- Windows / PowerShell. Use PowerShell commands, not bash syntax.
- Python: `.venv\\Scripts\\python.exe` (Python 3.14).
- Read `PROJECT_CONTEXT.md` before changing conversational-search behavior.
- Inspect current source and call sites before editing; do not rely on old
  architecture notes or historical logs.

## Golden rules

1. **Preserve Option C.** Gemini interprets each user turn in dialogue context
   and returns a validated structured decision. The conversation engine owns
   state and policy. Deterministic services own AliExpress retrieval, product
   validation, hard constraints, ranking, and Telegram delivery.
2. **No legacy ShoppingAgent loop.** Do not restore the removed full Gemini
   tool loop or give old chat routing / post-hoc curation decision authority.
3. **Systemic fixes only.** Do not add product-specific or phrase-specific
   exceptions to make an individual query pass.
4. **Minimal changes.** Touch only files required by the assigned task. Do not
   chase unrelated legacy test failures.
5. **Verify with offline tests.** Tests must use fakes/stubs and must not call
   Gemini, AliExpress, or Telegram. Report exactly what ran and its result.
6. **Honest data.** Never fabricate products, prices, attributes, availability,
   or affiliate URLs. Gemini may select only IDs in the real candidate pool.
7. **Preserve limits.** Default presentation cap is 30 cards. Gemini candidate
   selection defaults to 20; these are separate limits. Curated subcategory
   shelves are the bounded exception: one selector call may return up to 40
   approved IDs (20 primary + 20 reserve), while final presentation stays at 20.
   Their deterministic finalizer uses the subcategory's configured product-type
   aliases to balance represented types and refill from the already-filtered
   pool. Additional provider pages remain bounded by `max_search_pages`; never
   bypass the positive title-identity gate or add per-product Gemini calls.
8. **Protect secrets.** Keep credentials in the local `.env`; never commit it
   or include secret values in logs/reports.

## Test commands

```powershell
.venv\\Scripts\\python.exe -m pytest tests -q
.venv\\Scripts\\python.exe -m pytest tests/test_convsearch_engine.py -q
```

The repository currently documents stale legacy test modules excluded from
the conversational-regression workflow. Do not describe a filtered run as an
unfiltered full-suite pass.

## Where to look first

| Topic | File |
|---|---|
| Current architecture and decisions | `PROJECT_CONTEXT.md` |
| Conversation state, intent, clarification | `services/convsearch/state.py`, `engine.py` |
| Retrieval adapter and query strategy | `services/convsearch/retrieval.py` |
| Candidate validation and ranking | `services/convsearch/ranking.py`, `type_validation.py` |
| Gemini interpretation and candidate selection | `services/ai_service.py` |
| AliExpress API and provider | `services/marketplaces/aliexpress.py`, `aliexpress_client.py` |
| Telegram integration and card delivery | `handlers/search.py` |
| Provider orchestration and performance diagnostics | `services/search_service.py` |


## Catalog subcategory quality and repeat search (2026-11-12)

- Catalog shelf quality is a shared contract for all configured subcategories;
  do not solve live examples with one-off product/category keyword exceptions.
- Apply the principal-product/accessory role gate both during subcategory
  retrieval filtering and again at the finalizer boundary, after Gemini
  selection and reserve refill. Reserve candidates are not trusted merely
  because they came from the same earlier pool.
- Reuse the category-general validator in
  `services/convsearch/type_validation.py`; keep category-specific product
  types and exclusions in catalog configuration.
- The `🔄 Новый поиск` callback is only for a category subcategory shelf and
  appears on its final card. It must retain the same subcategory, exclude
  prior batches and persistent shown history, prevent concurrent/replayed
  callbacks, and never mutate favorites or price tracking.
- Offline tests must cover accessory/spare-part rejection, legitimate product
  sets, finalizer revalidation, and final-card-only button visibility.

- The bounded real-provider acceptance runner is
  `scripts/validate_catalog_subcategories.py`. Use it only for explicit
  live validation, not in offline pytest; default run checks every configured
  subcategory and writes a JSON report under `reports/`.
