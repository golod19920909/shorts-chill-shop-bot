# PROJECT_CONTEXT

Fast orientation for any agent/human working in `c:\shorts-chill-shop-bot`.

## What this is

A Telegram shopping-assistant bot (Russian UI): AliExpress Affiliate API
search, the «✨ Находки» feed, hot discounts, promo codes, photo search,
favorites, price tracking. Core invariant: **honest data** — the bot never
invents products, prices, links, or AI results.

## Stack

- Python 3.14 venv at `.venv\Scripts\python.exe` (Windows, PowerShell)
- `python-telegram-bot[job-queue]` 22.8, pydantic 2.13.5, httpx 0.28.1, `aiosqlite` 0.22.1
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

# focused and broad filtered regression suites are defined in
# .github/workflows/conversational-regression.yml
.venv\Scripts\python.exe -m pytest tests -q
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
with a closed action enum, confidence, optional core product type and (only
for clarification) one question. Non-clarify actions below 0.65 confidence,
malformed output, timeouts (bounded to 5 seconds), quota/auth errors and
missing Gemini all fall back to the deterministic classifier. Gemini never
returns product cards or bypasses any deterministic validation.

`observe()` remains the deterministic fallback and authoritative slot-ledger
transition. It also accepts only the engine's validated action enum as an
override; slot extraction, budget/exclusion parsing, retrieval, type gate,
ranking and Telegram rendering remain deterministic.

