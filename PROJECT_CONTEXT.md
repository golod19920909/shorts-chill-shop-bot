# PROJECT_CONTEXT

Fast orientation for any agent/human working in `c:\shorts-chill-shop-bot`.

## Current maintenance state (2026-11-17)

- Repository-only work branch: `chore/repository-cleanup-2026-11-15`; do not
  merge to `main` or move the production updater branch without separate
  approval.
- **Protected Smart Search algorithm remains at baseline**
  `3822fc95bed11070ba8f46c02ce4cdf69cc58af5` for retrieval stopping,
  candidate-pool semantics, route order, ranking and Gemini continuation.
  Earlier P0.1/P0.2 algorithm changes were reverted; do not reintroduce them
  without separate explicit approval. The opt-in selector snapshot privacy
  fix is diagnostic-only and does not change the Gemini prompt or search flow.
- SQLite schema migrations 001-003 are atomic and tested on temporary DBs for
  legacy upgrade, idempotence, rollback, foreign keys and delete semantics.
  No local or production database was opened or modified.
- Python 3.14 dependencies are pinned in `requirements.lock`; CI checks lock
  consistency, `pip check`, production compile and manual smoke syntax.
  Gemini operation telemetry separates interpretation, selection,
  continuation and no-results reply, with observed p50/p95.
- The primary conversational regression workflow is hardened with
  `actions/checkout@v7`, `actions/setup-python@v7`, read-only `contents: read`,
  and `persist-credentials: false`.
- Latest verified branch-head CI (17 Nov 2026):
  [#44014369963](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/44014369963)
  on `e28a608ee6f62fefb3bd5267b9b7002fe54ef29d` — PASS. Focused offline suite:
  **690 passed, 1 deselected, 604 subtests**; configured broad offline suite:
  **2,309 passed, 2 deselected, 1,259 subtests**. Dependency lock consistency,
  `pip check`, production compile and live-smoke syntax compile passed. One
  upstream Google GenAI SDK deprecation warning remains. The latest test-only
  addition covers concurrent SQLite writes through independent connections.
- Configuration tests explicitly clear admin IDs, analytics dashboard and
  price-tracking environment variables and cover safe defaults, parsing,
  dashboard port bounds and disabled tracking interval; they are included in
  the passing focused suite.
- `.env.example` now describes Gemini as the AI feature provider (not only
  natural-language parsing), documents the dashboard's HTTPS-only public
  exposure rule in one place, and has no duplicate dashboard block. This is
  documentation/template cleanup; runtime settings are unchanged.
- Focused CI now selects reviewed classes from mixed modules rather than
  excluding every useful test with the whole file: discovery favorites/text,
  category-country, menu wiring, shown-history, relevance and engine
  instrumentation; Option C session isolation; Gemini telemetry; and
  reviewed AIService contracts. See `docs/LEGACY_TEST_AUDIT_2026-11-15.md`
  for exact class-level boundaries.
- The broad suite remains intentionally filtered with explicit module and
  test exclusions. It is **not** an unfiltered full-suite run. Historical
  Agent/chat-router/temporary-runner tests remain preserved but excluded;
  do not restore removed entrypoints or add blanket xfails/skips.
- Live diagnostics are explicitly marked API-costing and are not run in CI.
  `reports/` and editor backup patterns are ignored; the obsolete tracked
  `tests/eval_b5_runner.py.bak_b6` backup was removed.
- A real 70-card AliExpress selector golden set with independent manual labels
  remains pending. The opt-in capture hook and blind replay validator are
  ready, but no qualifying reviewed snapshots exist. Do not make claims about
  gaming-laptop model quality or compare selector models without this evidence.
- Ordinary Telegram acceptance is still pending: new search, clarification,
  budget refinement, alternatives, product switch, accessory mismatch and
  honest no-results. These cannot be proven by offline mocks alone; no JSON
  preparation is required from the owner.
- The branch is not merged into `main` or the production updater branch.
  No local `.env`, SQLite file, PC environment, backups or user data were
  accessed, changed or uploaded.

- The manual live smoke runner `smoke_conversational_search.py` now supports
  `--query` for one controlled phrase and `--search-only` to avoid
  unnecessary follow-up/comparison model calls. It can run with
  `SHORTS_CHILL_SELECTOR_CAPTURE=1` to create the exact pre-selector raw
  candidate snapshot. Its metrics include SearchService retrieval time,
  provider route/page counts, Affiliate API calls/retries/latency, Gemini
  operations/tokens/known cost and candidate-stage counts. `--repeat 3`
  is opt-in only; default is one pass.
- Offline tests for the live-smoke metric helpers are in
  `tests/test_live_smoke_metrics.py` and are selected in focused CI. They
  exercise fake clients only and never call AliExpress, Gemini or Telegram.
- The 70-card golden-set process is now documented with a low-cost direct
  Option C capture command in `tests/eval_selector_capture_schema.md`.
  Raw captures still require independent provenance verification, page-depth
  annotation and human gold labels before any selector replay. No real capture
  has been run and no model-quality conclusion is available yet.
- Latest live-smoke measurement code and the custom-query/search-only capture
  path are covered by the latest passing CI. `tests/test_live_smoke_metrics.py`
  runs offline with fakes only; it does not consume provider quota.


## What this is

A Telegram shopping-assistant bot (Russian UI): AliExpress Affiliate API
search, the «✨ Находки» feed, hot discounts, promo codes, photo search,
favorites, price tracking. Core invariant: **honest data** — the bot never
invents products, prices, links, or AI results.

## Stack

- Python 3.14 venv at `.venv\Scripts\python.exe` (Windows, PowerShell)
- `python-telegram-bot[job-queue,rate-limiter]` 22.8, pydantic 2.13.5, httpx 0.28.1, `aiosqlite` 0.22.1
- `google-genai` 2.24.0 (Gemini; all calls in `services/ai_service.py`)
- Tests: pytest over unittest-style suites, fully offline (fakes, no network)

## Layout

```
handlers/    UI + scenarios (search.py = engine entry + product cards)
services/    shared infrastructure (search_service = provider adapter,
             ai_service = Gemini client, conversation = search_history helper,
             product_assistant, price_tracker, ...)
services/convsearch/  THE conversational search engine (authoritative)
services/marketplaces/  AliExpress provider + TOP-protocol client
curation/    feed pipeline (5 stages) + CurationGate
database/    SQLite schema + async wrapper
config/      env settings, catalog, promo, hot discounts
tests/       offline unit tests (pytest)
```

## Commands

```powershell
# run the bot
.venv\Scripts\python.exe main.py

# addressable offline checks
.venv\Scripts\python.exe -m pytest -q tests/test_database.py
.venv\Scripts\python.exe -m pytest -q tests/test_bot_startup.py
# authoritative focused/broad configured suites:
# .github/workflows/conversational-regression.yml
# broad CI is filtered; see docs/LEGACY_TEST_AUDIT_2026-11-15.md
```

## Conversational search architecture (authoritative, since 2026-10-28)

The search decision-making of this bot lives ENTIRELY inside
`services/convsearch/` — an adaptation of
https://github.com/Elinengu/anything-conversational-search (utterance ledger +
slot provenance + separate retrieval/validation/ranking stages), re-targeted
from the reference's Amazon benchmark at this bot's real AliExpress provider
and Telegram interface. One engine turn:

