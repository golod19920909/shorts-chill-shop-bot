# AGENTS.md

Guidance for AI agents and maintainers working in this repository.

## Current maintenance scope

The owner has authorized full-repository maintenance: inspect, repair, simplify,
and remove demonstrably unused code across all user-facing sections, including
«Умный поиск», «Находки», categories, photo search, product assistance,
favorites, price tracking, promotions, settings, and admin tools. No section is
protected from changes. Diagnose dependencies before deleting code, preserve
persistent user data, and verify every functional change with regression tests.
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
4. **Systemic but controlled changes.** The owner has authorized a full-bot
   audit and fixes across every section, including «Умный поиск» and
   «Находки». Fix confirmed root causes in the smallest coherent change;
   avoid unrelated rewrites and product-specific patches.
5. **Verify with offline tests.** Tests must use fakes/stubs and must not call
   Gemini, AliExpress, or Telegram. Analyze excluded/legacy test modules rather
   than assuming they are obsolete; preserve useful scenarios in current tests.
   Report exactly what ran and its result.
6. **Honest data.** Never fabricate products, prices, attributes, availability,
   or affiliate URLs. Gemini may select only IDs in the real candidate pool.
7. **Database migration safety.** Test upgrades from representative legacy
   schemas, not only fresh databases. SQLite `ALTER TABLE ... ADD COLUMN`
   cannot use non-constant defaults; backfill existing rows and explicitly
   populate new values where needed. Never drop or reset user tables/history.
8. **Currency integrity.** Never infer a current price currency from an older
   observation. Only trust the currency returned by the provider or a
   documented target currency actually requested. Unsupported target
   currencies must remain explicit and must not be silently converted.
9. **Preserve limits.** Default presentation cap is 30 cards. Gemini candidate
   selection defaults to 20; these are separate limits.
10. **Protect secrets.** Keep credentials in the local `.env`; never commit it
   or include secret values in logs/reports.

## Test commands

```powershell
.venv\\Scripts\\python.exe -m pytest tests -q
.venv\\Scripts\\python.exe -m pytest tests/test_convsearch_engine.py -q
```

The conversational-regression workflow runs a focused offline suite and
then the full remaining repository suite with no test-module exclusions.
When reporting validation, distinguish the focused count from the broad-suite
count and verify that the workflow's head SHA matches the branch being reported.

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


## General assistant mode

- Keep ordinary-chat memory separate from the Option C shopping ledger.
- Route free text through AssistantRouter; pass its validated shopping turn
  into ConversationSearchEngine so Gemini does not classify the same message
  twice.
- General chat must never call AliExpress retrieval or create product cards.
  Product facts and links remain restricted to the verified marketplace path.
- The /new command clears transient general-chat and shopping context only;
  it must not delete favorites, price tracking, or persistent user data.
- Keep general-chat history bounded and short-lived. Do not add permanent
  memory or external tools without a separate design and privacy review.
