# AGENTS.md

Guidance for AI agents and maintainers working in this repository.

## Environment

- Windows / PowerShell. Use PowerShell commands, not bash syntax.
- Python: `.venv\Scripts\python.exe` (Python 3.14).
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
   pool. Explicit thematic shelves may stop short rather than exceed their
   configured type/family diversity cap; ordinary catalog shelves retain
   their existing refill policy. Additional provider pages remain bounded by
   `max_search_pages`; never
   bypass the positive title-identity gate or add per-product Gemini calls.
8. **Protect secrets.** Keep credentials in the local `.env`; never commit it
   or include secret values in logs/reports.
9. **Protect existing user-facing sections.** Do not change, move, rename,
   reroute, or alter the behavior of the main-menu «Умный поиск» and
   «✨ Находки» flows as a side effect of repository cleanup or work on other
   features. A change to either requires the user's separate explicit approval.
10. **Product-card refresh is a narrow approved UI action.** The explicit
    «🔄 Обновить подборку» callback may re-run the same validated request with
    current/prior refreshed batches excluded. It must not alter Smart Search
    or Findings retrieval, relevance, selector, ranking, or curation logic;
    Findings keeps its dedicated bounded feed path.
11. **Product-image delivery is transport-only.** Keep Telegram's direct CDN
    URL send as the fast path; on failure, allow only bounded HTTPS downloads
    from AliExpress image-CDN hosts, with same-family redirect validation,
    image content-type validation, and strict timeout/size caps.
    When the first product has an image but the source menu message is text,
    send the photo card before deleting/replacing that text message; Telegram
    cannot convert text messages into photo media. Never invent or rewrite
    marketplace image URLs, and never change product selection.

## Test commands

```powershell
.venv\Scripts\python.exe -m pytest -q tests/test_database.py
.venv\Scripts\python.exe -m pytest -q tests/test_price_tracker.py
.venv\Scripts\python.exe -m pytest -q tests/test_bot_startup.py
.venv\Scripts\python.exe -m pytest -q tests/test_convsearch_engine.py tests/test_convsearch_type_validation.py
.venv\Scripts\python.exe -m pytest -q tests/test_search_service.py
```

The authoritative focused and broad configured offline suites are defined in
`.github/workflows/conversational-regression.yml`. The broad suite has explicit
legacy exclusions documented in `docs/LEGACY_TEST_AUDIT_2026-11-15.md`; never
describe it as an unfiltered full-suite run. Install the verified dependency
graph with `pip install -r requirements.lock`.

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

## Feature-specific guardrails

The detailed, still-active rules for catalog subcategory quality, repeat search, thematic retrieval isolation, route limits, shown-history scopes, and diagnostics are maintained in [docs/AGENT_GUARDRAILS_CATALOG_THEMATIC.md](docs/AGENT_GUARDRAILS_CATALOG_THEMATIC.md).

Read that reference before changing any catalog subcategory, repeat-search, thematic collection, or related provider/diagnostic behavior. Moving the rules does not relax them.
