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