```
Telegram text (handlers/search.py::search_message)
  -> ConversationSearchEngine.handle_message(user_key, text, ...)
       Gemini interprets the turn against recent turns + active slots
         -> validated action enum + optional core product type
         -> deterministic fallback on timeout / invalid output
       state.observe(text, intent_override=validated_action)
       state.record_slots(...)        # deterministic slots + provenance
       (COMPARE / ALTERNATIVES resolved over the ledger, no retrieval)
       build_retrieval_strategy(...)  # ledger -> ONE SearchRequest phrase
       RetrievalBackend.retrieve()    # explicit adapter -> SearchService RAW
       _evaluate_pool()               # type gate -> required features / compatibility -> exclusions -> budget
                                      #   -> dedup -> rank_candidates
                                      #   -> per-stage count report
       -> ONE SearchDecision
  -> handlers/search.py::_deliver_decision   # renders, never re-decides
```

### Intent classification (hybrid Option C)

For every text turn, `ConversationSearchEngine` first asks Gemini to
interpret the message against the last six user turns, active slot ledger,
shown-result count and last outcome. The response is a validated JSON object
with a closed action enum, confidence, optional core product type, one
clarification question or a bounded natural reply. The `respond` outcome is
limited to general conversation and concepts; it cannot carry product facts,
search slots or product identity. Non-clarify actions below 0.65 confidence,
malformed output, timeouts (bounded to 8 seconds), quota/auth errors and
missing Gemini all fall back to the deterministic classifier. Gemini never
returns product cards or bypasses any deterministic validation.

`observe()` remains the deterministic fallback and authoritative slot-ledger
transition. It also accepts only the engine's validated action enum as an
override; slot extraction, budget/exclusion parsing, retrieval, type gate,
ranking and Telegram rendering remain deterministic.

