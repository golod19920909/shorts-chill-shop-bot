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
   selection defaults to 20; these are separate limits.
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
