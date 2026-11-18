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
   Thematic shelves scan at most twice the ordinary 80-candidate pool cap,
   balance the Gemini input round-robin across validated canonical types, and
   require at least 75% of configured types (minimum four where available)
   before early stopping. Gemini's thematic primary 20 must be type-balanced;
   its approved reserve may refill only after the same final identity, duplicate,
   family and type checks. Never add unapproved IDs or extra Gemini calls.
   Ordinary catalog shelves retain their existing selection/refill behavior.
   Additional provider pages remain bounded by `max_search_pages`; never
   bypass the positive title-identity gate.
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
11. **Thematic card delivery reuses the Smart Search renderer.** `find_by_subcategory` owns thematic candidate selection and finalization; pass its finished result to `send_search_results(pre_curated=True)` so no second generic curation pass changes the selected list. Keep the shared browse/edit/photo delivery path unchanged.


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