A model-proposed product type is accepted only when its distinguishing head
noun is grounded in the user's current message. When the user explicitly names
the requested item, that item remains authoritative: Gemini may normalize its
wording/inflection, but cannot replace it with a relation target mentioned in
the same sentence (for example, an attachment's host tool).

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

The type validator must distinguish a listing that IS the requested product from
a different item whose title only mentions that product in a genitive phrase.
For example, «переключатель сварочного аппарата» is a switch, not a welding
machine, even though the title contains both words. Keep this category-general
and protect it with negative accessory-title tests plus positive real-product
title tests; never add a welding-machine-specific keyword exception.

When the title is generic but the product description names the requested
type, keep the listing only for bounded semantic review; description text is
not enough for an automatic identity pass. An explicit title identifying a
different accessory or spare part still wins over a description that mentions
the requested host product.

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

### Comparative follow-ups

Comparatives such as «полегче» are soft preferences, not hard requirements.
Persist them as active preference slots and add a canonical search term to the
provider query (for example, «сварочный аппарат легкий»), while preserving the
product type and budget. Do not reject listings solely because their titles
omit the preference: AliExpress titles often lack reliable weight/specification
metadata, so the bot must not claim it verified an unlisted attribute.

| `engine.py` | `ConversationSearchEngine.handle_message` — the single search decision-maker for one message: observe → record slots → (COMPARE/ALTERNATIVES resolved from the ledger) → build strategy ONCE → retrieve → validate/filter/dedup/rank → one `SearchDecision` with a per-stage count report. Clarification policy lives here (strategy-less or insufficient-info turns ask ONE question, cached in `pending_clarify`). |
| `retrieval.py` | `RetrievalBackend` + `build_retrieval_strategy`. The ONLY path to the provider: builds the request `SearchRequest` from the ledger (never via legacy parsers) and calls `SearchService.find_products(..., raw=True, raw_broaden=True)` — up to three grounded query variants on one provider page; route rankings are fused with deterministic Reciprocal Rank Fusion (RRF) before deduplication; no legacy relevance, ranking or curation is applied. The strategy builder composes the phrase from the type + compatibility + feature spans MINUS what the type already covers, plus new constraint tokens of THIS message minus budget/count/qualifier/colour/discourse framing — never a literal echo of the user's sentence. Also owns post-retrieval required-feature, exclusion, hard-budget filters and shown-id exclusion. |
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
one clarification instead of searching the literal question.

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
descriptions included in the evidence payload. Its orchestration timeout is
8 seconds rather than 3 seconds. If semantic validation times out or fails
while a negative constraint is active, the engine returns no candidates
instead of silently showing products whose compliance with that exclusion
could not be established. Without active exclusions, the deterministic
validated ranking remains the fallback.

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
selection is bounded by `AI_CONVERSATION_SELECTION_TIMEOUT_SECONDS = 8.0` and cannot block the turn indefinitely.
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
- Intent classification, constraint extraction and candidate evaluation are
  deterministic closed-set grammars, NOT an LLM. Gemini is only asked (at
  most) to phrase ONE clarifying question for a very short strategy-less
  message. This keeps the turn fast, offline-safe and auditable; a fuller
  LLM reasoner would need its own validation layer and quota budget.
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

### Conversational ranking request context (2026-10-30)

The live conversational engine now passes the exact RetrievalStrategy.request built from the active conversation ledger into ranking_mod.rank_candidates(..., request=...). This is required: the ranking module's optional SearchRequest argument drives the existing services.relevance.relevance_tier() and relevance_score() inputs. A missing request silently disables that relevance contribution while leaving the other deterministic score components active. Keep this propagation covered by an offline engine regression test whenever the ranking call or strategy contract changes. The optional argument remains optional for non-conversational compatibility callers.

### Negated refinement must not broaden the positive query (2026-11-02)

When the user excludes a product class in a follow-up (for example,
«Только не полуавтомат» while searching for a welding machine), the excluded
term is a negative constraint only. It must not be appended to the positive
AliExpress retrieval phrase or to `required_features`. Otherwise the provider
is asked for the forbidden class and can return an empty pool before the
post-retrieval exclusion filter has a chance to act. The retrieval strategy
builder now removes active exclusion tokens (using the same stem comparison
as the rest of the conversational query builder) from newly captured
constraint words. A focused offline regression test covers the full ledger →
strategy path. GitHub Actions run 41385685439: 95 focused tests passed.

### Product identity: exact vs semantically ambiguous heads (2026-11-02)

The deterministic product-type gate must not treat a shared modifier as proof
of product identity. For example, «сварочный стол» is not automatically a
«сварочный аппарат» merely because both titles contain «сварочный». However,
a different head noun can be a legitimate product variant («сварочный
полуавтомат»), so rejecting every title without the literal requested head
would also be wrong.

Current handling is deliberately two-stage:

- Clear principal-product evidence continues through the deterministic gate.
  Clear accessory / genitive-role mentions continue to be rejected there.
- A candidate whose title contains the request's type wording but has a
  different, non-accessory head is marked
  `type_identity_requires_semantic_review` and receives a low deterministic
  score. It is not considered a verified match.
- The bounded conversation selector decides whether that real listing is
  the same product / an acceptable variant for the active query. It receives
  the candidate's real fields and the full active constraints.
- If the selector is unavailable, errors, or times out, candidates marked for
  semantic review are removed; exact deterministic matches may remain. The
  legacy synchronous `shape_pool` also fails closed on these ambiguous rows.
- The selector can only return IDs from the supplied real candidate pool.

Regression coverage includes «сварочный стол» vs «сварочный аппарат»,
«сварочный полуавтомат» as a candidate requiring semantic confirmation,
successful semantic confirmation, and timeout fallback preserving exact
matches while removing ambiguous candidates.

This is not a per-category alias dictionary. Do not turn semantic ambiguity
into a growing list of product-specific keyword exceptions.

### Conversational exclusions must reach query-variant generation (2026-11-02)

Option C stores active follow-up exclusions in `SearchRequest.excluded_features`.
The legacy query-variant builder originally guarded derived phrases only with
`intent_excluded_product_types`, which is populated by the older query-intent
parser. Because the conversational request's `original_query` is the latest
user turn, a derived phrase could be built from «Только не полуавтомат» and
reintroduce «полуавтомат» even though the primary normalized phrase correctly
remained «сварочный аппарат».

`services.query_variants._excluded_stems` now combines both exclusion
sources, and the derived-noun guard runs when either source is populated.
The Option C regression test builds the real conversation ledger and
retrieval strategy, then passes that exact request through
`product_query_variants`; it asserts that no derived variant contains the
excluded «полуавтомат».

The AliExpress API's `resp_code=405` with `The result is empty` is an
empty-result response in the observed client behavior, not evidence by itself
of an API transport/authentication failure. Do not label this specific
response as an outage without inspecting the raw response contract. The
current change addresses the unsafe derived query; it does not claim that
every empty result after a negative refinement is solved.

GitHub Actions run 41390656114 on commit 332b268:
101 focused offline tests passed in 1.26s. Full repository suite and live
AliExpress/Telegram behavior are not claimed as verified.

### Option C state-integrity repair (2026-11-02)

A dedicated draft PR (#4, `fix/option-c-state-integrity`) is based on the current
PR #3 head; it does not modify `main` and does not merge PR #3.

**Additive exclusions must not be treated as broad corrections.** Gemini may
classify a message such as «Только не полуавтомат» as `correction`. The
conversation ledger now adds the new exclusion without superseding unrelated
required features or earlier exclusions. If the negated phrase directly
contradicts an active positive feature, only that matching feature is
superseded (using the existing stem matcher). A budget correction still
supersedes only the previous budget. Do not restore the old blanket
`feature + exclusion` supersession behavior.

**Clarification state is now explicit in the ledger.** `ConversationEngineState`
stores `last_outcome` and `last_question`; the turn planner receives these as
`last_outcome` and `pending_question` context on the next message. A
clarification outcome records its question, while a completed search or
provider failure clears the pending question. This lets Gemini interpret the
next user message as an answer to the actual pending question rather than
seeing only a list of user turns. The Telegram handler still owns the
`pending_clarify` TTL and combines the original request with the answer;
the complete live Telegram round-trip is not yet verified.

**Regression coverage / verification:** the focused GitHub Actions workflow
passed on the draft PR:
- commit `eb30f0d`: 102 passed in 1.54s (additive exclusion regression);
- commit `44372f4`: 103 passed in 1.24s (pending clarification context).
These are focused offline tests on Python 3.14.9, not the full repository
suite and not live Gemini/AliExpress/Telegram tests.

**Remaining work:** validate structured slot updates from Gemini; test the
Telegram clarification round-trip against the real handler; reconcile the
30-card browse cap with the separate 20-candidate AI selection rule; and
resolve stale legacy test collection before claiming a full-suite pass.
PR #4 remains a draft and must not be merged yet.

**Use-case context is a separate SOFT slot.** The general span splitter
intentionally drops one-word fragments, so the ledger's connector grammar
captures values introduced by «для» / English `for` (for example, «для
дома») into `use_case`, not `feature`. Use-case context is included in the
retrieval phrase and Gemini candidate-selection context, but is not placed in
`SearchRequest.required_features` and never hard-rejects a suitable listing
that omits those exact words. Explicit fitment introduced by «под» remains a
required feature. This distinction prevents «для дома» from being treated as
a literal title requirement.

A four-turn offline integration regression now exercises:
«Сварочный аппарат» → «Для дома» → «С двумя аккумуляторами» →
«Только не полуавтомат» (including Gemini classifying the final turn as
`correction`). The final pool retains the product type, soft household
context, hard battery requirement and both exclusions; it rejects the
forbidden class and candidates without battery evidence, while allowing a
suitable household listing whose title omits the literal word «дом».

GitHub Actions run 41392325716 on commit `71824d0`:
105 focused offline tests passed in 1.65s (Python 3.14.9). The earlier
multi-turn test commit `7b01fc6` intentionally failed (103 passed, 1 failed):
it exposed that «Для дома» was not being captured. The connector-grammar
capture was then separated into a soft `use_case` slot rather than a hard
feature. Latest focused verification: GitHub Actions run 41393292819 on
commit `6cc72ad`, **107 passed in 1.36s** (Python 3.14.9).

### Full-suite collection limitation (2026-11-02)

A full `python -m pytest -q tests/` run was attempted on the Option C repair
branch after the focused suite passed. Collection stops with 11 errors before
the full suite can execute. The affected legacy test modules import files that
are already absent from `main` (verified directly against `main`):
`services/agent_service.py`, `services/chat_router.py`, and generated
`tmp_g252d_*.py` runner files. PR #3 does not delete these files; they are
not present in the base branch. Do not restore the removed ShoppingAgent /
chat-router architecture to satisfy stale tests. The conversational PR gate
therefore remains focused on the active Option C tests; the full-suite
collection issue must be handled separately by identifying and retiring or
migrating stale legacy tests, not by weakening production code.

The full-suite attempt on commit `17199e9` had a passing focused run
(105 passed in 1.61s) followed by collection failure (11 errors; no full-suite
test count). This is not evidence that the full repository suite passed.

### Telegram clarification retry lifecycle (2026-11-02)

The Telegram handler now leaves `pending_clarify` in `context.user_data`
while the engine processes the user's answer. A pending answer bypasses the
shown-results shortcuts and the separate AI-question consumer, so it reaches
the conversational engine with the original request. The pending item is
consumed only after a completed non-provider-error outcome. Provider errors
and raised engine exceptions keep the pending item for retry; the engine also
keeps `state.last_question` on provider failure so Gemini still receives the
question in its next-turn context. Explicit cancellation, expiry, or an
invalid timestamp clears the ledger question as well as the handler item.

Coverage is in `tests/test_convsearch_handler_clarification.py` and
`test_provider_failure_preserves_pending_clarification_in_ledger`. Latest
focused verification on commit `1a1a371`: 112 passed in 2.10s (Python 3.14.9).
This verifies the lifecycle helpers and engine state transition offline; it
does not replace a live Telegram bot test.

**Expanded verification after excluding stale legacy test modules:** GitHub
Actions run 41394355651 on commit `3e40433` passed both workflow steps:
- focused Option C / Telegram clarification tests: **112 passed in 2.19s**;
- repository tests excluding 17 stale legacy modules: **1,989 passed,
  670 subtests passed, 1 warning in 29.77s**.

The 17 exclusions are explicit in
`.github/workflows/conversational-regression.yml`. This is a broad
current-code regression run, not the unfiltered full suite. Do not describe it
as a full-suite pass.

### Gemini candidate selection safety and 20/30 limits (2026-11-02)

The conversation selector is now advisory over the deterministic pool, not a
second hard filter. Deterministic product-type, feature, exclusion and budget
checks run first. On Gemini timeout, exception, malformed output or an empty
selection, the engine preserves candidates that already passed those checks.
Candidates marked `semantic_review_ids` remain quarantined unless Gemini
explicitly confirms them; a model outage cannot promote an ambiguous product
identity. This removes the previous behavior that cleared every result merely
because any exclusion slot was active.

The selector sees at most 20 top-ranked candidates. The 20-candidate AI
selection window is now separate from the 30-card presentation cap: after the
selected AI-ranked candidates, up to ten additional deterministic candidates
from beyond the selector window can fill the presentation list. The caller's
smaller explicit result limit is still respected. The report records
`ai_selected` and `ai_tail_added`.

Regression coverage includes timeout with active exclusion, selector
exception, empty selector response, semantic-review quarantine, and a
30-candidate pool where AI sees 20 and the presentation may include the
selected result plus up to ten candidates from the deterministic tail.

GitHub Actions run 41429830597 on commit `2bde4d4` passed:
- focused Option C tests: **114 passed in 1.88s**;
- broad repository tests excluding the 17 documented stale legacy modules:
  **1,991 passed, 670 subtests passed, 1 warning in 25.02s**.

This remains a broad filtered regression run, not an unfiltered full-suite
pass. Live Gemini/AliExpress/Telegram behavior is not verified by these
offline tests.

### Mandatory-feature evidence: unknown vs verified (2026-11-02)

The deterministic feature check no longer treats a missing phrase in an
AliExpress title/description as proof that the requested feature is absent.
The engine separates candidates into (1) listings with positive evidence for
every mandatory feature and (2) listings needing semantic review. Review
candidates are marked in both `feature_review_ids` and
`semantic_review_ids`, prioritized inside the bounded Gemini review window,
and are never shown unless Gemini finds positive evidence for every explicit
mandatory feature in the supplied title/description. If Gemini is unavailable
or returns no confirmation, these candidates remain quarantined. A listing
that does not mention «двумя аккумуляторами» is therefore neither silently
accepted nor immediately discarded: it must be verified from its data.

The AI selector prompt now explicitly requires positive evidence for each
mandatory feature and forbids inventing missing characteristics. Use-case
context remains a soft contextual signal, separate from mandatory features.
No product-specific keyword exceptions were added.

GitHub Actions run 41430433129 on commit `1ed67a8` passed after the
mandatory-feature review changes:
- focused Option C tests: **116 passed in 2.15s**;
- broad repository tests excluding the 17 documented stale legacy modules:
  **1,993 passed, 670 subtests passed, 1 warning in 27.31s**.

### Grounded structured slot extraction (2026-11-02)

The Gemini turn decision now includes a bounded `slot_updates` array for
explicit constraints that the deterministic connector grammar may miss.
Allowed kinds are `use_case`, `feature`, `exclusion`, and `preference`;
`type` and `budget` remain handled by their dedicated paths. Each value
must be a short verbatim substring of the current user message. Exclusions
also require an explicit negative marker; «не только» / `not only` is
rejected as a non-exclusion. Malformed kinds, unknown kinds, oversized lists,
and ungrounded values invalidate the AI turn decision and fall back to the
deterministic parser.

The model cannot freely edit the ledger. Every update is validated
against the current message and active slots. `add` is additive;
`replace` must name one exact active slot of the same kind and a new
verbatim value in the current message; `remove` must name one exact active
slot and an explicit removal request. The ledger applies replace/remove only
to that targeted slot, preserving unrelated requirements and exclusions.
Values are deduplicated case-insensitively. Malformed operation names and
kinds are rejected without raising. The engine passes validated updates only
for `new_search`, `refine`, and `correction` turns.

The retrieval builder now treats the active ledger as authoritative when the
current turn has produced explicit slots. It does not append leftover words
from the raw sentence as additional mandatory requirements. This prevents
grammatical wording such as «буду работать» from becoming hard product
features when the model has already identified «на стройке» as a soft use
case and «защита IP67» as a required feature.

Regression coverage:
- valid grounded use-case and feature extraction;
- ungrounded model values rejected;
- «не только» not interpreted as an exclusion;
- malformed non-string slot kind rejected without raising;
- full engine turn stores model-extracted use case and feature in the ledger;
- deterministic sentence spans overlapping a grounded use case do not become
  mandatory features;
- case-insensitive slot deduplication;
- retrieval does not convert leftover grammatical words into hard features.

GitHub Actions run 41474647605 on commit `009c74e`, Python 3.14.9:
- focused Option C tests: **121 passed in 1.50s**;
- broad repository tests excluding the 17 documented stale legacy modules:
  **1,998 passed, 670 subtests passed, 1 warning in 23.03s**.

This is a filtered broad regression pass, not an unfiltered full-suite pass.
Live Gemini, AliExpress and Telegram behavior remain unverified. PR #4 remains
a draft stacked on PR #3; `main` is unchanged.

### Targeted conversational slot operations (2026-11-02)

The structured turn contract supports three validated operations:
- `add`: add one explicit current-turn constraint;
- `replace`: replace only the named active slot of the same kind;
- `remove`: remove only the named active slot of the same kind.

AI validation requires replacement/removal targets to match an active slot
(case-insensitively). Replacement values must be verbatim in the current
message and require an explicit correction cue. Removal requires an explicit
removal cue and the target to appear in the current message. The state ledger
applies operations to the exact matching slot, not all slots of that kind.
This prevents a correction to one feature from wiping unrelated features or
exclusions.

Regression coverage includes valid/invalid grounded add, replace and remove,
unknown/inactive targets, malformed operation/kind types, and ledger-level
checks that replacing/removing one slot preserves other slots of the same or
different kinds.

GitHub Actions run 41475598191 on commit `d67201c`, Python 3.14.9, passed:
- focused Option C tests: **127 passed in 1.25s**;
- broad repository tests excluding the 17 documented stale legacy modules:
  **2,004 passed, 670 subtests passed, 1 warning in 23.00s**.

This remains a filtered broad regression run, not an unfiltered full-suite
pass. Live Gemini, AliExpress and Telegram behavior remain unverified.

### Replacement-target grounding (2026-11-02)

The validator now also requires the old value named by a structured
`replace` operation to appear in the current user message, in addition to
matching an active slot. This prevents Gemini from using a valid correction
cue and a grounded new value to silently replace a different, unmentioned
active constraint.

GitHub Actions run 41476894558 on commit `6c35da1`, Python 3.14.9, passed:
- focused Option C tests: **129 passed in 2.13s**;
- broad repository tests excluding the 17 documented stale legacy modules:
  **2,006 passed, 670 subtests passed, 1 warning in 28.39s**.

This remains a filtered broad regression run, not an unfiltered full-suite
pass. Live Gemini, AliExpress and Telegram behavior remain unverified.

A further correction guard was added: when a validated structured
`replace` targets one feature, the deterministic state transition uses that
exact target instead of broad token-stem overlap. This prevents replacing
«два аккумулятора» from also superseding another active feature such as
«аккумуляторная батарея» merely because both share the same stem. The old
phrase is superseded, not added as a permanent exclusion; unrelated existing
exclusions remain active.

GitHub Actions run 41476285666 on commit `1c8c0ed`, Python 3.14.9, passed:
- focused Option C tests: **127 passed in 2.16s**;
- broad repository tests excluding the 17 documented stale legacy modules:
  **2,004 passed, 670 subtests passed, 1 warning in 28.09s**.

This is still a filtered broad regression run, not an unfiltered full-suite
pass. Live Gemini, AliExpress and Telegram behavior remain unverified.
PR #4 remains a draft stacked on PR #3; `main` is unchanged.

### Exact-target correction fallback and use-case replacement (2026-11-02)

The correction path now receives the validated structured slot updates before
the state transition. If Gemini identifies an explicit feature replacement,
the deterministic transition supersedes only that exact feature slot rather
than every feature sharing one token stem. Without a structured replacement,
the fallback requires the complete negated phrase to match a feature before
superseding it, reducing collateral deletion of similar-but-distinct
requirements.

Use-case replacement follows the same targeted rule. Replacing «для дома»
with «для дачи» supersedes only the old use-case slot and does not re-add it
from the deterministic phrase extractor. Other use cases (for example,
«для автомобиля») remain active. A targeted «не X, а Y» replacement does
not create a permanent exclusion for X; unrelated existing exclusions remain.

Regression coverage includes feature replacement where another feature
shares a word stem, use-case replacement while preserving another use case,
and preservation of unrelated exclusions.

GitHub Actions run 41476668470 on commit `c51bb73`, Python 3.14.9, passed:
- focused Option C tests: **128 passed in 1.30s**;
- broad repository tests excluding the 17 documented stale legacy modules:
  **2,005 passed, 670 subtests passed, 1 warning in 21.91s**.

This is a filtered broad regression run, not an unfiltered full-suite pass.
Live Gemini, AliExpress and Telegram behavior remain unverified.


### Clarification answers remain separate turns (2026-11-02)

The Telegram handler no longer concatenates a pending clarification's original
request with the user's answer before sending it to the engine. The original
request is already present in the conversation ledger; the answer is recorded
as a distinct turn, while the combined wording is retained only for the
search-history display.

When Gemini identifies the missing product type in a clarification answer,
the engine treats it as a refinement of the pending request rather than a new
search that resets the ledger. The newly supplied type is recorded without
superseding existing budget, use-case, feature, exclusion or preference slots.
A clarification answer containing only a use case/preference and no product
type does not trigger an AliExpress search for that fragment; the engine asks
for the missing product instead.

Regression coverage checks that (1) Gemini receives the original request
and answer as two separate turns, (2) a product supplied in the answer resumes
search while preserving the original budget and use case, (3) a use-case-only
answer does not call the provider, and (4) a repeated answer after a provider
failure remains in the same pending clarification transaction even though
last_outcome is now PROVIDER_ERROR.

GitHub Actions run 41478575616 on commit
`aee8e67`, Python 3.14.9, passed:
- focused Option C / clarification tests: **130 passed in 1.19s**;
- broad repository tests excluding the 17 documented stale legacy modules:
  **2,007 passed, 670 subtests passed, 1 warning in 21.85s**.

This is a filtered broad regression pass, not an unfiltered full-suite pass.
Live Gemini, AliExpress and Telegram behavior remains unverified.


### Product-switch lifecycle integrity (2026-11-02)

A validated `new_search` action now starts a clean product context even when
the user's wording contains a reversal marker such as «на самом деле». Previously
the reversal guard prevented the ledger reset, so the new product type could
coexist with the old product's budget, use case, feature and exclusion slots.
The reset also clears `shown_product_ids`, because previous cards belong to
the old product and must not contaminate alternatives/comparison context for
the new product.

The regression test runs a multi-turn search with a product type, use case,
feature/exclusion, budget and displayed result, then switches product using a
reversal phrase. It verifies that only the new type remains active, old filters
are gone, old shown IDs are cleared, and the new retrieval request contains no
old budget/features/exclusions.

The first focused CI run exposed that «самом деле» could itself become a hard
feature after the switch. Those reversal-framing words are now excluded from
product-type extraction and required-feature parsing; the follow-up CI passed.

GitHub Actions run 41479786460 on code commit `02b18e6`, Python 3.14.9:
- focused Option C / clarification tests: **131 passed in 2.14s**;
- broad repository tests excluding the 17 documented stale legacy modules:
  **2,008 passed, 670 subtests passed, 1 warning in 28.03s**.

This is a filtered broad regression pass, not an unfiltered full-suite pass.
Live Gemini, AliExpress and Telegram behavior remains unverified.


### New product requests phrased as explanations (2026-11-02)

The deterministic fallback no longer treats generic explanation words
(«расскажи», «объясни», «подробнее», etc.) as sufficient evidence that the
user is asking about the previously shown products. Direct references such as
«о втором» / «про первый» and explicit comparison wording remain comparison
signals. A message like «Расскажи про пылесос» can therefore be recognized as
a new product request instead of being trapped in COMPARE.

Russian question/explanation framing words are excluded from product-type
extraction. Regression checks cover the intent classification and verify that
«Расскажи про пылесос», «Что насчёт пылесоса?» and «Какой пылесос лучше?»
produce only the product noun, without the question scaffolding.

GitHub Actions run 41480389791 on commit `a7527c5`, Python 3.14.9:
- focused Option C / clarification tests: **132 passed in 2.25s**;
- broad repository tests excluding the 17 documented stale legacy modules:
  **2,009 passed, 670 subtests passed, 1 warning in 28.67s**.

This remains a filtered broad regression pass, not an unfiltered full-suite
pass. Live Gemini, AliExpress and Telegram behavior remains unverified.


The final branch-head verification also completed successfully: GitHub Actions
run 41480630458 on commit `4a28cb5`, Python 3.14.9 — **132 focused tests
passed in 1.98s; 2,009 broad tests and 670 subtests passed in 26.78s, with
1 warning**. The broad run still excludes the 17 documented stale legacy
modules; it is not an unfiltered full-suite pass.


### Intent boundaries and clarification product switching (2026-11-02)

The turn ledger now protects strong deterministic distinctions from a
contradictory Gemini action:
- A message that names a new product (for example, «Расскажи про пылесос»)
  cannot be overridden to COMPARE solely by Gemini.
- A question clearly referring to the displayed set (for example,
  «Какой из них лучше?») cannot be overridden to NEW_SEARCH solely by Gemini.
These are two-way guards; normal model interpretation still handles turns
where the deterministic classifier is not decisive.

Pending clarification answers are no longer all forced into REFINE when
Gemini returns NEW_SEARCH. If the original request had no product type, the
answer fills that missing type while preserving captured budget/use-case
context. If a type is already active, the proposed product head is compared
with the active product head: an inflectional match continues the same search
and preserves constraints; a different head is treated as an explicit product
switch and resets the old product's slots and shown IDs.

Regression coverage includes both directions of the Gemini intent guard and
both clarification outcomes (same product vs. explicit product switch).
GitHub Actions run 41484570026 on code commit `2bde7d4`, Python 3.14.9:
- focused Option C / clarification tests: **136 passed in 2.25s**;
- broad repository tests excluding the 17 documented stale legacy modules:
  **2,013 passed, 670 subtests passed, 1 warning in 28.14s**.

This is a filtered broad regression pass, not an unfiltered full-suite pass.
Live Gemini, AliExpress and Telegram behavior remains unverified.


### Telegram shown-session invalidation on product switch (2026-11-02)

The engine clears `shown_product_ids` when a new product search begins, but
Telegram also keeps a separate `last_shown_session` pointer to the cached
cards. If a product switch failed or returned no eligible results, that
pointer could continue to reference the previous product. A subsequent
question such as «какой лучше?» could therefore be answered against stale
cards even though the engine ledger had already moved on.

The handler now clears `last_shown_session` whenever the engine reports
`new_search`, before delivering the outcome. If the new search returns
results, the normal delivery path stores the new session. A refinement leaves
the current session pointer untouched. Handler-level regressions cover both
behaviors.

GitHub Actions run 41485036451 on code commit `7f9c610`, Python 3.14.9:
- focused Option C / clarification / handler tests: **138 passed in 2.02s**;
- broad repository tests excluding the 17 documented stale legacy modules:
  **2,015 passed, 670 subtests passed, 1 warning in 27.96s**.

This is a filtered broad regression pass, not an unfiltered full-suite pass.
Live Gemini, AliExpress and Telegram behavior remains unverified.


### Intent metadata reaches Telegram on every result path (2026-11-02)

The handler's stale-session invalidation depends on the engine reporting the
current turn intent. The first implementation added this metadata to provider
errors and clarification responses, but the normal `_evaluate_pool` reports
for RESULTS / NO_CANDIDATES / NO_ELIGIBLE did not include it. As a result, the
handler could not clear the old session for a normal product switch. The
engine's candidate-evaluation report now includes `intent` from its input for
all pool outcomes, and the product-switch integration test asserts the
metadata is present. The Telegram helper then clears `last_shown_session`
whenever the reported intent is `new_search`; refinements keep the pointer.

The first CI run for this metadata assertion failed only because the new test
referenced `MessageIntent` without importing it (137 passed, 1 failed; broad
suite skipped). The missing test import was corrected. GitHub Actions run
41485566828 on code commit `a4041ab`, Python 3.14.9, passed:
- focused Option C / clarification / handler tests: **138 passed in 1.40s**;
- broad repository tests excluding the 17 documented stale legacy modules:
  **2,015 passed, 670 subtests passed, 1 warning in 22.72s**.

This is a filtered broad regression pass, not an unfiltered full-suite pass.
Live Gemini, AliExpress and Telegram behavior remains unverified.


### Conversation transition audit — restored ledgers and intent boundaries (2026-11-02)

The transition audit now covers the distinction between:
- a follow-up that continues the current product request;
- a targeted correction that replaces only the named slot;
- a new product request that resets the old product's constraints and shown IDs;
- a question about already-shown results, which must not trigger a provider search;
- an alternatives request, which keeps the active request but excludes already-shown product IDs;
- a clarification answer that fills a missing type or continues the same product, versus an explicit product switch.

A validated Gemini action can no longer override strong deterministic boundaries in either direction: it cannot turn a clear new-product message into REFINE/COMPARE, or a clear question about shown results into NEW_SEARCH. For a pending clarification with an existing product type, the proposed type head is compared with the active type: same head continues and preserves constraints; a different head starts a clean product search. A pending clarification with no active type fills the missing type without discarding the original budget/use-case context.

The audit also found that Telegram holds a separate pointer to the last displayed card session. New product searches now invalidate that pointer before delivery, including when the new search fails or returns no eligible results; a refinement keeps it. The engine reports the current intent on all candidate-evaluation outcomes so the handler can make this decision consistently.

A further state edge case was fixed: a restored ledger may contain active slots while its in-memory turn transcript is empty. The first locally observed message must not be treated as a brand-new empty search in that situation; targeted corrections must preserve unrelated slots. The first-turn NEW_SEARCH default now applies only when both transcript and active ledger are empty.

Latest GitHub Actions verification: run **41486514301**, code commit `e5689025575ccfa7865106b3315d34164b53ad2c`, Python 3.14.9:
- focused Option C / clarification / handler tests: **141 passed in 2.19s**;
- broad repository tests excluding the 17 documented stale legacy modules: **2,018 passed, 670 subtests passed, 1 warning in 26.99s**.

This is a filtered broad regression pass, not an unfiltered full-suite pass. The 17 excluded legacy modules remain outside this verification. Live Gemini, AliExpress and Telegram runtime behavior remains unverified; offline CI validates the state machine, routing and handler logic only.


### Conversational search performance audit — initial findings (2026-11-02)

Read-only inspection of the current Option C branch found these configured
latency bounds and call patterns. These are code-level limits, not measured
production timings:

- Gemini turn interpretation is wrapped in an outer timeout of
  `min(AIService.timeout_seconds, 5s)`; its fallback path is deterministic.
- Gemini candidate selection is wrapped by the engine in an 8-second timeout.
  It runs after deterministic candidate validation/ranking when the outcome
  contains results and a selector is available. The selector is capped at 20
  candidates; the separate presentation cap remains 30.
- The conversational retrieval adapter calls `SearchService.find_products`
  with `raw=True`, `broaden=True`, and `raw_broaden=True`. In this raw
  path, retrieval uses one page but evaluates every grounded query variant.
- `services.query_variants.product_query_variants` caps the phrase list at
  three. `SearchService.find_products` iterates those phrases sequentially;
  provider calls for the same phrase are gathered concurrently. The route
  rankings are fused with RRF after collection, so removing a route or
  changing execution order without preserving the full route set could alter
  candidate quality.
- The AliExpress HTTP client defaults to a 10-second HTTP timeout, one retry
  for transient timeout/transport/5xx failures, and a 1-second retry backoff.
  Thus a failing individual API call can consume roughly two request timeout
  windows plus the backoff; three sequential variants can multiply that wait.
  This is a worst-case configuration inference, not a measured end-to-end
  duration. HTTPX phase timeouts are not a strict whole-search wall-clock SLA.
- Existing diagnostics already record per-provider call latency and total
  `SearchService.find_products` latency in bounded in-memory samples. Gemini
  attempts log operation, model and elapsed milliseconds. No live samples were
  available during this GitHub-only audit, so actual p50/p95 latency and the
  frequency of retries remain unverified.

No runtime code or timeout policy was changed during this read-only audit.
Do not parallelize additional AliExpress phrase calls or reduce the grounded
variant set solely from these theoretical bounds: that could increase API
pressure or reduce recall/RRF quality. The next performance step should use
real bot diagnostics to separate Gemini interpretation, each AliExpress
variant/retry, and Gemini selection before choosing a bounded optimization.
The 5-second interpretation, 8-second selection, 20-candidate AI window and
30-card presentation cap remain unchanged.


### Material and composition follow-ups (2026-11-03)

Material/composition phrases are refinements of the active product, not new
product types. The conversation state recognizes connector-led forms such as
Russian «из X», «сделан/изготовлен/выполнен из X» and English "made of/from X"
as required feature evidence. A deterministic refinement boundary prevents a
contradictory Gemini `new_search` decision from replacing the active product
when the current message is grammatically a connector-led characteristic.
A real new product named in the message remains eligible to start a new search.
An offline integration regression simulates Gemini proposing the material
itself as a new product type; the engine must preserve the original product,
record the material constraint and retrieve against the original type.


### Explicit product-switch boundary (2026-11-03)

An explicit request construction that names a different item (for example,
«мне нужна насадка на дрель для дерева») is a new product search even when
the sentence also contains prepositional use-case/fitment context. The
conversation engine extracts the requested product phrase before those
context connectors and protects the switch from a contradictory Gemini
`refine` action. Old type, feature, budget, use-case and shown-result state
must not leak into the new product search. Prepositional targets introduced
by «на» / «для» are retained as soft use-case context, not hard product-type
tokens or mandatory title features.
