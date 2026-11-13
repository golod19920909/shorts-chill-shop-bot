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


## 2026-11-12 — Final curation and minimum-size clarification

- Thematic searches now run the shared `CurationGate` in `MODE_CATEGORY` for quality filtering, product-family deduplication, relevance and diversity. They pass `user_id=None` to that gate (`curation_history=False`) so ordinary/global seen-product history is not read; `record_views=False` prevents writing to the global curation novelty history. Thematic browsing is still recorded in the separate `FLOW_THEMES` history and that history is used as the exclusion set across all four collections.
- Both catalog shelf searches and thematic searches enforce the 12-product minimum again after the curation gate. A shelf is not filled with weak or unrelated items when the final eligible set falls below 12.
- Thematic searches are not marked `pre_curated`; they use the existing quality/diversity gate with isolated history. Existing feed/search callers retain their default curation history and pre-curated behavior.
- The latest catalog menu count is 59 visible ordinary shelves across 14 visible parent domains (15 configured; smartphones remains hidden pending live validation). The cycling shelf is also withheld because the current live sample did not support a sufficiently narrow, reliable shelf title.
- No tests, CI, or fresh live API calls were run, per the user's instruction. The local diagnostic script and post-change live catalog report remain pending.


## 2026-11-13 — Static implementation audit and navigation correction

- Recounted the ordinary catalog from source configuration: 60 base shelves + 36 expanded shelves = 96 unique shelf codes. SHELF_PARENT_BY_CODE contains exactly 96 unique assignments; no configured shelf is missing a parent and no mapping points to an unknown shelf.
- The navigation configuration contains 15 parent domains. Exactly 37 shelves are withheld/hidden by the current explicit quality/merge gates, leaving 59 visible shelves across 14 domains. Smartphones remain configured but have no enabled shelf until a verified live assortment exists.
- Confirmed the four approved editorial collections only: unusual useful devices, smart home solutions, little-known gadgets, and budget finds. The budget collection consists of $5/$10/$20 tiers; no automotive editorial collection exists.
- Confirmed that thematic callbacks use FLOW_THEMES, apply the shared category curation gate with ordinary curation history disabled, suppress global novelty writes, and enforce the 12-product threshold again after curation. Budget tiers explicitly reject missing prices, non-USD prices, and prices above their USD ceiling.
- Added handlers/catalog_navigation.py as a catalog/theme-only edit helper. It leaves the current message intact on transient/unclassified edit errors and only attempts send-then-delete replacement for known Telegram BadRequest responses that explicitly indicate the message cannot be edited. The shared search/feed helper was not modified.
- Routed catalog and thematic screens through the catalog-only helper. Added a per-theme in-flight guard so repeated taps cannot launch concurrent searches for the same theme; the existing «Показать ещё» guard remains separate.
- Static review found no remaining common.edit_message_or_replace calls in handlers/discovery.py or handlers/thematic_collections.py; the shared helper in handlers/common.py and the search/feed delivery path were not changed by this correction.
- No tests, pytest, CI, Telegram calls, AliExpress calls, or Gemini calls were run. This is a source-level audit only. The 96-shelf report remains historical evidence from before the latest configuration changes.
- Still blocked for final acceptance: user-local execution of scripts/diagnose_aliexpress_categories.py, a fresh live run of scripts/validate_catalog_subcategories.py, review of the resulting product-level diversity/quality report, and user-authorized offline regression tests. No AliExpress category IDs are verified or configured.


## 2026-11-13 — Strict product-card browsing scope clarification

- The catalog-only strict edit policy also applies after the first card: catalog/theme sessions persist an opt-in strict_navigation flag in their session metadata. Previous/next card browsing and first-card delivery replace the message only for a recognized Telegram edit prohibition or an objectively unsupported text/photo media transition.
- handlers/search.py now exposes strict_navigation=False by default. Only the three catalog category entry points and the two thematic collection entry points opt in. Existing Smart Search, photo search, findings/feed, and other search callers retain the old default path; handlers/common.py remains unchanged.
- The dedicated catalog home callback in handlers/start.py also uses the catalog-only helper. Callback registrations for catalog home, theme menu/tier/item, category, subcategory, and category repeat-search were confirmed in bot/__init__.py.
- This is still source-level review only; no syntax checks, tests, CI, or live Telegram interactions were run.


## 2026-11-13 — Source implementation and live-validation handoff

### Current source state

- The approved 15-parent taxonomy is implemented in the catalog-only navigation layer. The source contains 96 unique ordinary shelf codes and 96 explicit parent assignments. The current menu keeps 59 shelves visible across 14 parents; 37 weak/merged shelves remain hidden. The smartphone parent remains configured but is not shown until a real smartphone shelf is proven by live inventory.
- The full 96-row audit mapping matrix is appended to `docs/catalog-taxonomy-audit-2026-11-12.md`. It includes the required current parent, shelf, code, purpose, unverified AliExpress category ID, recorded overlaps, historical assortment/quality, loss diagnostics, recommendation and current destination.
- The documented Affiliate `category.get` operation is now exposed through `AliExpressClient.get_categories()`; `scripts/diagnose_aliexpress_categories.py` performs a sanitized, read-only diagnostic. It has deliberately not been run. No category IDs are mapped into production configuration.
- The four approved thematic collections are separate from the ordinary catalog and now have their own top-level main-menu entry (`menu:themes`). The catalog keyboard no longer contains a duplicate thematic entry. The automotive editorial collection remains absent.
- Thematic collection browsing retains its independent `FLOW_THEMES` shown-product history, category-mode curation, 12-product post-curation threshold, strict one-message navigation and USD-only budget-price validation.
- Added `scripts/validate_thematic_collections.py`: six live shelf checks (three editorial collections plus the $5/$10/$20 budget tiers), per-stage search/Gemini/curation diagnostics, product-level output, cross-collection and cross-budget-tier duplicate reports, price-validation rejection counts and checked-at timestamps. It does not call Telegram or write user history.
- Updated `scripts/validate_catalog_subcategories.py` to accept optional `--country` and `--currency`, and to run the same category-mode curation gate as Telegram delivery before reporting the final card count. It now distinguishes search-service output from the final post-curation shelf and reports curation-stage losses.
- The category and thematic main-menu entry paths acknowledge their Telegram callback exactly once. The catalog screen and theme screen remain isolated; the protected Smart Search and Findings delivery paths retain their defaults.

### Commands for the user's final live validation

Run from the repository root in PowerShell after local credentials and the virtual environment are available:

```powershell
.venv\Scripts\python.exe scripts\diagnose_aliexpress_categories.py
.venv\Scripts\python.exe scripts\validate_catalog_subcategories.py --max-pages 2 --country UA --currency USD
.venv\Scripts\python.exe scripts\validate_thematic_collections.py --max-pages 2 --country UA
```

The category diagnostic must be reviewed before any category ID is committed. The two validation runners make real Affiliate API and Gemini requests and write JSON reports under `reports/`; they are not offline tests. Change the country/currency flags to the user's intended delivery region and display currency where appropriate. The budget collection always requests and verifies USD.

### Verification status

- No pytest, tests, CI, syntax checks, Telegram calls, AliExpress calls or Gemini calls were run by the executor. This is intentional and required by the user.
- The above commands are prepared for the user's final validation; their results do not yet exist.
- Final acceptance remains blocked on the local category.get response, fresh 96-shelf and thematic JSON reports, product-level review of enabled shelves and cross-theme diversity, and the user's offline regression run.
- Do not interpret the historical 12.11 live report as validation of the latest taxonomy, curation, or navigation code.