A model-proposed product type is accepted only when its distinguishing head
noun is grounded in the leading requested-item phrase, before a relation target
such as «для дома», «на дрель» or «по дереву». A word appearing only in a
compatibility/purpose phrase is not sufficient grounding. When the user
explicitly names the requested item, that item remains authoritative: Gemini
may normalize its wording/inflection, but cannot replace it with a relation
target mentioned in the same sentence (for example, an attachment's host tool).

When the model is unavailable, `observe()` classifies each message from its
own evidence, in order:

1. **COMPARE** — a question about the ONGOING conversation: the message
   carries a closed-class question word («какой», «чем», "which"…) or a
   compare/explain marker, AND introduces no content token outside the
   active ledger (`state._is_about_current`). A question that DOES name a
   new product («какой ноутбук лучше?») is not a compare — it falls
   through to search.
2. **ALTERNATIVES** — an alternation marker («еще», «другой", "more"…)
   whose content words are all known context or scaffolding. An
   alternation with a NEW product name («покажи другие наушники») is a
   NEW_SEARCH.
3. **CORRECTION** — a reversal marker supersedes the affected slot kinds.
4. **NEW_SEARCH** — the span's own evidence says it names a different
   product: no stem overlap with the active type, not pure framing, not
   connector-introduced, not a negation.
5. **REFINE** — anything else that adds a constraint, budget, colour,
   connector-led qualifier or comparative suffix.
6. **AMBIGUOUS** — a message that is none of these.

Word classes live in closed-set grammars (question words, alternation
markers, comparative suffixes `_COMPARATIVE_SUFFIXES`, discourse markers
`_DISCOURSE_MARKERS`, colours, count/budget framing words) — productive
rules, not product- or query-specific phrase lists.

A pending clarification answer must continue the existing ledger even if
Gemini labels it as a refinement or is unavailable. The deterministic
product-type fallback keeps the leading item phrase and excludes relation
targets introduced by prepositions («Пылесос для дома» → «пылесос»;
«Насадка на дрель по дереву» → «насадка»). A reply containing only a
purpose/fitment phrase does not invent a product type; the bot asks again.

### Components (`services/convsearch/`)

| File | Responsibility |
|---|---|
| `contract.py` | `Outcome` enum (RESULTS / NO_ELIGIBLE / NO_CANDIDATES / PROVIDER_ERROR / CLARIFY / COMPARE / INSUFFICIENT_INFO) and the `SearchDecision` the Telegram layer renders. |
| `state.py` | `ConversationEngineState`: the ONE authoritative conversation ledger. `Turn` (utterance + provenance), `Slot` (type, soft use_case, hard compatibility, required feature, exclusion, budget, preference; each with provenance/status), intent classes, budget/currency parsing, negation→exclusion, reversal detection, pending clarification outcome/question. Per-user instance stored in `context.user_data["_convsearch_state"]`; the Telegram handler checks the 30-minute idle TTL before refreshing it, so expired ledgers are not revived. |
### Product-type gate regression safeguard

The requested product head is the rightmost noun-like token, not blindly the
last token in the user's phrase. Trailing descriptive adjectives (for example,
«фонарик карманный») must not replace the product identity «фонарик». Protect
both adjective orders with category-general tests.

The type validator must distinguish a listing that IS the requested product from
a different item whose title only mentions that product in a genitive phrase.
For example, «переключатель сварочного аппарата» is a switch, not a welding
machine, even though the title contains both words. Keep this category-general
and protect it with negative accessory-title tests plus positive real-product
title tests; never add a welding-machine-specific keyword exception.

When the title is a generic bundle or otherwise does not establish the
requested identity, but its title contents or description mention the requested
type, keep the listing only for bounded semantic review; this evidence is not
enough for an automatic identity pass. An explicit title identifying a
different accessory or spare part still wins over a description that mentions
the requested host product.

Provider limitation: the current AliExpress product.query normalizer maps the
documented title and commercial fields but does not populate Product.description.
Description-backed review therefore applies only when a provider actually
supplies that field; it is not evidence of live description enrichment for
ordinary AliExpress search results. Do not claim that the bot currently reads
full AliExpress product descriptions unless the detail API mapping is separately
verified and implemented.

Russian «по X» application/material phrases are captured as soft use-case
context just like «для X». They are carried into retrieval and semantic
selection, but are not treated as a mandatory literal title feature.

A soft use-case phrase is not a hard title filter: missing an exact word in a
listing does not by itself disqualify a candidate. The phrase remains context
for retrieval, ranking and Gemini's semantic decision. This is intentionally
different from explicit accessory compatibility and mandatory product
features, whose missing evidence is quarantined until semantic confirmation.

### Accessory compatibility is a hard requirement

For generic accessory product roles (attachments, cases, adapters, cables and
similar fitment items), an explicit fitment relation «X на/под Y» is stored as a
separate `compatibility` slot, not as soft use-case context. Compatibility
is carried into the provider query and checked against title/description
evidence. Ambiguous «для X» phrases stay soft context in the deterministic fallback
because X can be either a host device or an application/material; Gemini may
promote a clearly identified device relation to `compatibility`. Gemini may
also add compatibility from a later conversational follow-up; when it does,
the same phrase is not retained as a duplicate soft use-case slot. Missing evidence is quarantined for semantic review; it is not
silently accepted. Ordinary purpose phrases such as «подарок на свадьбу» stay
soft context because the requested item is not an accessory role.

### Principal-item evidence in mixed marketplace titles

A requested type that appears only after a comma/semicolon/pipe in a title is
not a deterministic identity match. Marketplace listings sometimes concatenate
multiple unrelated items into one title; a later mention can be incidental.
Such candidates are marked for bounded Gemini semantic review and remain
quarantined if that review times out or is unavailable. A type present in the
primary title clause can still pass the deterministic gate. This rule is
general across product categories and must not be implemented as product-name
exceptions.

A requested accessory mentioned only as something the host product «comes
with» («дрель с насадкой», «телефон с чехлом») is not the listing's principal
product. Leading descriptive adjectives remain valid («аккумуляторная
насадка»). This is a generic relation check, not a product-specific exception.

### Comparative follow-ups

Comparatives such as «полегче» are soft preferences, not hard requirements.
Persist them as active preference slots and add a canonical search term to the
provider query (for example, «сварочный аппарат легкий»), while preserving the
product type and budget. Do not reject listings solely because their titles
omit the preference: AliExpress titles often lack reliable weight/specification
metadata, so the bot must not claim it verified an unlisted attribute.

| `engine.py` | `ConversationSearchEngine.handle_message` — the single search decision-maker for one message: observe → record slots → (COMPARE/ALTERNATIVES resolved from the ledger) → build strategy ONCE → retrieve → validate/filter/dedup/rank → one `SearchDecision` with a per-stage count report. Clarification policy lives here (strategy-less or insufficient-info turns ask ONE question, cached in `pending_clarify`). |
| `retrieval.py` | `RetrievalBackend` + `build_retrieval_strategy`. The ONLY path to the provider: builds the request `SearchRequest` from the ledger (never via legacy parsers) and calls `SearchService.find_products(..., raw=True, raw_broaden=True)` — up to three grounded query variants across bounded documented pages: every phrase that returns rows on a page is paginated independently, while empty/failed routes are retired; route rankings preserve page order and are fused with deterministic Reciprocal Rank Fusion (RRF) before deduplication; no legacy relevance, ranking or curation is applied. The strategy builder composes the phrase from the type + compatibility + feature spans MINUS what the type already covers, plus new constraint tokens of THIS message minus budget/count/qualifier/colour/discourse framing — never a literal echo of the user's sentence. Also owns post-retrieval required-feature, exclusion, hard-budget filters and shown-id exclusion. |
| `ranking.py` | `validate_candidate` / `score_product` / `rank_candidates`: deterministic, requirement-aware ranking. Feature/compatibility evidence, budget fit, popularity (log-scaled orders) and `relevance_tier` are inputs; a violated exclusion or a failed product-type gate is a HARD rejection that no weight can outrank. Missing evidence is neutral (never treated as proof of presence). |
| `type_validation.py` | The hard product-type gate: principal-product vs accessory/incidental-mention, decided from the candidate's own title structure (head noun, «для …» role clauses, accessory head words, stem matching). Category-general; no per-product keyword patch. |
| `text.py` | Tokenisation / constraint-span extraction (reference `text.py` contract, rebuilt for RU/EN mixed marketplace queries). |

### Conversation state

`ConversationEngineState` (one per user, in `context.user_data`) holds:

- the turn list (every user message, with an `excluded` flag for declines);
- the slot ledger: product type, soft use-case context, required features,
  exclusions, budget and preferences — each with `source_turn` and
  `active | superseded` status;
- `shown_product_ids` (what this user already saw, for «покажи другие»);
- `last_outcome` and `last_question` (the pending clarification state).

`handlers/search.py` also keeps `CONVERSATION_KEY = ConversationState` with
the last effective `SearchRequest`, so non-search features (product
assistant, favorites-context) keep reading the same request the engine
produced. There is no second conversational source of truth: the ledger owns
WHY, the `ConversationState` request is the artifact the engine returned.

Use-case follow-ups («для дома», «for workshop», «по дереву») are stored as
soft `use_case` context and join the retrieval phrase / Gemini selection
context, but are NOT mandatory title matches. A suitable household listing
must not be rejected merely because its seller omitted «для дома». Explicit
fitment introduced by «под» (for example, «под iPhone 15») remains a required
feature checked against listing evidence. These are refinements of the active
product, not product switches. A COMPARE question
(«какие лучше?») is answered over the SHOWN candidates (via
`ProductAssistant`), never re-searched; with nothing shown the engine asks
one clarification instead of searching the literal question. Feature questions
that match real listing-title evidence re-deliver the selected existing
products through the unchanged Telegram card renderer; explanatory questions
remain text answers. Both paths use the cached result set and make no provider
search.

### Retrieval → AliExpress

`RetrievalBackend.retrieve` asks the EXISTING `SearchService` (its provider
auth, retries, rate limits, normalization and dedup stay untouched) with
`raw=True, raw_broaden=True`: the service gathers provider rows for the primary phrase and up to two additional user-grounded variants on one provider page, then fuses all collected route rankings with deterministic RRF before returning the RAW pool. Inside `find_products`, `raw=True` skips
**everything** decision-shaped — including the per-page `_collect` pass whose
relevance gate, constraint filter and sort used to run before `_collect_raw`
(the live symptom: legacy `Relevance filter …` / `Pipeline stages …` log
lines for engine turns). Only identity dedup and the documented retrieval
cap run in `_collect_raw`. Feed / category / discovery / photo flows keep
using the ordinary (non-raw) pipeline unchanged.

`_evaluate_pool` reports the REAL pool size after EVERY stage
(`after_shown` → `after_type` → `after_feature` → `after_exclusion` →
`after_budget` → `ranked`) plus the rejection counts of each gate, so a `no_eligible`
outcome always names the stage that emptied the pool
(`rejected_by_type_gate` / `rejected_by_required_features` /
`rejected_by_exclusions` / `rejected_by_budget` / `all_previously_shown`) instead of reporting a
bare zero. Ranking no longer re-counts budget as a score weight (budget
enforcement is the FILTER in step 2; `WEIGHTS["budget"] = 0.0`).
`filter_by_type` (the legacy stem-only duplicate of the type gate) was
removed — `type_validation.validate_product_type` is the one gate.

A valid empty candidate-ID list from Gemini is an explicit semantic
no-match, not a failed response. The engine must return no eligible products
rather than silently restoring the deterministic pool. Only an unavailable,
malformed, or timed-out selector response may use the documented deterministic
fallback (with candidates requiring semantic review still quarantined).

### Selector latency and fail-closed behavior (2026-10-30)

The conversational selector receives only the deterministic top 20 candidates
(the user-facing browse/presentation cap remains 30), with bounded real
descriptions included in the evidence payload. If Gemini returns fewer than
20 valid candidates, that partial selection is authoritative and must not be
padded with unseen tail rows. Append up to ten deterministic tail candidates
only when Gemini fills the full 20-item selection window. Its orchestration
timeout is 8 seconds rather than 3 seconds. If semantic selection times out,
errors or returns an unusable response, the engine falls back only to the
deterministic subset that has already passed the product-type, required-feature,
compatibility, exclusion and budget gates. Candidates explicitly quarantined
for semantic identity/feature review remain hidden. This applies equally when
negative constraints are active: the selector cannot override them, and the
fallback never restores candidates rejected by the deterministic exclusion gate.

### Semantic selector constraint context (2026-10-30)

The bounded Gemini candidate selector receives the active exclusion slots and
hard-budget slot in addition to product type, required features, and soft
preferences. Its prompt treats explicit exclusions and budgets as mandatory.
For a negated product class, absence of the forbidden word is not proof that a
candidate belongs to an allowed alternative class; an unknown class is not
selected by the model. Deterministic type, feature, exclusion, and budget gates
remain authoritative, and the selector can only return IDs from the supplied
candidate pool.

### AliExpress provider failure propagation (2026-10-30)

The AliExpress provider still returns a list-compatible result to preserve the
marketplace interface, but API failures carry a safe `provider_error` marker.
`SearchService` records those calls as failed rather than successful empty
searches. The exact documented empty-result response remains a normal empty
list; other business/API errors must surface as provider failures in the
conversational engine and must not be reported as "nothing found".

### Conversational hard-constraint enforcement (2026-10-30)

Active feature slots are mandatory constraints: a candidate must carry positive
evidence in its title or description before it continues. Comparative
preferences remain soft signals and are not hard-filtered. Hard budgets require
a known price and an explicit matching currency; missing prices, missing
currencies, and currency mismatches cannot pass. The RAW conversational
retrieval path now keeps route rankings separate and fuses them with
deterministic Reciprocal Rank Fusion (RRF), not route-order concatenation.

### Conversational hard-constraint enforcement (2026-10-30)

Active feature slots are mandatory constraints: a candidate must carry positive
evidence in its title or description before it continues. Comparative
preferences remain soft signals and are not hard-filtered. Hard budgets require
a known price and an explicit matching currency; missing prices, missing
currencies, and currency mismatches cannot pass. The RAW conversational
retrieval path collects all bounded grounded routes and fuses their rankings
with deterministic Reciprocal Rank Fusion (RRF), not route-order concatenation.

### LLM integration (bounded, honest)

Gemini participates in the ACTIVE conversational search flow in two
validated roles:

1. `AIService.decide_conversation_turn` interprets EVERY user message
   against the recent turns and active slot ledger. It returns only a
   closed action (`new_search`, `refine`, `correction`,
   `alternatives`, `compare`, `clarify`), confidence, an optional
   product type for a new search, and one clarification question. The
   engine validates the response; malformed, uncertain, timed-out or
   unavailable calls fall back to the deterministic state machine.
2. After deterministic type/exclusion/budget filtering and ranking,
   `AIService.select_conversation_candidates` may select up to 20
   candidate IDs from the real candidate pool. It receives only real
   product IDs, titles, prices, ratings, order counts and the active
   query/feature context. The engine maps returned IDs back to the
   original objects; invented IDs are discarded. A valid empty selection
   means no supplied candidate fits. A timeout/error/unusable response
   keeps the deterministic ranking, with an explicit diagnostic marker.

Gemini never creates products, prices, ratings, affiliate links or
attributes. The deterministic ledger, provider retrieval, hard filters,
budget enforcement and Telegram delivery remain authoritative. Candidate
selection is bounded by `AI_CONVERSATION_SELECTION_TIMEOUT_SECONDS = 15.0` and cannot block the turn indefinitely.
When a product name spans materially different use classes, the turn
prompt instructs Gemini to ask one high-value use-case question rather
than silently choosing a class (for example, household/garage vs
industrial welding equipment).

### Delivery / outcomes

`handlers/search.py::_deliver_decision` renders ONE decision:

- `RESULTS` → the existing card flow (`send_search_results`, already-ranked
  list, `pre_curated=True` so no hidden stage re-filters);
- `CLARIFY` → the engine's question, cached in `pending_clarify`; the next
  message is combined with the original request and re-entered into the
  engine;
- `COMPARE` → answered over the SHOWN cached set via `ProductAssistant`;
- `PROVIDER_ERROR` → honest outage text (never "nothing found");
- `NO_CANDIDATES` / `NO_ELIGIBLE` → honest empty text;
- engine exception → outage text, logged with the reason.

Result cap: the engine's own `RESULT_CARD_LIMIT = 30`; a user-stated count
(«покажи 5») is carried as `result_limit` and applied as a ceiling.

### Legacy components REMOVED (2026-10-28)

These no longer exist in the active call graph:

- `handlers/search.py`: `_search_with_context`, `_route_conversation`,
  `run_agent_turn`, `_agent_search_adapter`, agent history
  (`AGENT_HISTORY_KEY` / `AGENT_HISTORY_LIMIT`), the Gemini tool-calling
  turn and its imports (`chat_router`, `query_intent.apply_intent`,
  `parse_query_intent`, `resolve_request`, `search_diagnostics`,
  `services.relevance.STOPWORDS`).
- `services/agent_service.py` (`ShoppingAgent`, tool declarations,
  `run_tool`) and `services/agent_loop.py` (the bounded Gemini
  function-calling loop) — deleted; nothing imports them.
- The legacy text-search path through
  `AIService.parse_query` → `query_intent` → `resolve_request` →
  `filter_relevant` → `sort_products` → feed AI-selection: replaced by the
  engine turn. `SearchService.find_products` still exists (feed / category /
  photo / assistant use it) but text search no longer enters it except
  through `RetrievalBackend` with `raw=True`.
- `services/chat_router.py` intent/route layer — no longer imported by any
  handler; the clarify TTL now lives in `handlers/search.py`
  (`CLARIFY_TTL_SECONDS`).

Deliberately RETAINED (shared infrastructure, not search decisions):
`services/search_service.py` (provider adapter, feed, categories, photo,
assistant retrieval), `services/relevance.py` (only `relevance_tier` is
consumed, as ONE documented ranking input), `services/conversation.py`
(`ConversationState` holder + the count-only helper used by the
count/currency cache path), `services/text_matching.py`
(`extract_requested_result_count`), `services/product_assistant.py`
(answers COMPARE turns), `services/ai_service.py` (Gemini client for the
assistant and feed selection), curation, favorites, price tracking,
analytics, admin.

## Known limitations

- The reference repository's evaluator / Amazon dataset / reranker model
  are NOT ported (different catalogue, no external model dependency) — the
  adaptation keeps its architecture (ledger, strategy, validation,
  deterministic ranking) and replaces its data layer.
- Gemini interprets every user turn in the recent dialogue and active-slot
  context, returning a validated structured action and (for a new search) a
  proposed product type. The engine validates that proposal and owns the
  conversation transition. Deterministic code remains authoritative for slot
  provenance, budgets, exclusions, compatibility, product-type hard gates,
  ranking, AliExpress retrieval and Telegram delivery. A separate bounded
  Gemini selector may choose IDs only from the already retrieved real pool;
  on timeout/error, only the deterministic candidates that passed every hard
  gate remain eligible, while semantic-review candidates stay quarantined.
  Both Gemini calls have independent bounded timeouts and deterministic
  fallbacks.
- Automated tests were NOT written or run for the 2026-10-28 migration
  (explicit task constraint). Runtime behaviour below was verified with
  OFFLINE source-level smoke runs against a FAKE retrieval backend (no
  network); the real AliExpress path, Telegram rendering, COMPARE answers
  over `ProductAssistant` and the pending-clarify round-trip still need
  MANUAL verification by the owner.
- Currency remains a display/filter preference; no exchange rates exist.

## Conventions

- Russian user-facing text; comments/docstrings in English are fine.
- All agent final reports, task summaries, implementation notes, test
  reports and explanations must be written entirely in English. Code,
  identifiers, filenames, commands and user-facing application text stay
  in their original language.
- Honest-data invariant: never fabricate products, prices, links or
  "AI results"; a provider error is never reported as an empty catalogue.
- Callback data carries only `(session_id, index)`, never product data.

## Historical decision log

The dated implementation notes and prior live-test findings have been preserved verbatim in [docs/PROJECT_CONTEXT_HISTORY_2026-11.md](docs/PROJECT_CONTEXT_HISTORY_2026-11.md). Consult that archive when an earlier decision, regression, or implementation rationale is needed. Keep new current-state rules in this file; append dated historical records to the archive rather than allowing this orientation document to grow without bound.

## Current repository handoff — 2026-11-15

- Active cleanup branch: `chore/repository-cleanup-2026-11-15`.
- Baseline: `3822fc95bed11070ba8f46c02ce4cdf69cc58af5`.
- Do not merge this branch into `main` or move the production updater branch without explicit approval.
- Production updater branch `fix/gemini-35-flash-lite-tuning` remains at the rollback baseline; the user's PC, local `.env`, SQLite database, and runtime environment must not be updated as part of this repository-only task.
- The main-menu «Умный поиск» and «✨ Находки» flows remain protected. The user authorized only the narrow fix for the undefined `empty_keyboard` in the narrowing follow-up; this is not blanket permission for further behavior changes.
- Verified narrowing-fix CI: focused offline suite **503 passed, 1 deselected, 582 subtests passed**; configured broad offline suite **2,284 passed, 2 deselected, 1,259 subtests passed**. The broad workflow excludes a set of legacy modules; do not call it an unfiltered full-suite run.
- CI still reports non-failing pytest-asyncio event-loop-scope and SQLite worker-thread warnings. Investigate separately and avoid mixing unrelated fixes into protected flows.


- Repository documentation is split into a concise root guide plus preserved references: historical decision log `docs/PROJECT_CONTEXT_HISTORY_2026-11.md`, feature-specific catalog/thematic guardrails `docs/AGENT_GUARDRAILS_CATALOG_THEMATIC.md`, and preliminary CI exclusion audit `docs/LEGACY_TEST_AUDIT_2026-11-15.md`.


- Gemini telemetry CI: current usage extraction, turn scope, generate instrumentation, concurrency isolation, and persistence test classes are included in the focused offline workflow; historical AgentLoop tests remain excluded.


- Gemini telemetry timeout test now reflects the established one-reserve-retry behavior: two SDK attempts (primary and reserve) are recorded when both time out. This was a test-only correction; no production fallback behavior changed. Commit `2d9b4b8b`; CI rerun pending.


- Fixed a false-positive test in `tests/test_revision_ux.py`: a coroutine test method with no `await` was defined under `unittest.TestCase` and was never awaited. It is now a normal synchronous test so its assertions execute. Test-only change, commit `ff8b668d`; CI pending.


- Verified CI run `43742522032` on commit `17a156fc`: production compile PASS; manual smoke syntax PASS; focused offline **517 passed, 1 deselected, 582 subtests passed**; configured broad offline **2,284 passed, 2 deselected, 1,259 subtests passed**.
- Test-only correction `ff8b668d` changes an un-awaited async UX test into a synchronous test; GitHub Actions run `43742653098` passed on that commit (focused **517 passed**, broad configured **2,284 passed**; 582 and 1,259 subtests respectively).


- Scope correction: an audit found earlier unauthorized algorithm changes in `services/convsearch/engine.py` (retrieval stop and empty-selector continuation). The file and `tests/test_convsearch_engine.py` have been restored exactly to baseline `3822fc95` (commits `0d8e3ced`, `73aba910`). Only the user-authorized `empty_keyboard` narrowing-follow-up fix remains in protected Smart Search code. No further search algorithm, ranking, candidate-pool, or Gemini continuation changes without explicit approval.


- Post-restoration CI run `43743099397` on `77be512d`: production compile and manual smoke syntax PASS; focused suite **1 failed, 515 passed, 1 deselected, 582 subtests passed**; configured broad suite **1 failed, 2,282 passed, 2 deselected, 1,259 subtests passed**. The single failure is a pre-existing baseline Smart Search test reason mismatch (`unverified_required_features` expected vs `rejected_by_type_gate` actual). Do not change protected algorithm/assertion without explicit approval.


- Catalog category delivery fix: `handlers/search.py` now imports `edit_catalog_message_or_replace`, which was already used when category results fell below the minimum. Targeted `CategoryCountryPropagationTests` now pass; this is catalog navigation only, not Smart Search/Findings behavior.
- Latest verified CI on code commit `2c749777` (run `43743673727`): compile and smoke syntax PASS; focused **533 passed, 1 baseline failure, 584 subtests**; configured broad **2,282 passed, 1 baseline failure, 1,259 subtests**. The sole failure remains the protected baseline Smart Search reason mismatch.


- Added `pytest.ini` with explicit `asyncio_default_fixture_loop_scope = function`. CI run `43743967660` confirms the pytest-asyncio implicit-loop-scope deprecation warning is gone; SQLite worker-thread and Google GenAI SDK warnings remain for separate investigation. Test counts unchanged: focused **533 passed + 1 baseline failure**; broad configured **2,282 passed + 1 baseline failure**.


- Test lifecycle cleanup: two `tests/test_bot_startup.py` cases called `_post_init` (installing global Gemini telemetry persistence to a temp SQLite file) but omitted `_post_shutdown`. They now drain writes and detach the writer in `finally` before temp DB deletion. Test-only commit `3a134e21`; CI pending.

- Verified CI run `43744173805` (commit `3a134e21`): focused **533 passed, 1 baseline failure, 584 subtests**; configured broad **2,282 passed, 1 baseline failure, 1,259 subtests**. SQLite worker-thread / closed-loop warnings are now zero; pytest-asyncio loop-scope warning is zero. Only remaining warning is Google GenAI SDK deprecation.

- 16 Nov follow-up: legacy-test audit confirmed nine excluded historical modules
  are not current CI candidates: they import removed Agent/chat-router modules
  or dynamically load missing `tmp_g252d_*` experiment scripts. They remain
  preserved, not deleted; do not restore those retired entrypoints. Details:
  `docs/LEGACY_TEST_AUDIT_2026-11-15.md`.
- Verified current cleanup-branch CI run
  `43857002754` on commit `aa1bea772190f85f4f6bfabaf2a47d796b963f53`:
  production compile, manual live-smoke syntax compile, lock consistency and
  `pip check` PASS; focused offline **644 passed, 1 deselected, 598 subtests**;
  configured broad offline **2,295 passed, 2 deselected, 1,259 subtests**.
  One upstream Google GenAI SDK deprecation warning remains.
- The CI run exposed only a test-log selection bug in
  `EngineInstrumentationTests`; test-only fix selects the explicit
  `convsearch action=` summary line. No production behavior changed.
- The successful broad suite remains configured/filtered, not unfiltered:
  historical Agent/chat-router/temporary-runner modules remain excluded and
  preserved. See `docs/LEGACY_TEST_AUDIT_2026-11-15.md` for exact boundaries.


- The manual-only legacy diagnostic workflow was also hardened: checkout/setup-python upgraded to `@v7`, token restricted to `contents: read`, and checkout credentials are not persisted. It remains `workflow_dispatch` only so diagnostic suites do not run on every push. Commit `b3065f8`; the workflow itself has not yet been manually dispatched after this update.


- Final CI verification after primary and diagnostic workflow hardening: branch HEAD `3793d4bedd0cafe297124a25dfbe491967250e1c`, run [#43892353377](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/43892353377) — SUCCESS. Focused **689 passed, 1 deselected, 604 subtests**; configured broad **2,308 passed, 2 deselected, 1,259 subtests**. Dependency lock, `pip check`, production compile and live-smoke syntax checks passed. One upstream Google GenAI SDK deprecation warning remains. This does not manually execute `.github/workflows/legacy-test-audit.yml`; that diagnostic workflow remains manual-only.


- 16 Nov diagnostic workflow precision: `.github/workflows/legacy-test-audit.yml` now selects `WiringGuardTests` instead of the mixed legacy `test_search_intent.py` module, selects `ConversationSelectionEvidenceTests` for selector capture opt-in/privacy coverage, and selects only `EngineInstrumentationTests` from `test_latency_and_failures.py` (the other class depends on the retired chat router). Commit `2945aac842dd622e576f058a34db58dce8baa83f`. The manual-only workflow has not yet been dispatched; no test result is claimed for this change.


- Final branch-head refresh — 16 Nov 2026: branch HEAD is
  `322b31a75bbe42cea74bdf05b97abac66d7331f2`. CI run
  [#43910671976](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/43910671976)
  was inspected at job-log level and confirms the focused and configured broad
  counts above. The latest change only corrected the manual diagnostic workflow
  target selection and documented it. The workflow
  `.github/workflows/legacy-test-audit.yml` is still
  `workflow_dispatch` only and has not been manually dispatched; its isolated
  matrix tests therefore remain unverified. Do not infer their success from
  the primary CI run.


- README maintenance follow-up — 16 Nov 2026: commit
  `7481f98212ca0859b763c176e7160032e746ff2f` updates the repository tree to
  include the current `services/convsearch`, scripts, deploy assets, docs,
  workflows and lockfile. It also replaces the stale blanket
  `unittest discover` recommendation with the same targeted offline pytest
  checks already documented above. No runtime code or user-facing flow changed.
  CI for this README-only commit is pending at the time of this note.

### Linux deployment and SQLite backup documentation review — 16 November 2026

- Проверены README deployment instructions against
  `scripts/deploy_update.sh`, `scripts/database_maintenance.py`,
  `deploy/shopbot-backup.service` and `deploy/shopbot-backup.timer`.
- Исправлен порядок установки: создание systemd service и установка backup
  units выполняются администратором; приложение и guarded update — от
  `shopbot`. Ранее README предлагал выполнять `sudo cp` уже после перехода
  к `shopbot`, до описания sudoers setup, что могло завершиться отказом в
  правах. Добавлены явные команды установки systemd units, безопасный режим
  `.env` (0600), проверка sudoers и разделение ролей.
- Уточнены backup instructions и обновлена устаревшая CI-сводка в README.
- Commit: `b9865b2f5c4dffb333e040b6270ad73a54198149`.
- CI run на README commit ещё требуется проверить; на предыдущем HEAD
  `e857e69` CI run `43927807717` — SUCCESS: focused **689 passed,
  1 deselected, 604 subtests**; configured broad **2,308 passed,
  2 deselected, 1,259 subtests**; compile, live-smoke syntax, lock consistency
  and `pip check` passed. One upstream Google GenAI SDK deprecation warning.
- No deployment was performed. No server, local PC, `.env`, SQLite database,
  `main`, or production updater branch was touched.

- Дополнительно исправлен комментарий в `requirements-dev.txt`: он ошибочно
  утверждал, что весь тестовый набор можно запускать через стандартный
  `unittest` без pytest. Текущие тесты используют pytest fixtures и
  monkeypatch; файл теперь описывает реальную установку dev-зависимостей.
  Commit: `5d3383aa5838d58822fef7ceada74f67e1011f5c`.
- CI для README deployment update был запущен (run `43931387907`) и на момент
  этой записи выполнялся; более поздние commits README context / requirements
  также требуют проверки CI на актуальном HEAD.

- 17 Nov follow-up: README Linux deployment/backup instructions and
  `requirements-dev.txt` pytest guidance were corrected. Commits:
  `b9865b2f5c4dffb333e040b6270ad73a54198149`,
  `5d3383aa5838d58822fef7ceada74f67e1011f5c`; context follow-ups:
  `58b8eca702ac686d7934941e4279cb3d451d9b59`,
  `ce058b3bd8f3a7ac973c06ae433f59f9cea2abd2`. CI run
  `43931472972` passed on the latest branch HEAD.

- 17 Nov documentation consistency correction: `docs/LEGACY_TEST_AUDIT_2026-11-15.md`
  previously overstated the effect of general cleanup approval and implied
  protected P0 retrieval/continuation algorithm changes were currently
  authorized. The audit now explicitly records that those changes were
  reverted to baseline and require separate concrete approval. Commit:
  `ef8bdbd96f282899ac66ea1586c39f49e61aefa4`.
- CI run `43931472972` passed on prior HEAD
  `ce058b3bd8f3a7ac973c06ae433f59f9cea2abd2`; documentation-only commits
  `8784af9` and `ef8bdbd` are newer and their CI is pending. Do not claim
  their CI result until the new run completes.

- README Gemini reserve-model description was checked against
  `services/ai_service.py::_generate`. It incorrectly claimed reserve was
  skipped for account-wide quota/rate limits; implementation makes one reserve
  attempt for classified quota/rate-limit errors, then stops if reserve also
  fails. README now describes this accurately. Commit:
  `6e768b12317a4a47e50ed3d16034b53fce3cffb5`.


## Product-card refresh action — 17 Nov 2026

- User explicitly authorized adding «🔄 Обновить подборку» to every product-card browsing screen, including protected «Умный поиск» and «✨ Находки» flows. This is a narrow UI/action addition only; their retrieval, relevance, ranking, selector, and curation algorithms remain unchanged.
- The shared card keyboard carries a short pref:<session_id> callback on every card. The refresh handler reuses the cached validated SearchRequest, preserves section metadata, and excludes persistent shown history plus the current and prior refreshed batches. It serializes refreshes and rejects stale/replayed callbacks.
- «✨ Находки» uses its own bounded feed-page refresh and existing feed cursor/history; it does not switch to keyword search. Photo refresh repeats the grounded query without adding broad query variants. Thematic «Показать ещё» remains a distinct action and shares the refresh exclusion scope.
- The change is made only on chore/repository-cleanup-2026-11-15; local PC, .env, SQLite, main, updater branch, and production deployment are untouched. Awaiting offline CI and user live Telegram acceptance.
