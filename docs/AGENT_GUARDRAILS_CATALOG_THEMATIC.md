# Feature-specific agent guardrails — catalog and thematic collections

These rules were moved out of the root `AGENTS.md` on 15 November 2026 to keep the mandatory repository instructions concise.
They remain active. Before editing catalog subcategories, repeat-search behavior, thematic retrieval, or related diagnostics, read and follow every applicable rule below.

---

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

- Subcategory subtype reporting must use only aliases approved by the shared
  principal-product validator; when multiple valid aliases occur in one title,
  the earliest principal identity wins over later bundled add-ons. Keep shelf
  intent/exclusions in catalog configuration, not query-specific code.
- Live catalog reports distinguish provider health from shelf card-count
  status. AliExpress evaluate_rate is a positive-feedback percentage, not
  a 0-5 star rating; preserve it as positive_rate and never derive stars.

- The live subcategory acceptance report includes
  `selection_diagnostics` from `SearchService.last_subcategory_selection_stats`.
  Keep this measurement-only: do not change the one-call selector, 8-second
  timeout, strict Gemini-approved-ID boundary, or deterministic fallback
  semantics just to improve report counts.

## Final catalog completion pass (2026-11-12)

- Catalog subtype matching must not use four-character Russian prefixes: they
  conflate distinct identities such as «велосипедный» and «велофонарь».
  Keep the stricter five-character catalog stem rule local to
  services/subcategory_quality.py; do not change the shared conversational
  services/convsearch/type_validation.py matcher for a catalog-only issue.
- Broad shelves require concrete principal-product aliases. Generic labels
  such as «mini device» / «мини устройство» are not sufficient identity
  evidence. Keep the category intent in configured product types and routes.
- Preserve SearchOutcome.relevance_drop_reasons as measurement-only
  diagnostics and include it in the real 96-shelf acceptance report.
- A larger alias set is not permission to weaken the principal-product gate,
  Gemini-approved-ID boundary, duplicate/family rules, or 20-card target.



## Thematic retrieval isolation (2026-11-13)

- The six editorial thematic shelves may use up to twenty bounded product-type routes (one primary plus nineteen alternates) and at most one additional marketplace page. They stop deepening when the eligible pool target is reached. Ordinary catalog subcategories retain their three-route cap and existing page policy.
- Thematic route phrases are derived from the shelf's explicit canonical product-type groups. Keep every group represented by a route; do not add generic editorial search phrases or expand the global ordinary-catalog limits.
- Thematic shown history uses persistent `popular_shown.flow` scopes in the form `themes:<shelf_code>`. The database's existing `user_id` key provides user isolation. Do not use the legacy shared `FLOW_THEMES` value for new writes.
- Thematic screens display any positive count of validated products (1–20). Do not reintroduce a minimum of 12 or pad with unapproved products. Ordinary callers retain their existing delivery defaults.
- The live thematic diagnostic is opt-in and must never write shown history. It reports base availability; with `--user-id` it additionally reads the user's existing per-shelf history. Do not run it without explicit user authorization because it calls AliExpress Affiliate API and Gemini.
- The thematic diagnostic also reports explicit constraint-loss counts from `SearchOutcome.products_dropped_by_constraints`; for budget shelves this is the max-price constraint stage. Keep this counter observational only.
- Route execution is staged: run the three configured priority routes first, then canonical-type routes only while the eligible pool or minimum cross-type coverage is insufficient. Stop once the pool target and an adaptive represented-type target of 3–6 canonical types based on the shelf's configured type count are present, or when routes/pages are exhausted. The additional page is restricted to routes that returned rows on page one.
- Keep `SearchRequest.retrieval_queries` at its original two-alternate maximum. Thematic-only routes belong in `thematic_retrieval_queries` (max nineteen alternates); do not widen the ordinary request schema to accommodate thematic shelves.
- Thematic route phrases are carried in the private `SearchRequest._thematic_retrieval_queries` attribute, not a public parser/schema field. Preserve the Gemini parse schema and persisted SearchRequest JSON.
- The thematic Gemini selector receives each candidate's deterministic `canonical_product_type`, plus the shelf title/purpose. This enrichment is thematic-only; ordinary subcategory candidate payloads and prompt diversity rules remain unchanged.
- Diagnostic `planned_routes` must list the complete bounded route plan even when early stopping means only a subset was executed; `route_result_counts` and candidate route provenance describe only executed routes.
- The thematic collections brand symbol is `💡` consistently in the main-menu label, thematic shelf buttons, thematic screen/product headers, and the `/start` usage guide. Keep the separate `✨ Находки` and `💸` budget labels unchanged.

## Thematic retrieval performance boundary

- Thematic-only marketplace routes may prefetch at most two routes concurrently. Keep the result-consumption order deterministic and cancel any prefetched successor when the existing eligible-pool/type-coverage early-stop condition fires.
- AliExpress API traffic is globally bounded to two concurrent calls across search, detail and price-tracking operations. Never replace this with unbounded fan-out; if live API rate-limit/short-ban counters regress, return the bound to one.
- Do not weaken the principal-product gate, strict USD budget ceilings, Gemini approved-ID boundary, or type/family diversity to make retrieval appear faster.
- Per-route thematic timing logs are observational and must not include credentials, signatures, user text, or product data.
- Ordinary catalog routes, conversational RAW retrieval, «Умный поиск», «Находки», photo search, favorites and price tracking behavior must remain otherwise unchanged.
