# Stage 3 — Current catalog implementation review (2026-11-12)

## Status

**Source review: completed for the files listed below. Offline test execution: not performed in this environment. Credentialed live validation: blocked pending execution in the user's local Windows environment. No production catalog code has been changed.**

This report is intentionally not an acceptance claim for the 15-category redesign. Stage 3 remains open until offline regression results and live AliExpress evidence are available.

## Approval baseline

The user explicitly approved the complete 15-parent taxonomy and all seven decisions in Section 5 of `catalog-taxonomy-proposal-stage-2-2026-11-12.md`. That approval authorizes validation and implementation after the gates below. It does not authorize guessed provider category IDs or weakening product identity, Gemini-approved-ID, deduplication, or diversity rules.

## Files reviewed

- `AGENTS.md`
- `PROJECT_CONTEXT.md`
- `config/catalog.py`
- `config/subcategories.py`
- `config/subcategory_expansion.py`
- `config/subcategory_type_overrides.py`
- `services/search_service.py`
- `services/marketplaces/aliexpress.py`
- `services/marketplaces/aliexpress_client.py`
- `config/settings.py`
- `scripts/validate_catalog_subcategories.py`
- `tests/test_catalog_subcategories.py`
- `tests/test_subcategory_quality.py`
- `tests/test_subcategory_search.py`
- `tests/test_subcategory_navigation.py`
- `.github/workflows/conversational-regression.yml`

## Findings

1. **The approved taxonomy is not implemented yet.** `config/catalog.py` still defines the legacy 12 parent categories: electronics, home, auto, computers, games, clothing, beauty, tools, sport, pets, kids, garden. The approved target contains 15 parents and changes several ownership boundaries. The current subcategory configuration still uses the old parent codes.
2. **The current shelf configuration is still the old 96-shelf catalogue.** The three subcategory configuration modules and their lookup/composition logic must be reconciled against the approved mapping before menu changes. The approved hierarchy document provides the proposed mapping, but it is not yet runtime configuration.
3. **The existing live shelf runner is available.** `scripts/validate_catalog_subcategories.py` calls the production `build_search_service(settings)`, checks all configured shelves by default, caps page depth, and writes a JSON report under `reports/`. It reports provider health, raw/filtered/final counts, type distribution and selector diagnostics. It is an actual live API/Gemini operation, not an offline test.
4. **The category API method is not integrated.** `services/marketplaces/aliexpress_client.py` declares `METHOD_CATEGORY_GET = "aliexpress.affiliate.category.get"`, but the reviewed provider/client path does not expose a category-tree retrieval/normalization operation. Therefore no real category IDs or parent relationships can currently be obtained through a repository command. Do not guess them or copy category names as IDs.
5. **Existing tests cover important current contracts**: catalog callback resolution, keyboard navigation, last-card-only repeat search, category route dispatch, relevance gates, accessory rejection and finalizer revalidation. These tests do not yet prove the approved 15-parent hierarchy, old-code migration, duplicate-free placement across all 15 parents, or preservation of existing user sessions/history after category moves.
6. **The CI workflow contains focused catalog regressions and a filtered repository suite**, but the GitHub commit status endpoint returned no checks for the documentation-only approval commits. This is not evidence that tests passed or failed.
7. **No safe live test can be run from the GitHub-connected workspace.** The local `.env`, AliExpress credentials and Windows `.venv` are not available here. No secrets were requested or copied into GitHub.

## Required local validation before implementation

Run from the repository root in PowerShell, with the local `.env` loaded by the normal project settings:

```powershell
.venv\Scripts\python.exe -m pytest -q tests/test_catalog_subcategories.py tests/test_subcategory_quality.py tests/test_subcategory_search.py tests/test_subcategory_navigation.py
.venv\Scripts\python.exe scripts/validate_catalog_subcategories.py --max-pages 2
```

The second command performs real AliExpress Affiliate API and Gemini requests across the currently configured shelves and may take a substantial amount of time / consume API quota. Keep the generated JSON report local; share the report file, not `.env` or credentials.

Separately, a small credential-safe category-tree diagnostic must be added and reviewed before calling `aliexpress.affiliate.category.get`. It must use the existing signed client, request only documented parameters, store the actual response shape and category IDs/parent IDs, and redact all credentials/signatures. No category mapping should be committed until the live response is inspected.

## Implementation gates

- [ ] Focused offline catalog regression command passes.
- [ ] Current 96-shelf live run is reviewed for inventory quality, diversity, provider errors and repeatability; weak shelves are not padded.
- [ ] Credential-safe `category.get` diagnostic confirms the real API response and usable category hierarchy, or documents a genuine permission/API limitation.
- [ ] Approved 15-parent hierarchy is mapped to stable internal codes; every retained shelf has exactly one parent unless the user explicitly approves otherwise.
- [ ] Callback/session/history migration plan is tested before removing or changing old IDs.
- [ ] Regression tests prove `Умный поиск` and `✨ Находки` behavior/contracts remain untouched.

Until these gates are satisfied, do not modify catalog menu/configuration or enable candidate shelves.


## 2026-11-12 — Implementation follow-up (source changes; validation still pending)

- Reviewed the latest user-provided live report: `catalog_subcategory_live_20261112_132853.json` (96 shelves; 57 returned 20 cards, 37 were short, 1 provider error, 1 empty). Of these, 69 reached the minimum of 12 final products. This does not by itself prove diversity: the source report also exposed broad shelves whose 20-card output was dominated by a single narrow type or had wrong-family items.
- Applied a conservative visible-menu gate: merged/duplicate shelves are hidden; shelves below 12 products, provider-error/empty shelves, and shelves with materially misleading product-family output remain disabled pending a fresh live run. The current configuration exposes 60 ordinary shelves across the validated parent domains; the new smartphone domain is configured but omitted from the keyboard until its own live assortment is proven.
- Reworked the parent configuration to the approved 15 domains: Smartphones and accessories; Computers and office; Electronics and gadgets; Home, kitchen and household; Tools and repair; Auto and moto; Sport, tourism and leisure; Beauty and personal care; Kids; Pets; Clothing and shoes; Games and gaming devices; Toys, hobbies and creativity; Watches and jewelry; Garden and dacha. The ordinary automotive catalog remains; no automotive editorial collection exists.
- Moved the existing gaming, sportswear, toys/creative, watches/jewelry and streaming/microphone shelf ownership toward the approved single-parent taxonomy. Consolidated broad duplicate menu rows (audio, portable gadgets, table lamps, dash cameras, dog/cat-wide shelves, PC/console controller splits, gaming peripherals, woodworking tools and outdoor furniture) without deleting their historical identifiers.
- Narrowed labels/aliases for live output that was materially more specific than its former title (laptop stands/docks, auto shampoo/polish, bicycle lights, school pencil cases, bike computers/heart-rate straps, pet clickers, routers/network adapters, electric pruning shears and garden hand tools). Webcam-dominated streaming and low-volume skin-care/beauty shelves remain disabled until retested.
- Added exactly four thematic collections: unusual useful devices; smart home solutions; little-known gadgets; useful small items by budget. The budget collection has USD ceilings of $5/$10/$20. Each thematic search enforces the existing product-identity pipeline, a 12-product minimum and an explicit USD price check where applicable. The three editorial themes are separate; a shared thematic history prevents the same item from cycling between themes.
- Added a distinct `themes` history flow. Thematic sessions do not read/write the ordinary catalog search history or the global curation novelty history. The existing search service remains the retrieval/identity/selection engine (Option C); no ShoppingAgent loop was introduced.
- Catalog and thematic entry/back/home/product browsing paths were updated to edit the active Telegram message. Product cards now replace the current catalog screen in place; when Telegram cannot edit across incompatible media types, the fallback sends the replacement first and only then best-effort deletes the old message. «Умный поиск» and «✨ Находки» search/feed behavior was not changed.
- Added `AliExpressClient.get_categories()` and `scripts/diagnose_aliexpress_categories.py` as a read-only, credential-safe diagnostic. It writes only normalized `category_id`, `category_name`, `parent_category_id` and response metadata; it does not log credentials/signatures or apply guessed IDs. The diagnostic has **not** been run. No AliExpress category IDs have been committed.
- No pytest, CI, offline tests, or new live API calls were run during this implementation, per the user's instruction. The user-provided JSON was reviewed as existing evidence; the new category-tree diagnostic and a fresh live shelf run still need to be run locally before final acceptance.


## 2026-11-12 — Isolation correction for protected search flows

- The 15-parent taxonomy is now isolated in `config/catalog_navigation.py`, not in the shared `config/catalog.py` or shared search-category metadata. The original `config/catalog.py`, the original 96 ordinary `Subcategory` definitions, `config/subcategory_expansion.py` and `config/subcategory_type_overrides.py` were restored to their pre-implementation contents. Only new theme-specific subcategory codes were added to the lookup registry; `get_all_subcategories()` still returns the original 96 catalog shelves.
- Catalog navigation uses display-only copies returned by `get_catalog_subcategories()`; callbacks resolve the original `Subcategory` object before invoking SearchService. Reparenting and display-title changes therefore affect only the catalog UI and do not change Smart Search, conversational search, photo search, product identity aliases or the existing search ranking inputs.
- The category menu contains 15 configured parent domains. The smartphone domain is intentionally hidden until a verified live assortment exists. After applying the live-result minimum, merge rules and the additional cycling/streaming/skin-care quality holds, 59 ordinary shelves are currently visible across 14 domains.
- Shared main-menu and discovery helpers were restored for protected flows. Catalog and thematic screens use a dedicated `catalog:home` callback, catalog-only edit/replacement helper, and catalog session metadata. Non-catalog product cards retain their existing `menu:back` callback and prior replacement ordering; `✨ Находки` and Smart Search are not routed through the catalog-only home/edit helpers.
- The optional `max_price` argument on `SearchService.find_by_subcategory` defaults to `None`; only the three budget theme tiers pass a value. Existing catalog and protected search calls therefore retain their original request parameters.
- No tests or live API calls were run during this isolation refactor, per the user's instruction. A fresh local live run is still required after the UI mapping and theme routes are in place.
