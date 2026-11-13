# PROJECT_CONTEXT

Fast orientation for any agent/human working in `c:\shorts-chill-shop-bot`.

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
  `min(AIService.timeout_seconds, 8s)`; its fallback path is deterministic.
- Candidate selection asks Gemini for up to 20 products. For the default
  30-card presentation, the AI review window is normally 70 of up to 80
  deterministically ranked candidates; the ten-card tail is appended only
  when the AI selection reaches 20. If a valid first pass is short, one
  bounded continuation reviews only the still-unselected candidates.
- The outer AI selection budget is 17 seconds total for both passes. The
  Gemini SDK attempt cap is 8 seconds per model attempt. Selection telemetry
  distinguishes input count, requested count, returned IDs, validated IDs,
  final cards and elapsed time.
- Conversational retrieval uses the configured page depth, now five pages by
  default. Up to three grounded query variants are requested on page 1; each
  route that returns rows continues independently on later pages, while empty
  or failed routes are retired. Per-route rankings are fused with RRF.
- Retrieval has nested hard limits: 13 seconds per route, 32 seconds per page,
  and 60 seconds for the complete conversational retrieval pass. A route
  timeout retires that route when page/search time remains; completed route
  results are retained. The maximum is bounded even if the provider stalls.
- The AliExpress client still serializes requests for the shared application
  key and retains its bounded transient retry policy. Its HTTP timeout is
  10 seconds with one retry/backoff; the conversational route deadline can
  cancel a particularly slow retry, which should be monitored against live
  latency before any further timeout increase.
- Retrieval diagnostics now record pages requested/completed/failed, routes
  requested/completed/failed, raw and unique candidate counts, and total
  retrieval duration. The engine also records counts by rejection/quarantine
  stage (wrong type, unverified required evidence, explicit exclusion,
  budget/currency mismatch, ranking validation). Gemini diagnostics record
  selection funnel counts.
- `.env.example`, the `Settings` dataclass default and `load_settings()`
  default are all aligned at five pages; the user's actual `.env` is untouched.
- The 30-card presentation cap and five-card Telegram page size remain
  separate from the 20-product AI selection target.

The earlier read-only performance notes below were written before the
2026-11-07 bounded route/page/overall deadlines and are historical, not the
current timeout policy.

### Historical conversational retrieval pagination implementation (2026-11-04)

The following records the earlier November 4 implementation. It is superseded
by the November 7 update above: the current default is five pages, and later
pages continue every live grounded phrase rather than only the primary phrase.

The raw conversational path previously bypassed the existing
`SEARCH_MAX_PAGES` loop because the page condition required `not raw`.
That meant the engine fetched up to three grounded variants, but only from
page 1, even though the AliExpress adapter already forwarded `page_no` and
the project had a bounded three-page default.

Fixed at that time in `services/search_service.py`:
- Raw conversational SEARCH requests now use the same configured page range.
- Page 1 still gathers every grounded variant before any early stop, preserving
  complete RRF route coverage. Deeper pages originally repeated only the primary phrase.
- After each complete page, the service checks the fused unique pool and stops
  when the raw pool target is reached. Otherwise it continues only up to the
  configured page limit.
- The final candidate pool is fused across all fetched page/phrase routes;
  no provider products or fields are synthesized.

Added an offline regression in `tests/test_search_service.py` proving that a
thin first page triggers page 2 and that reaching the raw pool target stops
before page 3. Existing tests continue to protect first-page route fusion and
the single-page behavior when raw pagination is not explicitly enabled.

This change addresses a concrete retrieval-depth defect; it does not by itself
prove live catalog quality, fix every candidate-validation rejection, or
establish end-to-end response speed. Those still require the remaining
technical work and a live Telegram check with the user's own credentials.


### Natural no-results follow-up and pool diagnostics (2026-11-04)

When a completed conversational search returns `NO_CANDIDATES` or
`NO_ELIGIBLE`, the engine now asks the bound Gemini service to write one
short, natural follow-up question. The prompt receives only the user's latest
message, active slot values and the internal outcome reason - no candidate
rows, product facts, prices or URLs. The output is accepted only as plain
text up to 450 characters with exactly one question and no links/markup.
The generation is bounded to 3 seconds and is skipped after a candidate
selection timeout/error so a slow model cannot delay an already-failed turn.
If unavailable or invalid, Telegram uses one short, honest fallback question.
Provider/API failures remain a separate outcome and are not described as an
empty catalogue.

The existing `SearchDecision.reply` field carries this follow-up text as
well as ordinary `RESPOND` replies. The Telegram delivery layer prefers it
for no-result outcomes and otherwise uses the safe fallback.

The engine also logs one aggregate pool-stage line per turn:
raw → type → required-feature → exclusion → budget → dedup → rank-rejected
→ ranked → semantic-review → AI-selected → final. The report now records
the deduplicated count, candidates rejected by the final ranking validator,
and the actual number selected by Gemini (including an explicit zero for a
semantic no-match). This makes it possible to identify exactly where a live
pool shrinks (for example, 39 retrieved rows to a smaller eligible set)
without logging product titles or user text.


### Same-language synonym identity review (2026-11-04)

The type validator no longer treats a same-script title with zero shared type
tokens as an automatic mismatch. It is now quarantined for the same bounded
Gemini semantic review used for other uncertain identities. This handles
marketplace synonyms and alternate names without weakening the hard safety
rules: explicit accessory heads, host-product relations, role clauses and
genitive accessory mentions still fail before review. A quarantined candidate
is never shown when semantic confirmation is unavailable or times out.

Added regressions for a same-language alternate product name and for keeping
an accessory listing rejected. The latter also protects inflected Russian
accessory wording in a host-product relation.

Latest successful workflow on code head
`5916efac3ebc1b7ca7b96796c989875e2eb823f2`:
- Focused conversational regressions: **210 passed, 8 subtests passed**.
- Repository suite excluding documented stale legacy modules:
  **2,096 passed, 678 subtests passed, 1 warning**.
- This is filtered offline verification, not an unfiltered full-suite pass.
  No live Telegram, Gemini or AliExpress calls were made.


### Sparse-title semantic review (2026-11-04)

The product-type gate previously rejected every title that carried only the
requested head noun but omitted its subtype, even when the AliExpress
description explicitly confirmed that subtype. This discarded plausible
listings before Gemini could inspect them.

The validator now defers this narrow evidence pattern to the existing
semantic-review queue instead of auto-accepting it or rejecting it outright.
A conflicting subtype stated in the title remains a hard rejection; a
keyword-stuffed description cannot override a title that identifies another
product. Added cross-case regressions for both outcomes. This is a
category-general evidence rule, not a product-name exception.

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


### Conversational shown-product ledger follows Telegram delivery (2026-11-03)

The engine previously recorded every selected result as `shown` before the
Telegram handler attempted to send the browsing card. This conflated candidate
selection with actual user-visible delivery. It could also mark the whole
result pool as seen even though the UI initially sends only the first card.

The engine no longer mutates `shown_product_ids` when it creates a
`RESULTS` decision. The text-search handler passes its conversation state to
`send_search_results`; that function acknowledges only the first product
after `_send_product_cards` confirms the Telegram card send. A failed card
send returns no shown session and leaves the conversational shown ledger
unchanged. The shared card helper now returns an explicit delivery boolean,
and its analytics/view-history writes occur after the send rather than before
it. Failures in those secondary writes are logged without misreporting a card
that Telegram already accepted as undelivered.

When the user successfully browses to another product in that same
conversation-search session, the handler adds that product to the ledger
after the Telegram edit/replacement succeeds. Other discovery sessions do
not write into the conversational ledger. Engine regressions now assert that
a selected result is not considered shown before the handler acknowledges
delivery.

The optional post-card hint is also isolated from the primary delivery result:
if Telegram accepts the product card but rejects the separate hint message,
send_search_results still returns the valid session id. This keeps the
handler's last_shown_session pointer aligned with the card the user actually
received, so compare/browse follow-ups remain available. A regression covers
this failure path.

This change is covered by offline CI only. Live Telegram delivery behavior
has not been exercised in this environment.

GitHub Actions run **41702370560** on delivery and optional-hint regression
commit `fbcaaeb`:
- Focused conversational regressions: **182 passed, 8 subtests passed**.
- Repository suite excluding the documented stale legacy modules: **2,059 passed, 678 subtests passed, 1 warning**.
- All workflow steps passed. Coverage includes failed initial card delivery, successful card delivery, session-scoped browse acknowledgement, and a failed optional hint after successful card delivery.
- This remains a filtered repository pass, not an unfiltered full-suite pass. Tests are offline; no live Telegram, Gemini or AliExpress calls were made.


### Product identity grounding and final offline verification (2026-11-03)

Gemini's proposed product type is now grounded against the final head noun of
the leading requested-item phrase, not against any token in that phrase.
Matching a shared modifier is insufficient: for example, «стол сварочный»
cannot replace «сварочный аппарат» merely because both contain «сварочный».
The model may still normalize inflection when the requested product head
matches. The deterministic parser remains the fallback when the model's
proposal fails this validation. A regression covers this reversed-word-order
substitution.

GitHub Actions run **41703431090** on head commit
`72462efdf175f1bb6e25918db4c6df5b98fa0ee7` completed successfully:
- Focused conversational regressions: **183 passed, 8 subtests passed**.
- Repository suite excluding the documented stale legacy modules:
  **2,060 passed, 678 subtests passed, 1 warning**.
- All workflow steps passed. The suite is filtered; it is not an
  unfiltered full-suite pass.
- Tests are offline. No live Telegram, Gemini or AliExpress calls were made.
  Live delivery, provider latency, catalog relevance and real conversation
  quality remain unverified.

The pull request remains a draft and must not be merged or treated as
production/live-test ready until the remaining review and user-controlled
Telegram smoke test are completed.


## Live-log systemic correction — 2026-11-04

The first live Telegram run exposed two independent weaknesses that offline
tests did not exercise:

1. The turn interpreter correctly returned a valid response but treated a
   broad request with an underspecified purpose as ready for retrieval. The
   conversation prompt now explicitly evaluates *search readiness*, not merely
   whether a product noun is present. Broad application labels (for example,
   "for a car") must not be mistaken for a sufficiently specific task when
   materially different product classes remain possible. The model should ask
   one discriminating question and keep short answers attached to the pending
   request.
2. Turn interpretation and post-retrieval selection previously shared one
   eight-second cumulative budget. A normal ~4.7-second interpretation left
   too little time for the second model call, so candidate selection often
   timed out. They now have independent bounded windows: interpretation is
   capped at eight seconds in AIService; candidate selection receives its own
   fifteen-second window. Worst-case sequential model wait is therefore
   bounded at roughly twenty-three seconds, excluding provider retrieval.

3. If the semantic selector is actually attempted but times out, errors, or
   returns an unusable answer, the engine no longer presents the unreviewed
   deterministic pool as if it were semantically relevant. It returns an
   honest no-eligible outcome instead. A lexical product-type match alone
   cannot distinguish materially different classes that share a head noun.
4. A standalone inflected modifier (for example, «автомобильный») is not
   accepted as a product identity. The engine asks for the missing product
   name instead of searching AliExpress for an adjective.
5. If a clarification is pending and Gemini cannot interpret a short
   answer, the engine does not let deterministic intent guess that the user
   switched products. When the answer adds no grounded use-case, feature,
   compatibility, exclusion, preference or budget detail, the same question
   stays open and no provider search runs.
6. Telegram distinguishes a semantic-selection failure from a genuine
   catalogue no-match. When the selector fails, the bot explains that it
   withheld unverified cards rather than claiming that no products exist.

This is a first systemic correction, not proof of production readiness. It
does not change the Option C authority split, provider retrieval, deterministic
product validation, the 30-card presentation cap, or the 20-candidate AI
selection cap. Live Gemini/AliExpress/Telegram verification remains required.


### Comparing specifications stated in listing titles

Product follow-up answers may extract an explicit numeric specification from
the real marketplace listing title when the provider's normalized product
fields do not expose that specification separately. Brightness comparison
recognizes seller-advertised lumen values (lm / лм / люмен), compares only
those stated values across the complete shown result set, and labels them as
seller claims rather than independently verified measurements. The handler
passes the complete shown product set to ProductAssistant; an arbitrary
first-eight slice can hide the strongest stated specification. No lumen value
is inferred from adjectives such as «яркий».


### Natural conversation and product-continuity safeguards (2026-11-04)

The Option C conversation decision contract now includes a bounded `respond` outcome for greetings, acknowledgements and general concept questions that do not require marketplace facts. Such a turn is recorded in dialogue history without mutating the shopping ledger or calling AliExpress. Product-specific claims remain on the grounded product-answer path. A conversational aside does not consume an outstanding clarification, and the Telegram search-status message is emitted only when retrieval actually begins.

Product continuity is protected by grounded item identity rather than raw-token mismatch. A proposed new search for the same active item is treated as a refinement unless the user explicitly starts a new request. A new-search decision without a grounded product identity cannot silently repeat the previous query: modifier/constraint language remains attached to the active request, alternatives are handled as alternatives, and an otherwise untyped new request asks which product the user means. The state classifier may accept Gemini's validated refine/correction/compare/alternatives decision over a weak deterministic new-search guess only when it has no evidence that a different product was named. A clear different-product mention continues to win over a contradictory model action.

Comparative fragments and pronouns referring to the current result set are excluded from product-name extraction. This is grammatical handling, not a product-category exception. Latest verified offline CI after the visible-card and context-compaction changes: focused conversational regressions **203 passed, 8 subtests passed**; repository suite excluding the documented stale legacy modules **2,086 passed, 678 subtests passed, 1 warning**. This is not an unfiltered full-suite pass. Live Telegram, Gemini and AliExpress behavior remains unverified.

The conversation interpreter now also receives a bounded, grounded snapshot of up to 12 currently cached product cards (real product ID, title, price and currency). This is supplied by the Telegram handler from the existing shown-results cache; it is not generated by Gemini and does not change retrieval or ranking. The prompt uses this snapshot only to resolve references to already-shown products and choose the correct dialogue action.

The delivery ledger is authoritative for visibility: the result cache may hold up to 30 cards, but only the first card is visible initially. The handler therefore filters the cached result pool through the IDs actually opened in Telegram and passes only those cards (up to the 12 most recently viewed) to Gemini. The COMPARE/EXPLAIN answer path uses the same visible-card filter, so it cannot compare or make factual claims about a card the user has not opened. Legacy card-action flows without this conversation ledger retain their existing behavior.

The model context is compacted as a structured object before JSON serialization. It prioritizes active slots and the pending clarification, trims older turns and excess shown cards, and never slices serialized JSON at an arbitrary character boundary. This prevents a long conversation from corrupting the context payload or silently dropping its final fields. Product-category examples in the planner prompt were generalized to role-based examples; no product-specific rules were added.


### Cross-language product identity review (2026-11-04)

The live Telegram run showed that the product-type gate can reject a whole
AliExpress result pool before Gemini sees it. Product names are often written
in a different script from the user's request (for example, Cyrillic request
and Latin-script listing title). When no title/description identity evidence
matches and the dominant scripts differ, the validator now quarantines the
candidate for bounded semantic review instead of treating the lack of shared
stems as proof of mismatch. This is never an automatic acceptance: Gemini
must still confirm the real listing, and explicit accessory/host-product
relations remain rejected before this fallback.

The same regression exposed an English prefix collision: the generic
accessory marker `screw` matched the beginning of `screwdriver`, causing a
real tool to be classified as a spare part. English accessory markers now
require complete-token matches; intentional Russian stems retain inflection
matching. Added regressions for Russian-to-English product identity and for
English drill-bit accessories. CI on the latest branch head: focused suite
**205 passed, 8 subtests passed**; repository suite excluding the documented
stale legacy modules **2,088 passed, 678 subtests passed, 1 warning**. This
remains offline verification only; live model selection and marketplace
quality still require a Telegram run.

### Conversational candidate-review timeout (2026-11-04)

Live diagnostics showed that Gemini turn interpretation could complete
successfully while the separate 20-listing candidate review repeatedly hit
its own 8-second deadline. That meant the model understood the user's
follow-up, but often did not get enough time to inspect the retrieved
listings against the accumulated requirements; the engine then used its
safe deterministic fallback and withheld candidates requiring semantic
review. The bounded candidate-review window is now 15 seconds. The
conversation-turn interpretation window remains separately bounded, and
the 20-candidate AI selection / 30-card presentation limits are unchanged.
This is a latency trade-off, not proof that the model will always finish or
that the selected products are correct; verify with live telemetry and the
actual Telegram cards.

### Live multi-turn test: battery-drill refinement failure (2026-11-04)

A real Telegram run confirmed partial dialogue continuity but exposed a blocking
failure. The initial request «Нужен аккумуляторный шуруповёрт для дома» returned
seven cards; the budget follow-up «До 2500 грн» was classified as
`intent=refine` and returned three cards. The next follow-up
«Хочу, чтобы было два аккумулятора» was also classified as `refine`, but
retrieval returned 50 candidates and the product-type gate rejected all 50
(`reason=rejected_by_type_gate`); Gemini candidate selection was not called.
The user-facing response was therefore an empty-results message. A later
unrelated request for «Тэн для бойлера» correctly started a new search and
returned five cards. This proves that refinement/new-search routing works in
these observed turns, but does NOT prove that the full requirement ledger or
candidate validation is correct.

The same run recorded two candidate-selection timeouts at the 15-second
deadline; a later selection completed in 8.59 seconds. AliExpress also returned
an intermittent `resp_code=405` empty route and a scheduled price-tracking
`ApiCallLimit` response. These provider events are separate from the
observed type-gate rejection and are not established as its cause.

The existing `SearchDecision.report` already contains the rejected product
type and per-candidate rejection reasons, but the compact live action log did
not expose them. A diagnostic warning was therefore added immediately after
pool evaluation to log the active product type, active ledger slots, rejected
count and the first eight type-rejection reasons whenever the type gate rejects
candidates. This is an observability change only; it does not relax validation
or claim the conversational failure is fixed. Next live run must capture this
diagnostic before changing type-validation policy. The PR remains unmerged.


### AliExpress category metadata in semantic review (2026-11-04)

The affiliate product query/detail response documents `first_level_category_name` and `second_level_category_name`, but does not document a product-description or specification field. The normalizer now preserves those two real labels as `Product.category_path` (broad-to-specific) and Gemini candidate selection receives them as weak supporting context. The selector prompt explicitly forbids treating a category label as proof of product identity or using it to override a conflicting title. No category is inferred, no additional API call is made, and missing labels remain an empty list. This improves context for ambiguous/cross-language titles but does not provide missing technical specifications; those still require explicit listing evidence.


### Grounded evidence validation for Gemini candidate selection (2026-11-04)

The previous selector contract returned only product IDs. That meant the conversation engine could not independently verify whether Gemini's positive decision about an uncertain mandatory feature was supported by any listing field. The selector now returns a structured selection with exact quote snippets for every active mandatory feature and, for identity-review candidates, an identity quote. AIService checks each quote against the actual product title/description before allowing the ID through. Category paths are not accepted as proof of a feature or product identity. An empty selections array remains an explicit no-match; non-empty but ungrounded output is treated as unusable and invokes the existing safe deterministic fallback. The 20-candidate selection and 30-card presentation limits are unchanged. This does not create missing listing specifications: when AliExpress supplies no evidence, the candidate remains withheld and the user is asked to refine the request.

The evidence validator also logs only the count of candidate decisions rejected for ungrounded quotes (no titles or user text). Product titles/descriptions are explicitly treated as untrusted data in the selector prompt; embedded instructions are ignored. Regression tests cover multiple independent requirements and ensure category-path labels cannot substitute for listing evidence.

Evidence snippets are matched within one source field (title OR description), never across their boundary; a regression covers this. This prevents an apparently real quote from being assembled from two unrelated field fragments.


### Balanced Gemini candidate window (2026-11-04)

A mixed candidate pool can contain both (a) listings that passed deterministic
identity/mandatory-feature checks and (b) listings quarantined for bounded
semantic review. Previously the engine concatenated the entire review group
before the verified group and then cut at 20. A large review group could
therefore consume the full Gemini window, leaving verified candidates unseen.
The selector window now reserves up to half for each group when both exist,
then fills unused places from whichever group has more remaining candidates.
The selector still receives at most 20 candidates; the presentation ceiling
remains 30. If Gemini fills the selection window, remaining deterministic-safe
candidates outside that window may fill the remaining presentation places;
unreviewed semantic-review candidates remain quarantined. Added an offline
regression with 15 review and 15 verified candidates. This is a candidate
allocation fix, not evidence of improved live marketplace recall or Gemini
latency.


### Balanced semantic-review / verified-candidate window (2026-11-04)

A large semantic-review group could previously occupy all 20 Gemini selector
places because it was concatenated before deterministic-safe candidates.
The selector window now reserves up to 10 places for each group when both
groups are present, then reallocates unused places to the group with remaining
candidates. When Gemini fills its window, deterministic-safe candidates outside
the window may fill remaining presentation slots; unreviewed candidates stay
quarantined. The 20-candidate AI cap and 30-card presentation cap are unchanged.

Regression: 15 semantic-review + 15 deterministic-safe candidates produce a
20-row selector window containing 10 from each group; five additional
deterministic-safe rows may fill the final list, while five unreviewed rows
remain hidden.

Verification on branch head `31821424eeded25b8007e665b535678d4dc56656`,
GitHub Actions run **41866859694**:
- Focused conversational regressions: **213 passed, 8 subtests passed**.
- Filtered repository suite: **2,107 passed, 678 subtests passed, 1 warning**.
- The workflow excludes the documented stale legacy modules; this is not an
  unfiltered full-suite pass.
- Tests are offline. No live Telegram/Gemini/AliExpress calls were made.


### Subordinate-clause feature follow-ups (2026-11-04)

A natural request such as «Хочу, чтобы было два аккумулятора» begins with
the explicit-request verb «хочу». The product-type extractor previously took
the whole remainder («чтобы было два аккумулятора») as a possible new product
identity. That could supersede the active item and send the entire catalogue
pool through the wrong type gate. The generic explicit-item boundary now
recognizes «чтобы/чтоб» as the start of a subordinate characteristic clause;
the clause's own verb scaffolding («было/были») is also stripped from feature
spans. The requested quantity remains an active feature, while the existing
product type remains unchanged.

Regression coverage asserts that both explicit and fallback product-type
extractors return no new item identity for this wording, that the turn is
classified as a refinement, and that the clean «два аккумулятора» feature is
stored without «хочу/было». This is a general subordinate-clause grammar rule,
not a product/category-specific exception. CI verification is pending.


Verification for subordinate-clause follow-up fix on head
`0a269949bc9be576f5f1172ab905a5f6b821ed61`, GitHub Actions run
**41868280671**:
- Focused conversational regressions: **214 passed, 8 subtests passed**.
- Filtered repository suite: **2,108 passed, 678 subtests passed, 1 warning**.
- The workflow excludes the documented stale legacy modules; this is not an
  unfiltered full-suite pass.
- Offline only; no live Telegram/Gemini/AliExpress calls were made.


An additional integration regression now simulates Gemini incorrectly labelling
«Хочу, чтобы было два аккумулятора» as `new_search` with proposed type
«два аккумулятора». The engine must still retain the active «шуруповёрт» type,
classify the turn as REFINE, query the existing item, and store the battery
quantity as a feature. This protects the complete Option C boundary, not only
the two deterministic extraction helpers. Verification for this extra
integration regression is pending.


Final verification including the Gemini-misclassification integration regression
on head `53dbe86fb370dfdc9999651a0933193fe2381d33`, GitHub Actions run
**41869058952**:
- Focused conversational regressions: **215 passed, 8 subtests passed**.
- Filtered repository suite: **2,109 passed, 678 subtests passed, 1 warning**.
- The documented stale legacy modules remain excluded; this is not an
  unfiltered full-suite pass.
- Offline only. The live AliExpress/Gemini/Telegram sequence remains to be
  checked with the user's local credentials.


### Subordinate-clause feature normalization (2026-11-04)
- Extended the generic Russian conversational stopword set to remove pronoun/modal scaffolding from feature spans (e.g. «у него должно быть два аккумулятора»).
- Added offline regression coverage for several natural subordinate-clause variants. They must remain refinements of the active product, not create a new product identity, and must preserve the actual requested feature without retaining filler words.
- This is a grammar-level normalization, not a product/category exception. Live behavior remains unverified until the updated branch is run with the user's Telegram/Gemini/AliExpress credentials.


### Subordinate-clause feature normalization — verification (2026-11-04)
- The feature-span parser now treats «чтобы/чтоб» as a clause boundary and removes pronoun/modal scaffolding at the edge of the subordinate clause. This preserves the requested value (e.g. «два аккумулятора») without retaining «у него было/должно быть».
- Added five natural-language regression variants. They remain refinements of the active product and do not create a new product type.
- GitHub Actions run **41871384581** on code head `1d7e6df7bb3687286277c505e779f22d52d220d5` passed:
  - Focused conversational regressions: **216 passed, 13 subtests passed**.
  - Filtered repository suite: **2,110 passed, 683 subtests passed, 1 warning**.
  - The workflow excludes documented stale legacy modules; this is not an unfiltered full-suite pass.
- Offline tests only. No live Telegram, Gemini or AliExpress calls were made. The updated behavior still needs live validation with the user's credentials.


### Explicit product-switch identity correction (2026-11-04)

The turn router previously treated two product names as the same whenever ANY token overlapped. This incorrectly classified an explicit switch such as «сварочный аппарат» → «сварочный стол» as a refinement because the descriptive adjective «сварочный» is shared. Product-switch detection now compares the principal noun using the same head-noun rule as product-type validation. This preserves reordered/adjectival variants of the same item while recognizing a changed product head as a new search. Added direct and end-to-end regressions. GitHub Actions run **41885404974** on the PR merge ref containing branch head `1628d046d5d4e361ed723298c670c07d210536d2` passed: focused conversational regressions **218 passed, 13 subtests passed**; filtered repository suite **2,112 passed, 683 subtests passed, 1 warning**. The workflow excludes documented stale legacy modules; this is not an unfiltered full-suite pass. Tests were offline; no live provider calls have been made.


### Language-aware product token matching (2026-11-04)

The shared token matcher previously used a four-character prefix for both Cyrillic and Latin words. This can falsely merge unrelated English product nouns (for example, "table" and "tablet"). Matching is now language-aware: Cyrillic keeps the existing inflection stem rule; Latin uses exact matches plus conservative English plural normalization only. Added regressions for Russian inflections, English plurals, and unrelated prefix collisions. GitHub Actions run **41886076271** on code head `2cbd9765d4cb21248facee347359e71faf92d9f8` passed: focused conversational regressions **220 passed, 13 subtests passed**; filtered repository suite **2,114 passed, 683 subtests passed, 1 warning**. The workflow excludes documented stale legacy modules; this is not an unfiltered full-suite pass. Tests were offline; no live AliExpress/Gemini/Telegram calls were made.

### Search variant pagination correction (2026-11-04)

A successful-but-empty answer for the PRIMARY query on the first page incorrectly stopped the remaining grounded query variants on that same page. This contradicted the intended retrieval contract and could hide products found by a broader user-grounded phrase. The stop condition now applies only to empty deeper pages; all first-page variants are still attempted unless the provider itself fails. Added a regression where the primary first-page query is empty, a second grounded variant finds a real product, and the empty deeper primary page ends pagination. GitHub Actions run **41887570038** on head `32d65dbad57d144d97bb800f3e3c61381dc4117c` passed the focused and filtered repository jobs. This run preceded the subsequent language-aware feature-matcher change.

### Language-aware feature matching (2026-11-04)

The shared feature matcher also used four-character prefix matching for Latin words, so unrelated English words such as "table"/"tablet" and "wireless"/"wired" could be treated as equivalent feature evidence. It now keeps the established Cyrillic inflection rule and uses exact Latin matching plus conservative English plural normalization. Added regressions for the false-prefix pairs and English plurals. GitHub Actions run **41887754230** on head `5fdad47c29b05c8a085b572be40c93504c76694c` passed: focused conversational regressions **221 passed, 13 subtests passed**; filtered repository suite **2,117 passed, 683 subtests passed, 1 warning**. The workflow excludes documented stale legacy modules; this is not an unfiltered full-suite pass. Tests were offline; no live AliExpress/Gemini/Telegram calls were made.

### AliExpress catalogue-data boundary research (2026-11-04)

Reviewed current documented AliExpress Affiliate API surfaces while evaluating how to verify required product specifications. The documented `product.query` response exposes listing metadata such as title, category names, price/currency, rating, recent volume, images, product URL and promotion link; it does not document a full description or technical-specification payload. `productdetail.get` accepts comma-separated `product_ids` (batch detail lookup) but its documented response exposes essentially the same affiliate listing fields, not SKU/specification detail. The repo's existing live-verified note says this app key receives `InsufficientPermission` for `product.smartmatch`, so that endpoint is not a usable fallback for this deployment. Therefore the current official affiliate data source cannot prove a feature that is absent from the listing title; the consultant must keep treating missing evidence as unknown and must not invent specifications.

Investigated the MIT-licensed `jnslmk/aliexpress-mcp` project as a possible alternative source. It scrapes the AliExpress web search frontend and uses browser interception of undocumented internal MTop calls for full product details. Its own documentation reports active anti-bot challenges, client-side detail loading and the need for a browser transport. It is not a drop-in replacement for the affiliate provider: search results do not inherently provide our tracked affiliate links, and adopting it would add browser infrastructure, an undocumented API dependency, latency and anti-bot failure modes. Not integrated in this pass; keep it as a researched alternative only. Any future adoption must preserve AliExpress affiliate-link validation and fail closed when detail enrichment is blocked.

This is an outstanding product-data limitation against the full specification-quality requirement. It is not solved by the current pagination or matching fixes. Live provider calls remain unavailable in GitHub CI because credentials are intentionally not stored in the repository.

### Hard conversational presentation cap (2026-11-04)

The conversational engine previously used an explicit `strategy.result_limit` as-is in its deterministic evaluation stage. That allowed a request above 30 cards to exceed the required presentation ceiling whenever the Gemini selector was unavailable or not bound. The engine now treats 30 as an absolute cap and only lets an explicit user count lower it. Added regression coverage for requested counts 50, 5, and no explicit count against a 35-product pool. GitHub Actions run **41889443862** passed: focused conversational regressions **222 passed, 16 subtests passed**; filtered repository suite **2,121 passed, 686 subtests passed, 1 warning**. The workflow excludes documented stale legacy modules; it is not an unfiltered full-suite pass. Offline only; no live provider calls.

### Short AliExpress ApiCallLimit recovery (2026-11-04)

Live Telegram logs previously showed AliExpress returning an explicit temporary ban message stating that the ApiCallLimit ban would last one second. The client previously treated every business error as permanent, so it failed the current search/tracking call even when the server reported a very short cooldown. It now retries exactly once only when the error code/sub-code is `ApiCallLimit` AND the server message explicitly reports a duration greater than zero and no more than five seconds. The retry waits at least the server-reported duration. HTTP 429, missing/ambiguous duration, longer bans, quota/permission errors and other business errors remain non-retryable and visible. Added offline tests for the one-second recovery, missing duration, and 30-second ban. GitHub Actions run **41889443862** passed the same focused and filtered suites above. This does not establish that the account's AliExpress quota is healthy or eliminate concurrent call limits; live behavior must be checked with the user's credentials.

### Language-aware grounded query variants (2026-11-04)

The query-variant generator still used four-character prefixes when guarding excluded terms and deciding whether the user's own wording was a meaningfully different retrieval question. That was inconsistent with the language-aware matcher already adopted in the type and feature validators: unrelated English words such as `wireless`/`wired` or `table`/`tablet` could be treated as the same token, potentially suppressing a valid bounded search route. Production exclusion checks now use complete tokens with the shared language-aware `stems_match` rule; the own-phrase coverage check uses the same rule and preserves a second route when exactly half of a two-word query changes. The former private `_excluded_stems` view remains as a legacy diagnostic compatibility helper only; production route filtering does not use it. Added offline regressions for both word pairs and excluded-term handling.

### Full shown-card comparison context (2026-11-04)

The Telegram handler and Gemini turn interpreter previously passed at most the last 12 actually shown products into follow-up handling, even though the conversational search can return up to 30. This meant earlier visible cards could be omitted from «compare all» / numbered references after the user browsed more than 12. The handler now retains all 30 shown cards for follow-up matching, and the interpreter receives up to 30 grounded card summaries. Added handler and engine regressions proving all 30 IDs are retained and passed to the compare decision.

GitHub Actions run **41890182221** passed after these changes: focused conversational regressions **223 passed, 16 subtests passed**; filtered repository suite **2,125 passed, 686 subtests passed, 1 warning**. The workflow excludes documented stale legacy modules; this is not an unfiltered full-suite pass. Offline only; no live AliExpress/Gemini/Telegram calls were made. PR #7 remains draft and unmerged; `main` is untouched.

### Complete evidence for mandatory features (2026-11-04)

The required-feature evidence helper previously accepted 60% token coverage. That could mark a multi-word mandatory requirement as verified when one or more content words were absent (for example, three of four terms), allowing the row into the deterministic-safe fallback if Gemini timed out. Mandatory feature/compatibility slots now require every normalized content token to be present with the shared language-aware morphology matcher. Partial evidence remains in the semantic-review bucket and is hidden when Gemini is unavailable; the model still must provide grounded listing quotes before selecting it. Added regressions proving partial 3/4 coverage is quarantined and cannot leak through a Gemini-timeout fallback. GitHub Actions run **41891450786** passed: focused conversational regressions **226 passed, 16 subtests passed**; filtered repository suite **2,128 passed, 686 subtests passed, 1 warning**. Documented stale legacy modules are excluded; this is not an unfiltered full-suite pass. Offline only; no live provider calls.

### Compact 30-card conversational context (2026-11-04)

After expanding follow-up context from 12 to 30 actually shown cards, the bounded conversation snapshot still had a 10,000-character default and retained overlong identifiers/titles. The contract test expected a 4,000-character ceiling, and the new full-card context caused it to fail. The context is now capped at 6,000 characters and each shown-card summary is compacted to a 24-character ID and 48-character title while retaining price and currency. Added a regression proving all 30 shown product IDs fit in the bounded snapshot with representative conversation history and active slots. This is context compaction, not removal from the handler's 30-card shown-product cache. The same CI run **41891450786** passed the focused and filtered suites above. Live Gemini token/latency behavior remains unverified.

### Eligibility-driven conversational pagination (2026-11-04)

The conversational raw search previously stopped as soon as the unfiltered marketplace pool reached its raw target (normally 80). With several grounded query variants on page 1, irrelevant listings and accessories could fill that target before the deeper pages of the primary phrase were requested. The engine would then apply its product-type, mandatory-feature, exclusion and budget checks to an already-truncated pool.

The conversational path now passes a bounded continuation decision from the engine into the existing SearchService. After each fetched page, the engine runs the same deterministic candidate evaluator used for the final answer and checks how many ranked candidates are verified rather than awaiting semantic review. It continues the primary phrase onto the next configured page until the requested number of verified candidates is available, or the provider returns an empty deeper page / the configured page limit is reached. When this callback is active, the bounded page results are preserved for the final engine evaluation instead of being truncated a second time by the raw-row target. The AI selector remains capped at 20 and Telegram presentation remains capped at 30.

Added offline regressions for a raw pool that reaches its small raw target on page 1 but must still request page 2, preservation of the full bounded candidate set, and uncertain listings not counting as verified pagination supply. The callback is passed only through the existing Option C retrieval adapter; product validation, ranking, and delivery remain in the conversation engine.

GitHub Actions run **41892880227** on head `434ceeebbdb0d3cc0580c0686be83c8eed1568e1` passed:
- Focused conversational regressions: **227 passed, 16 subtests passed**.
- Filtered repository suite: **2,129 passed, 686 subtests passed, 1 warning**.
- The workflow excludes documented stale legacy modules; this is not an unfiltered full-suite pass.
- Offline tests only. No live AliExpress, Gemini or Telegram calls were made; current account quota, response latency and real catalogue quality remain unverified.

### Pagination diagnostics (2026-11-04)

The eligibility-driven page decision now emits a privacy-safe count-only line for each page: raw candidates, ranked candidates, verified candidates, target count and whether another page is requested. The final conversational pool summary also includes the verified count. No product titles or user message text are logged by these new lines. This makes the live run auditable: a raw pool may be large while the verified count remains low, and the log shows whether the engine correctly continued.

GitHub Actions run **41893379896** on head `87e7bc661162a740599adcd9dba2c05ed7fa2377` passed:
- Focused conversational regressions: **227 passed, 16 subtests passed**.
- Filtered repository suite: **2,129 passed, 686 subtests passed, 1 warning**.
- Documented stale legacy modules are excluded; this is not an unfiltered full-suite pass.
- Offline only; live provider behavior remains unverified.

### Shared AliExpress client concurrency guard (2026-11-04)

The live logs previously showed short `ApiCallLimit` cooldowns while the bot also has background price tracking. Searches, product-detail lookups and the tracker use the same application credentials and may run as independent async tasks. The shared `AliExpressClient` now serializes complete calls with one per-client async lock, including the bounded server-directed short cooldown and its one retry. This prevents overlapping requests from the bot's own tasks from bursting the same app key. The lock adds no fixed delay between otherwise sequential requests, so it does not impose an arbitrary latency penalty. It does not guarantee that AliExpress will never rate-limit sequential requests or calls made by another process; the existing bounded `ApiCallLimit` retry and visible error reporting remain in place.

Added a concurrent transport regression: three simultaneous search/detail operations against one client are proven to have a maximum of one in-flight HTTP request.

GitHub Actions run **41894916161** on source head `45a99fb3c0f3cfd2ecbec65138894ffe3d989264` passed:
- Focused conversational regressions: **227 passed, 16 subtests passed**.
- Filtered repository suite: **2,129 passed, 686 subtests passed, 1 warning**.
- Documented stale legacy modules remain excluded; this is not an unfiltered full-suite pass.
- Offline only; no live AliExpress, Gemini or Telegram calls were made.

### Positive-span evidence for mandatory features (2026-11-04)

A second audit found that the previous deterministic feature check could treat a requirement as proven when its words were scattered across the combined title/description (for example, "two modes" in the title and "one battery" in the description). It also treated a negated mention as positive evidence. That could bypass the semantic review bucket.

The deterministic fast path now requires the complete feature token sequence, in order, within one actual field (title OR description), allowing the existing Russian/English word-form matcher. It also checks a small context window around the matched span for common Russian/English/Ukrainian negation markers. Scattered, incomplete, or locally negated mentions remain unverified and go to the existing Gemini semantic-review path; if Gemini is unavailable, they stay hidden. This deliberately prefers an honest clarification over silently treating unknown evidence as confirmed. It does not claim to understand every possible negation or synonym; semantic review remains responsible for ambiguous wording.

Regression coverage includes affirmative contiguous evidence, tokens scattered across title and description, explicit negation before/after the phrase, and a positive compatibility mention.

GitHub Actions run **41895844850** on head `beb11166a803d4a061f621f8d6b95803ec2cc865` passed:
- Focused conversational regressions: **228 passed, 21 subtests passed**.
- Filtered repository suite: **2,131 passed, 691 subtests passed, 1 warning**.
- Documented stale legacy modules remain excluded; this is not an unfiltered full-suite pass.
- Offline only; no live provider calls were made.


### Partial product-type evidence scoring correction (2026-11-05)

A validator audit found that partial product-type coverage was assigned a lower score, but a later head_ok branch overwrote it with the exact-match score whenever the principal noun was present. This made a listing that matched the head noun and only some requested qualifiers rank as fully exact. The final exact-score branch now runs only when all requested type tokens are covered; partial matches retain the lower coverage score and the partial_type_coverage reason. Added a regression using a three-token product identity where the title contains the head noun and one qualifier but omits another. This changes ranking confidence only: it does not turn partial matches into hard rejects or bypass semantic review. CI for the new commits is pending; no live provider calls were made.


### Host-first accessory identity guard (2026-11-05)

A role audit found that accessory-first titles (for example, "case for phone") were rejected, but host-first compounds ("phone case", "camera lens", "drill bits", "battery charger") could still pass as the requested host because the host noun appeared first. Added a category-general check for recognized accessory-role nouns following the requested type within the primary title clause. Explicit relations such as "phone with case" remain valid host listings, and a later comma-separated cross-sell fragment does not reject the primary product. The first CI run exposed an important morphology false positive: Russian descriptive adjectives such as «проводные» matched the stem for «провод». The guard now skips recognized adjectives before testing accessory-role nouns, with regressions for both the false positive and the host/accessory distinctions.

Latest successful GitHub Actions run **41963951316** on head `656eec12b8a64081fee544764005377ab259217a`:
- Focused conversational regressions: **233 passed, 21 subtests passed**.
- Filtered repository suite: **2,136 passed, 691 subtests passed, 1 warning**.
- The workflow excludes documented stale legacy modules; this is not an unfiltered full-suite pass.
- Offline only. No live AliExpress, Gemini or Telegram calls were made.


### Cross-category product/component identity matrix (2026-11-05)

The product-role gate previously recognized several generic accessory nouns but did not cover enough common component roles across unrelated categories. Expanded the category-general role vocabulary to include components such as batteries, screens/displays, keyboards/mice, straps/belts, filters/hoses/nozzles, remotes, cartridges/refills, protectors/films/glass, modules/sensors and motors. This is a shared role vocabulary, not a set of category-specific search rules.

The matrix tests host-first component listings across smartphones, laptops, cameras, vacuum cleaners, watches, garden equipment, headphones and generic tools/appliances. Positive controls verify that a user can search for the component itself (keyboard, filter, wrist strap, camera battery, protective glass) without the host/accessory guard rejecting it. The first CI pass exposed two false positives: Russian adjective «аккумуляторная» was being read as an accessory noun, and the word «replacement» was being treated as the requested component's role. The guard now skips descriptive adjectives and treats replacement/spare as modifiers rather than accessory heads; the positive watch-strap fixture uses the explicit requested phrase «wrist strap» rather than relying on an unverified compound synonym.

Latest successful GitHub Actions run **41964699548** on head `0fe74c69b3316a5176add353844ca66df8444c24`:
- Focused conversational regressions: **235 passed, 21 subtests passed**.
- Filtered repository suite: **2,138 passed, 691 subtests passed, 1 warning**.
- Documented stale legacy modules remain excluded; this is not an unfiltered full-suite pass.
- Offline only; no live AliExpress, Gemini or Telegram calls were made.


### End-to-end cross-category pool-evaluator regression (2026-11-05)

Added a second, higher-level regression matrix through the actual `ConversationSearchEngine._evaluate_pool()` used by production, not only the standalone type validator. For eight unrelated product families (smartphones, laptops, cameras, vacuum cleaners, garden hoses, headphones, electric drills and smartwatches), each pool contains a valid host product and a more popular host-first component listing. The evaluator must retain the actual product and reject the component even when the component has substantially higher sales. This checks the full type-gate → filter → dedup → rank path and guards against popularity outranking product identity.

Latest successful GitHub Actions run **41964867978** on code head `dc1f95e35da545a4c7925b4071200c9fb553276f`:
- Focused conversational regressions: **236 passed, 29 subtests passed**.
- Filtered repository suite: **2,139 passed, 699 subtests passed, 1 warning**.
- Documented stale legacy modules remain excluded; this is not an unfiltered full-suite pass.
- Offline only; no live AliExpress, Gemini or Telegram calls were made.


### Conversational retrieval wiring correction (2026-11-05)
- The earlier alternate-query work was present in the legacy structured-intent route, but the active conversational engine explicitly builds its own SearchRequest and does not call that legacy intent parser. Therefore the alternate phrase was not reaching the real Telegram search path.
- The conversation-turn interpreter now returns at most one bounded marketplace_terms phrase for a fresh search. The AI service rejects same-script phrases and caps length/word count; the engine copies the accepted phrase into SearchRequest.intent_marketplace_terms.
- services.query_variants then adds it as a secondary query only when its script differs from the user's original wording. The primary phrase remains first. Refinements and other conversation actions do not inject a new alternate phrase.
- Added an offline engine integration regression proving the phrase reaches the actual retrieval strategy without replacing the primary query. This is a wiring fix, not proof of live AliExpress recall; validate the marketplace's actual response and downstream candidate-loss counts separately.

### Cross-language marketplace query route (2026-11-05)
- Structured query intent now supports at most one concise alternate seller-catalogue phrase for the same requested product, usually English when the user writes in Russian/Ukrainian (or vice versa).
- The existing Gemini intent call proposes the phrase; schema normalization limits it to two candidate strings, each at most 80 characters / 6 words. Only the first is stored on a fresh search.
- Retrieval adds the phrase only when its dominant writing system differs from the user's original wording (so it remains useful even when the primary parser query is already translated). The original/parser query remains first and unchanged. The extra route does not alter product identity, budget, compatibility, required/excluded attributes or ranking.
- Every candidate still passes the existing product-type, feature, exclusion and relevance checks. If Gemini is unavailable or returns no alternate, deterministic search behavior is unchanged.
- This is a bounded recall route, not evidence of live marketplace success. Verify title-language coverage and candidate-loss rates across unrelated product categories before treating it as complete.

### Cross-category feature-review routing matrix (2026-11-05)
- Added an eight-family integration matrix through the production ConversationSearchEngine pool evaluator and AI-selection boundary: cordless drill, headlamp, wireless headset, bagless vacuum, garden hose, water filter, watch strap and security camera.
- Each fixture uses an English seller-style title with a Russian requested type/mandatory feature. The deterministic layer must quarantine missing feature evidence, mark the candidate unsafe for direct display, and pass the feature plus identity-review IDs to the bounded AI selector instead of silently dropping the candidate.
- The selector is mocked in this routing test. It proves category-agnostic routing only; it does not test Gemini's semantic judgement or live AliExpress recall. Separate AIService tests verify that selected IDs require exact quotes from the listing fields.

### AliExpress affiliate API evidence limitation (2026-11-05)
- Inspected the official AliExpress Affiliate `product.query` and `productdetail.get` documentation. The documented response examples expose title, category, prices, image/video URLs, store, ratings/sales and affiliate fields; they do not expose a product-description/specification field.
- The current `services/marketplaces/aliexpress.py` mapper therefore does not populate `Product.description`. Do not claim that the Portals Affiliate API can supply seller descriptions/specifications or add an unverified field. The `productdetail.get` method exists, but its documented response example still does not establish description/specification availability.
- Consequence: a mandatory attribute absent from the returned title remains UNKNOWN and must stay quarantined unless another supported source provides grounded evidence. Do not relax this honesty gate or infer features from category/popularity. A separate officially supported detail/specification source would need verification before implementation.
- Official references: https://developer.alibaba.com/docs/api.htm?apiId=45803 (`product.query`) and https://developer.alibaba.com/docs/doc.htm?articleId=48595&docType=2&treeId=674 (`productdetail.get`).

### Preserve complete mandatory phrases in retrieval (2026-11-05)
- Found that `build_retrieval_strategy()` removed any feature/compatibility token whose first four Cyrillic characters matched a product-type token. This could delete a distinguishing noun even when it carries the attribute relationship, e.g. product type `аккумуляторный шуруповерт` plus mandatory feature `двумя аккумуляторами` became a retrieval phrase containing only `двумя`.
- Changed retrieval construction to preserve the full normalized feature and compatibility phrases, including nouns/stems that also occur in the product type. Small intentional duplication is preferable to silently deleting the user's required attribute. Use-case simplification remains separate.
- Added regressions for shared type/feature nouns and shared type/compatibility nouns, plus the eight-family feature-review routing matrix described above. Latest GitHub Actions run 41967175369 on head 76f0a4851a0fbd71a826fa5ee71306746f0856f9 passed: focused suite 243 passed / 37 subtests; filtered repository suite 2,149 passed / 715 subtests / 1 warning. Stale legacy modules remain excluded.

### Cross-language seller phrase on refinement turns (2026-11-05)
- Extended the bounded marketplace_terms hint from fresh searches to search-producing refine, correction and alternatives turns. Gemini receives the bounded active-slot context and can form one alternate-script seller phrase for the same product plus explicitly active mandatory feature/compatibility constraints.
- The phrase must not add a product, model, accessory, brand, unrequested feature, budget or exclusion. Compare/clarify/respond turns cannot attach it. The primary query remains first; the alternate phrase remains secondary and passes through the same candidate validation.
- Increased the bounded phrase cap from 80 characters / 6 words to 120 characters / 10 words to allow a short product-plus-feature phrase. Same-script phrases remain suppressed by query_variants.
- Added tests for parser acceptance/rejection and for an active two-battery refinement reaching the retrieval strategy. Latest GitHub Actions run 41967175369 passed: focused suite 243 passed / 37 subtests; filtered repository suite 2,149 passed / 715 subtests / 1 warning. Stale legacy modules remain excluded.


### Seller-language context reaches semantic candidate review (2026-11-05)
- The cross-language retrieval phrase was already reaching AliExpress and the type validator already routes different-script titles to semantic identity review rather than auto-accepting them. However, the final Gemini selector received only the original-language query and the listing, not the interpreter's bounded seller-language phrase.
- Passed the single bounded `intent_marketplace_terms` phrase through the engine to `AIService.select_conversation_candidates`. Gemini is explicitly told this is a language/terminology hint only, never product evidence; selected products still require exact quotes from their own title/description for identity and each mandatory feature.
- Added a prompt regression for an English seller phrase attached to a Russian request. This checks context propagation and preserves the evidence gate; it does not claim that Gemini's semantic decision or live AliExpress results have been validated.


### Carry seller-language context into grounded candidate selection (2026-11-05)
- The different-script identity validator already defers English seller titles for Russian/Ukrainian requests to semantic review; it does not auto-accept them. The eight-family cross-language feature matrix exercises that route.
- Found that the final Gemini selector received the original-language request and listing text, but not the interpreter's bounded alternate seller phrase. The engine now passes that phrase to the production AIService selector.
- The selector prompt labels the phrase strictly as a language/terminology hint, never as evidence. Every selected identity and mandatory feature still requires an exact quote from the actual listing title/description, and the quote is checked against the supplied field before the ID is accepted.
- Kept compatibility with injected/older selector adapters: the engine inspects the callable signature and only supplies the optional argument when supported. The first CI attempt caught two legacy test adapters without the new keyword; the compatibility guard was added and the focused suite passed on head `f037373a7e66aa6a3e287ccc26075e1cc89d49f9`.
- Added a further engine-boundary regression to verify the seller phrase reaches the semantic review selector. Latest run for head `86557e4be261c13e47a6e5e10835f7b5b780ee90` is pending at the time of this note.
- This verifies data propagation and evidence-gate wiring offline only. It does not prove Gemini's semantic accuracy or live AliExpress catalogue recall. No live provider calls were made.

- Final verification for the current documentation/code head `566e6a9c7e8deb0e649157f93eda97eaa6798329`: GitHub Actions run [41968566566](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/41968566566) passed. Focused conversational regressions: **245 passed / 37 subtests**. Filtered repository suite: **2,151 passed / 715 subtests / 1 warning**. Documented stale legacy modules remain excluded; this is not an unfiltered full-suite pass. Offline only; no live provider calls.

- Added a positive cross-language evidence regression: for a Russian mandatory feature `двумя аккумуляторами`, Gemini may cite the exact English listing span `2 batteries`; AIService accepts the ID only because that quote is present in the actual title. This validates quote grounding across languages, not the model's semantic interpretation.
- Latest successful CI on code head `64c227bd6f66498ef5c08982c2d8050a7419df4a`: run [41968890387](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/41968890387), focused **246 passed / 37 subtests**; filtered repository suite **2,152 passed / 715 subtests / 1 warning**. Stale legacy modules remain excluded. Offline only.


### Official AliExpress richer product-data route researched (2026-11-05)
- The current affiliate methods used by this bot (`aliexpress.affiliate.product.query` and `aliexpress.affiliate.productdetail.get`) document title, price, category, store, sales/feedback and affiliate-link fields, but do not expose the full `detail` HTML / item-property / SKU-property structure needed to verify many mandatory product features.
- Found an official AliExpress Open Platform alternative: `aliexpress.ds.product.get` in the AE-Dropshipper API. Its documented response includes `ae_item_base_info_dto.detail` / `mobile_detail`, `ae_item_properties`, SKU properties and stock/price data. Official reference: https://developer.alibaba.com/docs/api.htm?apiId=60452 (response fields at lines 94-165 and 172-229; item attributes at lines 347-383).
- This method is marked **authorization required**. AliExpress' Dropshipping onboarding guide says Dropshipping API access is for users who join the Dropshipping Center and requires an access token/session: https://developer.alibaba.com/docs/doc.htm?articleId=120418&docType=1&treeId=762. This is a different access path from the affiliate Portals API currently configured for the bot.
- Do NOT silently wire this into production or assume the current affiliate app key can call it. It is a possible AliExpress-native enrichment path only if the owner obtains the required Dropshipper access/token and confirms its use. Affiliate product discovery and affiliate promotion links can remain on the existing Portals API; the optional detail lookup would only enrich the same AliExpress product IDs.
- Until such access is available, descriptions/specifications missing from the affiliate API remain UNKNOWN; Gemini must not infer mandatory attributes from a seller phrase or category label. Keep the evidence gate strict.


### Cross-language titles for requested accessory products (2026-11-05)
- Found a category-general false-negative in `validate_product_type`: the generic accessory-role marker set includes nouns that are also products in their own right (case, band, filter, etc.). When a Russian request named the accessory but the English seller title began with that noun (e.g. "Phone case for iPhone"), the validator could classify it as an accessory for a host and reject it before cross-language semantic review.
- Updated the role gate: when the requested product type itself is a generic component/accessory, a leading accessory noun in a different-script title is quarantined for semantic identity review instead of hard-rejected. It is NOT auto-accepted; the bounded Gemini selector must still establish identity from the actual listing text. Host-product searches (e.g. requesting a phone and seeing a phone case) retain the hard rejection path.
- Added validator and engine-level regressions for phone cases, watch bands and water filters. Existing host/accessory rejection tests remain in place.
- CI runs for this patch are pending; do not claim this change passed until the run for the current PR head completes.


- Follow-up integration correction: the engine's report builder now recognizes `requested_component_identity_requires_semantic_review` as a semantic-review verdict. Generic bundle heads keep their original `type_identity_requires_semantic_review` reason and route. Component detection checks actual non-adjective accessory nouns, not broad four-character stems, so modifiers such as `аккумуляторный` do not turn a drill into an accessory request.
- Current head `dbfedeb9e2e420d1e7178ccd684837c4c97480b9`: focused offline CI passed after these corrections; the filtered repository suite is still running. The earlier failures were useful regressions: they exposed both the missing report handoff and the over-broad adjective/stem classification. Do not count those failed runs as verification.
- Completed verification for code head `dbfedeb9e2e420d1e7178ccd684837c4c97480b9`: workflow [41970208379](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/41970208379) passed. Focused offline suite: **249 passed / 43 subtests**. Filtered repository suite: **2,155 passed / 721 subtests / 1 warning**. The repository suite intentionally excludes the 17 documented stale legacy modules; this is not an unfiltered full-suite pass. Latest documentation-only head `4f8f500966915e8665cfcd9b710ff4b1a33782b5` has a separate CI run in progress.
- Latest PR head `b0c5bf716ebcba25cbf4de377087a5f0b69b7b23` (documentation/PR-body update after the code verification) also passed workflow [41970355845](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/41970355845): focused **249 passed / 43 subtests**; filtered repository suite **2,155 passed / 721 subtests / 1 warning**. This remains an offline, filtered suite; no live AliExpress/Gemini/Telegram calls were made.

### Accessory-role adjective false positive (2026-11-05)

- Continued the cross-category type/role audit and found that `is_accessory_product_type()` used Russian stem matching without excluding descriptive adjectives.
- This could classify a host product such as `аккумуляторный шуруповёрт` as an accessory because the adjective shares the stem of the noun `аккумулятор`. In a request containing a later `на ...` phrase, that could misroute a technical specification (for example, voltage) into the hard compatibility slot.
- The helper now ignores adjective tokens while continuing to recognize actual component nouns. Added positive and negative regression cases across cordless tools, wired tools, headphones, camera batteries, watch straps and drill attachments.
- Code commit: `69a0bcde167c324540d1d7e9695ad6dad9be18ef`; test commit: `95581195f867dadaeec8bd65cd7058676f6cf130`.
- CI run `41971390777` is pending at the time of this update. Do not count the change as verified until the focused and filtered repository suites complete.
- Offline only; no live Gemini, AliExpress or Telegram calls were made.

### Fresh-search deterministic preservation of explicit features (2026-11-05)

- A fallback-path audit found that `ConversationEngineState.record_slots()` deliberately skipped every feature span on a `NEW_SEARCH` turn. If Gemini interpretation timed out or was unavailable, an explicit connector-led requirement such as `с двумя аккумуляторами` could therefore disappear from the ledger and the retrieval phrase before AliExpress was called.
- The state now preserves only connector-led feature phrases already isolated by the deterministic parser on a fresh search; arbitrary request scaffolding (for example, `покажи 5 вариантов`) is still excluded. Explicit connector features also bypass the generic count/characteristic-word suppression, so `с двумя батареями` is not lost as a generic battery mention.
- Added offline regressions for both Russian battery phrasings and for request-scaffolding exclusion. Updated a product-switch fixture so its listing actually satisfies the explicit two-battery requirement; the previous fixture had no evidence for a requirement that is now correctly enforced.
- Related commits: `f7c8ac2`, `d9c6b83`, `95efcbf`, `3b0323c`, `007ba85`, `ca070b9`.
- Latest CI run `41972045632`: focused conversational regressions have passed; filtered repository suite was still running at the time of this note. Do not count the whole run as successful until it completes.
- Offline only; no live Gemini, AliExpress or Telegram calls were made.

### Semantic-review hard-requirement cap (2026-11-05)

- Auditing the final Gemini candidate-selection boundary found that `AIService.select_conversation_candidates()` silently truncated the active mandatory feature/compatibility list to the first 12 entries. With a long conversation, a candidate could therefore be approved without the selector checking later hard requirements.
- The selector now deduplicates the bounded list and fails closed when more than 12 distinct hard requirements are active. It does not call Gemini or approve uncertain candidates under a silently weakened requirement set; the engine's existing fallback keeps deterministic-safe candidates and quarantines semantic-review candidates.
- Added an offline regression proving a 13-requirement request skips the selector call and returns an unusable result rather than dropping constraints.
- Code/test commits: `502a3a8`, `203d427`. CI run `41972357712` is in progress.
- The preceding fresh-search feature fallback passed CI run `41972045632`: focused **254 passed / 43 subtests**; filtered repository suite **2,160 passed / 721 subtests / 1 warning**. The 17 documented stale legacy modules remain excluded; this is not an unfiltered full-suite pass.
- Offline only; no live Gemini, AliExpress or Telegram calls were made.

### Primary-route API error recovery (2026-11-05)

- The raw conversational retrieval loop previously stopped immediately when every provider failed on the first query variant. With AliExpress as the only configured provider, a query-specific error on the primary phrase could prevent the different-script seller phrase from being tried at all.
- Raw conversational retrieval now still attempts the remaining bounded page-one variants after a primary-route error. It retains any successful alternate-route products, then stops deeper pagination of the failed primary phrase instead of repeating it. Non-raw legacy search behavior is unchanged.
- Added an offline regression where the primary route returns an AliExpress API error and the alternate seller phrase returns a real candidate; the alternate candidate must survive and the route failure remains recorded.
- Code/test commits: `8f6a5b5`, `96c6e59`. CI run `41972799610` is in progress.
- The semantic-review hard-requirement cap passed workflow `41972357712`: focused **255 passed / 43 subtests**; filtered repository suite **2,161 passed / 721 subtests / 1 warning**. The documentation head also passed workflow `41972415498` with the same counts. Both are offline filtered-suite runs, not unfiltered full-suite passes.
- Offline only; no live Gemini, AliExpress or Telegram calls were made.

### Continue pagination on the successful alternate phrase (2026-11-05)

- Extended the primary-route API-error recovery: when the primary query fails but a bounded alternate seller phrase succeeds, deeper pages now continue on that successful alternate phrase. This preserves multi-page retrieval breadth instead of merely returning the first alternate page.
- The same engine eligibility callback still decides when enough verified candidates have been collected; pagination remains bounded by the configured page limit. If the active fallback route fails or returns an empty deeper page, traversal stops.
- Added a regression proving the primary error does not block the alternate route, page 2 uses that alternate phrase, and the engine's continuation decision stops before page 3.
- Code/test commits: `5098392`, `d0c6a9f`, `9935806`, `547bb6b`. CI run `41973338394`: focused suite passed; filtered repository suite was still running at the time of this note.
- The previous API-error fallback code head passed workflow `41972799610`: focused **256 passed / 43 subtests**; filtered repository suite **2,162 passed / 721 subtests / 1 warning**. The documentation head passed workflow `41972896883` with the same counts.
- Offline only; no live Gemini, AliExpress or Telegram calls were made.

- Final verification for documentation/code head `5699e20300bf7a0b3494dc030358eab0527631c0`: workflow `41973454655` passed. Focused regressions: **256 passed / 43 subtests**; filtered repository suite: **2,162 passed / 721 subtests / 1 warning**. The 17 documented stale legacy modules remain excluded. Offline only.

### Grounded follow-ups to shown products (2026-11-05)

A Gemini `respond` outcome is not allowed to answer deictic/anaphoric
questions about cards already shown in Telegram. References such as «он»,
«этот», «второй», «из них», and their English equivalents are routed to the
existing `COMPARE` / grounded product-answer path. This prevents a free-form
LLM response from asserting an unverified product attribute. Standalone
concept questions such as «Что означает IPX7?» remain eligible for `RESPOND`,
even while a product set is active. Offline regressions cover both branches.

### Role-labelled dialogue memory (2026-11-05)

- A central conversation audit found that Gemini received only the last six USER messages (`recent_turns`), active shopping slots, the latest pending question and shown products. The engine did not preserve the assistant's previously delivered words as dialogue context. This weakened follow-up understanding when a user referred to an earlier assistant question or explanation.
- Added a separate bounded `dialogue_history` (last 12 role-labelled user/assistant events; the engine passes the available recent events). It is deliberately separate from retrieval `turns`, so assistant messages do not alter slot provenance or turn numbering.
- The engine now passes `recent_dialogue` alongside the backward-compatible `recent_turns`. The interpreter prompt explicitly instructs Gemini to use both roles and the order of prior turns.
- The Telegram delivery layer records successfully delivered natural replies, clarification questions and grounded product explanations into the same per-user conversation ledger.
- Added an offline engine regression asserting that the interpreter receives the prior user message and the assistant's actual clarification question before interpreting the user's answer.
- Code commits: `ac34581`, `80b924f`, `c97fc3a`, `f73b26f`; regression test commits: `a3334cb`, `9bbc837`.
- CI for head `9bbc837c15df1c3742a2c0c8153d691a2f0b1c28` has not yet been observed. Do not count this change as verified until the workflow completes.
- This is an offline architecture change only; no live Telegram/Gemini/AliExpress test has been performed.

- Extended delivery integration after the initial change: successfully delivered no-result/outage messages and the post-card result-count hint are also recorded as assistant turns. This keeps the role-labelled history aligned with what the user actually saw, not merely what the engine intended to send.

### Dialogue-memory CI correction (2026-11-05)

- First CI run after the role-labelled memory integration exposed that `_bounded_conversation_context()` did not whitelist the new `recent_dialogue` field. Two context-contract tests failed with JSON parsing errors because the test extracted the prompt section after the newly inserted explanatory text; the production payload itself remained valid JSON, but the context serialization contract was not updated.
- Corrected the bounded context builder to include only validated `user` / `assistant` events, retain at most 12 entries and 1,000 characters per event, and discard the oldest transcript events first when compacting. This preserves the latest exchange while keeping the prompt under its existing 6,000-character cap.
- Expanded context tests to cover role-labelled transcript compaction and the 30-card context boundary.
- The failed run was workflow `41982000866`: **257 passed, 2 failed, 43 subtests passed** in the focused suite; the repository suite was skipped. Do not count it as a pass.
- Workflow `41982179657` on head `953bb808b73ea872abf5f56a52e28a35d89dc458` then exposed two remaining prompt-contract failures: the role guidance sat inside the delimited JSON section, confusing the test/parser boundary, and inflated the measured context length; the 30-card test consequently saw trailing prose after JSON. The serialized payload itself is built with `json.dumps` and remains structurally valid.
- Moved the role-guidance prose before the `Контекст диалога (JSON):` marker so the marked block contains only JSON. Commit `9faa8709b9b4bb5291902ef612a3ee7e59a01eb1`. Follow-up workflow [41983469021](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/41983469021) passed on code head `9faa8709b9b4bb5291902ef612a3ee7e59a01eb1`: focused regressions **259 passed / 43 subtests**; filtered repository suite **2,165 passed / 721 subtests / 1 warning**. The workflow excludes 17 documented stale legacy modules; this is not an unfiltered full-suite pass. The subsequent documentation-only head `81a573fb23f9f4582f259feb5c2e4c88187135e1` is not included in that run. Offline only; no live provider calls.


### Continue pagination on a successful alternate route when the primary is empty (2026-11-05)

- A further retrieval audit found a recall gap after the earlier API-error fallback: when the primary AliExpress phrase returned a valid empty page (not an API error) but a grounded alternate seller-language phrase returned products, page 2 still repeated the empty primary phrase. This could miss additional matching listings on the alternate route.
- Raw retrieval now records per-phrase page-one row counts. If the primary route failed or returned zero rows, deeper pagination switches to the first successful alternate route that actually returned products. A non-empty primary route remains preferred; row counts are used only to detect an empty route, never to rank or approve products. Existing eligibility checks, page limits, deduplication and relevance gates remain unchanged.
- Updated the regression so the primary phrase returns an empty page, the alternate phrase returns a candidate on page 1 and another on page 2, and page 2 must use the alternate phrase.
- Code/test commits: `36a1af0`, `85c4333`. Latest successful workflow [41983871056](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/41983871056) on code head `85c4333690f3d84623ba5be6d57a1ea55d0b4a0e`: focused **259 passed / 43 subtests**; filtered repository suite **2,165 passed / 721 subtests / 1 warning**. The workflow excludes 17 documented stale legacy modules; this is not an unfiltered full-suite pass.
- Offline only. No live AliExpress, Gemini or Telegram calls were made; live catalogue recall and latency remain unverified.


### Preserve AliExpress page order during multi-query RRF fusion (2026-11-05)

- A retrieval-ranking audit found that page 1 and page 2 of the same AliExpress phrase were being fused as independent RRF routes. This could let page 2's first result outrank page 1's 50th result, even though the marketplace had already ordered those rows within one query.
- Raw retrieval now groups provider rows by the exact grounded query phrase and appends deeper pages to that same ordered route. RRF still combines distinct user-language/seller-language phrases, but page order within each phrase is preserved. The same grouped ranking is used by the pagination eligibility callback and final fused pool.
- Added a regression with 50 page-one results, an empty alternate phrase and one page-two result; page-one rank 50 must remain ahead of page-two rank 1 from the same phrase.
- Code/test commits: `96722f0`, `2fe07f4`, `984aeab`. Successful workflow [41984178716](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/41984178716) on code head `984aeab62313dec014344f1fbf57a57bb11e8e3a`: focused **260 passed / 43 subtests**; filtered repository suite **2,166 passed / 721 subtests / 1 warning**. The 17 documented stale legacy modules are excluded; this is not an unfiltered full-suite pass.
- Offline only; no live AliExpress/Gemini/Telegram calls were made.


## 2026-11-05 — conversational AI review pool width

A retrieval/selection audit found that the provider could collect a deep raw
pool, but the conversational engine truncated ranked candidates to the
30-card UI limit before calling Gemini. The selector then saw at most 20
candidates, so it could not choose the best 20 from the broader retrieved set.

The engine now keeps up to 80 deterministically validated/ranked candidates
internally when the conversational selector is bound. For the default 30-card
view, Gemini reviews up to 70 candidates and returns at most 20 selected IDs;
up to 10 remaining already-gated candidates may fill the presentation tail
only when the model fills its 20-result selection. When the user requests
fewer than 20 cards, Gemini can review the full 80-candidate pool because no
deterministic tail is needed. The UI remains capped at 30 (or the user's
smaller requested count). The no-selector path also explicitly enforces that
presentation cap. This does not expand AliExpress page limits or claim a
full-catalogue crawl; it fixes the premature cut between retrieval and AI
selection.

Regression coverage added for an 80-candidate pool, a 70-candidate Gemini
review window, 20 AI-selected results, a ten-card tail, and the no-selector
30-card cap. Live Gemini/AliExpress/Telegram behavior remains unverified.



### Retrieval target aligned with broad AI review (2026-11-05)

A second audit found that the engine's pagination callback still stopped as
soon as it had 30 deterministically safe candidates, even though the later
selector now supports an internal pool of up to 80. This made the expanded
candidate-pool limit ineffective for many ordinary searches.

Fixed in `services/convsearch/engine.py`:
- When the conversational selector is callable and reports available, the
  retrieval continuation target is now 80 safe/ranked candidates, not the
  30-card presentation count.
- If Gemini is unavailable, retrieval keeps the smaller requested display
  target and does not spend extra AliExpress calls collecting an unused AI pool.
- The final AI-selection stage uses the same availability decision when
  applying the internal 80-candidate cap.

Added offline integration regressions proving that retrieval continues at 30
and stops at 80 when AI selection is available, while an unavailable selector
with a five-card request stops at five. This validates the engine callback
contract; the fake backend does not validate real AliExpress pagination.


### Conversational retrieval breadth and wall-clock budget (2026-11-05)

The conversational RAW retrieval path now paginates every grounded query
variant independently. A phrase continues to the next configured AliExpress
page only when it returned rows on the preceding page; empty and failed
routes are retired without preventing other routes from continuing. Pages
within each phrase retain marketplace order, and distinct phrase rankings are
fused with RRF. The legacy non-RAW search path keeps its primary-only deeper
pagination.

The conversational retrieval path also has a 30-second total wall-clock
budget. It stops requesting additional routes/pages when the budget expires,
but preserves completed route results and passes them through the normal
eligibility, hard-constraint, ranking and AI-selection stages. This budget
does not apply to unrelated search/feed/photo flows. The per-request AliExpress
timeout and its one transient retry remain unchanged.

Regression coverage:
- multiple grounded phrases are each paginated and fused;
- a slow later route cannot erase an already completed route;
- the configured page limit and earlier page-order regressions remain covered.

The conversational regression workflow now also runs on pushes to
`work/consultant-reliability`, so commits on the active working branch do not
depend solely on PR event timing to receive CI coverage.

CI results for the new deadline and regression test must be read from the
workflow run on the commit containing both; earlier successful runs predate
this change and do not validate it.


### Product-question coverage across the full displayed page (2026-11-05)

A comparison-path audit found a mismatch: the Telegram handler passed every
actually browsed product to the product assistant, but AIService silently
truncated the factual prompt to the first eight products. Questions about later
cards could therefore be answered without considering them.

Raised the grounded product-question context cap to 30, matching the maximum
presentation page. Only product_facts fields are sent; URLs and unreported
attributes remain excluded. Added an offline regression that supplies 30
products and verifies that both the first and last product IDs reach the
question prompt. This change affects factual question/comparison prompts only;
candidate selection remains capped at 20 and presentation at 30.

### Grounded product-question answers (2026-11-05)

Product-question answers previously accepted free-form Gemini prose after
checking only whether its numbers appeared somewhere in the supplied facts.
That did not verify qualitative claims (for example, materials or included
accessories). Replaced that path with structured evidence: Gemini must return
the exact product ID, source field, and a contiguous quote from the listing.
The application verifies the quote against the actual title/description and
constructs the user-facing answer itself; model-authored factual prose is not
sent to Telegram. Missing descriptions remain absent, not inferred. Added
offline tests for valid evidence, invented quotes, and unknown product IDs.

### Conditional budget rules for conversational AI selection (2026-11-05)

A prompt audit found that the Gemini candidate selector was told to enforce a
hard price ceiling even when the user had not supplied any budget. The engine
only added a budget phrase to the selector query when an active budget slot
existed, so the prompt contained a contradictory, unconditional budget rule.

The selector now receives the active budget explicitly. With a budget, the
prompt treats its stated amount/currency as a hard constraint. Without a
budget, it explicitly forbids inventing an upper price limit; known positive
price and currency are still required for an honest product card. Compatibility
with injected selectors that implement the older signature is preserved.
Offline prompt regressions cover both the no-budget and explicit-budget paths.

This fixes a prompt-contract defect; it does not by itself establish live
catalogue relevance or explain every short result set. No live provider calls
were made for this change.

### Disambiguate price questions from recommendation wording (2026-11-05)

- A ProductAssistant audit found that the substring `стоит` was treated as a price marker. This misclassified ordinary recommendation questions such as «Что из этого стоит выбрать?» as price comparisons and could return the cheapest item instead of answering the actual question.
- Price intent now requires explicit price/cost wording (including «сколько стоит», «во сколько», «стоимость», and common English price phrases). The ambiguous verb «стоит» alone no longer triggers numeric price comparison.
- Added offline regressions for both the ambiguous recommendation wording and an explicit «сколько стоит» question.
- This is a question-routing correction; it does not infer product quality or add product-specific rules.
- Successful GitHub Actions run **42007330810** on code head `3ab66744c9108f1878ef3d37c0a7e2a0ecaac1d3`: focused regressions **268 passed / 43 subtests**; filtered repository suite **2,183 passed / 721 subtests / 1 warning**. The workflow excludes 17 documented stale legacy modules; this is not an unfiltered full-suite pass. Offline only.

### Missing-price listings when no budget is specified (2026-11-05)

A follow-up audit found a second contradiction in the candidate-selection
contract. The no-budget branch correctly said not to invent a price ceiling,
but then still instructed Gemini to require a known positive price and
currency. The product model and card formatter explicitly support missing
prices and render them as unknown, so this rule could unnecessarily shrink
otherwise relevant results even when the buyer had not asked for a price limit.

The no-budget prompt now says a missing price is not, by itself, a reason to
reject a relevant listing; the card will show that the price was not supplied.
The model is still forbidden to invent a price or currency. The explicit-budget
branch remains strict: a known price/currency is needed to enforce the limit.
Added a prompt regression for this distinction.

### CI verification — 2026-11-05, head f4e4b36731a8c26969c73525b4a2b88dc7a1b536

GitHub Actions workflow run **42007511406** completed successfully on the
current PR head. The job ran:

- Focused conversational regressions: **268 passed, 43 subtests passed**.
- Filtered repository suite: **2,183 passed, 721 subtests passed, 1 warning**.

The repository command excludes 17 documented stale legacy modules; this is
not an unfiltered full-suite pass. The warning is a dependency deprecation
warning from the installed Google GenAI package. This run validates offline
regressions only. It does not test live Telegram delivery, current AliExpress
catalogue recall/quota, or Gemini latency and semantic decisions.

PR #7 remains open, draft and unmerged. The base branch `main` is unchanged.

### Category-neutral candidate identity prompt — 2026-11-05

A prompt review found one illustrative welding-specific example inside the
otherwise category-general Gemini candidate-selection rules. It was not a
runtime filter, but it unnecessarily anchored the model's identity guidance
to one product family. Replaced it with a category-neutral instruction to
compare the listing's main item, its role, and explicitly evidenced
characteristics. Added a prompt regression asserting that the general rule is
present and the category-specific example is absent.

This is a prompt-generalization change only; deterministic identity gates,
listing-evidence validation, retrieval and the 20/30 limits are unchanged.
The change still requires the new branch CI run before it is considered
verified.

### Shown-product questions with use-case context — 2026-11-05

A dialogue-intent audit found that a question such as «А что из этого лучше
для дома?» could be treated as a refinement because the new use-case phrase
was not recognized as context for a comparison of already shown products.
The state classifier now treats demonstrative references and the target of a
grammatical use-case phrase as dialogue scaffolding when evaluating whether a
question refers to the current shown set. A genuinely named product remains
unknown content and is not converted into a shown-product comparison.

Added regressions for «что из этого лучше для дома?» and «какой из них лучше
для дома?». These tests exercise deterministic intent classification; they
do not simulate Gemini or live Telegram.

### Follow-up safeguards for shown-product question classification — 2026-11-05

The first regression run for use-case-aware comparisons exposed an important
boundary: without an active product type or delivered shown cards, a first
request such as «Найди что-нибудь полезное для дома» must remain a new search,
not be interpreted as a comparison. The shown-set question detector now
requires an existing product context.

A second interaction boundary is preserved: an explicit request for more or
different results takes precedence over the broad comparison grammar. The
alternatives detector now treats a grammatical use-case target («для дома»)
as context rather than a newly named product, so «А покажи ещё варианты для
дома» remains an alternatives request. Added regressions for the first-search
boundary and the alternatives-with-use-case case.

The initial CI attempt exposed these false positives and a misplaced prompt
assertion in the test file; both were corrected before the current CI run.


### Alternatives require an existing task context (2026-11-05)

A final intent-boundary review found that the alternatives grammar could
classify «Покажи другие варианты» as a continuation after a non-shopping
turn, because it checked only the alternation wording and absence of new
product terms. Alternatives now require an active task slot or delivered
shown products. Added a regression proving that a greeting followed by an
alternatives phrase does not create a phantom shopping continuation. Existing
same-task alternatives, including use-case context, remain covered.


### Alternatives-context regression — CI verified (2026-11-05)

The alternatives classifier now refuses to infer a continuation when there
is no active task slot and no delivered product context. This closes the case
where a greeting followed by «Покажи другие варианты» could be misrouted as
an alternatives search. The existing household/use-case alternatives
regression remains intact.

GitHub Actions PR workflow **42020885457** completed successfully on the
current PR head **274c24cf68d41cfc171025522e6d8d29a8bba064**:
- Focused conversational regressions: **270 passed, 45 subtests passed**.
- Filtered repository suite: **2,185 passed, 723 subtests passed, 1 warning**.
- The repository workflow excludes 17 documented stale legacy modules; this
  is not an unfiltered full-suite pass. The warning is a dependency
  deprecation warning. Tests are offline; no live Telegram, Gemini or
  AliExpress request was made.
- PR #7 remains open, draft and unmerged; the base branch remains unchanged.


### Bounded latency for shown-product questions (2026-11-05)

A latency audit found that factual questions about already-shown products
called Gemini without a whole-operation deadline. The underlying model call
could consume its full per-model timeout and then try a fallback model, so a
simple follow-up question could wait substantially longer than the main turn
interpreter. The product-question operation now has an eight-second hard cap
(or the lower configured AI timeout). On timeout it returns no AI answer and
the existing honest no-data fallback is used; it does not fabricate a reply.
Added an offline regression with a deliberately slow model stub.


### Product-question timeout test compatibility correction (2026-11-05)

The first CI run after adding the eight-second product-question deadline reported five failures in the legacy AI-operation test stub: its overridden _generate method did not accept the optional operation keyword. The production call contract is unchanged; the stub now accepts the optional telemetry argument like the base method. This is a test-double compatibility fix, not a production behavior change. The corrected head must pass CI before this latency change is considered verified.


### Telegram client-side rate limiting dependency (2026-11-05)

A startup-log audit found the bot's own wiring intentionally enables PTB's AIORateLimiter when installed, but requirements.txt installed only the job-queue extra. Consequently normal installs emitted a warning and ran without client-side Telegram throttling. Added the rate-limiter extra to the pinned python-telegram-bot dependency and an offline startup regression asserting the limiter is available. This protects message/card navigation delivery from avoidable Telegram flood-limit bursts; it does not change AliExpress API throttling.


### Product-question timeout and Telegram rate limiter — CI verified (2026-11-05)

The product-question path now has an eight-second whole-operation deadline (or the lower configured AI timeout), so an already-shown product question cannot wait through a full primary-model timeout plus a fallback-model timeout. Timeout returns to the existing honest no-data response. The first CI attempt caught a test-double signature mismatch for the optional operation telemetry keyword; the stub was corrected, and the production behavior then passed.

The pinned python-telegram-bot dependency now installs both the existing job-queue extra and the rate-limiter extra. This activates the AIORateLimiter already configured by bot startup; previously the dependency was absent and startup logs showed that Telegram client-side throttling was disabled. Added a startup regression asserting the limiter is importable and constructed.

GitHub Actions PR workflow **42027079330** completed successfully on head **1dd2f37c32fb562ebe446658b64e31a9624c3d23**:
- Focused conversational regressions: **270 passed, 45 subtests passed**.
- Filtered repository suite: **2,187 passed, 723 subtests passed, 1 warning**.
- The filtered command still excludes the 17 documented stale legacy modules that import removed pre-Option-C files. This is not an unfiltered full-suite pass. Tests are offline; no live Telegram, Gemini or AliExpress requests were made.
- PR #7 remains open, draft and unmerged; main is unchanged.


### Evidence-grounded recommendations over shown products (2026-11-05)

The shown-product question path previously returned verified listing quotes but did not have a structured way to make a recommendation when the buyer explicitly asked which option better matched a stated use case. The question prompt now allows a recommendation only when Gemini supplies a real product ID and at least one direct title/description quote from that same listing. The application verifies the quote and constructs the recommendation sentence itself; invalid IDs, invented quotes, missing evidence, or unspecified selection criteria cannot produce a winner. Existing claim-only answers remain supported. Added offline regressions for valid evidence, invented evidence and unknown IDs. This does not turn sparse affiliate listings into full specifications: if AliExpress did not provide relevant text, the assistant must say it cannot establish the difference.


### Evidence-grounded recommendations and malformed model output — CI verified (2026-11-05)

The already-shown product question path can now return a bounded recommendation when the buyer explicitly states a selection criterion and a listing's own title/description directly supports it. Gemini must return a real ID plus exact quote(s) from that same listing; the application validates the ID, field type and quote before composing the recommendation. If criteria are absent or evidence is missing/invalid, no winner is announced and the existing verified-quote / honest no-data behavior remains. Added regressions for valid evidence, invented quotes, unknown IDs and malformed field types.

GitHub Actions push workflow **42027370747** completed successfully on head **bae5c79461eff12d2557d31e53286d76ee7507eb**:
- Focused conversational regressions: **270 passed, 45 subtests passed**.
- Filtered repository suite: **2,191 passed, 723 subtests passed, 1 warning**.
- The same workflow excludes 17 documented stale legacy modules; this is not an unfiltered full-suite pass. Tests are offline; no live Telegram, Gemini or AliExpress requests were made.


### Clarify missing product-selection criteria (2026-11-05)

A follow-up audit of the shown-product comparison path found that an underspecified «which is better?» question with no listing evidence fell straight to a generic missing-data statement. The structured question response now supports one bounded clarification question when the buyer asks for a choice but has not supplied a criterion. The question is accepted only as short plain text with exactly one question mark and no links/markup; evidence-backed recommendations and verified factual claims retain precedence. Added offline tests for a valid clarification and rejection of multi-question/link output. This avoids inventing a winner while keeping the dialogue moving.


### Comparison clarification and structured-output hardening — CI verified (2026-11-05)

When a buyer asks which shown product is better without giving a selection criterion, Gemini may now return one short clarification question instead of the generic missing-data response. The question is accepted only as bounded plain text with exactly one question mark and no links or markup. If there is a criterion, a recommendation still requires a valid shown-product ID and exact supporting listing quote. Both recommendation evidence and ordinary factual claims now validate the field type before membership checks, so malformed JSON values cannot crash the answer path. Regression coverage includes valid clarification, multiple questions, links, invented quotes, unknown IDs and malformed evidence fields.

GitHub Actions PR workflow **42027857348** completed successfully on head **06f87a874202802468cd493269dacef267086594**:
- Focused conversational regressions: **270 passed, 45 subtests passed**.
- Filtered repository suite: **2,194 passed, 723 subtests passed, 1 warning**.
- 17 stale legacy modules remain excluded because they import removed pre-Option-C files; this is not an unfiltered full-suite pass. Offline tests only; no live Telegram, Gemini or AliExpress calls.


### Complete the shown-product comparison clarification loop (2026-11-05)

A product-comparison clarification now remains attached to the exact result session. When Gemini cannot choose responsibly because the buyer has not named a criterion, the handler stores the question and shown-session ID for 30 minutes. The next short answer is combined with the original comparison and sent back to ProductAssistant over the same already-shown products; it does not trigger a new AliExpress search. An explicit new product request or alternatives request clears this pending comparison and continues through the normal conversation engine. The user can cancel it, expired/malformed state is discarded, and /new clears it. The pending question is stored only AFTER Telegram successfully delivers it, so a failed send cannot consume the next ordinary message. User and assistant turns are recorded in dialogue history in delivery order.

Offline regressions cover pending-state round trip, expiry, malformed state, cancellation-key isolation, criterion-vs-new-task routing, the AI clarification delivery path and failed Telegram delivery. GitHub Actions PR workflow **42029090509** passed on head **ba0c031542804c7866736c4372f0842438c58500**:
- Focused conversational regressions: **277 passed, 45 subtests passed**.
- Filtered repository suite: **2,201 passed, 723 subtests passed, 1 warning**.
- The 17 documented stale pre-Option-C modules remain excluded; this is not an unfiltered full-suite pass. Offline only; live Telegram, Gemini and AliExpress behavior is still not proven by CI.


### Comparison follow-up delivery integration — CI verified (2026-11-05)

Added handler-level regressions proving that a delivered AI clarification is retained for the next user turn, that the next criterion answer is combined with the original comparison and answered against the same already-shown product session without invoking the search engine, and that a failed Telegram send does not create a pending question. The /new reset path also clears pending comparison state. This closes the compare → ask criterion → receive criterion → compare loop at the handler boundary, not just in isolated helper tests.

GitHub Actions PR workflow **42029653913** completed successfully on head **75ec450a7cdf72527c2f5a221a833a710227b6b3**:
- Focused conversational regressions: **279 passed, 45 subtests passed**.
- Filtered repository suite: **2,203 passed, 723 subtests passed, 1 warning**.
- 17 documented stale legacy modules remain excluded; this is not an unfiltered full-suite pass. Offline tests only; live AliExpress/Gemini/Telegram catalog quality and latency are not verified by this workflow.


### Preserve pending product comparison through courtesy replies (2026-11-05)

A handler-path review found that a courtesy reply such as «спасибо» could be mistaken for a selection criterion and consume a pending comparison question. Short, exact acknowledgements/greetings in Russian, Ukrainian and English are now treated as conversational asides: the pending question stays alive while the normal conversation engine handles the aside. Explicit new-product and alternatives requests still clear the pending comparison; meaningful criterion replies still resume it. Added offline coverage for courtesy phrases versus actual criteria.


### Product-comparison courtesy handling — CI verified (2026-11-05)

A short courtesy/acknowledgement (e.g. thanks, hello, got it) is no longer consumed as the buyer's missing comparison criterion. The pending product question remains alive while the normal conversation engine handles the aside. Explicit new product requests and alternatives still switch away; a meaningful criterion still resumes comparison over the same shown session.

GitHub Actions PR workflow **42030839337** completed successfully on head **9bd7a2bb3ed0ad2d008bc56d3a97036bdbf7e907**:
- Focused conversational regressions: **280 passed, 45 subtests passed**.
- Filtered repository suite: **2,204 passed, 723 subtests passed, 1 warning**.
- The 17 documented stale legacy modules remain excluded; this is not an unfiltered full-suite pass. Offline tests only; live Telegram, Gemini and AliExpress behavior remains to be checked with the user's credentials.


### Live smoke script syntax verification (2026-11-05)

The two manual live diagnostics are intentionally not executed by CI because they require the user's private AliExpress/Gemini credentials and make real API calls. The conversational-regression workflow now compiles both `smoke_aliexpress.py` and `smoke_conversational_search.py` on every run, catching syntax/import-independent compilation errors without loading `.env` or spending API quota. This verifies only that the scripts compile; it does not claim the live API smoke has run.


### Manual live smoke readiness — comparison assertion aligned to supported evidence (2026-11-05)

Before handing the live smoke scripts to the user, aligned the shown-product comparison check with a criterion that the current ProductAssistant can answer deterministically from returned marketplace fields (price). The prior smoke used a rating question even though rating is not yet an accepted evidence field for the grounded Gemini recommendation path; that could falsely fail the diagnostic despite correct search. The script now asks which shown product is cheaper, which is answered only from known prices and within the same currency. PR #7 remains draft and unmerged. Latest verified CI before this adjustment: 280 focused tests + 45 subtests; filtered suite 2,204 passed + 723 subtests, 1 warning. CI must pass again on the updated head before asking for live checks.

### Candidate-selector latency refinement (2026-11-05)

Live Telegram logs on the current branch show that Gemini turn interpretation
and product-question calls sometimes complete, but the post-retrieval
candidate selector repeatedly reaches its independent 15-second deadline.
The selector is a bounded classification/ranking step over real listings
after deterministic identity, feature, exclusion and budget checks; it does
not need the default automatic reasoning depth used for open-ended tasks.

The conversation_selection Gemini operation now explicitly requests low
thinking effort and JSON response MIME. Other Gemini operations retain their
existing generation defaults. This is a latency-focused model-call
configuration, not a relaxation of candidate identity/evidence validation or
the 20-item selection / 30-card presentation limits. Added an offline
transport-contract regression for the operation-specific config and a
regression that unrelated Gemini operations receive no new config.

The selector also logs candidate count, active mandatory-feature count,
identity-review count and prompt character count (never prompt text or user
content) so the next live run can correlate selector latency with request
size. This change is pending CI and live Telegram verification; it is not yet
evidence that selector timeouts are resolved.

### Selector timeout fallback correction (2026-11-05)

An offline code-path audit found that the selector's outer 15-second deadline
could cancel `_generate` before its normal 20-second per-model timeout expired.
That meant the configured fallback model was never reached when the primary
selection model stalled, even though `_generate` already had a fallback chain.

Conversation selection now caps each model attempt at 6.5 seconds and permits
the configured fallback model after a primary client timeout. The existing
outer 15-second selector deadline remains the hard total bound; quota failures
still stop immediately, and timeouts in unrelated Gemini operations retain
their previous no-retry policy. Both model attempts use the low-thinking JSON
configuration. Added offline regressions for selector timeout → fallback
success and for preserving the no-timeout-retry policy on other operations.

This is a code-path correction based on the current timeout nesting, not a
claim that live Gemini latency is solved. Confirm on the next Telegram run
using per-attempt model/duration logs and the final `ai_selection` outcome.


### Gemini fallback logging compatibility correction — CI verified (2026-11-05)

The selector timeout-fallback change initially altered the warning text used by the existing model-unavailable observability contract. The fallback itself was being attempted correctly, but the repository suite caught the log-message regression. Restored the established `is unavailable` wording specifically for classified model-unavailable failures; timeout fallback retains the distinct `failed; trying fallback` wording. No test was weakened or removed.

GitHub Actions run **42056187765** on head **a9f855bbcc5b37025badf5897b7017bb79d26450** completed successfully:
- Focused conversational regressions: **280 passed, 45 subtests passed**.
- Filtered repository suite: **2,208 passed, 723 subtests passed, 1 warning**.
- The 17 documented stale legacy modules remain excluded; this is not an unfiltered full-suite pass. CI is offline and does not verify live Gemini latency, AliExpress recall or Telegram delivery.


### AliExpress product-evidence source audit (2026-11-05)

A source-level audit was completed before changing product validation. The current provider is correctly wired to the AliExpress Portals Affiliate API:
- `aliexpress.affiliate.product.query` is documented as an unauthenticated affiliate-product search method. Its documented product response exposes title, category labels, product URL/ID, images/video, prices/currencies, sales volume, evaluation rate, shop identifiers/URL, shipping estimate and promotion link. It does **not** document a long description, structured specifications, SKU attribute table, or review text.
- `aliexpress.affiliate.productdetail.get` is already implemented as `AliExpressProvider.get_product()`, but it is still the Affiliate product-detail method; its documented response is still affiliate product metadata, not the DS product specification payload. Calling it for every search result would add API latency/quota without supplying the missing specification evidence.
- The `Product` model's `description` is optional and the Affiliate provider currently leaves it unset because the documented response does not supply it. This is intentional honest-data behavior, not a missed mapping.

Official documentation reviewed:
- Affiliate product query method and its documented response fields: https://developer.alibaba.com/docs/api.htm?apiId=45803 (request/response fields; response example).
- Affiliate product-detail method: https://developer.alibaba.com/docs/api.htm?apiId=48595.
- The separate `aliexpress.ds.product.get` method belongs to the AliExpress Dropshipping API, is marked as requiring authorization, and its current official documentation category is explicitly labelled **Dropshipping (deprecated)**: https://developer.alibaba.com/docs/doc.htm?articleId=60452&docType=2&treeId=762.
- The historical DS onboarding documentation describes a separate Drop Shipping app, OAuth/session authorization and Dropshipping Center participation; this is not the Portals affiliate credential flow: https://developer.alibaba.com/docs/doc.htm?articleId=120418&docType=1&treeId=762.

Architecture decision:
1. Keep the Portals Affiliate API as the production search/price/affiliate-link source. Do not assume its app key can call DS methods.
2. Do not integrate the deprecated DS API as a production dependency based only on old examples or third-party SDKs. It would require a separately approved app/authorization and a live permission/response check; none is established for this project.
3. Do not scrape product pages or infer missing specifications from category, price, rating, images, or generic product conventions as a shortcut.
4. Continue to represent unavailable product attributes as UNKNOWN. Keep candidates with unverified mandatory requirements quarantined; do not describe them as confirmed matches. If no supported source can verify a hard requirement, the consultant must disclose that limitation and ask for a product link/details or a relaxed criterion rather than inventing an answer.
5. A future data-source integration must first pass a small isolated feasibility spike: documented current API status, app permission, real response schema, latency/quota, and a safe mapping into Product evidence. Only then should the product-validation pipeline consume it.

This is an evidence-based architectural finding, not a claim that the product-data limitation has been solved. No runtime behavior was changed in this audit.

 
### Neutral wording for unverified product attributes (2026-11-05)

The product-question fallback previously said that AliExpress had not supplied the requested information whenever deterministic extraction and the AI answer both returned no result. That statement was too strong: a missing answer can also be caused by a Gemini timeout/unavailability or by evidence that the current answer path cannot interpret. Changed the fallback to say that the bot cannot reliably confirm the attribute from the available cards and will not guess. Updated the two product-assistant regressions that assert the user-facing fallback wording.

This does not change selection, evidence validation, provider calls or the product data schema. It is a truthfulness correction: absence of a verified answer is not automatically proof that the marketplace omitted the field. Pending CI on the updated branch head.


### Ordinary AliExpress product-page enrichment feasibility audit (2026-11-05)

The proposal to automatically open ordinary AliExpress product pages and extract descriptions/specifications was evaluated separately from the existing Portals API integration.

**Technical finding**
- Third-party scraping vendors advertise extraction of product-page attributes/specifications, SKU variants, stock, shipping and seller data. Their published workflows use JavaScript rendering and, in some cases, proxy/CAPTCHA infrastructure. This demonstrates that richer page data can sometimes be extracted technically; it does not establish permission, stable coverage, or a suitable production SLA.
- Coverage is not guaranteed: page layouts, regional variants, selected SKU, dynamic content and anti-bot responses can change. Written review text is not necessarily available.
- The current official Affiliate `aliexpress.affiliate.productdetail.get` method is already implemented by `AliExpressProvider.get_product()`. Its documented response example remains affiliate metadata (title, category, price, rating/evaluation rate, volume, images/video, shop and promotion link); it does not document long description, structured product specifications, or SKU attribute tables. The provider's `_normalize()` maps only documented fields and deliberately leaves `Product.description` unset. Repeated calls to this method do not solve the missing-evidence problem.

**Terms / authorization finding**
- AliExpress.com Terms of Use, Part A, updated 2026-08-26 and effective 2026-09-26, clause 3.2(a), prohibits copying/downloading/reproducing Site Content and prohibits systematic retrieval to create or compile a collection, compilation, database or directory by robots, spiders, automatic devices or manual processes without written permission from AliExpress.com. The stated DSA research exception is limited to qualifying Article 40(12) research and is not a general commercial product-consultant exception.
- The AliExpress Affiliate Program Service Agreement (effective 2025-04-01) defines permitted “AliExpress Content” as product text/artworks/copies made available through the participant's Portals account or authorized designated Portals APIs for advertising on media controlled by the participant. It does not itself grant permission to scrape ordinary product pages.
- A robots.txt allowance, a page being public, or a third-party scraper's ability to fetch it is not written permission under these terms.

**Decision for this project**
1. Do NOT add automatic HTML/browser scraping of ordinary AliExpress product pages to the production bot without written AliExpress permission covering this use.
2. Do NOT add proxy rotation, CAPTCHA solving, fingerprint evasion, or a scraping SaaS as a workaround for access restrictions.
3. Keep the Portals Affiliate API as the authorized source for product cards, prices and affiliate links. Keep unsupported attributes UNKNOWN; never infer them from photos, categories, price or seller wording not actually received through an authorized source.
4. The next legitimate feasibility step is to request written clarification/permission from AliExpress Portals support for automated retrieval of product descriptions/specifications for an affiliate shopping assistant, and ask whether a designated API or partner feed exposes those fields.
5. Only if permission or an authorized source is confirmed, run a bounded cross-category spike (5–10 products) measuring field coverage, SKU/region correctness, latency, failures, caching and evidence provenance before considering production integration.

This is a completed source/terms feasibility audit, not a live scraping test and not a runtime implementation. No production code or credentials were changed.


### Project handoff and decision-log policy (2026-11-05)

`PROJECT_CONTEXT.md` is the durable project handoff document. Keep it useful for resuming work in a fresh conversation without re-reading the entire chat.

Record significant, durable information when it changes:
- current project goal, accepted architecture and non-negotiable requirements;
- important technical decisions and why alternatives were accepted, rejected or deferred;
- root causes, affected files, implemented fixes and regression coverage;
- commit hashes, branch/PR status and verified CI outcomes;
- live-test findings, clearly separated from offline tests and static analysis;
- external dependencies, source limitations, operational risks and unresolved blockers;
- paused work, the exact condition required to resume it, and explicit "do not revisit until" decisions.

Do not turn this file into a raw activity log. Exclude routine tool output, repeated conversation, speculative hypotheses presented as facts, secrets, credentials, `.env` contents, and unverified claims. Prefer concise dated sections with links/commit identifiers where useful. Update the existing section when a status changes instead of leaving contradictory active and paused instructions. After each material project milestone, review whether this file needs an update before ending the work session.

### Status: ordinary AliExpress product-page extraction is PAUSED (2026-11-05)

The feasibility and terms audit is documented in the preceding section, committed as `114858e20f884c68757f0e6954e3a1a1c0fca80e`.

**Current status: PAUSED — no implementation and no further scraping experiment.**

Do not implement or test automated extraction from ordinary AliExpress product pages, browser automation, HTML scraping, proxy-based access, CAPTCHA handling or scraper services for this project unless one of these prerequisites is first met:
1. AliExpress provides written permission that explicitly covers automated retrieval of product descriptions/specifications for this commercial affiliate shopping assistant; or
2. AliExpress confirms an official, authorized API/feed that provides the required product evidence and the project's credentials are approved for it.

Until then:
- keep the Portals Affiliate API as the authorized production source for search, product cards, prices and affiliate links;
- keep unsupported attributes UNKNOWN and never infer compatibility/specifications from photos, category, price or generic conventions;
- do not spend further implementation time on the paused page-extraction route;
- continue work on the main consultant/search reliability requirements using authorized sources and honest evidence handling.

Resume this item only after the user explicitly reopens it and the permission/authorized-source prerequisite has been verified. The audit remains useful historical context; the pause is a deliberate project decision, not a claim that page extraction is technically impossible.

### Handoff verification — extraction pause recorded and CI passed (2026-11-05)

The handoff/decision-log policy and the explicit pause on ordinary AliExpress
product-page extraction are now committed on the active working branch.

Commit: `ed81a1dd2fa7ecff47d01d5279e2cef08bb24c1f`
GitHub Actions run: [42059950804](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/42059950804) — completed successfully on this exact head.

Verified workflow results:
- Focused conversational regressions: **280 passed, 45 subtests passed**.
- Filtered repository suite: **2,208 passed, 723 subtests passed, 1 warning**.
- Both manual live smoke scripts compiled successfully.
- The repository suite excludes the 17 documented stale legacy modules; this is not an unfiltered full-suite pass.
- CI tests are offline. They do not verify live Gemini latency, live AliExpress recall, or Telegram delivery.

Repository status at this handoff:
- Branch: `work/consultant-reliability`.
- PR #7 remains open, draft and unmerged.
- `main` is unchanged.
- Ordinary AliExpress product-page extraction remains PAUSED under the prerequisite recorded above.

Next main-TЗ work should continue with broad, category-neutral search and conversational reliability. In particular, re-check live Gemini candidate-selection latency/fallback after the 6.5-second per-model timeout and low-thinking JSON configuration; the earlier live run predates those changes. Continue investigating relevance using actual multi-category evidence, without weakening identity/evidence gates or adding product-specific exceptions. No new live run is implied by this CI result.

### Generic host-plus-addon identity correction (2026-11-05)

The earlier live «Кухонный кран» search included listings whose title named
the requested host product together with an add-on (notably aerator/mat-style
items). The category-neutral product-role validator already rejected many
host/accessory compounds, but its generic accessory-role vocabulary omitted
these two add-on nouns. A title could therefore pass the host-product gate
even though the actual listing was the add-on.

Updated `services/convsearch/type_validation.py`:
- Added generic Russian/English role nouns for aerator and mat.
- The existing host-followed-by-accessory relation check now rejects these
  compounds when the user requested the host product.
- These words remain valid requested product identities when the user asks
  for the add-on itself; no product category or brand exception was added.

Added `tests/test_convsearch_type_validation.py` coverage for:
- Russian and English host-plus-aerator/mat titles being rejected;
- standalone aerator and mat products remaining valid requested products.

Commits:
- Implementation: `3046df1abbbaff587915ecf0f749a1989cf523fd`.
- Regression coverage: `322b4783bb07aeec6e67d414665fc8c47020f15e`.

GitHub Actions run [42064073102](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/42064073102) passed on the test commit:
- Focused conversational regressions: **282 passed, 52 subtests passed**.
- Filtered repository suite: **2,210 passed, 730 subtests passed, 1 warning**.
- The 17 documented stale legacy modules remain excluded; this is not an unfiltered full-suite pass.

This is a narrow expansion of generic product-role vocabulary, not a live
catalogue-quality proof. The prior Telegram result predates this correction.
A new live multi-category Telegram run is still required to verify that
aerator/mat accessories are removed from host-product searches while direct
searches for those add-on products still work. Do not claim the kitchen-faucet
issue is fully resolved until that check is completed.


### Buyer-friendly comparison of shown products (2026-11-05)

A live Telegram turn reached the correct comparison route (engine-compare)
and completed the product_question Gemini call, but the user-facing answer
only repeated each product's full title as a quoted claim. The underlying
issue was the generic evidence-answer renderer: it emitted verified quotes
before considering the prompt's clarification field, and it had no dedicated
side-by-side comparison layout. This was an output-quality failure, not a
search or routing failure.

Changed services/product_assistant.py:
- Explicit compare requests are now rendered deterministically from the two
  highest-ranked distinct products already shown to the user. No new
  AliExpress search is made.
- Each option is separated and shows its real title and only the marketplace
  fields actually present (price, rating, positive-review rate, orders and
  delivery data).
- The comparison section computes price differences only when both prices
  and the same currency are available. Rating/order differences are shown
  only when both values exist. Different or missing currencies are never
  numerically compared.
- If fewer than two products are available, the bot says so instead of
  fabricating a pair. Missing attributes remain unknown.
- This is category-neutral: it uses the existing ranked result order and
  generic Product fields; no Lenovo/keyboard-specific rules were introduced.

Regression coverage added in tests/test_product_assistant.py for:
- two distinct options with actual titles/prices/ratings/orders;
- correct same-currency price delta;
- no duplicated “title quoted as evidence” wording;
- one-product comparison without inventing a second option;
- deterministic behavior without spending a Gemini call.

Commits:
- Comparison renderer: 4cd61d8f3abe183e0fde482b24ac29e6c69f63fe.
- Regression tests: 69d030b6dd223974beea2ce9ff7257488b068659.
- Currency/missing-price clarification: 2c1a169711d1cd5261b2598e517d7b5e165d421a.

GitHub Actions run [42080812552](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/42080812552)
completed successfully on the code/test head:
- Focused conversational regressions: **282 passed, 52 subtests passed**.
- Filtered repository suite: **2,212 passed, 730 subtests passed, 1 warning**.
- Manual live smoke scripts compiled successfully.
- The 17 documented stale legacy modules remain excluded; this is not an
  unfiltered full-suite pass.
No live Telegram retest has been performed for this change.

### Live conversational issues to re-check (2026-11-05)

- In the supplied Telegram log, a fresh «Клавиатура для ноутбука» request
  appeared alongside active slot diagnostics from the previous sink-mat
  search. This may indicate stale context crossing a product switch, but the
  log alone does not prove whether those slots affected the final candidate
  pool. Reproduce with an unrelated consecutive search and inspect the
  intent, slot provenance, retrieval query and per-stage candidate counts.
- A later «Для Lenovo ThinkPad» refinement retrieved some ThinkPad models
  outside the exact T490s/T495/T14s group named earlier. The compatibility
  gate must continue to distinguish a family-level mention from a confirmed
  exact model match; do not claim exact compatibility unless listing evidence
  contains it.
- The user-facing comparison issue above has an offline implementation now,
  but it remains unverified in a live Telegram session. The user's manual
  live test is still required to confirm that the answer is understandable and
  useful with real AliExpress card data.

These are current quality checks under the main TЗ, not reasons to add
product-specific exceptions. Continue broad category-neutral search and
dialogue verification; ordinary AliExpress product-page extraction remains
PAUSED under the separate authorization decision above.


### Product switch before purpose/fitment connector (2026-11-05)

A live Telegram log showed a fresh «Клавиатура для ноутбука» request after
a sink-mat search while Gemini turn interpretation timed out. The active
ledger still contained the previous sink-related slots, and the new target
«ноутбука» appeared as context. Source inspection identified the fallback
classifier defect: it treated any «для» connector as proof of refinement,
even when the user had already named a different product before that
connector. This could mix the old product type, budget, features, use case
and shown IDs into the new search.

Changed services/convsearch/state.py:
- Added a category-neutral leading-product-head check before purpose/fitment
  connectors such as «для», «на», «по», «под», «for» and «with».
- When the leading product head differs from the active product head, the
  deterministic fallback classifies the turn as NEW_SEARCH even if Gemini
  is unavailable or proposes a contradictory refinement.
- Request scaffolding, generic feature nouns, comparative framing and common
  action words are excluded so a material refinement such as
  «Оставь варианты из нержавеющей стали» remains attached to its active item.
- The existing NEW_SEARCH lifecycle then supersedes the previous slots and
  clears shown-result IDs before the new retrieval. No product/category/
  brand-specific exception was added.

Added an engine integration regression in
tests/test_convsearch_turn_interpretation.py. It simulates unavailable Gemini,
starts with a sink-mat request plus old budget/feature/shown IDs, then sends
«Клавиатура для ноутбука». It asserts that the new type and notebook context
are used and that the previous budget, feature and shown IDs do not leak.

The first CI attempt [42081512813](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/42081512813)
caught an over-broad interpretation of «Оставь варианты из нержавеющей стали»
as a new product. The request-framing vocabulary was corrected to include
common selection/editing imperatives; no regression was deleted or weakened.

Added a category-neutral state regression matrix in
tests/test_convsearch_engine.py for new products named before purpose/fitment
connectors across four unrelated item families (keyboard, vacuum filter,
drill attachment and phone case). This checks that the rule is not tied to
the single keyboard incident.

Latest successful GitHub Actions run
[42082829929](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/42082829929)
on code/test head 66bfb3fd1c331891295a828584b8b5822e2d4928:
- Focused conversational regressions: **284 passed, 56 subtests passed**.
- Filtered repository suite: **2,220 passed, 736 subtests passed, 1 warning**.
- Manual live smoke scripts compiled successfully.
- The 17 documented stale legacy modules remain excluded; this is not an
  unfiltered full-suite pass.

This is offline verification only. The reported keyboard-after-sink-mat
transition still needs a live Telegram retest with Gemini deliberately
unavailable or timed out. The 17 excluded legacy modules remain outside this
workflow. PR #7 remains draft/open/unmerged; main is untouched.


### Exact-evidence product comparison extension (2026-11-05)

The deterministic side-by-side renderer now has an optional grounded-evidence
step. ProductAssistant still chooses the first two distinct items from the
already AI-ranked, user-shown results and renders all factual fields itself.
AIService exposes only an evidence-extraction method
(extract_paired_listing_evidence): Gemini receives those two listings and may
return paired text spans showing a useful difference. Each quote is checked
against the correct product ID and exact title/description before display;
full-title repetition, invented text, misassigned quotes and malformed output
are rejected. The application does not display model-written prose or a
model-selected winner. If Gemini is unavailable, times out or returns no
valid pairs, the deterministic price/rating/order comparison remains.

This preserves the removed comparison-service API contract: an earlier broad
CI run [42082556083](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/42082556083)
caught the test that explicitly forbids an AIService method named
compare_products. The evidence method was renamed to
extract_paired_listing_evidence; the contract test remains unchanged.

Files:
- services/ai_service.py — paired-evidence prompt, bounded call and exact quote validation.
- services/product_assistant.py — combines verified paired evidence with deterministic product facts.
- tests/test_ai_service.py — exact quotes, invented/misassigned quote rejection, one-product and timeout fallback.
- tests/test_product_assistant.py — buyer-facing comparison rendering with verified paired distinctions.

Relevant commits:
- AI evidence implementation: cf4f1733116d45727f92818f824d23b9fcd20d11.
- AI evidence API name preserving the removed-method contract: d54957c6af168e6c1de8c4b80826cd6cae0e9f93.
- ProductAssistant integration: cb1080d6d634168b1cc7fe403972dacf7ea77eb8.
- ProductAssistant API rename: 03233de72ef71ab76fcda54769b7920f9913887c.
- AI validation tests: 7e175271a5b1e1fb5331ca74c8244292e982d005 and rename commit 3089bbb1ba809aed69fac8d1454e9bcb55126a5a.
- User-facing evidence test: 5e27810f6f371db0f0651c457e5ca347906e9ae9 and rename commit 66bfb3fd1c331891295a828584b8b5822e2d4928.
- Four-family product-switch matrix: 27b149879861bb7a91a9c34c8c3a09a3de210277.

The successful CI run 42082829929 above covers this comparison extension and
the product-switch matrix. It remains offline-only; no live Telegram retest
has yet confirmed the resulting natural-language experience with actual
AliExpress descriptions. The separate ordinary product-page extraction
decision remains PAUSED.


### Same-product model/specification refinements (2026-11-05)

A review of the new leading-product detector found a possible false switch
when a buyer restates the current product before a purpose connector and adds
a brand/model code, e.g. «Ноутбук Lenovo ThinkPad T14s для работы». The
detector now checks whether the active product head appears anywhere in the
leading phrase, rather than comparing only the final token. Alphanumeric
model/specification codes are not treated as product nouns. This also guards
attribute phrases such as «защита IP67 для телефона».

Changed services/convsearch/state.py:
- Retains the new-product transition when the leading item noun is genuinely
  different from the active product head.
- Keeps same-product phrases with brand/model tokens or numeric spec codes
  as refinements when the active product noun is still present.
- Does not add product-specific model or category lists.

Added state regressions in tests/test_convsearch_engine.py for:
- switching across the four unrelated item families documented above;
- keeping «Ноутбук Lenovo ThinkPad T14s для работы» attached to the active
  laptop search;
- keeping «Нужна защита IP67 для телефона» attached to the active phone search.

Commits:
- Model/spec-aware product-head logic: 2e724039b222807189a695ffc5e11ab19842e9bf.
- Numeric model-code regex correction: 8d26e006ff4e020206d3c201f427b590a88837de.
- Regression tests: 83a629b35c0ee45f1f2c5819b59caeee88ee8398.

Latest successful GitHub Actions run
[42083275395](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/42083275395)
on code/test head 83a629b35c0ee45f1f2c5819b59caeee88ee8398:
- Focused conversational regressions: **285 passed, 58 subtests passed**.
- Filtered repository suite: **2,221 passed, 738 subtests passed, 1 warning**.
- Manual live smoke scripts compiled successfully.
- The 17 documented stale legacy modules remain excluded; this is not an
  unfiltered full-suite pass.

Offline verification only. Live Telegram retest is still required to confirm
that the fresh-product and same-product refinement boundaries behave
naturally with the actual conversation ledger and provider results.

### Make paired product comparisons understandable at a glance (2026-11-05)

The exact-evidence comparison extension still rendered two valid quotes as
unlabelled «Вариант 1 / Вариант 2» fragments. Although the text was grounded,
a buyer could not tell whether the pair described compatibility, keyboard
layout, material, dimensions or package contents. This repeated the
user-observed clarity problem in a less severe form.

Changed:
- services/ai_service.py — comparison evidence now includes a required,
  allow-listed aspect (compatibility, layout, material, size, package,
  power, other) for each paired quote. Both quotes must still pass exact
  product-ID, field and source-text validation; unknown aspect labels are
  rejected.
- services/product_assistant.py — verified pairs are rendered under a clear
  Russian dimension label (e.g. «Совместимость / модели» or
  «Язык / раскладка»), with separate values for variant 1 and variant 2.
  The renderer still does not show Gemini-authored prose or infer a winner.
- tests/test_ai_service.py — valid aspect output, invalid aspect rejection,
  and quote-grounding checks.
- tests/test_product_assistant.py — buyer-facing labelled comparison output.

Commits:
- AI aspect extraction/validation: 01a0eef4ddff1f14ddc9babd69e2c4d970df8c3e.
- Labelled deterministic rendering: 2afe6624a59d1861d15bcd42844bbebda03c9b7e.
- AI validation tests: 0f5abfd9e16a90ccfc9c0268994c6ca3969f3838,
  1b84a95636279fa4e37259716b84b3f41ccf1e11.
- Buyer-facing rendering regression: 1df551edeb554cd88594bacdf123dccda09426d1.

GitHub Actions run
[42085698806](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/42085698806)
completed successfully on code/test head 1df551edeb554cd88594bacdf123dccda09426d1:
- Focused conversational regressions: **285 passed, 58 subtests passed**.
- Filtered repository suite: **2,222 passed, 738 subtests passed, 1 warning**.
- Both manual live smoke scripts compiled successfully.
- The 17 documented stale legacy modules remain excluded; this is not an
  unfiltered full-suite pass.

Offline CI only. No live Telegram comparison has yet confirmed that Gemini
assigns the right comparison dimension to real AliExpress listing text.
The comparison aspect is an allow-listed model classification, while every
displayed quote remains checked against its source listing. PR #7 remains
draft/open/unmerged; main is untouched.

### Broaden the manual conversational smoke across eight product families (2026-11-05)

The manual Option C live smoke previously covered four broad product families.
It now covers eight Russian-language queries: cordless drill, portable power,
under-sink water filter, outdoor security camera, wireless headphones,
bagless vacuum, garden hose and headlamp. It still uses one provider page per
grounded phrase to keep the manual run bounded and prints only a few real
titles plus bounded diagnostics; it does not print credentials or affiliate URLs.

Gemini candidate-selection fallback is now reported as a degradation warning
rather than an automatic smoke failure when the deterministic safety path
still returns valid cards. A true empty/failed search, duplicate IDs, missing
affiliate links, a cap violation, or a failed shown-product comparison still
fails the smoke. The shown-product follow-up now explicitly requests a
side-by-side comparison so the newly labelled comparison path is exercised.

Changed file: smoke_conversational_search.py.
Commit: d625ffe24dbb4413e022995a301ff1b916810142 (includes the eight-query
scope wording correction; initial expansion commit 4c63e5c82dcacedd5324ec63b91136386929ad3c).

GitHub Actions run
[42086111895](https://github.com/golod19920909/shorts-chill-shop-bot/actions/runs/42086111895)
completed successfully on the updated branch head:
- Focused conversational regressions: **285 passed, 58 subtests passed**.
- Filtered repository suite: **2,222 passed, 738 subtests passed, 1 warning**.
- Both live smoke scripts compiled successfully; they were NOT executed against
  AliExpress or Gemini by CI.
- The 17 documented stale legacy modules remain excluded; this is not an
  unfiltered full-suite pass.

Next live verification must run the expanded script locally with the user's
authorized credentials. Its output is diagnostic and not a benchmark or proof
of whole-catalogue coverage. PR #7 remains draft/open/unmerged; main is untouched.


### Keep compatibility constraints consistent in deterministic fallback helpers (2026-11-05)

A focused audit found that the main _evaluate_pool path already treated both
mandatory feature and compatibility slots as hard evidence requirements
through partition_required_feature_evidence. However, the separate
filter_by_required_features helper used by the compatibility shape_pool
path checked only feature slots. That left two deterministic filtering
paths with different interpretations of the same hard compatibility rule.

Changed:
- services/convsearch/retrieval.py — the deterministic required-evidence
  filter now checks both active feature and compatibility slots. Missing
  evidence remains rejected by this helper; use-case and preference slots
  remain soft and are not added.
- tests/test_convsearch_engine.py — added a regression with an iPhone case
  whose description explicitly confirms iPhone 15 compatibility, a Samsung
  case, and a listing with no compatibility evidence. Only the confirmed
  listing survives.
- PROJECT_CONTEXT.md — records the audit finding and the exact verification
  status.

Commits:
- Implementation: c256068af20ab4bec35b8304290da810267556ab.
- Regression: 6d55ed2c1782560174e0eae03c77fbdba4dbf5c7.
- Contract/docstring clarification: 09f96e236fd6ebedb5c42f8def68edcb8385dad2.

Verification:
- The commit diffs were fetched from GitHub and checked against the intended
  source/test changes.
- GitHub combined status currently reports no status entries for these
  commits, and the available commit-workflow lookup returned no PR-triggered
  runs. Therefore CI for this change is NOT YET VERIFIED; no test pass is
  claimed here.
- The regression is offline and does not call AliExpress, Gemini or Telegram.

Remaining: confirm the focused engine test and filtered conversational
workflow on this branch. The active _evaluate_pool path was already
enforcing compatibility evidence; this change aligns the secondary
deterministic helper and its compatibility shim, rather than claiming a
change to live search behavior. PR #7 remains draft/open/unmerged; main is
untouched.


### Quarantine all uncertain product-identity verdicts (2026-11-05)

A deeper audit found a safety gap in the bridge between the product-type
validator and the conversation engine. The validator can explicitly mark a
candidate as uncertain and require semantic review for several reasons.
However, the active engine copied only three of those reasons into its
semantic-review quarantine list. Two important verdicts were omitted:
- bare product nouns whose subtype is confirmed only by description;
- cross-language titles that cannot be matched deterministically.

Those candidates could therefore be treated as ordinary deterministic-safe
results and shown when Gemini was unavailable, despite the validator having
explicitly requested review. The old shape_pool compatibility helper had an
even narrower check and could also pass some uncertain identities.

Changed:
- services/convsearch/engine.py — introduced one shared allow-list of all
  semantic-review verdict reasons. The active evaluator now quarantines every
  such verdict; the no-Gemini fallback keeps it hidden. The compatibility
  shape_pool path uses the same list and also fails closed.
- tests/test_convsearch_engine.py — added regressions for a bare «катушка»
  title whose description supplies the fishing subtype and an English
  «Cordless screwdriver» title for a Russian «шуруповёрт» request. Both must
  be marked for semantic review, remain hidden without Gemini, and stay out
  of shape_pool's deterministic results.
- The preceding compatibility-filter correction remains in place: the
  deterministic helper checks both feature and compatibility slots.

Commits:
- Shared identity-review quarantine: b8e5ed5fa040c04e9dedd0671aa73dafa679df12.
- Regressions: 7c4994bd1e7cafa751f5330420bf2c1810d59b33.
- PROJECT_CONTEXT update: pending.

Verification:
- The code and regression commits were fetched from GitHub; the diffs and
  expected review-reason paths were inspected.
- GitHub combined status has not reported test checks for the new commits.
  No local test runner is available in this remote editing session, so the
  new regressions are NOT YET EXECUTED and no CI pass is claimed.
- Tests are offline and use fixed Product objects; they do not call Gemini,
  AliExpress or Telegram.

This is a correction to a real deterministic-fallback safety gap, not a
relaxation of product matching. The candidate remains hidden until semantic
review confirms it. PR #7 remains draft/open/unmerged; main is untouched.


### Fix stale budget constraints across refinement turns (2026-11-06)

A state-lifecycle audit found that an updated numeric budget replaced the
previous budget only when the deterministic intent was CORRECTION. A normal
REFINE turn such as «а теперь до 3000 грн» could therefore leave the old
2000 UAH ceiling active alongside the new one. The same lifecycle also had
no explicit way to clear a budget when the user said that price did not
matter, so an obsolete ceiling could continue constraining later searches.

Changed:
- services/convsearch/state.py — budget supersession now happens for every
  turn containing a newly parsed numeric budget, regardless of whether the
  turn is classified as REFINE or CORRECTION. Added conservative explicit
  no-budget phrases in Russian and English; these supersede the old budget
  without adding a replacement ceiling. Other constraints and product
  identity remain untouched.
- tests/test_convsearch_engine.py — added regressions for changing 2000 UAH
  to 3000 UAH during a REFINE turn and clearing the budget using five
  explicit no-limit phrasings.
- PROJECT_CONTEXT.md — records the finding, change and verification status.

Commits:
- State lifecycle fix: 610f98854cf7f8c69e5042920d4b286825724670.
- Regex escaping correction: a0e75f1c6177bc7f30142923c50ae68d51e9d7b6.
- Regression tests: 305f902a3e4dfe772f5dbcb6a98670f21fd1a1ba.
- PROJECT_CONTEXT update: pending.

Verification:
- Source and test changes were fetched back from GitHub and inspected.
- The budget-reset regular expression is stored with the intended Python
  word-boundary and whitespace escapes.
- GitHub Actions run 42158103054 completed successfully on the PROJECT_CONTEXT
  commit 36746bea9b38ffd36e82160c149ceeb6fc80bfb0.
- Focused conversational regressions: 289 passed, 65 subtests passed.
- Filtered repository suite: 2,226 passed, 745 subtests passed, 1 warning.
  The workflow excludes 17 stale legacy modules; this is not an unfiltered
  full-repository run.
- Both manual live smoke scripts compiled successfully; CI did NOT execute
  them against AliExpress, Gemini or Telegram.
- The suite's warning is a google-genai Python deprecation warning. GitHub
  runner also reports Node 20 action deprecation warnings; neither caused
  the workflow to fail.

Remaining: review the wider conversation lifecycle and search-quality gaps,
then prepare a bounded live Telegram check. The live check is not yet
requested because this pass has only verified offline behavior. PR #7
remains draft/open/unmerged; main is untouched.


### Apply the active search currency to budgets without an explicit currency (2026-11-06)

A retrieval-path audit found a mismatch between budget parsing and budget
enforcement. When a user wrote «до 2000» without naming a currency, the state
ledger correctly stored a numeric ceiling but had no currency in that slot.
The search request already carried the bot's active currency (for example
UAH), yet the hard budget filter ignored it and rejected every product
because the budget currency was blank. Explicitly named currencies did work.

Changed:
- services/convsearch/retrieval.py — filter_by_budget accepts an optional
  default_currency. It uses that only when the user did not specify a budget
  currency; an explicit USD/EUR/UAH value takes precedence. Missing product
  price or product currency still fails closed.
- services/convsearch/engine.py — the active evaluation path now passes
  the optional strategy request currency to the hard budget filter. It uses
  getattr so older/minimal request fixtures without a currency field remain
  supported and fail closed.
- services/convsearch/retrieval.py — if the user explicitly names a supported
  budget currency (UAH/USD/EUR/RUB), the marketplace SearchRequest now uses
  that currency too. If the user gives no currency, the existing active
  search/display currency remains the default.
- tests/test_convsearch_engine.py — added regressions for an unspecified
  budget using UAH search currency, explicit USD overriding UAH both in the
  filter and marketplace request, and unspecified budget with no available
  default currency.
- PROJECT_CONTEXT.md — records this retrieval/budget consistency correction.

Commits:
- Filter fallback currency: 3f7ca22722a008f5d4c2de8044c25230d5d4e076.
- Engine wiring: 782442a8eb53189aee78263970bc943af78cfad5.
- Initial regression tests: 6a0ec089098b4b7952cefc01fb1b2b81c06c153f.
- Compatibility with minimal request objects: 3dd1d2e216d3902d44023e0c379a3a312f81927c.
- Explicit budget currency controls provider request currency: 750c46a8d1f9797ffcd56e399a1c4b3cbf4310fc.
- Request-currency regressions: bf59ccba9b9903db0a9b7aacdf8b8ec01993e4e7.
- PROJECT_CONTEXT update: pending.

Verification:
- The changed source and test files were fetched back from GitHub and
  inspected. The engine passes the request's active currency; explicit
  budget currency remains authoritative.
- First CI attempt exposed a compatibility issue: several established
  engine fixtures use minimal request objects without a currency attribute.
  The new direct attribute access caused 27 failures across otherwise
  unrelated scenarios. The engine call was corrected to read the optional
  currency with getattr; absent currency therefore remains None and the
  filter continues to fail closed. This fix is in
  3dd1d2e216d3902d44023e0c379a3a312f81927c.
- The compatibility correction has passed GitHub Actions run
  42158404800: 292 focused tests + 65 subtests, and 2,229 filtered-suite
  tests + 745 subtests, with one google-genai deprecation warning. Seventeen
  stale legacy modules remain excluded.
- CI run 42158456293 passed after the explicit-budget-currency request
  change: 292 focused tests + 65 subtests, and 2,229 filtered-suite tests
  + 745 subtests, with one google-genai deprecation warning. This run was
  created before the two new strategy regressions were added.
- CI run 42158469493 completed successfully with both new strategy
  regressions included: 294 focused tests + 65 subtests, and 2,231 filtered
  repository tests + 745 subtests, with one google-genai deprecation warning.
  The workflow also compiled both manual live smoke scripts successfully.
- The filtered suite excludes 17 stale legacy modules; this is not an
  unfiltered full-repository test run. No live AliExpress, Gemini or Telegram
  calls were made by CI.
- The final currency-change commit sequence includes the compatibility
  correction (3dd1d2e216d3902d44023e0c379a3a312f81927c), the explicit
  provider-currency override (750c46a8d1f9797ffcd56e399a1c4b3cbf4310fc),
  and the two request-currency regressions
  (bf59ccba9b9903db0a9b7aacdf8b8ec01993e4e7). CI run 42158469493 covers
  the test commit and passed all reported checks.
- The previous budget-lifecycle change has passed GitHub Actions run
  42158103054: 289 focused tests + 65 subtests, and 2,226 filtered-suite
  tests + 745 subtests. Seventeen stale legacy modules were excluded.
- These new currency regressions are offline and do not call AliExpress,
  Gemini or Telegram.

Remaining: audit the remaining dialogue and retrieval lifecycle, then request
a bounded live Telegram run covering currency-aware budgets and the existing
multi-category search scenarios. Live testing is still not requested in this
pass because the latest verified code has not yet been pulled into the user's
local bot. PR #7 remains draft/open/unmerged; main is untouched.


## Live-log fixes: currency-free budgets and title identity review (2026-11-06)

The 2026-11-06 Telegram log exposed two systemic issues:

- A first-turn request such as «Спиннинг до 2000» did not create a budget slot because the deterministic budget regex required a currency suffix. The parser now accepts an amount without currency only when it is explicitly framed as a ceiling/budget (for example, «до 2000», «бюджет 2000», «до 2 тыс»). Unframed numbers remain ignored to avoid treating dimensions/model numbers as budgets. The resulting slot keeps currency empty; the hard budget filter inherits the active `SearchRequest.currency` and remains fail-closed when neither currency is known.
- The semantic selector previously instructed Gemini to reject a product type found after a comma whenever the title began with another product name, without adequately distinguishing a synonym/descriptor of the same physical item from a second cross-sold product. The prompt now says a comma alone is not proof of a second item: the full title must be interpreted; a coherent same-item synonym/variant may be confirmed with an exact title quote, while a distinct accessory/part/second item must still be rejected and ambiguous cases remain unselected.
- A currency-free budget is now normalized with the active search currency before it is sent to Gemini's semantic selector and included in the selector query. This keeps the AI's budget rule aligned with the deterministic hard filter (for example, `2000.0 UAH`, not a currency-less ceiling).

Changed files:
- `services/convsearch/state.py`
- `services/convsearch/engine.py`
- `services/ai_service.py`
- `tests/test_convsearch_budget_parsing.py`
- `tests/test_convsearch_budget_selection.py`
- `tests/test_convsearch_semantic_identity_prompt.py`

Implementation commits:
- `4f36ca4f7fc025fab94913f289afd2b2124629a3` — currency-free budget parsing.
- `919fb2681c4eb25ff62c7c75459481a8df35bd55` — parser regression tests.
- `368b1c4d2f275437729ef7f4a21d5f32b34d580c` — inherit request currency for AI budget review.
- `f32603ccda87b9884b17db21fe25c105ddfa785a` — selector budget-currency regression.
- `804f365e581d75d7fda8e98759e6fb4bfc3d3da3` — refine comma-separated title identity prompt.
- `a9eff40f8fe380aedf4b3640fca1368af44af03d` and `80dc5416fa03e2dddc66a48391ca8d5656161284` — semantic prompt regression and assertion correction.

Verification status at this update:
- Offline tests have been added but have **not yet been run** in this session.
- The GitHub workflow is configured for pushes to `work/consultant-reliability`; no workflow run or commit status was visible through the available Actions results at the time of this update. Do not claim a CI pass.
- No live Gemini, AliExpress or Telegram calls were made by these changes.
- The branch remains `work/consultant-reliability`; PR #7 remains draft/open/unmerged; `main` is untouched.

Remaining work:
- Verify the new tests and the filtered regression workflow.
- Re-run the two observed live scenarios: «Спиннинг до 2000» must not show any item over 2000 in the active search currency; «Спиннинг до 1000 грн» must allow a coherent rod/spinning-rod title to reach semantic review without accepting separate accessories.
- Continue auditing automatic alternate-query recovery when the semantic selector returns a valid empty selection. This log showed one retrieval route after the turn interpreter timed out; the no-results path must make a bounded retry with another grounded seller phrase before asking the user to clarify.


## Conversation-turn Gemini timeout fallback (2026-11-06)

The same live log showed the turn interpreter timing out and falling straight to deterministic intent. That also removed the interpreter's opportunity to provide the bounded alternate seller-language phrase, leaving the follow-up search with only one route.

- `AIService._generate` now allows a client-timeout fallback for `conversation_turn`, as it already did for `conversation_selection`. The primary turn-model attempt is capped at 4 seconds; the configured fallback model gets its own bounded attempt, while `decide_conversation_turn` keeps an 8.5-second outer deadline.
- Quota/rate-limit errors still stop immediately; other operations keep their existing no-retry behavior.
- Added an offline fake-client regression proving a primary turn-model timeout advances to the configured fallback model.
- Changed files: `services/ai_service.py`, `tests/test_ai_conversation_turn_fallback.py`.
- Commits: `826d96ba31158f3d718312d98485965cf28031d0` (implementation), `193820ee3beb9b2b1b02ae024889be6befa05cf3` (regression), `65fdbc5bb65275a6a8e6ae9487ba38c03112d3de` (policy documentation).

Verification remains pending: the new offline tests have not been run in this session, and no live Gemini/AliExpress/Telegram calls were made. This is intended to preserve seller-language query expansion during transient primary-model timeouts; it does not guarantee the fallback model will always respond before the outer deadline.


## Runtime dependency warning from the 2026-11-06 log

The startup warning says Telegram client-side rate limiting is disabled. This is not a missing repository dependency: `requirements.txt` already declares `python-telegram-bot[job-queue,rate-limiter]==22.8`. The active local `.venv` appears not to have the optional rate-limiter extra installed (or has not been refreshed from the current requirements). After pulling the branch, install/synchronize runtime dependencies with `python -m pip install -r requirements.txt`; do not change the dependency declaration solely to silence this warning.

The same log shows the scheduled `price_tracking` job executed successfully and no AliExpress transport/API failure during the displayed search. The Google GenAI AFC message is an SDK usage advisory, not by itself evidence of a failed search. End-to-end search latency (about 25.3 seconds on the first turn) remains a performance issue to measure after the correctness fixes.

## New workstream: AI cost and performance optimization (2026-11-06)

The user has approved a separate, comprehensive workstream to reduce the cost of AI operations without degrading conversational understanding, search quality, product validation, answer quality, reliability, or response speed. The full user specification is the governing scope for this workstream; this section records its durable requirements and current audit status.

### Non-negotiable boundaries

- Measure first, research alternatives second, select an architecture third, and implement only after evidence supports the change.
- Preserve the current Option C architecture: Gemini interprets dialogue turns and performs bounded semantic candidate review; deterministic state, AliExpress retrieval, hard constraints, compatibility/type validation, ranking, and Telegram delivery remain authoritative.
- Do not change AliExpress retrieval, candidate-pool breadth, type/feature/compatibility gates, budget/exclusion filters, ranking, card construction, prices, or affiliate links to save AI cost.
- Do not replace language understanding with keyword-only logic or discard context needed to preserve task identity, budget, compatibility, exclusions, or prior clarifications.
- Do not move high-impact decisions to a cheaper model without comparative quality evidence. A model must never approve unverified product attributes or compatibility.
- Do not add paid providers/gateways or uncontrolled parallel model calls. No secrets or .env access. Do not change main or merge PR #7 without explicit approval.
- Ordinary AliExpress product-page extraction remains paused and is explicitly outside this workstream.
- No live Telegram run is currently possible. Use source inspection, offline tests, official documentation, pricing research, and reproducible offline comparisons; mark live-dependent conclusions as pending.

### Required investigation and deliverables

1. Full inventory of every AI/LLM call site, operation, model, prompt/context payload, timeout, retry/fallback policy, call frequency, usage metadata, and failure mode.
2. Per-scenario and per-dialogue cost/latency analysis: new search, budget and feature constraints, refinements, budget changes, exclusions, task switches, compare, product questions, compatibility, sparse/no results, and long multi-turn sessions.
3. Current, dated research of concrete Gemini, OpenAI, Anthropic, DeepSeek, Qwen and other viable models/providers, using official pricing/API documentation and independent developer reports where useful. Verify Ukraine API availability, Russian-language behavior, structured output, context, rate limits, caching, latency, and reliability.
4. Compare Gemini-only optimization, Gemini plus a cheaper model, a second full provider, deterministic task routing, and gateways such as LiteLLM/OpenRouter/Vercel AI Gateway. Include gateway fees, added latency, lock-in, and maintenance.
5. Audit prompt/context/token payloads, structured state, prompt specialization, safe reuse, and cache invalidation. Never drop semantically relevant context.
6. Build a diverse dialogue evaluation set and compare models/architectures on intent, context, constraints, task switching, retrieval outcomes, compatibility, unsupported claims, comparison quality, and final answers.
7. Build a financial model for 20/50/100/500 active users with multiple realistic activity levels; separate measured facts from projections and fixed from variable costs.
8. Only after the evidence-based architecture decision, implement small reversible changes with regression coverage, cost/latency comparison, fallback checks, and an updated context record.

### Initial repository inspection (2026-11-06; preliminary, not a completed audit)

- Working branch: `work/consultant-reliability`.
- Branch head at start of this workstream: `f08590381780ef2aa4f5da5951325378a519668c`.
- PR #7 remains draft/open/unmerged; `main` is untouched.
- `AGENTS.md` was read. Its Option C, minimal-change, offline-test, honest-data, 20-selection/30-card, and secret-protection rules remain in force.
- Gemini client calls are centralized in `services/ai_service.py` via `AIService._generate`; the current code also contains photo understanding, product analysis, feed selection and legacy/helper methods. The active conversational engine calls turn interpretation and candidate selection; product-question and no-results reply paths are separate.
- Identified callable AI operations in `AIService`: `parse_query`, `rank_candidates`, `describe_image_query`, `analyze_image`, `analyze_product`, `select_candidates`, `select_feed_candidates`, `select_conversation_candidates`, `compose_no_results_reply`, `plan_product_search`, `extract_paired_listing_evidence`, `answer_product_question`, `decide_conversation_turn`, and `decide_chat_action`. Some are legacy, feed-only, photo-only, or product-assistant paths and must be traced to actual callers before counting them as active consultant traffic.
- Most `_generate` calls currently inherit the generic operation label `generate`; only key conversational paths explicitly label `conversation_turn`, `conversation_selection`, `no_results_reply`, and `product_question`. This prevents reliable operation-level attribution until the call-site inventory is completed and labels are made meaningful.
- `services/gemini_telemetry.py` already records individual attempts with model, operation, duration, outcome, and available usage metadata fields: prompt, candidate, total, cached-content, and thinking tokens. Missing usage metadata is correctly marked unavailable; failed/time-out attempts have unknown token usage and must not be treated as zero.
- Existing telemetry does not yet calculate money. It has a ContextVar-based per-turn collector, but this initial inspection has not established that production request paths actually enter `turn_scope`; integration must be verified before relying on per-dialogue aggregates.
- Model defaults in `config/settings.py`: smart/fast/vision roles currently default to `gemini-3.8-flash`; fallback defaults to `gemini-3.1-flash-lite`. These are configuration facts, not proof of current price, availability, or comparative quality.
- `AIService._generate` appends the configured fallback model to the model chain. Quota errors stop immediately; model-unavailable errors can fall back; client-timeout fallback is enabled only for `conversation_turn` and `conversation_selection`. Conversation-turn attempts are capped at 4 seconds each; conversation-selection attempts at 6.5 seconds each, with an outer 15-second selector deadline and 8.5-second turn deadline. These policies must be included in cost and latency measurements.
- The existing telemetry is useful groundwork, not a complete cost-control system: it lacks verified per-operation production aggregation, provider price tables/cost calculation, dialogue-level scenario attribution, user/activity cohort reporting, budget alerts, and a complete accounting strategy for timeouts whose provider-side token usage is unavailable.
- The 2026-11-06 live log previously supplied by the user showed one new search making a successful `conversation_turn` call, then a primary `conversation_selection` timeout at 6.509 s followed by a successful fallback call at 4.248 s. It also showed a later follow-up where turn interpretation timed out and the no-results reply timed out. These are examples only, not a statistically valid cost or latency baseline.
- That same live log showed first-search end-to-end latency around 25.3 seconds. This is a performance observation to investigate, not a Gemini-only duration and not a benchmark.
- The startup rate-limiter warning is an environment issue: `requirements.txt` already declares `python-telegram-bot[job-queue,rate-limiter]==22.8`; the local venv may not have the extra installed. It is separate from Gemini spend.
- No provider/model has been selected for migration. No AI behavior, prompts, search logic, model routing, or provider configuration has been changed as part of this new workstream yet.

### Current status and next steps

- Stage I: started. Initial architecture, model defaults, operation entry points, telemetry fields, timeout/fallback rules, and key measurement gaps have been identified. Full caller tracing, prompt-size inventory, and operation-by-operation measurements remain open.
- Stage II: not started; requires representative real usage telemetry and/or controlled replay scenarios. The supplied single log is insufficient to calculate average dialogue cost.
- Stage III–XI: not started; current dated provider pricing/API research and comparative evaluation are required before choosing a model or gateway.
- Stage XII: not started; no optimization changes are authorized until the audit and comparison support them.
- Stage XV observability: partially present at per-attempt token/duration level; operation attribution, actual cost, reliable dialogue aggregation, provider/user cohorts, and financial alerts remain to be designed.
- No tests were run for this documentation-only update. No live bot, Gemini, AliExpress, or Telegram calls were made.
- Update this section after each material audit finding or implementation, recording exact files, commit SHA, verification evidence, and unresolved risks. Keep the existing PR #7 draft and `main` unchanged.

### Stage I — deeper source-level AI call inventory (2026-11-06)

The following is a source-traced preliminary inventory. A method existing in `AIService` is not automatically counted as a production consultant call; caller reachability is being separated from legacy/feed/photo paths.

| Operation / method | Current operation label | Main role / caller evidence | Initial importance |
|---|---|---|---|
| `decide_conversation_turn` | `conversation_turn` | Active `ConversationSearchEngine.handle_message` turn interpretation | Critical: decides conversational action and context |
| `select_conversation_candidates` | `conversation_selection` | Active engine's post-retrieval semantic review | Critical: bounded review of real candidates; deterministic safety gates remain authoritative |
| `compose_no_results_reply` | `no_results_reply` | Active engine's empty-result follow-up path | Medium: natural wording only; no product facts |
| `extract_paired_listing_evidence` | `product_question` | ProductAssistant explicit comparison path | Medium/high: evidence extraction for two already-ranked real products |
| `answer_product_question` | `product_question` | ProductAssistant only after deterministic factual answer cannot answer | Medium/high: evidence selection; app constructs final claims |
| `analyze_product` | generic `generate` | ProductAssistant «Стоит ли покупать?» | Optional advice; factual fallback exists |
| `plan_product_search` | generic `generate` | ProductAssistant similar/cheaper/better search planning | Optional search wording; deterministic intent fallback exists |
| `select_candidates` | generic `generate` | ProductAssistant selection from an already-retrieved candidate pool | Optional selection; deterministic fallback exists |
| `parse_query` | generic `generate` | Legacy/non-raw SearchService path; not used by the authoritative convsearch request builder | Reachability needs a caller-level check before inclusion in consultant scenario costs |
| `rank_candidates` | generic `generate` | Feed curation / discovery ranking | Feed-only, not the core conversational search turn |
| `select_feed_candidates` | generic `generate` | Feed selection helper | Caller reachability and overlap with `rank_candidates` still need confirmation |
| `analyze_image` / `describe_image_query` | generic `generate` | Discovery photo search; current discovery handler chooses structured analysis OR legacy query description, not both in that path | Photo-only |
| `analyze_photo_with_vision` | generic `generate` | Separate `handlers/photo_search.py` / `services/vision_search.py` path calls the shared `_generate` method | Photo-only; verify whether this path overlaps with discovery's photo flow |
| `decide_chat_action` | generic `generate` | Narrow older clarification helper; active engine now uses `decide_conversation_turn` | Likely legacy/compatibility; confirm no active caller |
| `AIService._generate` | N/A | Central remote call wrapper | Every attempt, including fallback attempts, is instrumented here |

Important attribution defect: only four named operation values are currently explicit in the main paths (`conversation_turn`, `conversation_selection`, `no_results_reply`, `product_question`). Many semantically different calls share `generate`. Next instrumentation change should assign stable operation names at each real call site, but only after completing caller reachability and regression impact review.

### Current per-scenario call shape (source-derived, not measured average)

- Ordinary new/refined search: one `conversation_turn`; if the bounded pool reaches semantic review, one `conversation_selection` (which may be two provider attempts if primary times out and fallback is tried); no-results composition may add one `no_results_reply` call when the engine reaches that path.
- Explicit comparison of shown cards: one `product_question` paired-evidence request; deterministic renderer creates the comparison. It is not supposed to search AliExpress again.
- A factual product question: deterministic answer first; only if insufficient evidence, one `product_question` call. Do not count the deterministic fast path as an LLM call.
- «Стоит ли покупать?» on a product: optional `analyze_product` call, with deterministic advice fallback.
- Similar/cheaper/better product follow-up: ProductAssistant may ask `plan_product_search` for a search phrase, then retrieve candidates and call `select_candidates`; this path needs full handler tracing to establish exact production call count and latency.
- Photo search: one of the structured or legacy discovery vision paths, or the separate photo-search vision adapter; do not assume both are called in one photo request.

### Telemetry and accounting caveats confirmed in source

- `services/gemini_telemetry.py` records one `gemini_attempt` per actual `_generate` attempt. Primary and fallback model calls are distinct attempts; no higher-level duplicate counting is intended.
- Available usage fields are prompt tokens, candidate/output tokens, total tokens, cached-content tokens, and thoughts tokens. Google's Gemini thinking documentation states billable response tokens are the sum of candidate/output tokens plus thoughts tokens. Treat thoughts as a separately reported component of output billing (not a separate price tier); calculate output cost from candidates_token_count + thoughts_token_count. The sample log's total_token_count confirms this arithmetic (prompt + candidates + thoughts).
- Token metadata is accepted only from the SDK response. A failed or timed-out call has unknown usage, not zero. Any total derived from logged usage is therefore incomplete when `partial=true` or an attempt has unavailable tokens.
- The ContextVar `turn_scope` collector is implemented and unit-tested, but no production use was found in the inspected handler/engine/product-assistant paths. Do not rely on its aggregate until the full call graph confirms or adds the scope around the complete Telegram turn.
- The admin AI screen currently reports lifetime-since-process-start counters and the configured primary model; it does not report per-operation token totals, actual USD cost, daily/weekly/monthly spend, provider split, user cohorts, or financial alerts.
- The current logs include per-attempt model/duration/outcome/usage but not a verified monetary cost, pricing version, request class/region, or persistent scenario/user aggregation. The log does not expose prompts or secrets, which must remain true.
- Gemini selection explicitly requests JSON and low thinking level; most other calls currently do not pass a per-operation response configuration and inherit provider defaults. The cost and latency impact of thinking configuration therefore needs to be measured per operation rather than assumed.
- `AIService._generate` appends the configured fallback model to each call chain. A model-unavailable response may trigger fallback for all operations; client-timeout fallback is restricted to `conversation_turn` and `conversation_selection`; quota/rate-limit failures stop without trying another model. This policy itself can materially affect per-dialogue spend and tail latency.

### Stage III — initial official pricing/provider research (checked 2026-11-06)

This is a shortlist for deeper evaluation, not a migration decision. Prices are USD per 1M tokens unless noted, and must be rechecked before implementation because aliases, promotions, regional endpoints and billing terms can change.

| Provider / candidate | Officially surfaced price | Notes for our workload |
|---|---:|---|
| Google Gemini 3.8 Flash (`gemini-3.8-flash`) | Standard: input $0.75, output including thinking $3.75; cached input $0.075; Flex input $0.375/output $1.875 through 2026-12-31 | Current project primary model. Google pricing page states higher prices from 2027-01-01; Flex is a separate service tier and must not be assumed equivalent in latency/availability. |
| OpenAI GPT-6 Luna (`gpt-6-luna`) | Input $0.10, cached input $0.01, output $0.50; Flex/Batch 50% of Standard; Fast 2x | Official model page describes it as high-volume/focused; supports reasoning effort and structured API features. Candidate for controlled evaluation, not yet proven equivalent on Russian dialogue/candidate review. |
| Anthropic Claude Haiku 5.5 (`claude-haiku-5-5`) | From $0.10 input / $0.50 output | Official model overview positions it for high-volume, latency-sensitive classification/extraction/routing. Confirm exact tier, availability and structured-output behavior before testing. |
| Anthropic Claude Sonnet 5.5 (`claude-sonnet-5-5`) | Input $2 / output $10; cache reads $0.10 per MTok | More expensive than current Gemini standard for tokens; likely not a cost-saving default, but may be useful as a quality reference. |
| DeepSeek V4.1 Flash (`deepseek-flash`) | Off-peak input miss $0.15 / cache hit $0.003 / output $0.60; peak input miss $0.30 / cache hit $0.006 / output $1.20 | Official docs confirm 1M context, JSON output, tool calls, OpenAI-compatible and Anthropic-compatible endpoints. Time-of-day pricing doubles during stated weekday peak hours. |
| Qwen 3.5 Flash (`qwen3.5-flash`) | International/EU scope: input $0.10 / output $0.40; explicit cache create $0.125 / read $0.01 | Official Alibaba docs list 1M context and multiple regional endpoints. Germany/Frankfurt has a Global/EU deployment scope; exact account region, endpoint and eligibility need confirmation. |
| Qwen 3.8 Flash (`qwen3.8-flash`) | International/global pricing surfaced at input $0.113 / output $0.382; EU scope $0.15 / $0.47 | Newly surfaced in the official pricing table; verify current model availability, stable API model ID, JSON/tool support and region before treating as an evaluation candidate. |

Ukraine availability check (official region pages): OpenAI API lists Ukraine with certain exceptions; Anthropic lists Ukraine but excludes certain occupied regions; Alibaba Model Studio publishes international/EU scopes and region-specific endpoints, but account eligibility and billing need confirmation. Do not infer that every API product/model is enabled for every account. Google Gemini API supported-region check remains outstanding.

Official sources consulted:
- Google Gemini API pricing: https://ai.google.dev/gemini-api/docs/pricing
- OpenAI API pricing: https://developers.openai.com/api/docs/pricing
- OpenAI GPT-6 Luna model page: https://developers.openai.com/api/docs/models/gpt-6-luna
- Anthropic pricing: https://platform.claude.com/docs/en/about-claude/pricing
- Anthropic model overview: https://platform.claude.com/docs/en/models/overview
- Anthropic supported regions: https://platform.claude.com/docs/en/api/supported-regions
- DeepSeek official models and pricing: https://api-docs.deepseek.com/quick_start/pricing/
- Alibaba Model Studio pricing: https://www.alibabacloud.com/help/en/model-studio/model-pricing
- Alibaba Qwen 3.5 Flash model/pricing: https://www.alibabacloud.com/help/en/model-studio/qwen3-5-flash
- Alibaba Model Studio regions: https://www.alibabacloud.com/help/en/model-studio/regions
- OpenAI supported countries: https://help.openai.com/en/articles/5347006-openai-api-supported-countries-and-territories

No provider or model has been selected for production. No benchmark, real user cost, or saving percentage is claimed from list prices alone.

### Cost calculation from the supplied 2026-11-06 live log (sample only)

Official Google documentation clarifies that for thinking models the billable response is **candidate/output tokens + thinking tokens**. The SDK fields are separate; `total_token_count = prompt + candidates + thoughts`. This is now the accounting rule for Gemini in this project. Source: https://ai.google.dev/gemini-api/docs/generate-content/thinking and https://ai.google.dev/api/generate-content

Rates checked 2026-11-06:
- Gemini 3.8 Flash Standard through 2026-12-31: $0.75 / MTok input and $3.75 / MTok output (including thoughts).
- Gemini 3.1 Flash-Lite Standard: $0.25 / MTok input and $1.50 / MTok output (including thoughts).
- Google lists Ukraine as a supported Gemini API / AI Studio region: https://ai.google.dev/gemini-api/docs/available-regions

Applying those rates to successful attempts with complete usage fields in the supplied log:
- 05:53:31 `conversation_turn`, Gemini 3.8 Flash: 1,891 input + (47 candidate + 132 thoughts) output → **$0.0020895**.
- 05:53:50 primary `conversation_selection`, Gemini 3.8 Flash: timed out; usage metadata unavailable → **unknown cost**, not zero.
- 05:53:55 fallback `conversation_selection`, Gemini 3.1 Flash-Lite: 12,367 input + (1,391 candidate + 113 thoughts) output → **$0.00534775**.
- Known subtotal for those successful attempts: **$0.00743725**, excluding the timed-out primary selector attempt. This is a lower bound for that search, not its complete cost.
- 06:00:54 `conversation_turn`, Gemini 3.8 Flash: 2,369 input + (44 candidate + 188 thoughts) output → **$0.00264675**.
- 06:00:57 generic `generate`, Gemini 3.8 Flash: 194 input + (20 candidate + 143 thoughts) output → **$0.00075675**.
- 06:01:23 `conversation_selection`, Gemini 3.8 Flash: 11,684 input + 5 candidate output; thoughts field was not present in the logged metadata → **at least $0.00878175**, with the output amount incomplete if thought tokens were omitted rather than zero.
- 06:01:15 turn timeout and 06:01:26 no-results-reply timeout: usage unavailable → unknown cost.

These figures demonstrate why thinking tokens and fallback attempts matter. They are not a monthly forecast, average conversation cost, or complete bill reconciliation. The supplied log has multiple user turns and incomplete timeout metadata; provider billing export/console data is needed to reconcile unknown usage.

### Additional audit leads (not yet changed)

- The live log shows a successful primary `conversation_selection` timeout followed by a fallback model success. This is expected under current policy, but the timed-out primary may still have incurred provider-side work and its token cost is unknown to the application.
- The follow-up log includes a successful turn interpretation followed by a generic `generate` call before the engine returned `engine-clarify`. The source shows a separate `_clarification_question` helper can call `decide_chat_action` for short, strategy-less messages. The exact relationship between those two calls needs trace-level confirmation; do not label it redundant without replaying the exact request/decision.
- The legacy `SearchService.find_products_from_text` path calls `AIService.parse_query` and then `parse_query_intent`, whose `_ai_intent` helper can also call the same AIService's `_generate`. The authoritative conversational engine does not use this legacy text parser, so first establish whether any production handler still reaches this path before including it in active consultant costs or changing it.
- `services/vision_search.py` calls the shared `AIService._generate` directly without a specific operation label; this is another reason operation-level accounting is currently incomplete.
- No code or model-routing change has been made based on these leads. Each candidate optimization must be assessed against actual caller reachability, quality, and latency first.

### Prompt-size and call-path audit (2026-11-06)

Static prompt source sizes measured from `services/ai_service.py` (characters, not tokens):

| Prompt | Source characters | Current use |
|---|---:|---|
| `CONVERSATION_TURN_PROMPT` | 7,444 | Every active engine message when Gemini is available |
| `QUERY_PARSING_PROMPT` | 2,311 | Legacy structured text parser |
| `QUESTION_PROMPT` | 1,859 | Product questions |
| `COMPARISON_PROMPT` | 1,661 | Paired listing comparison evidence |
| `IMAGE_ANALYSIS_PROMPT` | 1,304 | Structured photo recognition |
| `RANK_PROMPT` | 728 | Feed ranking |
| `CHAT_ACTION_PROMPT` | 698 | Narrow clarification helper |
| `ANALYSIS_PROMPT` | 627 | Single-product advice |
| `SELECT_PROMPT` | 536 | Feed/product-assistant selection |
| `SEARCH_PLAN_PROMPT` | 363 | Alternative product search wording |
| `IMAGE_QUERY_PROMPT` | 360 | Legacy photo query recognition |

Dynamic payload facts:
- The turn interpreter serializes a bounded context snapshot (up to 6,000 characters before JSON encoding), with recent turns/dialogue, active slots, shown cards and pending question. The latest user message is capped at 500 characters.
- The live selector log shows 70 candidate records, a 31,862-character rendered prompt and 12,367 input tokens on one fallback-model attempt. The current candidate payload includes ID, full title, description up to 1,500 chars, category path, price/currency, discount, rating, orders and availability; no product URL is sent. Do not remove product evidence fields just to shrink tokens without a blinded quality test.
- Product-question prompts may include up to 30 products (`MAX_PRODUCTS_IN_PROMPT`); the product-assistant candidate picker separately caps its candidate pool at 12 and final shown alternatives at 5.
- The selector's large prompt is currently the clearest measured input-token hotspot. The conversation-turn prompt is comparatively compact in the supplied log but is called on every turn, so its cumulative cost still matters.
- No explicit provider context-cache object/configuration was found in the inspected `AIService._generate` request construction. Telemetry can read cached-token metadata, but that alone does not mean caching is enabled or producing hits.
- The default `_generate` call does not set a universal thinking budget. The conversation selector explicitly requests JSON and `thinking_level=low`; other operations rely on prompt-level output requirements and provider defaults. A change to thinking settings requires operation-specific quality/latency tests.

Call-path observations:
- Active text search binds `ConversationSearchEngine` directly in `handlers/search.py`; it does not call the legacy `SearchService.find_products_from_text` method on this path.
- The legacy `find_products_from_text` method does call `parse_query` and then `parse_query_intent`, whose helper can make another `_generate` request. This path is not part of the authoritative Option C search engine; no production caller has yet been established, so do not include it in active user-dialogue cost counts unless a caller is found.
- The engine's `_clarification_question` helper can make a separate `decide_chat_action` request for short (≤4-word) messages that cannot produce a retrieval strategy. This is not automatically a duplicate: it runs only after the turn interpreter did not return the validated `clarify` action (which already returns early). Its marginal quality benefit and added wait/cost need measurement before any removal.
- ProductAssistant's explicit comparison path uses paired evidence extraction; ordinary product questions first try deterministic evidence and only call Gemini when that cannot answer. This conditional structure should be preserved in any cost optimization.

### Stage III/IV — gateway and structured-output capability research (2026-11-06)

This is a source-based technical comparison, not a selection or integration decision.

| Gateway | Published cost model | Relevant capabilities | Trade-offs for this bot |
|---|---|---|---|
| LiteLLM Proxy | Open-source self-hosted gateway: $0; enterprise features are quote-based | OpenAI-compatible interface for 100+ providers; spend tracking by key/user/team/tag; budgets/rate limits; retries and fallbacks; logs/metrics | No per-token gateway fee for self-hosted OSS, but we operate the proxy, database/metrics, security, upgrades and availability. Adds another service and a network hop. |
| OpenRouter | Standard: 5.5% fee when buying credits; Business: 8%; no inference markup. BYOK: no fee on first $25k/month of list-price inference, then 5% | Large model/provider catalogue; usage/activity export; automatic routing/fallback options on paid plans | Convenient for experiments and provider substitution, but credit-purchase fee, external dependency and account/provider policy need consideration. Free model tier has strict daily limits and is not a production cost assumption. |
| Vercel AI Gateway | Provider list prices with no token markup; pay-as-you-go credits; published $5/month free team credit | One API key; unified spend/observability; provider/model routing and fallbacks; supports OpenAI Chat Completions/Responses and Anthropic Messages formats | No stated inference markup, but introduces a third-party gateway, prepaid-credit/account dependency and another runtime hop. Need verify exact provider availability, data policy, SLA and failover behavior for our candidate models. |

Official sources:
- LiteLLM self-hosted gateway: https://docs.litellm.ai/docs/simple_proxy
- LiteLLM pricing: https://www.litellm.ai/pricing
- LiteLLM routing/retries: https://docs.litellm.ai/docs/routing
- OpenRouter pricing/fees: https://openrouter.ai/pricing
- Vercel AI Gateway pricing: https://vercel.com/docs/ai-gateway/pricing
- Vercel AI Gateway capabilities: https://vercel.com/ai-gateway

Structured output support surfaced in official API documentation:
- OpenAI Structured Outputs: strict JSON Schema, including typed fields/enums, for supported current models; stronger contract than plain JSON mode.
- Anthropic Claude: JSON Schema structured outputs and strict tool use are available across current models on the Messages API.
- DeepSeek: JSON Object mode is supported; the Responses API documents JSON Schema output for current V4 series. JSON Object mode guarantees valid JSON but not a specific schema.
- Qwen: JSON Object mode supports most Qwen models; JSON Schema mode is available only on selected model families. Current docs list newer Qwen 3.7/3.8 Flash families for schema mode, while Qwen 3.5 Flash is listed as JSON Object rather than strict schema.
- Gemini: the current bot already requests JSON MIME type for candidate selection, but most other AI calls rely on prompt instructions and application-side validation. A cross-provider adapter must preserve the exact validated contract, not assume all providers' JSON modes behave identically.

Official sources:
- OpenAI structured outputs: https://developers.openai.com/api/docs/guides/structured-outputs
- Claude structured outputs: https://platform.claude.com/docs/en/build-with-claude/structured-outputs
- DeepSeek JSON mode: https://api-docs.deepseek.com/guides/json_mode/
- DeepSeek Responses API: https://api-docs.deepseek.com/api/create-response/
- Qwen structured output: https://docs.modelstudio.console.alibabacloud.com/en/model-studio/qwen-structured-output

Preliminary architecture implications (not a recommendation yet):
- For only two or three providers, a small project-owned adapter/router may be cheaper and easier to operate than adding a gateway, provided usage logging, retry policy, and provider-specific structured output are implemented correctly.
- LiteLLM is the strongest self-hosted candidate if unified spend tracking, provider fallback and multiple model groups become a real operational need; it is not free in engineering/hosting cost.
- Vercel AI Gateway has no published token markup and is worth keeping on the comparison list, but it is an external dependency rather than a required feature.
- OpenRouter is useful for broad model experimentation, but its standard credit purchase fee makes it less attractive as a permanent production path when direct provider accounts are available.
- No gateway has been connected, and no provider key/configuration has been added.


### Stage I — verified production call graph and further cost risks (2026-11-06)

This update records source-level reachability from the PR head `98b3630f4d11a5f136987445053381452912932d`. It does not change application code.

#### Confirmed active conversational call sequence

1. The normal text handler first handles several local shortcuts (pending product-question continuation, narrowing already-shown cards, count/currency-only follow-ups). Those paths can complete without a new Gemini call.
2. Other ordinary text messages enter `ConversationSearchEngine.handle_message` and call `decide_conversation_turn` once when the AI service is available. This is the main per-message interpretation cost, including natural `respond` turns.
3. A validated `respond` action exits before retrieval and candidate selection. Compare/alternatives over already-shown products are also resolved from the ledger/cache and do not call AliExpress retrieval.
4. A retrieval turn builds one strategy and invokes the existing provider path. If it returns a non-empty `RESULTS` decision, `_apply_ai_candidate_selection` may call `select_conversation_candidates` once. The default 30-card view reviews up to 70 candidates and asks the model for at most 20; up to 10 deterministic-safe tail cards are appended only if the model returns the full 20.
5. If the completed search ends in `NO_CANDIDATES` or `NO_ELIGIBLE`, `compose_no_results_reply` may make one more call. It is skipped after selector timeout/error/unavailable or an exhausted turn budget. Its output is wording only; it receives no product rows.
6. The separate `decide_chat_action` helper can run only on the short, strategy-less clarification path (up to four words). It is not a second interpretation call on every turn and is not automatically redundant with `conversation_turn`; it is conditional on the engine reaching that branch.

#### Product-assistant call counts (conditional)

- «Стоит ли покупать?» calls `analyze_product` once when AI is available; deterministic advice is the fallback.
- «Найти похожий / дешевле / лучше» can call `plan_product_search` once to formulate the query, perform a real AliExpress candidate search, then call `select_candidates` once after deterministic goal-specific filtering. This is a distinct, potentially two-LLM-call workflow plus provider retrieval.
- Explicit «сравни» uses `extract_paired_listing_evidence` once for the top two distinct shown products, then the application renders the comparison. It does not need a second generic product-question call.
- Other product questions first run `deterministic_answer`; only if that cannot answer does `answer_product_question` make one model request. Preserve this deterministic-first gate.

#### Photo-search duplicate-call path — confirmed issue to evaluate

The active Telegram photo handler is `handlers.discovery.photo_message` → `handlers.photo_search.photo_message_handler`. It first calls `analyze_photo_with_vision` from `services/vision_search.py` (operation currently labelled generic `generate`). If that call returns no usable `query`, the handler invokes `handlers.discovery._legacy_photo_message` with the already-downloaded image bytes. That fallback then calls `AIService.analyze_image` (another Gemini request) when the real AIService is present.

Therefore a single photo upload can make **two sequential vision requests when the first request fails or returns unusable output**. The comment calls the second path a legacy fallback, but the active photo handler reaches it in production. This is not a confirmed defect to remove yet: structured analysis may recover a useful result after the first prompt fails. It is, however, a real retry/fallback cost path that must be measured separately, and quota/auth failure should be distinguished from a genuinely unrecognized image before deciding whether a second request adds value. The older `describe_image_query` method is not used by this fallback when the production AIService exposes `analyze_image`.

#### Feed and legacy reachability boundaries

- The normal «✨ Находки» `show_feed` handler calls `find_feed_page`; its source explicitly preserves AliExpress order and does not invoke Gemini selection.
- `select_feed_candidates` is reached through `SearchService.apply_feed_selection` for non-raw search outcomes and for the merged photo-search pool. It is separate from the core Option C selector.
- `rank_candidates` is called by `SearchService.find_discovery`. No caller from the inspected active Telegram handlers was found yet; keep it out of the live consultant cost model until reachability is proven.
- `parse_query` is called by `SearchService.find_products_from_text`, the legacy structured-text entry point. The authoritative `handlers.search.search_message` path binds and calls `ConversationSearchEngine` directly, not this parser. The legacy method then calls `parse_query_intent`, which can make a further model request; do not count this as a normal conversation flow without a production caller.
- `decide_chat_action` has the conditional engine clarification caller described above; do not treat the method as globally dead.

#### Telemetry gaps now verified more narrowly

- The `turn_scope` class exists, but no production `with turn_scope(...)` use was found in the inspected active engine, Telegram handlers, ProductAssistant, SearchService or startup wiring. The per-turn aggregate is therefore not currently a dependable production metric; per-attempt INFO logs remain the reliable application-level records.
- Telemetry is in-memory/context-local and emitted to logs; no durable token/cost ledger or per-user/per-scenario roll-up is implemented in `gemini_telemetry.py`.
- The operation label is still too coarse: the four named labels cover the core conversation and product-question paths, while photo understanding, product advice, search planning, feed selection, legacy parsing and several other helpers use `generate`. A stable operation taxonomy is needed before cost-per-operation reports can be trusted.
- `_generate` wraps the SDK call once and records one application attempt. SDK-internal retries, if any, are not separately represented in the current telemetry; this needs verification against the pinned SDK's retry configuration before treating application attempts as exact HTTP request counts.
- No `max_output_tokens` limit is passed by `_generate`. Prompts impose textual limits, but those are not a hard provider-side output-token cap. The only per-operation generation config currently set in `_generate` is JSON MIME type plus low thinking level for `conversation_selection`.
- The 6 November startup warning about direct AFC use must not be interpreted as evidence that the bot is performing ten model/tool rounds. Inspected `_generate` requests do not pass `tools` or function declarations; the SDK warning is not proof of repeated remote calls. No function-calling workflow is part of the Option C architecture.
- The project pins `google-genai==2.24.0`. Automatic continuation for `generate_content` was introduced in a later SDK release (2.30.0); do not attribute hidden continuation hops to the currently pinned 2.24.0 runtime based on current upstream documentation. Reassess if the SDK is upgraded. Sources: https://pypi.org/project/google-genai/2.24.0/ and https://github.com/googleapis/python-genai/releases (v2.30.0 release notes).

#### Audit disposition

- Confirmed optimisation lead: photo-search failure can trigger a second sequential vision request; quantify success/failure categories and marginal recovery before changing it.
- Confirmed observability gap: production turn aggregation is not wired; operation labels are incomplete; there is no monetary cost calculation or durable spend ledger.
- Confirmed output-control gap: no provider-side max output token cap is set centrally; evaluate safe per-operation limits against the required structured contracts.
- Not a confirmed optimisation: the separate short-message clarification call. It is conditional and may add conversational value; benchmark it rather than deleting it.
- Not active core consultant calls unless further caller tracing proves otherwise: legacy `parse_query` / `find_products_from_text` and discovery `rank_candidates`.
- No source code, model routing, prompts, search retrieval, ranking, product validation, or provider configuration was changed. No tests were run for this documentation-only audit update. No live bot, Telegram, Gemini or AliExpress calls were made.


### Stage I — final clarification of feed-selection reachability and ProductAssistant cost (2026-11-06)

A deeper trace of `SearchService.find_products` corrected and expanded the earlier ProductAssistant call estimate:

- `ProductAssistant.candidate_products()` calls `search_intent()`; when AI is available this can call `plan_product_search`.
- It then calls `SearchService.find_products(request)` with the default `broaden=True` and `feed_selection=True`.
- The normal non-raw `find_products` path can call `SearchService.apply_feed_selection` → `AIService.select_feed_candidates` once after building its broad candidate pool.
- After that result returns, `ProductAssistant._select()` applies goal-specific deterministic filtering and can call `AIService.select_candidates` once more.
- Therefore a ProductAssistant «похожий / дешевле / лучше» action can issue **up to three Gemini requests** (search-plan wording + generic feed selection + goal-specific selection), with the latter two both selecting from real candidates. The exact count is lower when a stage is skipped due to no candidates, no AI service, or failure.
- The two selectors have different contracts: generic feed selection applies shared broad quality rules; the later ProductAssistant selector applies the user's goal. This is not yet proven redundant, but the generic selector may pre-trim the candidate set before the goal-specific comparison and may spend a request that could be unnecessary. Candidate optimization for later evaluation: test calling `find_products(..., feed_selection=False)` from ProductAssistant while retaining the same retrieval depth and deterministic safety/goal filters, then compare candidate recall, selected quality and latency. Do not implement before a controlled evaluation; inspect `pool_target` and candidate limits to ensure disabling selection does not silently narrow retrieval.
- The previous source-derived estimate of two calls for this action is superseded by this verified upper bound of three.

Additional reachability clarifications:
- Category browsing (`handlers.discovery.category_callback`) calls `SearchService.find_by_category`, which delegates to `find_products` with its default `feed_selection=True`; category browsing can therefore make one `select_feed_candidates` call after retrieval.
- The ordinary «✨ Находки» feed is different: `show_feed` calls `find_feed_page` and intentionally makes no Gemini selection request.
- The active photo handler uses `find_products(..., broaden=False)` for its individual image hypotheses and then applies the feed selector once to the merged photo pool. The per-hypothesis searches do not each invoke the generic selector because the `broaden=False` path bypasses inline feed selection.
- The separate `SearchService.find_discovery` / `rank_candidates` method remains without a confirmed caller in the inspected Telegram handlers; keep it classified as unverified/legacy rather than active consultant traffic.

### Runtime configuration facts confirmed in source

- Default AIService request timeout is 20 seconds. The active conversation-turn wrapper uses an 8.5-second outer deadline and a 4-second per-model attempt; candidate selection uses a 15-second outer deadline and a 6.5-second per-model attempt. The no-results composer has a 3-second outer bound. Product-question helpers use the separate `PRODUCT_QUESTION_TIMEOUT_SECONDS` bound.
- Primary model defaults for fast, smart and vision roles are all `gemini-3.8-flash`; fallback defaults to `gemini-3.1-flash-lite`. Environment overrides exist as `GEMINI_FAST_MODEL`, `GEMINI_SMART_MODEL` (plus legacy `GEMINI_MODEL`), `GEMINI_VISION_MODEL` and `GEMINI_FALLBACK_MODEL`.
- The settings comments describe role intent, but no separate per-operation model routing table exists: callers pass a model tuple to `_generate`; fallback is appended centrally. Any future router should map operations explicitly rather than merely mapping the three role names.
- The default search service uses a deep pool when its feed selector is available. For non-raw requests it may perform one AI selection; the raw conversational adapter returns before that feed-selection step and leaves semantic selection to `ConversationSearchEngine`. This avoids double selection on the core text-search route.
- Category and ProductAssistant searches use the non-raw path and therefore have a different cost profile from the main conversational text-search route.

No implementation or runtime settings were changed. No tests or live API calls were made. This is source-level call-graph evidence only; observed call frequency and actual costs still require telemetry or controlled replay.

### Stage II preparation — source-derived scenario call-count matrix (not yet a cost benchmark)

The following matrix describes possible application-level Gemini operations from the current source. It is **not** a measured average, and fallback-model attempts are additional provider attempts. One _generate operation may use the primary and then the fallback model under the existing error policy.

| Scenario | Source-derived Gemini operations | Conditional branches / notes |
|---|---|---|
| 1. New product search | conversation_turn + optional conversation_selection | Selector only when deterministic evaluation leaves a non-empty RESULTS pool. |
| 2. Add or change budget | conversation_turn + optional conversation_selection | Same core route; no separate budget-specific model call. |
| 3. Add mandatory feature / compatibility | conversation_turn + optional conversation_selection | Semantic review may be needed for unproven listing evidence; deterministic gates remain first. |
| 4. Add exclusion / preference | conversation_turn + optional conversation_selection | No separate model call for deterministic slot extraction. |
| 5. Switch to a different product | conversation_turn + optional conversation_selection | Explicit product switch is resolved inside the same interpretation/ledger route. |
| 6. Ask for alternatives | conversation_turn + optional conversation_selection | Retrieval is conditional on the engine resolving an alternatives strategy. |
| 7. Compare already-shown cards | conversation_turn + optional product_question | Explicit comparison uses paired evidence extraction; deterministic comparison rendering follows. |
| 8. Ask a factual question about a shown product | conversation_turn + optional product_question | The product assistant tries deterministic evidence first. Menu-originated pending questions can bypass the general turn interpreter. |
| 9. Ask about compatibility / a feature | conversation_turn + optional product_question | If title matching or deterministic facts answer it, the second call is skipped. |
| 10. Short ambiguous / incomplete message | conversation_turn + conditional decide_chat_action | The second operation is only for the short, strategy-less clarification branch (≤4 words); do not assume it runs on every short message. |
| 11. No candidates / no eligible candidates | conversation_turn + optional conversation_selection + optional no_results_reply | Selector only if there was a non-empty pool; no-results wording is skipped after selector timeout/error/unavailable. |
| 12. AliExpress provider failure | Usually conversation_turn only | Provider-error outcomes do not enter the no-results wording path and do not invoke candidate selection. |
| 13. Long multi-turn conversation | One conversation_turn per non-shortcut text message, plus conditional selector/no-results/question calls | Context is bounded; cumulative spend depends on turns, shown-card payload, active slots and how often the user asks follow-ups. |
| Photo search (separate flow) | One vision call normally; a second vision call if the first produces no usable query; optional one merged-pool feed selection | The two vision prompts are different, but this is a sequential retry after first-stage failure. |
| Product menu: similar / cheaper / better | Up to three calls: plan_product_search, select_feed_candidates, select_candidates | The generic selector is currently applied inside SearchService before the goal-specific ProductAssistant selector. This is a candidate for controlled optimization, not an approved change. |
| Category browsing | Optional one select_feed_candidates | find_by_category delegates to non-raw find_products with feed selection enabled. |
| Ordinary «✨ Находки» feed | Zero | show_feed uses find_feed_page; no Gemini call. |

#### What this matrix does and does not establish

- It establishes source-level upper/lower call shapes and conditional gates; it does not establish frequency, average tokens, average provider attempts, or dollars per scenario.
- The same source-level scenario may make zero calls when the user message is consumed by a cache/local shortcut or Gemini is unavailable.
- A model-unavailable response can cause a fallback attempt for any operation. Client-timeout fallback is currently limited to conversation_turn and conversation_selection. Quota/rate-limit errors stop immediately.
- The sample 6 November log gives token examples for two conversation turns, one successful selector fallback and one generic helper call. It is not enough to extrapolate token counts for all rows in this matrix.
- The 20/50/100/500 active-user forecast cannot be made honestly until average active turns/user/day, scenario mix, selector invocation rate, prompt-size distribution, fallback rate and timeout unknown-cost rate are measured or explicitly declared as scenario assumptions.
- Stage II remains open. Build a representative 13-scenario evaluation/replay set and capture per-operation usage before presenting a financial forecast. Any forecast must show assumptions and a range, not a single invented monthly bill.


### Stage II — preliminary core-text cost sensitivity (illustrative, not production forecast)

A small transparent sensitivity model can be calculated from the two successful conversation-turn samples and one successful candidate-selection sample in the user's 2026-11-06 live log. It is intentionally limited to the core text-search route.

Observed unit samples, using Google's current Standard API rates through 2026-12-31:
- Mean of two successful conversation turns on gemini-3.8-flash: **$0.002368 per turn** (mean of $0.0020895 and $0.00264675).
- Successful conversation selector on gemini-3.1-flash-lite: **$0.00534775** (12,367 input + 1,504 billable output/thought tokens).
- Sensitivity case for the same selector token volume on gemini-3.8-flash: **$0.01491525**. This is a price-only model, not an observed successful 3.8 selector response.
- Google currently lists gemini-3.8-flash at $0.75/M input and $3.75/M output through 2026-12-31, rising to $1.50/M and $7.50/M on 2027-01-01. Gemini 3.1 Flash-Lite is listed at $0.25/M input and $1.50/M output. Official pricing page: https://ai.google.dev/gemini-api/docs/pricing

Declared activity assumptions for this sensitivity only:
- “Active user” means one daily active user (DAU), not a monthly active user.
- 3 ordinary text messages per DAU/day reach conversation_turn.
- 1.5 candidate-selection calls per DAU/day (one selector for every two interpreted messages on average).
- 30 active days per month.
- No product-menu actions, photo searches, no-results replies, category selections, SDK/provider retries, unknown timeout charges, taxes, cache discounts, or inactive days are included.

| DAU | Monthly cost if selectors use observed Flash-Lite unit rate | Monthly cost if selectors use 3.8 Flash unit rate |
|---:|---:|---:|
| 20 | $9.08 | $17.69 |
| 50 | $22.69 | $44.22 |
| 100 | $45.38 | $88.43 |
| 500 | $226.89 | $442.16 |

Formula per DAU/month: 30 × (3 × $0.002368125 + 1.5 × selector_unit_cost).

Interpretation:
- This is a core-text workload sensitivity, not a forecast of the bot's actual monthly bill. It uses two turn observations and one selector observation; those are too few to represent token distributions or model-selection success rates.
- The low column assumes the selector uses Flash-Lite at the observed token volume; the high column assumes the same volume is billed at Flash pricing. Neither column captures the unknown provider-side cost of the timed-out primary selector attempt in the live log.
- For the first observed search, the successful turn plus successful Flash-Lite fallback selector subtotal is $0.00743725. If the timed-out primary selector consumed the full input volume, that would add at least about $0.00927525 at the current Flash input rate; if it also generated output comparable to the successful fallback, it would add about $0.01491525. Those are sensitivity examples only; actual timeout usage is unavailable.
- The current 3.8 introductory rate expires at year-end. From 2027-01-01, the 3.8-based column's per-turn and selector unit costs approximately double if token volumes remain unchanged. Recalculate before that date.
- These figures should not be presented as the 20/50/100/500-user final model until actual DAU definition, activity distribution, selector frequency, operation-specific token percentiles, and timeout billing are measured. Stage II remains open.

### Stage I — token-payload structure and avoidable repetition (2026-11-06)

Further inspection of the exact prompt builders identified several measurable optimization candidates. None has been changed.

#### Conversation-turn input

- The interpreter receives both recent_turns (up to six user-only messages) and recent_dialogue (up to twelve role-labelled user/assistant messages). User utterances therefore overlap between these two fields. The prompt explicitly says they complement one another, so removing one without a dialogue-quality benchmark is not approved; measure whether the duplicate user history adds accuracy.
- The context builder also sends active slots, pending question, last outcome, shown-product count and up to 30 shown-product summaries (ID, title truncated to 48 characters, price and currency). This grounding is relevant to follow-up references but can contribute materially to input size as the conversation grows.
- The serialized context is bounded to 6,000 characters and compacted structurally; the current message is separately capped at 500 characters. This is already a useful guard and should be preserved.
- Candidate study: compare the current context against a compact variant that removes only duplicated user-history material while preserving the newest user/assistant exchange, active slots, pending question and shown-card references. Evaluate all 13 scenarios, especially corrections, clarification answers, pronouns and product switches.

#### Candidate-selection input

- Each candidate payload contains ID, full title, description truncated to 1,500 characters, category path, price, currency, discount, rating, order count and availability. Product URLs are not sent.
- The current Affiliate API mapper does not populate Product.description; in ordinary live search this field is generally empty. The serializer still emits an empty description field for every candidate. Omitting only empty description values is a low-risk token-saving candidate because it removes no evidence; preserve non-empty descriptions if a future authorized provider source supplies them.
- The full category path is useful weak context, but the prompt correctly says it is not proof of identity or a required feature. Its token cost versus quality contribution should be measured before shortening or removing it.
- The query string repeats active use-case, compatibility, feature, preference, exclusion and budget information. The prompt separately includes a required-feature JSON list and a detailed budget rule. Some repetition is deliberate (high-priority constraints are restated in explicit fields), but it is a concrete place to measure marginal quality per token rather than delete blindly.
- The output contract asks for identity_evidence and requirement_evidence fields on every selected row, even when a row has no semantic identity review and there are no active required features. A future schema could request evidence only where required and IDs alone for already deterministic-safe rows; this is only a candidate because the current exact-evidence contract is a key safety boundary.
- The 70-candidate input pool remains the dominant measured prompt. Do not reduce the review window or remove listing fields before blinded tests prove the same cross-category identity/feature quality and no worse recall.

#### Output configuration

- _generate does not set a central max_output_tokens. The candidate selector sets response_mime_type=application/json and thinking_level=low; other structured operations such as feed selection, product selection, conversation-turn interpretation and product questions rely on prompt-level JSON instructions plus local parsing/validation.
- No per-operation token cap or universal JSON Schema is enforced at the provider boundary. This is a reliability/cost trade-off to evaluate separately: a too-low cap can truncate evidence and cause a deterministic fallback or hide a valid result.
- The product-question payload can contain up to 30 product records at the AIService boundary; ProductAssistant's menu-originated product-question helper commonly limits its session set to eight, while the conversation handler can pass the products actually shown. Explicit paired comparison sends only two records.

#### Prioritized token-efficiency hypotheses (not approved changes)

1. Omit empty description keys from candidate JSON; preserve every non-empty source field.
2. Measure whether the duplicated recent user history in recent_turns + recent_dialogue is useful; remove overlap only if blind quality tests remain equivalent.
3. Measure the value of repeated constraints across the free-form query and explicit requirement/budget fields; retain explicit hard-constraint representation regardless.
4. Consider conditional evidence output for only the candidates/requirements that actually require semantic review; do not weaken exact quote validation.
5. Evaluate reusable static prompt-prefix/context caching only after checking cache minimums, cache creation/storage charges, cache-hit telemetry and latency. No explicit cache resource is configured today.

No code, prompt, model, or search behavior was changed. No tests or live calls were run for this documentation-only audit.


### Correction — Gemini implicit context caching (2026-11-06)

The earlier statement that no cache configuration was found is accurate only for **explicit** cache objects. It must not be read as “Gemini caching is disabled.”

Google's current Gemini API documentation states:
- Implicit caching is enabled by default for Gemini 2.5 and newer models, including the project's gemini-3.8-flash and gemini-3.1-flash-lite.
- The documented minimum input size for implicit caching on Gemini 3.8 Flash is 4,096 tokens.
- To improve hit probability, large common content should be placed at the beginning of the prompt and repeated requests should have similar prefixes.
- The response usage metadata exposes cached-token counts; the current telemetry extractor already attempts to read cached_content_token_count.
- Explicit caching is a separate, manually managed mechanism with cache storage charges and TTL; it is not currently configured in this project.

Official source: https://ai.google.dev/gemini-api/docs/caching

Current code implications:
- Conversation-turn prompts start with a large static instruction, but the supplied live turn usage was below the 4,096-token minimum overall (1,891 and 2,369 input tokens), so those examples could not have met the documented minimum for an implicit cache hit.
- Conversation-selection prompts exceed 4,096 tokens, but dynamic query/identity/seller fields occur before the large candidate payload and much of the dynamic content changes between searches. A large total prompt alone does not guarantee a reusable common prefix.
- No cached-token hit was visible in the supplied live log sample. This is absence of evidence in that sample, not proof that implicit caching never hits in production.
- The telemetry must report cached tokens distinctly and cost calculations must price those tokens at the applicable cached-input rate rather than regular input. Do not subtract cached tokens twice.
- A prompt-order/cache experiment is a later optimization candidate. First measure real cached-token hit rates from repeated production-like calls; do not add explicit cache objects until the stable prefix size, hit rate, storage TTL and savings justify their complexity.

This corrects and supersedes any interpretation of the earlier cache note that implied Gemini had no caching enabled by default.


### Stage VIII preparation — existing evaluation assets and their limits (2026-11-06)

The existing repository has useful regression coverage, but it does **not** yet provide the required blind multi-model benchmark for the active Option C consultant.

- tests/test_ai_conversation_turn.py contains focused tests for validated actions, grounded slot updates, cross-language seller terms, exact quote evidence, budgets, exclusions and product identity.
- tests/test_convsearch_turn_interpretation.py contains a broad set of multi-turn engine/handler regressions: shown-card grounding, product switches, clarification answers, budget refinements, exclusions, compare-vs-new-search, natural replies and provider failures.
- tests/test_convsearch_engine.py covers retrieval-pool behavior, 30-card cap, up to 80/70/20 candidate selection, semantic review, no-results replies, budgets and multiple product-family scenarios (including fishing reels, cordless screwdrivers, power banks and household alternatives).
- tests/test_product_assistant.py covers advice, cheaper/similar/better selection, deterministic answers, comparisons, currency consistency and grounded evidence.
- These are primarily unit/integration contract tests with mocked Gemini responses. They prove application validation and fallback behavior, not that a live model understands Russian/Ukrainian dialogue correctly or that one provider is as good as another.

Important legacy-evaluation mismatch:
- tests/eval_b5_dataset.py contains 35 controlled RU/UK/EN single-query cases across product, numeric, budget, exclusion, ambiguous, discovery and cross-category groups.
- tests/eval_b5_runner.py sends those cases through SearchService.find_products_from_text, which is the legacy structured-query entry point. The active Telegram text-search path uses ConversationSearchEngine directly and does not call that method.
- Therefore B5 is not a valid benchmark for the active Option C conversational interpreter or its per-turn Gemini cost. It may still be useful as a legacy parser/retrieval diagnostic, but its runner must not be presented as active consultant quality evidence.
- Do not run B5 as the primary optimization benchmark or use its results to compare models for the active dialogue route.

Proposed evaluation design to complete Stage VIII:
1. Build a separate, versioned dialogue dataset with at least the 13 required scenarios and multi-turn transcripts, not isolated search phrases.
2. Cover Russian, Ukrainian and English; include mixed-script seller-title cases and language switches.
3. Use several unrelated product families from the current regression suite plus additional categories, avoiding one product-specific benchmark.
4. For the interpreter, score action classification, product identity, retained/replaced/removed slots, clarification quality, product-switch detection, and whether retrieval should or should not run.
5. For the selector, replay identical real or carefully captured AliExpress candidate pools to every model; score exact product identity, each mandatory feature, exclusions, budget, evidence quote grounding, precision/recall and safe abstention.
6. Keep application validators and deterministic gates identical for all models. Separate raw model output quality from final application acceptance.
7. Run blinded model comparisons with the same prompts, candidate payloads, output schema, timeouts and concurrency; record p50/p95 latency, input/output/thought/cached tokens, valid-schema rate, accepted-candidate precision/recall and total task cost.
8. Include provider failures, timeouts, quota errors and fallback chains. A model that is cheap but causes more empty searches, retries or unsafe candidates must not win on token price alone.

No evaluation dataset or runner has been added yet. No live provider calls were made. Stage VIII remains open.


### Correction — SDK HTTP retry behavior verified against pinned version (2026-11-06)

The earlier audit note left SDK-internal HTTP retries unresolved. This has now been checked against the exact upstream source tag for the project's pinned google-genai 2.24.0:

- AIService constructs the client as genai.Client(api_key=api_key) without http_options or retry_options.
- In google-genai v2.24.0, retry_args(options=None) explicitly returns a one-attempt policy (stop_after_attempt(1)). The SDK does not automatically retry transient HTTP 408/429/5xx/network errors when retry_options is absent.
- Therefore, for the current pinned runtime, one AIService._generate model attempt corresponds to one SDK HTTP attempt; a configured fallback model is a separate application-level attempt and is already recorded separately by gemini_telemetry.
- Do not import retry defaults from newer SDK documentation into the v2.24.0 cost model. If the SDK is upgraded or retry_options are configured later, re-audit the exact version and include internal retry counts in telemetry.

Pinned-version source: https://github.com/googleapis/python-genai/blob/v2.24.0/google/genai/_api_client.py (retry_args; async request path). Client construction: services/ai_service.py, AIService.__init__.

This resolves the previous "SDK-internal retries, if any" uncertainty for the current pinned configuration. Provider-side work after a client timeout can still have unknown billing; that is separate from client retries.


### Stage I — default thinking level and output-limit implications (2026-11-06)

Google's current official thinking documentation confirms:
- gemini-3.8-flash defaults to thinking_level=medium; supported levels are low, medium and high.
- gemini-3.1-flash-lite defaults to minimal thinking.
- The current candidate selector explicitly sets low thinking for its Gemini request.
- The active conversation-turn interpreter and most other structured operations do not set a thinking level, so the 3.8 primary uses the medium default. This includes the per-message interpreter, no-results reply, product questions, product advice, search planning, feed selection and ProductAssistant selection.
- The live sample's two successful conversation turns generated 132 and 188 thought tokens respectively, against 47 and 44 candidate/output tokens. This makes thinking effort a material cost/latency hypothesis, not a cosmetic setting.
- A controlled low-vs-medium experiment on conversation_turn and the simpler helper operations is worth prioritizing. It must compare action/slot correctness, clarification quality, product-switch detection, candidate quality and latency before any default changes.
- Google's documentation warns that max_output_tokens is a hard cap over both thought and candidate tokens and can truncate a response while still billing the generated thought tokens. A small global cap is therefore not a safe cost shortcut. Prefer evaluating a lower thinking level per operation, then set generous operation-specific output caps only if truncation behavior is understood.

Official sources:
- https://ai.google.dev/gemini-api/docs/thinking
- https://ai.google.dev/gemini-api/docs/generate-content/thinking

No thinking level, output cap or prompt configuration has been changed. These are benchmark hypotheses only.


### Stage VIII — initial active Option C dialogue corpus created (2026-11-06)

Added:
- `tests/eval_consultant_dialogue_dataset.py` — versioned `consultant-dialogue-v1` corpus with exactly 13 distinct scenario groups and 18 turn-level examples.
- `tests/test_eval_consultant_dialogue_dataset.py` — structural tests for scenario count/uniqueness, RU/UK/EN coverage, role-labelled assistant clarification context, and the distinction between a successful empty search and total AliExpress provider failure.

Coverage:
1. New product search.
2. Budget refinement.
3. Mandatory feature + use-case context.
4. Exclusion + preference.
5. Targeted correction replacing one active feature without dropping another.
6. Explicit product switch with stale prior constraints in context.
7. Alternatives within the same product family.
8. Comparison of already-shown cards.
9. Factual question about a shown card.
10. Short ambiguous message requiring clarification.
11. Search that succeeds technically but returns no eligible products, followed by the no-results path.
12. AliExpress total provider failure, kept separate from an empty catalogue outcome.
13. Four-turn dialogue with an assistant clarification, its answer, a budget refinement and a later preference.

Languages: Russian, Ukrainian and English. Every turn includes a bounded context snapshot and gold action/product-type/slot-update/retrieval expectations. The D13 first turn deliberately expects clarification for "compressor for garage": that purpose alone does not distinguish tire inflation, painting or other compressor tasks. Its later answer is evaluated in the context of the assistant's actual prior question.

Limitations / next work:
- This is an initial gold corpus, not yet a blind model benchmark. Gold labels and edge cases require review against actual anonymized user dialogues before using scores to select a provider.
- It has not yet been replayed through the live model or active engine. Existing mocked unit tests do not establish live multilingual model quality.
- No authentic AliExpress candidate-pool snapshots were found in the repository. Therefore no synthetic product listings were fabricated for selector benchmarking. A separate selection corpus must be built from anonymized, permitted real API result snapshots, preserving the exact title/description/price/currency/category evidence that the selector receives.
- A provider-neutral, blind replay runner is still required. It must send identical prompts/context to each candidate model, capture raw output and usage/latency, run the same application validation contract, randomize model labels for scoring, and report per-scenario results. Do not change production routing while building it.
- No production code, model config, prompts, retrieval, ranking or validation behavior was changed. The new corpus and its structural tests were committed, but no test command or live model/API run was executed in this session. GitHub combined status currently returned no status entries; this is not a test pass.

Latest corpus commits:
- `95804e0d23ada55e28611f228cf30629ff0d2a1a` — initial corpus.
- `4c69724861708e92ac7ec01f3aa495b72918b178` — clarify ambiguous compressor scenario.
- `887947092fa86d58e2c667282797e7e000c4b21f` — remove duplicated assistant turn in D13.
- `15307450ea73e4b11460b36823384ba1c70f2419` — structural test file.


### CI verification of the new dialogue corpus (2026-11-06)

The corpus structural tests were added to the focused offline CI command in `.github/workflows/conversational-regression.yml`.

Observed GitHub Actions result for commit `b969cdc6b3ded0f0f72c578ced9f934f7fb5a03f`:
- The new corpus tests were collected successfully after correcting the import to `tests.eval_consultant_dialogue_dataset`.
- Focused suite result: **297 passed, 2 failed, 65 subtests passed**. The five new structural tests account for the increase from the previous run's 292 passing tests.
- The same two failures are present on the pre-corpus commit `0fafe3ae1de79f0cb53b04882076353948d03c92` (run #1078), so they predate this evaluation-corpus work:
  1. `test_conversation_selector_prompt_enforces_exclusions_and_budget` expects the literal phrase «после запятой», while the current prompt has the equivalent updated paragraph but not that exact wording.
  2. `test_text_query_is_parsed_and_searched` reaches legacy `SearchService.find_products_from_text` and fails because `AIService._with_result_limit` is missing.
- Neither failure was introduced by the new corpus. Both are left untouched because they concern an old prompt wording assertion and a legacy search path outside the current cost-audit scope.
- The workflow's repository-wide filtered suite is skipped because the focused command fails first. Do not report a green CI run or a full-suite pass.
- The corrected import and all five corpus structural tests are confirmed to pass by the 297-pass result; the two baseline failures remain.

The CI workflow change only adds `tests/test_eval_consultant_dialogue_dataset.py` to the focused offline command. It does not change runtime bot behavior.


### Stage VIII — blind benchmark protocol and direct API compatibility verified (2026-11-06)

Added `tests/eval_consultant_benchmark_protocol.md`. It defines:
- identical active production interpreter prompt/context across providers;
- common application validation as the final gate;
- model aliases randomized for blind review, with mapping stored separately;
- repeated randomized request order, p50/p95 latency, token/cache/thinking usage and exact tariff capture;
- per-scenario and per-language scores with hard safety gates, not one aggregate score;
- separate selector evaluation using identical authentic AliExpress candidate snapshots only;
- a first single-attempt capability pass and a separate production-timeout/fallback resilience pass.

Direct API documentation checked for the initial candidate roster:
- OpenAI GPT-6 Luna: model ID `gpt-6-luna`; Responses API supports strict JSON Schema via `text.format`. Official docs: https://developers.openai.com/api/docs/models/gpt-6-luna and https://developers.openai.com/api/docs/guides/structured-outputs
- Anthropic Haiku 5.5: model ID `claude-haiku-5-5`; Messages API supports JSON Schema output through `output_config.format`. Official docs: https://platform.claude.com/docs/en/models/haiku-5-5/overview and https://platform.claude.com/docs/en/api/http/messages
- DeepSeek V4.1 Flash: model ID `deepseek-flash`; OpenAI-compatible base URL `https://api.deepseek.com`; documented JSON Output is JSON-object mode, not strict JSON Schema. Official docs: https://api-docs.deepseek.com/quick_start/pricing/ and https://api-docs.deepseek.com/guides/json_mode/
- Alibaba Qwen 3.5 Flash: model ID `qwen3.5-flash`; supports structured output. International and Frankfurt deployment availability are documented, but access uses a workspace-specific endpoint; the runner must take the exact base URL from environment/config and must not guess a workspace ID. Official docs: https://help.aliyun.com/zh/model-studio/qwen3-5-flash and https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-responses
- Gemini remains the production baseline: `gemini-3.8-flash` and `gemini-3.1-flash-lite`.

Fairness note: the APIs expose different levels of structured-output guarantees. Record each mode explicitly. OpenAI and Anthropic can use JSON Schema; DeepSeek's documented JSON Output guarantees valid JSON only; Qwen's documented structured-output support must be verified against the exact regional endpoint and request mode. The same application validator remains mandatory for every provider.

No model request was made and no provider credentials were added. The protocol is design documentation, not a completed benchmark. The next implementation item is a provider-neutral, no-retry replay runner that reuses the real interpreter prompt and validation path while capturing each provider's native usage metadata. Authentic selector snapshots remain a separate prerequisite.


### Stage VIII — provider-neutral blind interpreter runner implemented (2026-11-06)

Added `tests/eval_consultant_benchmark_runner.py`, an opt-in direct-provider replay tool. It is separate from production runtime routing and never runs automatically against paid APIs in CI.

Verified design/implementation:
- Reuses the actual `AIService.decide_conversation_turn` prompt builder and application validation path. Only the transport call is replaced in-memory for each isolated request.
- Uses the same corpus message/context for every model; request order is shuffled with a recorded seed.
- Randomizes blind aliases A/B/C... per run. Model/provider mapping and resolved model versions are written to a separate map file; the result file contains aliases only.
- Captures raw model text, accepted validated decision, exact gold decision, per-dimension scores, finish reason, provider/wall latency and usage metadata.
- Captures p50/p95 and acceptance/exact-decision/action accuracy by model alias, scenario and language.
- No retry or fallback is made in the first capability pass. The run manifest explicitly records zero retries/fallbacks and the primary attempt timeout.
- API keys are read only from process environment variables. The runner does not load `.env`, does not use Telegram/AliExpress/database connections, and never writes credentials to outputs/logs.
- Requires at least two distinct provider APIs; two Google models under one API key alone do not count as a cross-provider comparison.
- Optional dated rate JSON enables per-request cost estimates. If token usage or the relevant cached/cache-write tariff is unknown, cost stays unknown rather than being reported as zero.
- Gemini output-token accounting includes both candidate and thought tokens exactly once. Anthropic input usage is normalized across uncached, cache-read and cache-write token categories. Cached input is priced separately; cache-write pricing must be supplied where applicable.

Initial adapter roster and modes:
- Google Gemini 3.8 Flash and Gemini 3.1 Flash-Lite: REST generateContent, JSON MIME mode, provider-default thinking (model defaults differ).
- OpenAI GPT-6 Luna: Responses API, strict JSON Schema, reasoning effort medium.
- Anthropic Claude Haiku 5.5: Messages API, JSON Schema output, effort medium.
- DeepSeek Flash: Chat Completions, JSON Object mode, provider-default thinking.
- Alibaba Qwen 3.5 Flash: workspace-specific OpenAI-compatible endpoint from `QWEN_BASE_URL`, strict JSON Schema with thinking disabled. Official Qwen docs state Qwen3.5 Flash supports schema output in non-thinking mode; the adapter deliberately disables thinking and omits max_tokens in this mode. This setting difference is recorded in the blind map and must be considered in scoring. Source: https://docs.modelstudio.console.alibabacloud.com/en/model-studio/qwen-structured-output

Offline tests added for schema contract, decision scoring, Qwen endpoint normalization, cached-input/cached-write billing, OpenAI response extraction and distinct-provider roster validation. The tests are included in the focused CI workflow.

CI evidence:
- The latest completed focused run before the final follow-up commits reported **304 passed, 2 failed, 65 subtests passed**. The seven new benchmark-runner unit tests and five corpus tests were included in the passing count.
- The same two pre-existing failures remain: old selector-prompt wording assertion («после запятой») and missing `AIService._with_result_limit` in the legacy text-search path. They were present before the evaluation work began. The repository-wide filtered suite remains skipped because the focused step fails first.
- Subsequent changes added cache-write billing coverage, exact model-version capture, UTC run timestamp, per-scenario/per-language summaries, Qwen strict schema mode, and normalized effort settings. Their latest CI run is pending/needs verification; do not claim those newest changes passed until a completed run confirms them.

No provider API calls have been made; no model benchmark results exist yet. No production code, prompts, search/retrieval/ranking behavior, provider routing or credentials were changed. Selector benchmarking remains blocked until authentic, permitted AliExpress candidate-pool snapshots are captured.


### Latest runner verification update (2026-11-06)

GitHub Actions completed for code head `d6eede7fe9d743a0a39bdb57e4f3653928b45860`:
- Focused offline result: **305 passed, 2 failed, 65 subtests passed**.
- All 12 new evaluation tests (5 dialogue-corpus structure checks + 7 benchmark-runner checks) are included in the passing count.
- The two failures are unchanged and predate this work: the stale literal prompt assertion «после запятой» and legacy `AIService._with_result_limit` missing-method failure.
- Workflow still skips the repository-wide filtered suite after the focused step fails. No full-suite pass is claimed.
- This run includes the provider adapter changes for Qwen strict JSON Schema/non-thinking mode, reasoning-mode manifest, billable Gemini thought-token handling, cache-write accounting, and safe non-JSON HTTP error classification.
- These are offline tests only. No provider API was called, so endpoint credentials, region access, live schema compatibility, actual model versions, costs and latency remain unverified until a deliberate benchmark run.


### Selector snapshot prerequisite verified (2026-11-06)

Inspected the existing live evaluation artifacts rather than assuming they contain candidate data:
- `tests/eval_b5_results_live.json` is a genuine 2026-10-21 live run, but each of its 35 cases stores aggregate stage counts, outcome, latency and diagnostics only. It contains no final product titles, descriptions or selector input rows.
- `tests/fixtures_stage44b4_capture_runner.py` captures `parse_query` / `parse_query_intent` raw text and parsed outputs. Its `CaptureRecord` has no candidate-pool field and its live runner is explicitly disabled pending separate owner authorization.
- `tests/fixtures_stage44b4_replay.py` replays captured parser responses; it does not replay candidate selection.
- `tests/eval_b5_runner.py` uses generated mock product rows for its offline path. These are not authentic AliExpress listings and are not valid selector benchmark evidence.
- The user's supplied 6 November live log contains counts/token metadata but not the full 70-candidate payload.

Added `tests/eval_selector_capture_schema.md` to define the exact snapshot shape, provenance, privacy rules, independent gold labels and replay invariants. It contains a clearly marked schema example only, not fake benchmark products.

Selector quality comparison remains blocked until authentic, permitted AliExpress candidate snapshots are available. Do not fabricate candidate rows or infer product-level gold labels from aggregate B5 counts.


### Stage I — consolidated operation inventory published (2026-11-06)

Added `tests/eval_gemini_operation_inventory.md` as the readable source/live-log matrix for:
- active Option C conversation operations;
- ProductAssistant and category/discovery helper calls;
- photo and legacy/unverified methods;
- prompt template character sizes versus actual measured token counts;
- exact successful request cost samples using the correct candidate + thoughts billing;
- unknown token/cost cases and telemetry gaps.

The inventory deliberately distinguishes:
- active production callsites from legacy/unverified methods;
- prompt characters from provider tokens;
- successful observed requests from timed-out requests with unknown usage;
- operation names from generic `generate` logs whose exact helper cannot be attributed from telemetry.

No application code or production telemetry was changed.


### Stages VI–VII — model-routing hypotheses and latency evidence documented (2026-11-06)

Added:
- `tests/eval_model_routing_hypotheses.md` — operation-by-operation risk assessment. Highest-priority cheap-model candidates are no-results wording, short clarify-vs-search and search-phrase planning; core conversation interpretation and semantic candidate selection remain on the quality baseline until benchmark evidence exists. Product advice, comparisons and photo vision are separately gated by factual/multimodal quality.
- `tests/eval_gemini_latency_baseline.md` — the limited live timings from the supplied 6 November session plus source-level per-attempt/outer bounds. It explicitly refuses to infer p50/p95 from a handful of observations or attribute the full 19.9s no-eligible route to Gemini alone.

No model routing, timeout, retry, or production behavior was changed.


### Stage II — expanded DAU cost sensitivity scenarios (2026-11-06)

Added `tests/eval_gemini_cost_sensitivity.md` with low/base/high daily activity assumptions and tables for 20/50/100/500 DAU, both current rates and the 2027 Gemini 3.8 price increase. The document clearly defines DAU, shows the formula, and lists excluded operations and unknown timeout charges.

This remains a core-text sensitivity model based on two observed conversation turns and one successful selector fallback. It is not a final production forecast. The full 13-scenario cost model still needs actual per-operation token distributions, activity mix, selector rate, fallback rate and timeout usage.


### Stage I follow-up — verified scenario call graph and redundancy candidates (2026-11-06)

Re-read the active handler registration and traced the relevant methods through `bot/__init__.py`, `handlers/search.py`, `handlers/ai_assistant.py`, `handlers/photo_search.py`, `handlers/discovery.py`, `services/convsearch/engine.py`, `services/search_service.py`, `services/product_assistant.py`, `services/ai_service.py`, and `services/query_intent.py`.

Important path correction:
- The current branch does not contain `services/consultant.py` or `services/consultant_answers.py`. The active Option C interpreter is `services/convsearch/engine.py` + `services/ai_service.py`; product-card advice and questions are in `services/product_assistant.py`.
- The active photo handler is `handlers.discovery.photo_message` → `handlers.photo_search.photo_message_handler` → `services.vision_search.analyze_photo_with_vision`. If that first vision result is unusable, it delegates to `handlers.discovery._legacy_photo_message`, which can make a second request through `AIService.analyze_image`. The fallback then applies feed selection once to the merged hypothesis pool. This is sequential fallback, not parallel duplicate traffic.
- The first successful photo-vision path uses `broaden=False` and does not invoke the AI feed selector; Telegram curation on that path is deterministic.

Call graph / logical operation counts are now documented in `tests/eval_gemini_operation_inventory.md`. Key confirmed patterns:
- Ordinary Option C search/refinement with eligible results: one `conversation_turn` and one `conversation_selection`; each may issue a primary plus one fallback attempt.
- A no-safe-results turn may add `no_results_reply`; it is skipped after selector timeout/error/unavailable or exhausted turn budget.
- Explicit shown-product comparison calls paired listing-evidence extraction before deterministic rendering. Ordinary product questions first use deterministic evidence and call Gemini only if that cannot answer.
- Product-card similar/cheaper/better can make three sequential operations: search phrase planning, generic SearchService feed selection, then goal-specific ProductAssistant selection. This is a real candidate-recall/cost investigation item: the generic selection may prune listings before the goal-specific selector sees them. No change made.
- ProductAssistant explicit comparison always requests paired evidence before the deterministic renderer. The incremental value of that call when structured product fields already suffice is not measured. No change made.
- Category browse can call feed selection once. Ordinary «✨ Находки» pagination makes zero Gemini calls.
- Short clarify helper is conditional and is not called when the main interpreter already returned a validated clarification.
- Legacy/unverified paths: `SearchService.find_products_from_text` → `parse_query` + `parse_query_intent`; `SearchService.find_discovery` → `rank_candidates`; `AIService.describe_image_query`; `services.conversation.resolve_request`. They are not included in normal Option C scenario totals without a live caller.

Fallback policy confirmed from `AIService._generate`:
- Only `conversation_turn` and `conversation_selection` retry the fallback model after a client timeout.
- Generic helpers, `no_results_reply` and `product_question` may fallback on model-unavailable errors; generic timeouts do not trigger a second attempt. Quota/rate-limit errors stop immediately.
- `conversation_turn`: 4s per attempt, 8.5s outer bound. `conversation_selection`: 6.5s per attempt, 15s outer bound. `no_results_reply`: 3s AIService bound / 3.2s engine wrapper. Feed selection has an 8s caller wall bound.

Benchmark harness hardening:
- It no longer initializes the Google SDK with a fake placeholder key. It sets only the availability sentinel needed by the production validator and injects the isolated provider transport.
- Each result records HTTP status and the provider-resolved model. Billing uses the resolved model ID when returned; the run manifest records the exact rate table and its hash.
- The UTC run timestamp is captured before requests, not after completion.
- The benchmark protocol now explicitly states that the current runner tests interpreter decisions only; it does not execute post-search no-results, selector, product-question, comparison, or vision behavior. Separate operation-specific corpora are required before routing those operations.
- Added a blind human-review rubric for language naturalness, clarification quality, constraint preservation, unsupported claims and marketplace-term usefulness.

CI for the current benchmark/protocol changes:
- Commit `628e0ea99e3de25d5a4eed7888682243427247e1`: **305 passed, 2 failed, 65 subtests passed**.
- The same two known baseline failures remain: stale literal selector-prompt wording assertion and legacy `AIService._with_result_limit` missing-method failure. The workflow skips the filtered repository-wide suite after the focused command fails. No full-suite pass is claimed.
- No model API calls, provider credentials, production prompt changes, search-core changes or live Telegram tests were made.


### Benchmark runner fairness and blind-review hardening (2026-11-06)

The interpreter benchmark now preserves the **actual production Gemini request mode**: prompt-only JSON instructions, with no `responseMimeType` or `responseSchema`, because `AIService.decide_conversation_turn` does not enable structured output in production. A unit test covers this exact request body.

Provider-native output/reasoning differences are explicitly recorded in the private map:
- Gemini: production prompt-only JSON; provider-default reasoning; no explicit output-token cap.
- OpenAI: strict JSON Schema; medium reasoning effort; no explicit output-token cap.
- Anthropic: JSON Schema; medium effort; 2048 max tokens required by Messages API.
- DeepSeek: JSON Object mode; provider-default reasoning; no explicit output-token cap.
- Qwen: strict JSON Schema in documented non-thinking mode; no explicit output-token cap.

This is a practical comparison of valid native provider modes, not a claim that the inference controls are identical. Any finalist needs a separate effort/latency sweep before production configuration.

Blind artifacts were tightened:
- The reviewer-facing results file contains only aliases, scenario/language, raw output, application-validated decision, gold label and quality score.
- Provider/model IDs, resolved versions, seed, tariff table, token usage, latency, HTTP/provider errors and per-request costs are written only to the separate private map/performance artifact.
- Cost calculations use the resolved provider model ID when returned. Rate data and its hash are private. The reviewer-facing file no longer contains latency, token counts or costs that could bias or partially unblind scoring.
- Added an offline test that runs the harness with mocked transports and asserts that provider identity and performance data are absent from the blind artifact.

Latest CI for branch head `eb49bdce147a557598661dee3994547bf74cedb5`:
- **307 passed, 2 failed, 65 subtests passed**.
- The only two failures remain the same pre-existing baseline issues: stale selector-prompt literal wording assertion and the legacy `AIService._with_result_limit` missing-method failure.
- The repository-wide filtered suite is skipped after the focused step fails. No full-suite pass is claimed.

No real provider requests were made. The benchmark remains opt-in and requires credentials for at least two distinct providers supplied through process environment variables. No API keys were added to the repository. The runner is not yet a completed benchmark and no alternative has been selected.

### Benchmark runner operation-dispatch and Qwen contract correction (2026-11-06)

A source review of the latest auxiliary-corpus addition found that the task list contained chat_action and no_results_reply cases, but the runner still invoked decide_conversation_turn for every task. Those auxiliary cases therefore could not be measured through their real production methods. Fixed the benchmark-only runner so it dispatches to:
- AIService.decide_conversation_turn for the main multi-turn corpus;
- AIService.decide_chat_action for short clarification/search classification;
- AIService.compose_no_results_reply for no-results wording.

The injected transport now preserves the benchmark operation label when a production helper uses the generic _generate default, so each provider receives the correct structured-output or plain-text mode. The runner reports operation-specific summaries and private per-operation latency/token/cost breakdowns. The conversation-turn primary attempt timeout is capped at the production 4-second per-model bound; chat-action and no-results retain their own bounded timeouts. No fallback/retry is used in the first capability pass.

A second official-documentation check found that the prior Qwen setting was invalid: Qwen 3.5 Flash is documented for JSON Object output in non-thinking mode, not strict JSON Schema. The runner now uses JSON Object + enable_thinking=false for Qwen 3.5/3.6 Flash, and uses strict JSON Schema for Qwen 3.7/3.8 families where documented. No max_tokens is set for Qwen structured-output requests. The blind map records the actual mode and reasoning setting per model. The benchmark tests cover Qwen 3.5, Qwen 3.8 and plain-text no-results behavior.

Files changed:
- tests/eval_consultant_benchmark_runner.py — operation-specific dispatch, attribution, timeout manifest, Qwen family-specific output configuration, per-operation summaries.
- tests/test_eval_consultant_benchmark_runner.py — fake transport covers all three operations; tests for Qwen structured-output modes and operation-level summaries.
- tests/eval_consultant_benchmark_protocol.md — corrected Qwen mode documentation, runner scope and remaining blockers.

Commits:
- 78c5c4c73d3e947c90405018858dad63dca67a3f — runner operation dispatch and Qwen output modes.
- 2ba63a6835c0dd0efb3ae17ddc146d4616f425bb — operation/Qwen regression tests.
- 37fe59681c5531b1eac3fdabd002d7b1c06d16ae — protocol correction.
- 4138ab770152382c7debdbc2cceb16df111f7787 — operation-specific cost/latency summaries.
- a9d894f38336f1c66c403481d606a884c33ac323 — summary regression tests.

CI evidence on commit 37fe59681c5531b1eac3fdabd002d7b1c06d16ae, workflow run 42178983152:
- Focused suite: 313 passed, 2 failed, 65 subtests passed.
- The two failures are the same known pre-existing baseline failures: an old selector-prompt test expects the literal phrase «после запятой» although the prompt uses equivalent updated wording, and a legacy SearchService test expects the removed _with_result_limit helper.
- The filtered repository suite was skipped because the focused step failed. This is not a green CI run or a full-suite pass.
- The new Qwen and auxiliary-operation tests are included in the passing count. The run confirms the benchmark test fake exercises the three operation paths without provider API calls.
- CI for the subsequent operation-summary commits is pending/being checked separately.

Official Qwen sources rechecked 2026-11-06:
- Structured-output support matrix: https://docs.modelstudio.console.alibabacloud.com/en/model-studio/qwen-structured-output
- Qwen 3.5 Flash model capabilities: https://docs.modelstudio.console.alibabacloud.com/en/model-studio/qwen3-5-flash
- Current pricing/region table (last updated 2026-11-05): https://www.alibabacloud.com/help/en/model-studio/model-pricing

No production bot code, model routing, prompts, retrieval, validation, ranking or .env was changed. No real provider API was called. Candidate selection remains blocked until authentic, permitted AliExpress candidate snapshots are available.
### Production Gemini operation labels (2026-11-06)

Added explicit telemetry operation names at all inspected production AIService call sites and direct helpers. This is instrumentation-only and does not alter model routing, prompt contents, timeouts, retry/fallback behavior, AliExpress retrieval, product validation, ranking or Telegram output.

Labels now distinguish the core Option C operations (`conversation_turn`, `conversation_selection`, `chat_action`, `no_results_reply`), ProductAssistant and feed operations (`product_search_plan`, `feed_candidate_selection`, `product_candidate_selection`, `product_advice`, `product_comparison_evidence`, `product_question_answer`), photo operations (`photo_vision_search`, `photo_image_analysis`, `photo_query_description`) and legacy/unverified paths (`legacy_query_parse`, `legacy_query_intent`, `discovery_ranking`).

Files changed:
- services/ai_service.py — explicit operation labels for every inspected internal _generate call.
- services/query_intent.py — label legacy structured intent request.
- services/vision_search.py — label photo-to-search-query request.
- tests/test_query_intent.py and tests/test_ai_service.py — regression checks for query-intent and chat-action labels.
- tests/eval_gemini_operation_inventory.md — operation inventory updated to match actual telemetry labels.

Commits:
- 0ea22929d8bbb75ed525c27d2c028c817fd57809 — AIService operation labels.
- 5b98bef8277799de6fec6e261883cb95c76f973b — legacy query-intent label.
- 327b7dab789dc391afe405139e22b82952b3261a — photo vision label.
- 2b946680619389372e2462ea40012cac5229ecd6 — query-intent telemetry test.
- 0dff561f6f904332cac807d5f751860b367da594 — chat-action telemetry test.

Per-attempt `gemini_attempt op=...` logs can now be grouped by operation. The existing `turn_scope` collector is not yet wired into active Telegram handlers, so request-level aggregate totals and durable per-dialogue cost accounting remain incomplete. This is the next measurement-infrastructure gap; no user IDs or prompts should be added to logs.
### Provider pricing and Ukraine access matrix (2026-11-06)

Created tests/eval_provider_pricing_2026-11-06.md with current official list prices, cache/peak-hour rules, model-specific Qwen deployment tiers and documented API availability from Ukraine.

Verified highlights:
- Gemini 3.8 Flash: $0.75/M input and $3.75/M output through 2026-12-31; both double from 2027-01-01. Output pricing includes thinking tokens.
- OpenAI GPT-6 Luna: standard $0.10/M input and $0.50/M output; cached input $0.01/M.
- Claude Haiku 5.5: $0.10/M input and $0.50/M output up to 100K prompt tokens; higher rates above 100K. Cache read/write rates are separate.
- DeepSeek V4.1 Flash: input cache miss $0.15/M off-peak or $0.30/M peak; cache hit $0.003/M or $0.006/M; output $0.60/M or $1.20/M. UTC peak hours make its actual price time-dependent.
- Qwen pricing is deployment-scope and context-tier dependent. Qwen3.7 Flash Global has a low first-tier rate ($0.028/M input, $0.110/M output up to 32K); Qwen3.5 and Qwen3.8 have different EU/Global rates. The actual workspace base URL/scope must be confirmed before cost calculation.

Ukraine access checked against official provider lists: Gemini, OpenAI and Anthropic document Ukraine support (OpenAI with certain exceptions; Anthropic excludes Crimea, Donetsk and Luhansk). DeepSeek's checked API documentation does not publish an equivalent supported-country list. Alibaba documents EU Frankfurt and Global Qwen deployment scopes, but the user's actual workspace/account access still needs verification.

These are published tariffs, not measured cost comparisons. Tokenization, reasoning, cache-hit rates, region, context tier and request timing differ. No alternative model has been called through a live API and no production route has changed.
### Gateway comparison update (2026-11-06)

Created `tests/eval_gateway_comparison_2026-11-06.md` with current official terms for LiteLLM, OpenRouter and Vercel AI Gateway. Preliminary decision: do not add a gateway during the current benchmark. Direct provider adapters remain the simplest/lowest-dependency approach for the present shortlist. LiteLLM is the strongest later self-hosted candidate if centralized multi-provider budgets and fallback justify operating another service; OpenRouter is mainly useful for model discovery but has credit/BYOK fees; Vercel AI Gateway has no inference markup but adds Vercel credit/account dependency. No universal latency overhead is assumed; it must be measured from the bot host.

Files: `tests/eval_gateway_comparison_2026-11-06.md`, `tests/eval_model_routing_hypotheses.md`.

Gateway research is advisory only; no gateway was installed, no API keys were added, and no production request was routed through a gateway.
### Gemini-only benchmark enablement (2026-11-06)

Removed the unnecessary requirement for two different provider families in the benchmark runner. It now accepts two distinct provider/model combinations and records `comparison_scope` as either `same_provider_model_comparison` or `cross_provider`. This enables the first real comparison using the existing Gemini key: Gemini 3.8 Flash versus Gemini 3.1 Flash-Lite. Duplicate identical provider/model pairs remain rejected.

Added `tests/eval_gemini_rates_2026-11-06.json` with standard paid-tier and cached-input rates for those two models. The rates are valid through 2026-12-31; the Gemini 3.8 Flash price increases on 2027-01-01 and the rate file must be refreshed then. Updated `tests/eval_provider_pricing_2026-11-06.md` and the benchmark protocol with the rate file and a local execution command.

Commits: `b5cca7a23a2c256f29063ea841a0ea23e8414adf` (runner), `0c3bb708325de985e7f26e55ba4fab6f9e5c6090` (tests), `75211a14429dfc6fbccd5790c83c95c1e33e27e7` (protocol), `77d4fc6a24a6fa847fddf68fa5392614aff15091` (Gemini rate file).

No provider API calls have been made. The benchmark still needs a real local run; no production model or route has changed.
### Anthropic benchmark schema compatibility (2026-11-06)

Official Anthropic Structured Outputs documentation explicitly lists numeric JSON Schema constraints (`minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`, `multipleOf`) as unsupported. The benchmark's shared interpreter schema contains `confidence.minimum` and `confidence.maximum`; sending it unchanged to Claude would cause a schema rejection rather than a fair model comparison.

Added `_anthropic_compatible_schema()` in the benchmark runner. It recursively removes only unsupported numeric-bound keywords from the Anthropic request schema. The production `AIService` application validator remains unchanged and still enforces the confidence range after the model response. The blind manifest labels this mode `json_schema_without_numeric_bounds`; other providers keep their documented native schema mode.

Files/commits:
- `tests/eval_consultant_benchmark_runner.py` — provider-specific Anthropic schema sanitizer and mode label (`e6a660cc858e13f30b38d7dfbf03646749a8a0fe`).
- `tests/test_eval_consultant_benchmark_runner.py` — recursive schema compatibility regression test (`33fc2547f012749127d0b9baebd4280d503cd60e`).
- `tests/eval_consultant_benchmark_protocol.md` — provider-specific schema behavior recorded (`57eedc2674ec0562eb9f7d71ffa65b3b551b640a`).

Official source checked 2026-11-06: https://platform.claude.com/docs/en/build-with-claude/structured-outputs. No provider API calls have been made; the change prevents a known invalid request configuration in the benchmark only.
### Product-advice factual validator restored (2026-11-06)

Reviewing the full repository test output exposed a real active-path defect: `ProductAssistant.analyze()` calls `AIService.analyze_product()`, but `analyze_product()` referenced `self._validated_ai_text()` which did not exist. That meant the AI product-advice path could raise `AttributeError` instead of safely validating observations and falling back to deterministic advice.

Implemented the missing static validator. It accepts concise text only when all numeric tokens are present in the actual `product_facts()` payload, rejects URLs/HTML and overlong text, and excludes product IDs from the allowed numeric evidence. The factual product block remains deterministic; this helper does not alter search, candidate selection or ranking.

Also repaired test fakes affected by the new telemetry operation argument: the query-intent circuit-breaker fake and the legacy capture runner spy now accept/forward `operation`. Updated the timeout test to exercise a non-retrying product-question operation rather than incorrectly asserting that `conversation_turn` does not fallback; conversation-turn fallback remains intentional.

Files/commits:
- `services/ai_service.py` — restored `_validated_ai_text` (`0b3869f1c24f97d989d6d6157de227d62599e0de`; URL-regex correction `0ead7a7c13e1379ec50e365183ed8af1a56f79f4`).
- `tests/test_ai_operations.py` — updated the timeout expectation to target a non-retrying operation (`41276033dc53ca9c3c06c2fbea69a37979f019d8`).
- `tests/test_intent_circuit_recovery.py` — fake accepts the operation keyword (`0c8025abe7c114a4b3bd8b6778d7dee25b1bb6d1`).
- `tests/fixtures_stage44b4_capture_runner.py` — capture spies forward the operation keyword (`c6c0cdede0217e6aea44eca6fc933471b91f342e`).
- `.github/workflows/conversational-regression.yml` — active product-advice and circuit-breaker tests added to the focused suite (`8a2c7f2baae57ada01676e21e5c2e0564d67b1ac`).

CI on commit `0ead7a7c13e1379ec50e365183ed8af1a56f79f4`: focused offline test step completed successfully. The repository-wide step is still being evaluated; its prior run exposed hundreds of old tests that invoke removed legacy AIService parser helpers. Do not restore `_fallback_parse` or `_with_result_limit` solely to satisfy those legacy tests: active Option C uses `services/query_intent.py`, `services/consultant.py` and deterministic SearchService paths. Keep the legacy-suite status explicit.
### CI status and branch state — 2026-11-06 07:27 UTC

- The focused offline regression step passed on commit `16fb34beded01387c221926276525a8cb1e48d5b`, including the new product-advice and circuit-breaker tests. The same code was rerun after the latest protocol documentation commit; focused tests again completed successfully.
- The last completed repository-wide run before the final fake-signature corrections reported 1,742 passed, 553 failed, 1 deselected. The dominant failures were legacy tests invoking removed `AIService._fallback_parse`, `_with_result_limit` and `_merge_is_safe` methods. The old full suite is not green. Operation-keyword test fakes found in the same run have since been updated; a subsequent full-suite run was still in progress when this note was written.
- Do not restore removed legacy parser helpers merely to satisfy those tests. Keep the current active-architecture focused suite as the acceptance gate for these changes, and continue documenting the legacy full-suite gap until its test fixtures are migrated separately.
- Existing PR #7 remains open against `main`, head `b0d892f3bfda316424d3f913a1cf91282c501728`. No changes were made to `main`, and no merge was performed.
- No live provider API calls were made. No `.env` was read or modified; no credentials were copied into logs or files.

## Conversational live smoke now exercises configured pagination (2026-11-06)

- Found a verification gap: `smoke_conversational_search.py` explicitly forced `SearchService.max_search_pages = 1`. It therefore checked live Gemini/AliExpress results and comparisons, but bypassed the production multi-page conversational retrieval depth.
- Removed the forced one-page override. The smoke now uses the page depth configured by the built `SearchService` (currently the default is 3) and prints that depth before running. Added an optional `--max-search-pages N` override for an intentionally shallower manual run.
- This is a live-test harness correction only. No production search, ranking, candidate validation, Gemini routing, page limit or timeout behavior was changed.
- File/commit: `smoke_conversational_search.py` — `79db47ea3920888875bcbaf9b85b56fb41ddde31`.
- Checks: the GitHub workflow compiles the manual smoke scripts; CI for this commit is pending. The live smoke has not yet been run, so real multi-page retrieval through the complete consultant remains unverified.
- Remaining TЗ work: run the live smoke with configured page depth; review real candidate funnel diagnostics across product families; manually verify multi-turn refinements/corrections/product switches; exercise provider and Gemini failure paths. PR #7 remains draft/open/unmerged; `main` is untouched.

## 2026-11-06 — Shared Gemini conversation contract and product-identity guard

Live Telegram testing exposed a conversation-continuity failure: after the user
started a laser-level search, a recommendation/budget follow-up was interpreted
as a new product identity ("бы посоветовал"), and the engine started a search
for irrelevant listings. This was not simply a missing prompt on the fallback
model: both primary and fallback already use the same `decide_conversation_turn`
path and the same prompt/context. The failure was in the deterministic
product-identity extraction used by the engine to override intent.

Changes on `work/consultant-reliability`:

- `services/ai_service.py`: introduced `CONVERSATION_TURN_SCHEMA` and now
  sends JSON response mode plus this structured schema for
  `conversation_turn`. Because this configuration is built inside the shared
  `_generate` request path, every model in the configured primary/fallback
  chain receives the same output contract. The existing semantic validation
  after generation remains mandatory; structured output guarantees shape, not
  correctness of the model's decision.
- `services/convsearch/engine.py`: expanded the general request/question
  scaffolding vocabulary used by product-identity extraction to exclude common
  Russian recommendation verb forms and pronouns, plus generic English
  recommendation framing. This prevents such conversational framing from
  becoming a fake product type while preserving extraction of a genuinely
  named new product.
- `tests/test_convsearch_engine.py`: added offline regression coverage for
  generic recommendation questions in Russian and English and for preserving
  extraction of an explicitly named new product.

Commits:
- `a27be7617e6fd1c05006674aa910cc972f0c098b` — shared structured Gemini
  conversation-turn schema.
- `7e762e6fd6cb6ba056624fab335750061f9cd3e0` — Russian recommendation
  wording excluded from product identity.
- `41f1643ee8b07810af70558fff2cc457accf3fc9` — regression tests.
- `7d24a035ade6142bb773336fcebad8317db21a00` — English recommendation
  framing normalization.

Verification status:
- Source changes are committed to the feature branch; no production/main
  branch change and no PR merge.
- Offline tests have NOT yet been run against these commits. The new tests
  therefore remain unverified until CI or a local focused pytest run completes.
- Live Telegram behavior after these changes has NOT yet been tested.
- No Gemini/AliExpress live benchmark or additional paid API run was performed.

Remaining work:
- Run the focused offline regression tests and correct any failures.
- Verify the schema is accepted by both configured Gemini models using the
  same shared call path.
- Repeat the live multi-turn Telegram scenario and broader natural-language
  refinement/product-switch scenarios.
- Continue the main consultant TЗ; this fix alone does not establish readiness.

## 2026-11-06 — Follow-up continuity guard: state-ledger root cause and CI corrections

The first regression test revealed that filtering recommendation words only in
the engine's product-type reader was insufficient. The lower-level
ConversationEngineState.observe() independently classified the same
conversation scaffolding as a new product because its own constraint_spans
pipeline used a separate stopword/qualifier vocabulary. That lower-level
NEW_SEARCH transition superseded the active product before retrieval, even
though the engine had decided the turn was a refinement.

Additional changes:
- services/convsearch/text.py: expanded shared RU/EN stopwords for generic
  recommendation, choosing and assistance language so those words do not
  survive as content terms in constraint spans.
- services/convsearch/state.py: added common budget framing inflections and
  a narrowly scoped preserve_active_product signal. When the engine has
  identified a refinement and no explicit product switch, this signal can
  override a false lower-level product-switch classification.
- services/convsearch/engine.py: passes the continuity signal only when an
  over-eager AI new_search action is being converted to REFINE, there is an
  active product and no grounded explicit product switch.
- tests/test_convsearch_engine.py: broadened natural-language
  recommendation cases and added an integration regression that simulates an
  over-eager new_search model action while a product is active.
- tests/test_ai_operations.py: added checks that conversation_turn receives
  the shared JSON schema and that primary/fallback attempts receive the same
  schema; updated an old test that incorrectly expected no config for
  conversation_turn.

CI findings and corrections:
- The first focused CI run after the schema change reported 2 failures:
  one test expected the unnormalized spelling with ё; one existing operation
  config test still expected conversation_turn to have no config.
- The next run showed the recommendation follow-up still reached clarification.
  Investigation identified the second, independent classifier in
  state.observe(); the stopword and continuity changes above address this.
- An intermediate run after the broad continuity override exposed three
  existing regressions: shown-product questions were routed to results, and
  an explicitly named new product was incorrectly kept in the old search.
  The guard was narrowed so it only overrides a false NEW_SEARCH
  classification when the model itself over-called new_search; deterministic
  COMPARE and genuinely named product switches retain priority.

Relevant commits after the previous context entry:
- 0caf6476e33858ea9045e81420912a0cdf432d08 — broader engine-level
  conversational scaffolding.
- baf0ffdefb4dabadc68cbf3cfaaa44d3ec52b08a — recommendation continuity
  integration regression.
- 531ea6b25a091487a1023d883282c8653a692cab — normalized product token
  expectation in regression.
- 54ace946d3192d9e042a2c91304f1cedbbddc4f6 — Gemini config tests aligned
  with the new shared schema.
- 2a59748cc0134ebc648ff0a436eed6ce4b9205ab — shared text stopwords for
  recommendation language.
- 640b29fd3c9351c69c6d47da623a9176b2681fae — budget framing vocabulary.
- 3dfcef93759ba98ff45e435141d4e9595d8c1e9f — state-ledger continuity
  protection signal.
- 2c5d50168868999adcc78331a1acc07249929402 — engine passes grounded
  continuity decision into the ledger.
- 89595cc540dd0d44d9bf9fda790230e6a825d6d0 — primary/fallback schema
  equality regression.
- ba725030a2cabc8fac5c010b6ad9fc1cbd2a73e6 — narrow the state continuity
  override to false product-switch classifications.
- a7045fae6afbf27af7bd80dde7bfd38d47b1295f — apply the continuity signal
  only to an over-eager AI new_search action.

Verification:
- CI for earlier commits did fail as described above; those failures were
  used to identify and narrow the change. Do not describe those runs as green.
- CI runs for the latest commits ba725030... and a7045fae... are in progress
  at the time of this update.
- Live Telegram tests after these changes have not been run.
- No production/main changes, no PR merge, no .env access and no live
  provider benchmark were performed.

Remaining:
- Wait for focused CI on the latest head and fix any new regression before
  asking for a local pull.
- Verify both model attempts receive and accept the same structured schema
  through a live or provider-backed smoke (no new paid benchmark).
- Then repeat the live Telegram multi-turn test: initial product request,
  budget recommendation follow-up, a real product switch, and a question
  about already-shown cards.
- Main consultant TЗ remains in progress; this is not a readiness sign-off.

## 2026-11-06 — Recommendation follow-up vs. comparison boundary

Further CI evidence isolated why the recommendation follow-up still returned
CLARIFY after the earlier continuity fixes: the deterministic state classifier
recognized a question word («какой») and classified the turn as COMPARE even
though no products had yet been shown. The engine's recommendation follow-up
decision was therefore blocked by a comparison safeguard intended for
questions about delivered cards.

- services/convsearch/engine.py: added a general recommendation-follow-up
  signal (Russian recommendation/advice/choosing verbs and English
  recommend/suggest/choose/pick forms) to the active-product refinement
  decision when the model over-calls new_search and no new product identity
  is present.
- services/convsearch/state.py: COMPARE is now protected from a lower-priority
  refinement override only when there are actually shown product IDs. Before
  any cards are shown, a grounded recommendation/refinement decision may
  proceed to retrieval; questions about displayed cards remain COMPARE.
- Commit 8b5278fd1750bf5ae37705ea0c34d3cb24005523 — recommendation wording
  recognized as a follow-up.
- Commit 417e55f1295d4c218fa39507d6bab0ecae11ef89 — do not force COMPARE
  when no products have been shown.

Verification:
- CI run 42196116005 on commit 417e55f1295d4c218fa39507d6bab0ecae11ef89:
  manual smoke scripts compile successfully; focused offline test step passes.
- The wider repository test step is still running at the time of this note.
- Earlier CI failures are retained above as history; they are not represented
  as passing checks.
- Live Telegram behavior with the updated branch is still unverified.

Remaining:
- Await the wider repository test result.
- Once the branch is green at the focused gate, update local working copy and
  perform a controlled Telegram test of recommendation follow-up, explicit
  product switch and comparison of shown cards.
- Do not merge PR #7 or change main without explicit instruction.

## 2026-11-06 — CI result for recommendation continuity work

The focused CI gate on code commit 417e55f1295d4c218fa39507d6bab0ecae11ef89
completed successfully, including the new recommendation-follow-up continuity
regression, Gemini conversation schema tests, and primary/fallback schema
equality test. The smoke scripts also compiled successfully.

The wider repository suite then completed with:
- 1,749 passed
- 551 failed
- 1 deselected
- 591 subtests passed

The failures are concentrated in stale legacy test modules that still invoke
removed AIService._fallback_parse and AIService._with_result_limit methods.
This is the known legacy-suite incompatibility documented earlier, not a green
repository-wide run. Do not restore those removed helpers as a workaround:
the active Option C architecture uses the conversational engine and
deterministic retrieval path. The focused acceptance gate for this change is
green, while the full legacy suite remains non-green.

Current code change status:
- Focused offline gate: PASS on 417e55f.
- Smoke-script compilation: PASS on 417e55f.
- Wider legacy suite: FAIL, 551 legacy failures / 1,749 passes as above.
- Live Telegram after these changes: NOT YET TESTED.
- Latest branch tip includes documentation commit
  e97c5669f293b61de5b298cba3b4099788a69dcd.
- PR #7 remains draft/open/unmerged; main is untouched.

Next:
- Verify PR checks on the latest documentation tip.
- Then update the local working copy and conduct controlled live Telegram
  checks. Do not claim overall consultant readiness from the focused CI pass.



## 2026-11-06 — Latest branch CI audit after conversational reliability work

PR #7 head was checked directly: `2cfffff06c1b29d96f27005d58f5c1661606fed3`.
GitHub Actions run `42196374083` completed with a mixed result:

- Manual smoke-script compilation: PASS.
- Focused offline conversational regression step: PASS.
- Repository suite step: FAIL — 551 failed, 1,749 passed, 1 deselected, 591 subtests passed.
- The failures are concentrated in 42 older test modules that still call removed legacy methods such as `AIService._fallback_parse` and `AIService._with_result_limit`. The active focused acceptance step passed. Do not restore removed legacy methods merely to make those tests pass; first decide whether the obsolete tests should be migrated to the active Option C interfaces or explicitly retired with evidence.
- This is not a green repository-wide run. The filtered suite configuration currently still includes several stale modules, so its label must not be interpreted as proof that all included tests are current.
- Source audit confirms the shared `conversation_turn` structured schema is configured inside the common Gemini request path, and both primary/fallback attempts receive the same schema. Engine-level and state-ledger continuity guards are present. Budget filtering can inherit the active request currency when the user omits a currency, while remaining fail-closed for unknown product price/currency.

Live Telegram, Gemini and AliExpress behavior after the latest branch changes remains unverified. No live API calls were made during this audit. PR #7 remains draft/open/unmerged; `main` is untouched.

Next actions:
- Keep the main consultant TЗ as the acceptance target; do not spend effort restoring removed legacy parser APIs.
- Review whether the remaining active dialogue/search lifecycle has uncovered functional defects, and add narrowly scoped regressions for any confirmed defect.
- Once the branch is stable enough, pull it locally and repeat controlled Telegram tests for: a new search, budget recommendation follow-up, feature refinement, explicit product switch, comparison of actually delivered cards, and model fallback.


## 2026-11-06 — Structured output extended to candidate selection

The main TЗ requires one consistent model contract across primary and fallback Gemini models. A source audit found that `conversation_turn` already used a shared structured schema, but the second AI stage — `conversation_selection` — only requested `application/json` without a response schema. That left the product-selection output less constrained than the dialogue-decision output.

Change:
- `services/ai_service.py`: `select_conversation_candidates` now builds a per-turn response schema from the exact active mandatory feature/compatibility requirements. Each required evidence key is explicitly declared and required. The schema is passed into the shared `_generate` method, whose primary/fallback model loop applies the same schema to every attempt.
- `tests/test_ai_service.py`: added a regression asserting the generated schema includes every active mandatory evidence field; the test also verifies a candidate missing one required quote is rejected by the existing semantic validator.

Commits:
- `08c5628b14b8f0f5eb0740aee2265e7062aad8bd` — shared structured schema for candidate selection.
- `3d08c0f8001a816215a8bcc79263b110da67aa5f` — regression test for requirement-specific evidence schema.

Verification status:
- Source edits committed to `work/consultant-reliability`.
- Automated tests have NOT yet run for these two commits.
- Live Gemini/Telegram behavior has NOT yet been tested.
- The previous focused CI pass applies to earlier code, not these new commits.
- The full repository suite remains non-green due to the previously documented legacy test failures; do not claim otherwise.

Remaining TЗ work:
- Obtain CI results for the new commits and correct any failures.
- Continue validating context continuity, comparison of shown cards, feature/compatibility enforcement, multi-page retrieval, and the 20-AI-selection / 30-card presentation contract.
- Run controlled live Telegram scenarios only after the latest branch passes the focused gate.
- PR #7 remains draft/open/unmerged; `main` is untouched.


CI wiring follow-up for the structured candidate-selection schema:
- `.github/workflows/conversational-regression.yml` now explicitly includes `tests/test_ai_service.py::ConversationSelectionEvidenceTests` in the focused offline gate. The broader suite excludes `tests/test_ai_service.py`, so without this explicit focused entry the new schema regression would not run in CI.
- Workflow change commit: `3fffa770f1db1286a4ff1c3feccf88e1abcfe7b2`.
- The workflow run triggered by the preceding code commit was still in progress at the time of this note; no pass/fail conclusion is claimed for the new schema change. A new run for the workflow update had not yet appeared in the commit-runs query.



## 2026-11-06 — CI result and current source audit for candidate-selection schema

GitHub Actions run `42198968177` (job `conversational-regressions`) completed on the PR merge ref containing the candidate-selection schema work.

Verified results:
- Focused conversational regression command: **434 passed, 1 deselected, 76 subtests passed** in 3.36 seconds. This command explicitly includes `tests/test_ai_service.py::ConversationSelectionEvidenceTests`.
- Smoke-script compilation step completed before the test steps.
- Wider repository test command: **551 failed, 1,749 passed, 1 deselected, 591 subtests passed**. The failures again cluster around legacy tests calling removed `AIService._fallback_parse` and `AIService._with_result_limit`. The focused gate is green; the wider suite is not. Do not describe the repository-wide suite as passing and do not restore removed legacy APIs merely to silence obsolete tests.
- The CI run checked the PR merge ref; the PR head at the time was `8105425b2c76eccdaf9bbf0d682b699613abda9a`. PR #7 remains draft/open/unmerged with base `main`.

Source audit performed:
- `services/convsearch/engine.py`: every turn is interpreted against recent user/assistant dialogue, active slots, pending clarification, and up to 30 shown product identities; explicit product switches are separated from refinements; comparison of shown products is routed without a new provider search; search turns build one retrieval strategy, gather a broad pool, apply hard deterministic gates, then run bounded AI selection.
- The default display ceiling remains 30; candidate collection target is up to 80 when semantic selection is available; the selector's output cap is 20. Smaller valid pools are not padded.
- Provider errors are returned as explicit provider-error outcomes while pending clarification is retained. Empty/no-eligible results may receive a short grounded follow-up reply, with a deterministic fallback if the model is unavailable.
- `services/ai_service.py`: both `conversation_turn` and `conversation_selection` now pass structured schemas through the shared primary/fallback request loop. Selection evidence schema is built from the active exact mandatory requirements; the existing semantic validator still requires each evidence quote to occur in the listing title or description.
- This source review is not a live provider check. It does not prove that Gemini accepts the dynamic evidence schema in production or that both models behave equally on real Telegram conversations.

Current verification status:
- Focused offline gate on the current PR merge ref: PASS (434 tests + 76 subtests).
- Wider repository suite: FAIL (551 legacy failures; 1,749 passed).
- Live Gemini/AliExpress/Telegram after the candidate-selection schema change: NOT YET TESTED.
- No .env access, no production/main changes, and no PR merge.

Remaining main-TЗ work:
- Perform a provider-backed smoke of the dynamic selection schema on the primary and fallback models, confirming the same schema is accepted by both.
- Continue focused evaluation of multi-turn context, feature/compatibility enforcement, comparison of shown cards, alternative searches, and provider/model failure behavior.
- After the branch passes the focused gate, perform controlled live Telegram chains across several unrelated product categories, not a single product example.
- Keep PR #7 open and do not merge without explicit instruction.

## 2026-11-06 — Ongoing GitHub Actions CI monitoring for PR #7

### Monitoring instruction (user-authorized, ongoing)
Check GitHub Actions CI daily for repository golod19920909/shorts-chill-shop-bot, primarily open draft PR #7 and branch work/consultant-reliability. On every check:
1. Fetch the current PR head SHA first; never assume yesterday's SHA is still current.
2. Fetch workflow runs associated with that exact SHA. Distinguish runs for the current head from runs on earlier commits and do not report stale runs as current.
3. For each relevant run, report status accurately: in-progress is neither pass nor failure; completed runs must include their conclusion.
4. For failures, identify the failed job, inspect job logs, and report the concrete exception and affected checks.
5. For success, report exact SHA, successful jobs and exact test counts from logs.
6. If no new run exists for the current head, say so explicitly.
7. Deliver a concise Russian daily report, including SHA and concrete evidence.
8. Never modify source, files, CI settings, PRs, branches or main; never merge PR #7. Never access, request or expose .env or secrets. Never claim live Telegram, AliExpress or Gemini checks unless actually performed. Do not stop monitoring after one successful run.

### Verified CI snapshot — 2026-11-06
PR metadata fetched directly from GitHub:
- PR: https://github.com/golod19920909/shorts-chill-shop-bot/pull/7
- Title: Draft: Improve conversational product identity and relevance
- State: open; Draft: true; merged: false.
- Base: main; head branch: work/consultant-reliability.
- Current head SHA at this check: 6bb1a1c528c163fb2cf90183469f5d3da1c42414.

Current-head workflow query returned PR-triggered run:
- Run 42199294173, workflow Conversational regression tests, status completed, conclusion failure.
- Job 127830317853, conversational-regressions: completed / failure.
- Steps setup, checkout, Python setup, dependency install, manual smoke-script compilation and focused offline tests all succeeded.
- Failed step: Run repository suite excluding stale legacy modules.
- Exact summary from job log: 551 failed, 1,749 passed, 1 deselected, 1 warning, 591 subtests passed in 65.67s.
- Focused offline conversational regression step passed (the current log confirms step success; the earlier report recorded 434 passed and 76 subtests; verify exact counts from this run's log before repeating them in a future report).
- Concrete recurring failures include AttributeError: 'AIService' object has no attribute '_with_result_limit' at services/ai_service.py:1345, plus tests expecting removed AIService._fallback_parse and AIService._merge_is_safe. Failures appear in legacy test modules, including tests/test_stage3d_currency_detection.py, tests/test_stage35_generic_container.py, tests/test_stage3g1_relevance_fixes.py, tests/test_stage3g3_relevance_fixes.py, tests/test_stage43_short_token.py, tests/test_stage44b4_capture_safety.py, tests/test_stage44b6_negation.py, tests/test_stage44b7_negation_intent.py, tests/test_stage51_context.py, and tests/test_stage53_context_anaphora.py.
- Interpretation: the focused current conversational gate passed, but the broader configured suite failed because stale tests still call removed legacy AIService APIs. This is not a repository-wide pass. Do not reintroduce deleted helpers solely to satisfy obsolete tests without first establishing whether those tests should be migrated or excluded with documented evidence.
- A second push-triggered run 42199283693 was also reported for the same SHA in the immediately preceding audit, with failure; current fetch_commit_workflow_runs wrapper only returned the PR-triggered run above. Treat the PR run as the verified detailed run unless a future query confirms the push run and its details.
- No live Telegram, Gemini or AliExpress checks were performed. No source or workflow changes were made by the monitoring audit. No .env access. PR remains draft/open/unmerged; main untouched.

### Next monitoring / engineering handoff
- Re-fetch PR #7 head before every report.
- Check all available workflow runs on that exact SHA, and inspect failed job logs when applicable.
- The immediate CI issue to resolve in the project work is the stale legacy test boundary/configuration, not restoring old APIs by default.
- After a green focused gate and an appropriate broader offline gate, manual live Telegram scenarios remain a separate user-controlled step. CI does not prove provider-backed Gemini/AliExpress behavior.


## 2026-11-06 — New workstream: monetization, promotions, cross-selling and affiliate analytics

### User decision and goal
The user reviewed an external proposal for monetizing an AliExpress AI shopping consultant and agreed with the balanced direction: build on the existing Shorts Chill Shop Bot rather than bolt on a separate sales bot. The user explicitly noted that promo codes, promotional sections and an admin panel had already been implemented in earlier project work. The user asked for a GitHub audit and a single implementation TЗ; that TЗ was prepared in the conversation. This entry preserves the decisions and implementation handoff.

Core principle, in priority order:
1. User benefit, relevance, compatibility and truthful recommendations.
2. Reliability and verified marketplace data.
3. Affiliate revenue optimization, only within the first two constraints.

Do not rank an unsuitable product higher for commission. Affiliate commission may only be a tie-breaker between comparably suitable, trustworthy products, and only when real commission data is available.

### Repository audit performed
Repository: `golod19920909/shorts-chill-shop-bot`
Branch inspected: `work/consultant-reliability`
File audited: `PROJECT_CONTEXT.md` (existing large project history retained; this section is appended, not a replacement).

Relevant source files fetched and inspected:
- `config/promo.py`
- `handlers/promo_codes.py`
- `config/sales.py`
- `handlers/sales.py`
- `config/hot_discounts.py`
- `services/analytics.py`
- `database/models.py`
- `handlers/admin.py`
- `manage_sales.py` (identified in the project structure and sales implementation docs)
- Relevant tests identified: `tests/test_promotions.py`, `tests/test_promo_codes.py`, `tests/test_product_promo_card.py`, `tests/test_admin_panel.py`, `tests/test_sales_section.py`.

Important: this was a targeted source audit, not a complete runtime/live API audit. Do not imply every historical feature is currently active or that AliExpress endpoints have been tested live.

### Verified existing functionality and constraints

1. Product-level promo data:
- Existing AliExpress product processing extracts `promo_code_info` (documented by `handlers/promo_codes.py` and the existing product-card implementation).
- Product-level promo information is separate from the standalone promo landing page.
- Preserve this working pipeline and its existing tests. Do not replace it with invented codes or a new parallel promo model.
- During implementation, inspect the exact current extractor, Product model fields, card rendering, saved/favorite product serialization and tests before editing.

2. Standalone «🎟 Промокоды»:
- `config/promo.py` defines one fixed `PROMO_REFERRAL_URL`.
- `handlers/promo_codes.py` renders one screen and one Telegram URL button to that configured destination.
- The current implementation intentionally is NOT a dynamic list of promo campaigns.
- The source comments state that the app's available AliExpress API access does not document a user-facing coupon-code list through the campaign records; internal campaign IDs/names are not to be presented as coupons.
- Legacy `pcamp:<index>` callbacks are handled safely without resurrecting the removed campaign list.
- Preserve the configured URL verbatim unless a separately verified and approved change is made.

3. Sales events / «Чёрная пятница»:
- `config/sales.py` contains a reusable event registry; currently `black_friday` is the configured event.
- Event enabled state and destination URL are stored in the existing database `bot_config` key/value configuration using `sale.<event_id>.enabled` and `sale.<event_id>.url`.
- `handlers/sales.py` uses a reusable landing screen and admin-aware visibility.
- Disabled events are hidden from ordinary users and independently rejected server-side if a callback is forged. Admins can preview disabled events.
- URLs are validated as HTTP(S) with a host; missing/invalid destinations fail safely.
- Sales landing pages deliberately do not invent product feeds, historical prices or discounts. A future event should reuse this machinery rather than copy handlers.

4. «🔥 Горячие скидки»:
- `config/hot_discounts.py` defines exactly four configured affiliate destinations: Super discounts, Brand discounts, Product bundles, Coins discounts.
- Current implementation is a set of direct fixed partner destinations, not a dynamically verified product/deal feed.
- Its comments explicitly prohibit inventing additional destinations or reconstructing/modifying the provided URLs.
- Preserve these four existing destinations unless the owner explicitly approves a change and the replacement partner URLs are verified.

5. Affiliate analytics:
- `services/analytics.py` has a controlled event vocabulary and labels for the admin panel.
- Existing event names distinguish affiliate clicks (`affiliate_clicked`) from ordinary external clicks (`external_clicked`).
- The source comments explicitly document a measurement gap: current «Купить» URL buttons do not report button presses back to the bot, so the existing click event producer is dormant for that direct-URL-button flow. Historical rows and aggregation labels remain.
- Do not report product-link handoff or card display as a real click, order, sale or commission.
- A safe click-measurement solution needs a separate compatibility/security assessment before implementation. A redirect/tracking endpoint must preserve the complete original AliExpress affiliate URL and attribution parameters, validate destinations against an allowlist or trusted stored records, prevent open redirects, avoid leaking secrets, and be tested with Telegram and AliExpress Portals. Never claim attribution is guaranteed.

6. Admin panel:
- The existing admin panel is in `handlers/admin.py`; it already contains multiple operational/statistics screens, including users, searches, favorites, product tracking and other system diagnostics.
- Sales operations are also supported by `manage_sales.py` and the shared DB config.
- Extend the existing panel; do not create a second admin interface or duplicate existing controls.
- Preserve the existing `ADMIN_IDS` authorization and server-side authorization checks. Never expose API keys, tokens, signed URLs or other secrets in admin output/logs.

7. API access caution:
- Existing project history records `hotproduct.query` as unavailable due to `InsufficientPermission`; `link.generate` is not currently used.
- These historical constraints must be re-verified against the current app permissions and official API documentation before any design depends on them.
- Do not assume AliExpress Portals exposes coupon lists, per-product commission values, order conversion details or all promotional feeds through the current API key.
- The user uses AliExpress Portals directly; do not redirect the project to Admitad/ePN or other marketplaces. AliExpress remains the only marketplace in scope.

### Approved implementation direction / requirements

A. Preliminary audit before code:
- Read `AGENTS.md` and this `PROJECT_CONTEXT.md`.
- Re-fetch current PR #7 head and branch status before making changes; do not assume the branch tip from a prior audit is current.
- Inspect actual current files, tests, DB migrations/schema, Product fields and API permissions.
- Produce a concise inventory: working, partial, intentionally removed, unavailable due to API/permissions, and missing.
- Avoid duplicate implementations and do not revive deleted legacy campaign screens without new evidence and explicit approval.

B. Affiliate-aware ranking:
- Preserve relevance, correctness, compatibility, real quality evidence, price/value and user constraints as primary ranking criteria.
- Commission can be a secondary tie-breaker only for comparable candidates and only if verified commission data is available.
- If commission data is unavailable, omit commission-based ranking entirely; do not infer category rates or use promotional claims such as universal 20–50% Hot Products commissions.

C. Promo codes and deals:
- Preserve product-level `promo_code_info` extraction, validation, storage and card display.
- Investigate official, currently authorized sources for seller coupons, platform promo codes, coupon conditions, minimum order and expiry.
- Implement additional dynamic coupon retrieval only if an official permitted endpoint/source returns verifiable user-facing coupon data.
- Never convert campaign IDs/names or discount percentages into coupon codes.
- Never fabricate coupon values, expiry dates, eligibility or final prices.
- Keep the current «🎟 Промокоды» partner landing link as a safe fallback; do not break or silently replace it.
- Preserve «🔥 Горячие скидки» four configured destinations and the reusable sales-event system.
- Dynamic deal/product feeds are optional and conditional on verified API data and permissions.

D. Cross-selling:
- Add optional contextual recommendations only after the primary shopping request has been properly handled.
- The AI may decide whether an offer is relevant; deterministic services must retrieve and validate actual products.
- Suggest one or two genuinely useful complementary products, not a stream of ads.
- Check compatibility with the selected/main product using real specifications/evidence.
- Clearly distinguish accessories from the main product.
- Ask the user whether they want to see options; only search when the user agrees.
- If the user declines, continue the conversation naturally and do not repeat the offer persistently.
- No hardcoded product-specific categories or special cases; examples are illustrative only.
- Additional search failure must not damage the main consultation.

E. Partner links and measurement:
- Audit the complete `promotion_link` lifecycle from provider response through candidate/product model, Telegram card, favorites/saved products and tracking.
- Preserve all partner parameters exactly; do not silently substitute plain marketplace URLs.
- Investigate click measurement because Telegram direct URL buttons do not report presses.
- Implement tracking only after proving the redirect approach is compatible with Telegram and AliExpress affiliate attribution. Secure against open redirects and tampering.
- Separate metrics: product/card shown, affiliate link click, ordinary external click, partner-reported order, confirmed purchase, approved commission.
- A click is not a purchase; an order is not necessarily a confirmed commission.

F. Affiliate and commercial analytics:
- Extend existing `services/analytics.py` and `analytics_events` schema rather than create a parallel analytics system.
- Add useful breakdowns for affiliate clicks by category, search/consultation flow, cross-sell, promo/sales section and product where the data is legitimately available.
- Investigate official Portals reports/API for orders, confirmed purchases and commission. If API access is unavailable, consider a safe manual report import only after auditing report format and privacy.
- Do not claim sales/revenue from clicks or unconfirmed order data.
- Respect privacy: do not duplicate free-form user text in analytics; never log secrets or signed URLs.

G. Admin panel:
- Extend existing `/admin` screens with useful partner funnel, promotion status, cross-sell and verified order/commission data.
- Provide safe enable/disable controls for new features, reusing existing DB config patterns where appropriate.
- Preserve server-side admin checks and fail-safe behavior.
- Do not add settings that imply unsupported API capabilities.

H. Architecture and reliability:
- Preserve the agreed Option C hybrid architecture.
- Gemini handles natural-language understanding, dialogue, intent and deciding whether a cross-sell offer is appropriate.
- Deterministic services handle AliExpress retrieval, product validation, compatibility, ranking gates, promo extraction, affiliate URL handling, analytics and Telegram delivery.
- Do not restore the removed full Gemini ShoppingAgent loop or legacy parser APIs.
- Do not hardcode specific products/categories as logic.
- Keep AliExpress as the only marketplace.
- Avoid unnecessary Gemini/API calls; cache verified promo/deal data where appropriate.
- Additional promo/cross-sell/analytics failures must fail safely and must not block the primary consultation.
- Preserve conversation state and idempotency across repeated button presses/restarts.

I. Verification:
- Add focused regressions for every changed behavior and run the relevant existing tests.
- Run the focused conversational acceptance gate and applicable regression suites; accurately report the known wider legacy-suite failures documented above.
- Do not claim a full repository pass if stale tests still fail.
- Offline tests do not prove live provider/API behavior.
- Prepare controlled manual Telegram scenarios for the owner: normal search, promo-bearing product, no-promo product, coupon expiry/eligibility, cross-sell accept/decline, incompatible accessory rejection, partner link open, disabled/enabled sales section, admin-only controls, API/Gemini failure fallback.
- Live checks using real credentials are performed only when explicitly authorized and available; never access or expose `.env`.

### Execution order
1. Stage A — source/API audit and inventory; no code changes until findings and scope are clear.
2. Stage B — partner-link lifecycle and truthful click/funnel analytics; validate attribution safety before implementation.
3. Stage C — verified promo-code/deal data improvements, preserving existing promo landing pages and product promo cards.
4. Stage D — opt-in contextual cross-selling with deterministic compatibility checks.
5. Stage E — integrate controls and metrics into the existing admin panel.
6. Stage F — regression, security, performance and manual Telegram test preparation.

Do not expand scope or skip ahead without reporting the audit findings. Do not rewrite functioning components without evidence.

### Documentation and handoff rule
After every substantial change, update this file with exact files, behavior, commit SHA, tests and remaining risks. Preserve previous project history. The full detailed implementation TЗ was supplied to the user in the 2026-11-06 conversation under the title:
`Shorts Chill Shop Bot — развитие монетизации, промокодов, скидок, дополнительных продаж и партнёрской аналитики`.
This section is the durable engineering handoff and acceptance summary.

### Current status of this new workstream
- Audit: targeted source inspection completed on 2026-11-06.
- Code changes for this workstream: NOT STARTED.
- Live AliExpress API/Portals permission checks for additional coupons, deals, commission or order reporting: NOT YET PERFORMED.
- Dynamic cross-selling: NOT YET IMPLEMENTED.
- New partner click tracking: NOT YET IMPLEMENTED; attribution compatibility is an open prerequisite.
- Existing promo/product-card, sales-event, hot-discount and admin mechanisms must be preserved.
- Next action: re-fetch the current PR #7 head/branch and review the remaining relevant files/tests in full; then produce the Stage A audit inventory and confirm any API permission limitations before implementation.


## 2026-11-07 — Gemini usage persistence and cost analytics (PR #8)

### Scope and preserved boundaries
The user approved implementation of durable Gemini token/cost analytics. Work is isolated on branch `feat/gemini-usage-analytics`, based on the current `work/consultant-reliability` head. Draft PR: https://github.com/golod19920909/shorts-chill-shop-bot/pull/8, targeting `work/consultant-reliability`.

PR #7 remains draft/open/unmerged. Do not merge PR #7 or PR #8 without the user's explicit instruction. `main` remains untouched.

No changes are permitted to Gemini prompts, model routing/fallback policy, conversation state decisions, AliExpress retrieval, product validation/ranking, product limits, Telegram card rendering, or credentials. No live Gemini, Telegram or AliExpress calls were made.

### Audit findings
- The existing `services/gemini_telemetry.py` already extracted real `usage_metadata` fields and logged individual attempts. It recorded prompt, candidate, total, cached and thinking token counts, but had no durable history or price calculation.
- The existing `turn_scope` collector was not wired into the main Telegram search/AI handler entry points.
- The project already uses `aiosqlite` and has a single SQLite `Database` layer, plus an existing admin AI diagnostics screen. Reuse these rather than adding a second local database or a separate Telegram admin system.
- Existing analytics events are user/product activity events; Gemini request accounting uses a dedicated `gemini_usage` table to avoid mixing unrelated metrics.

### Implemented on the feature branch
1. Added `services/gemini_pricing.py` with versioned Standard paid-tier estimates:
   - `gemini-3.8-flash`: $0.75 / 1M input and $3.75 / 1M output through 2026-12-31; $1.50 / $7.50 from 2027-01-01, per Google's published rate schedule.
   - `gemini-3.1-flash-lite`: $0.25 / 1M input and $1.50 / 1M output.
   - Cached input uses the published cached-input rates.
   - Billable output is derived from `total_token_count - prompt_token_count`, which includes candidate and thinking tokens once; `thoughts_token_count` is separately displayed but is not added a second time.
   - Unknown model or incomplete usage metadata produces unknown cost, never a fabricated zero.
   - Rates are based on Google's published Standard paid-tier Gemini API pricing. They are estimates, not a direct billing export; compare against AI Studio Usage.
2. Extended `services/gemini_telemetry.py`:
   - Unique attempt IDs and per-handler request IDs.
   - Per-attempt token/cost/status/pricing-version logging.
   - Async best-effort persistence callback and pending-write flush at shutdown.
   - A `track_usage` decorator for request-level correlation.
   - No prompt, user text, Telegram ID, key, cookie or authorization data is stored.
3. Added `gemini_usage` table and indexes in `database/models.py`.
4. Added idempotent persistence and aggregate-by-model/operation queries in `database/database.py`.
5. Wired the telemetry writer after database initialization and flush before shutdown in `bot/__init__.py`.
6. Added request scopes to text search, category search, discovery feed, photo search and product AI action handlers.
7. Extended the existing admin AI screen with period filters and persisted token/cost breakdowns by model and operation.
8. Added offline tests:
   - `tests/test_gemini_pricing.py`
   - persistence tests in `tests/test_gemini_telemetry.py`
   - database idempotency/aggregation test in `tests/test_database.py`
9. Added focused workflow `.github/workflows/gemini-usage-analytics.yml` to compile application packages and run pricing, telemetry and database tests on the feature branch and PRs targeting `work/consultant-reliability`.

### Verification state at handoff
- Latest feature branch head at this handoff: `19516d3d3a330a68ae99d8ebd1a4499b2a00c77a`.
- GitHub status query returned no check runs/statuses yet. Do not claim tests passed until the new workflow has completed and its logs have been inspected.
- No local Python test execution was performed in this environment.
- PR #8 is draft/open; no merge performed.

### Remaining work
- Inspect CI results and fix any failures.
- Verify the new table is created against an existing local database and that persisted rows survive a bot restart.
- Manually verify that the VS Code terminal shows per-attempt token counts and estimated USD cost, and that the admin AI screen reflects persisted totals. This requires the user's local environment and credentials; do not run live provider calls from CI.
- Add a safe recent-attempt detail view if useful.
- Web dashboard, PostgreSQL migration and server deployment remain later stages. Telegram slash commands were subsequently added in the continuation noted below.
- Confirm all other Gemini call entry points that may run outside the four decorated handlers; per-attempt persistence still works, but those attempts may have no request_id until their handler is scoped.

### Continuation update — 2026-11-07

The implementation was continued on the same PR #8 branch after the first local-persistence increment.

Additional changes:
- Added request-grouped history query `Database.list_gemini_usage_requests()`; it aggregates attempts by request ID and exposes only models, operation names, token totals, timestamps and estimated costs.
- Added admin-only Telegram commands: `/stats_today`, `/stats_week`, `/stats_month`, `/stats_models`, `/stats_requests`. Existing user-facing `/stats` remains unchanged.
- Updated Telegram command menu registration for the new admin commands.
- Rechecked the official Google pricing schedule: Flash's published rate increase from 2027-01-01 is explicitly listed, so the estimator applies the documented $1.50 / $7.50 rates rather than guessing. Unknown model IDs still return `unknown_model`.
- Added a database test for request grouping and updated the pricing test for unverified future rates.
- The existing admin AI screen already provides persisted model/operation aggregates and period filtering; it is reused rather than creating a duplicate admin screen.

Files additionally changed in this continuation:
- `services/gemini_pricing.py`
- `database/database.py`
- `handlers/admin.py`
- `bot/__init__.py`
- `tests/test_database.py`
- `tests/test_gemini_pricing.py`

Verification:
- Focused GitHub Actions workflow was running on commit `2e1f4924dfa545d871a755bdb4322ee2722c0e4d` when checked. Newer commits followed; check the latest head's workflow run before claiming a pass.
- No local Python execution or live provider calls were performed.
- Web dashboard, PostgreSQL migration and server deployment remain unimplemented.
- Manual local verification is still required for VS Code output, persistence across restart, and the Telegram commands.

### Verification update — 2026-11-07 10:05 UTC
- Latest successful focused workflow: run #44, commit `6dc7d1829db03ce3f5868c6eb894404984932f2b`.
- Compile step passed; focused suite passed: **31 passed, 1 warning, 7 subtests passed**.
- The preceding run #24 had 30 passed and one failed pricing-boundary assertion because the test expected `0.0000045` instead of the correct `0.0045`. The test expectation was fixed; the implementation matched Google's published pricing.
- The successful run includes the grouped request-history and admin Telegram command code, because both were present before commit #44.
- PR #8 remains draft/open/unmerged. PR #7 remains draft/open/unmerged. `main` is untouched.
- The branch is ready for local integration verification; the local database migration, actual VS Code logs and Telegram commands have not yet been manually tested on the user's PC.
### Dashboard and latest CI update — 2026-11-07
- The optional read-only web dashboard is implemented in `services/gemini_dashboard.py` and wired into `_post_init` / `_post_shutdown` in `bot/__init__.py`.
- It is disabled by default. Set `ANALYTICS_DASHBOARD_PASSWORD` in `.env`; optional `ANALYTICS_DASHBOARD_USERNAME`, `ANALYTICS_DASHBOARD_HOST` (default `127.0.0.1`) and `ANALYTICS_DASHBOARD_PORT` (default `8765`) configure access. It uses HTTP Basic auth, no-store/security headers, read-only endpoints, and never exposes prompt text. Keep it loopback-bound behind HTTPS reverse proxy on a server; do not expose Basic Auth over public plain HTTP.
- Local dashboard URL with defaults: `http://127.0.0.1:8765`.
- README and `.env.example` now document dashboard settings and the admin usage commands.
- Gemini call instrumentation is at `AIService`'s remote generation boundary (`record_attempt`), so retries/fallbacks are individually recorded. Request scopes cover text search, category search, discovery feed, photo search and product AI actions.
- Latest CI run #60 on commit `47e88457e715a27e3f539f71c71a4db21812be82` passed: compile changed packages; **35 passed, 1 warning, 7 subtests passed**. Dashboard auth/API tests are included.
- PR #8 is still open and draft. No local machine or live Telegram/Gemini/marketplace calls were used. Manual local verification is the remaining step before merge.

## 2026-11-07 — Gemini 3.8 Flash conversational timeout investigation

### Live evidence and diagnosis
- The user's local live log showed conversation_turn timing out at **4,008 ms** against a **4.0 s** limit, then the fallback model succeeding in 1,533 ms.
- The same request showed conversation_selection timing out at **6,506 ms** against a **6.5 s** limit, then the fallback succeeding in 3,563 ms.
- Those durations align almost exactly with the application's own asyncio.timeout limits. This establishes that the immediate timeout trigger is the local per-attempt deadline, not a confirmed Google HTTP/API error.
- The selector prompt in that run was **27,981 characters** for 64 candidates. The successful fallback processed 8,888 prompt tokens. Prompt size is a plausible contributor to model latency, but this single sample does not prove causation.
- The existing duration surrounded the SDK call as a whole. It could not separate local prompt/request preparation from time spent awaiting the SDK's HTTP/API operation. SDK wait itself still combines network transport and remote provider processing; it must not be mislabeled as pure model inference time.

### Implemented on PR #8 branch feat/gemini-usage-analytics
- services/ai_service.py:
  - Raised the primary/fallback per-attempt budget for conversation_turn from 4.0 s to **5.5 s**.
  - Raised the whole turn-interpretation deadline from 8.5 s to **12.0 s**, leaving room for both bounded attempts.
  - Raised the per-attempt budget for conversation_selection from 6.5 s to **8.0 s**.
  - Added privacy-safe timing fields to per-attempt logs: operation, local request setup, SDK wait, response-text extraction, total duration and prompt character count. No prompt text or credentials are logged.
  - Added prompt-construction timing and bounded context/prompt character counts for turn interpretation and prompt/payload character counts for candidate selection.
- services/convsearch/engine.py:
  - Raised the complete semantic-selection deadline from 15.0 s to **17.0 s**, enough for two 8-second attempts plus small local overhead.
  - Added total semantic-selection elapsed time and outcome logging.
- tests/test_gemini_latency.py:
  - Added an actual fake-client cancellation test proving a primary client timeout advances to the configured fallback.
  - Added assertions that whole-operation deadlines cover both attempts.
  - Added a log regression proving local setup and SDK wait are reported separately without logging prompt contents.
- .github/workflows/gemini-usage-analytics.yml now runs the latency regression and the existing turn-fallback regression.

### Boundaries preserved
- Option C hybrid architecture, Gemini 3.8 Flash primary, Gemini 3.1 Flash-Lite fallback, quota handling, deterministic validation/ranking, candidate evidence fields, 20-item AI selection cap and 30-card presentation cap remain unchanged.
- No candidate evidence fields or prompt context were removed; no model, thinking level, retrieval, ranking, or product validation behavior was changed.
- No .env, credentials, local database or user-local files were accessed or modified.
- PR #8 remains draft/open/unmerged; PR #7 and main remain untouched.

### Verification status
- Changes are committed on feat/gemini-usage-analytics; latest implementation commit before this context update: 6b6a01e5ecda9a7aefbfd61bc597e35a0a961b9b.
- Focused CI and local Python tests are pending at this handoff. No live Gemini/Telegram call has yet tested the new budgets.
- Do not describe the timeout issue as fully resolved until CI passes and the user runs representative Telegram searches against this updated branch. Compare primary-model success rate, timeout/fallback rate, per-attempt SDK wait, prompt size, and end-to-end response time.

### Verification update — 2026-11-07
- Latest successful workflow run **#82** on code commit `22a0b29f69d29a25309ab687dea1016bf5a74e15`: compileall passed; focused suite **140 passed, 1 warning, 61 subtests passed**.
- The focused CI now includes `tests/test_gemini_latency.py`, `tests/test_ai_conversation_turn_fallback.py`, `tests/test_ai_conversation_turn.py`, and `tests/test_convsearch_engine.py`, alongside the Gemini analytics/pricing/persistence/dashboard tests.
- An intermediate expanded run failed because `tests/test_ai_service.py` contains stale legacy tests that call removed `_with_result_limit` / `_fallback_parse` methods. In accordance with AGENTS.md, the stale module was excluded from this focused workflow; its tests were not modified. The active conversation-turn and engine suites pass.
- This verifies offline timeout cancellation/fallback behavior, timeout-budget arithmetic, instrumentation logs, conversation-turn behavior and engine regressions. It does **not** establish live Gemini 3.8 latency or prove the issue is fully resolved in production.
- Local runtime and live Telegram verification remain pending. User should pull the updated feature branch and run representative new searches/refinements; compare primary-model success, fallback frequency, prompt_build_ms, setup_ms, sdk_wait_ms, parse_ms, selection elapsed time and end-to-end response time.
- Latest implementation commit: `22a0b29f69d29a25309ab687dea1016bf5a74e15`. PR #8 remains draft/open/unmerged; PR #7 and `main` remain untouched.


## 2026-11-07 — Gemini 3.1 primary / Gemini 3.8 reserve migration (in progress)

### User-approved direction
- Move the full Option C consultant workload to gemini-3.1-flash-lite as the primary generation.
- Keep gemini-3.8-flash configured as the reserve model; do not remove it.
- Do not merge PR #8 or touch main without explicit approval.
- Preserve Option C, dialogue context, candidate evidence, deterministic AliExpress retrieval/validation/ranking and Telegram delivery. Browse remains 30 cards; AI candidate selection remains capped at 20.

### Implementation started on feat/gemini-usage-analytics
- Primary/reserve defaults are being migrated in services/ai_service.py and config/settings.py.
- New environment overrides are GEMINI_PRIMARY_MODEL (default 3.1 Flash-Lite) and GEMINI_RESERVE_MODEL (default 3.8 Flash). Old GEMINI_MODEL, GEMINI_SMART_MODEL, GEMINI_FAST_MODEL, GEMINI_VISION_MODEL and GEMINI_FALLBACK_MODEL variables are intentionally ignored by the new loader so an existing local .env cannot silently keep 3.8 as primary. The user's actual .env is not accessed or modified.
- The generation boundary is being updated so malformed consultant structured output, empty structured responses, model-unavailable errors, server errors, transport failures and bounded consultant timeouts can advance once to the reserve model. Account-wide quota/rate-limit, authentication and invalid-request errors must not trigger a reserve call.
- Structured turn/selection schemas and semantic grounding validators remain intact. The reserve receives the same prompt and schema; there is no parallel dual-model call.
- Offline regressions are being added for malformed structured output and transport failure. No live Gemini, Telegram or AliExpress calls are part of this work.

### Handoff / verification
- Current PR #8 head before this migration: 1c4e8cd0b289ac761939d9fad2bc8d9dc60a05cf.
- The migration is not complete until code changes are committed to this feature branch, CI passes, and the user performs manual Telegram tests with one bot poller.
- Preserve local uncommitted files and user evaluation artifacts. Never overwrite eval-consultant-blind-map.json, eval-consultant-blind-results.json, or local-backup-2026-11-06/.


### Migration implementation update — 2026-11-07
- Changes are committed directly to PR #8 head branch feat/gemini-usage-analytics; no merge or main-branch update.
- Primary defaults in AIService and Settings are now 3.1 Flash-Lite for fast/smart/vision roles. Reserve defaults are 3.8 Flash.
- The new GEMINI_PRIMARY_MODEL and GEMINI_RESERVE_MODEL variables are the only model overrides. Legacy role variables are intentionally ignored so an existing local .env does not leave 3.8 as primary. .env itself was not read or modified.
- _generate now tries the reserve after model-unavailable, server (5xx), transport and bounded conversation timeouts; quota/rate-limit, auth and invalid-request errors still stop without a second call. Malformed or empty structured conversation_turn / conversation_selection responses now receive one reserve attempt with the same prompt/schema. Semantic grounding remains with existing validators.
- First CI run #95: compileall passed; focused tests initially had 1 failure because a telemetry test still expected the former 3.1 fallback. 141 tests passed; this expectation is being updated to 3.8 and settings tests are being added to the focused workflow. Re-run CI after this correction.
- No live API calls were made. Manual Telegram validation is still pending; stop duplicate pollers before running the bot.


### Structured contract hardening — 2026-11-07
- The pre-fallback shape gate for conversation_turn now checks the complete required field set, action enum, confidence range, nullable string fields, marketplace_terms and slot_updates field shapes. This catches schema-invalid model output before accepting the attempt; the same prompt is then sent once to the reserve.
- Conversation-selection shape gate checks the required selection object fields and non-empty candidate IDs. Existing quote/ID grounding validation remains downstream and unchanged.
- Offline latency fixtures were updated to emit the full required conversation-turn schema rather than abbreviated JSON.


### Documentation and selector regression update — 2026-11-07
- README model configuration table now documents GEMINI_PRIMARY_MODEL=gemini-3.1-flash-lite and GEMINI_RESERVE_MODEL=gemini-3.8-flash; legacy model-role variables are marked deprecated/ignored.
- Added an offline regression proving a structurally invalid conversation-selection response advances once to the reserve and accepts a correctly shaped response.
- The first CI attempt (#95) exposed one stale expected fallback model and was corrected from 3.1 to 3.8. Subsequent commits were made through the GitHub connector; a fresh workflow result is not yet visible through the available run lookup. Do not claim the corrected head has passed CI until verified.


### Latest migration checkpoint — 2026-11-07
- Latest code commit at this checkpoint: 32348a30cb733444dea3df32be11cf3dd2ac7cc9 on feat/gemini-usage-analytics.
- Added required-field presence checks and defensive type guards to the conversation-turn structured response gate; malformed list/object values must be rejected, not raise from set membership.
- README and .env.example now describe the new primary/reserve variables.
- Focused workflow now includes tests/test_settings.py. Tests were updated for the new 3.8 reserve and complete conversation-turn schema. Added offline tests for invalid turn JSON, invalid selection shape and transport failover.
- CI status for the latest head is not verified: the only retrieved run (#95) was on an earlier commit and failed on one stale telemetry expectation, which was corrected. The GitHub connector's commit-run lookup currently returns no run for the latest push commits. Do not claim the latest head passed.
- Current branch remains feat/gemini-usage-analytics, PR #8 open/draft/unmerged, base work/consultant-reliability. main and PR #7 remain untouched.


### Final handoff at current checkpoint — 2026-11-07
- Latest implementation commit before this context entry: a98570da32766e808609d927647e49aa3d6576c1.
- The structured turn validator now requires every schema-required top-level field and all four slot-update fields, and validates action/operation/kind types before enum membership. This avoids malformed model JSON raising TypeError inside validation.
- Latest changed files across this migration: services/ai_service.py, config/settings.py, .env.example, README.md, bot/__init__.py, tests/test_settings.py, tests/test_gemini_latency.py, tests/test_ai_conversation_turn_fallback.py, tests/test_gemini_telemetry.py, .github/workflows/gemini-usage-analytics.yml, PROJECT_CONTEXT.md.
- Verification limitation: no runner or repository checkout is available in this session. GitHub Actions run #95 on the earlier migration commit passed compileall but had 1 failed stale telemetry expectation (141 passed, 1 failed, 61 subtests); the expectation was corrected and settings/selection fallback tests were added afterward. The current head has NOT been verified by CI or local pytest yet.
- Next required verification: user fetches this branch in their existing Windows working tree without discarding local modifications, then runs the focused workflow command from .github/workflows/gemini-usage-analytics.yml (including tests/test_settings.py). If tests fail, fix them before manual Telegram testing.
- Do not edit .env. Because the loader intentionally ignores the old model-role variables, the code defaults now select 3.1 primary and 3.8 reserve even if those old values remain in the user's local .env.
- Keep PR #8 draft/open/unmerged. Do not merge, do not touch main or PR #7.

## Conversational search completion work — 2026-11-07

Work is isolated on branch `fix/conversational-search-completion-20`, based on
`feat/gemini-usage-analytics`. Do not merge either branch or retarget the
existing Gemini migration PR without the user's explicit approval.

### Changes made for the incomplete-result task

- The default product-search depth is now five documented AliExpress pages:
  `MAX_SEARCH_PAGES = 5` and `SEARCH_MAX_PAGES` defaults to 5; the existing
  environment override and hard maximum of 10 remain.
- Conversational retrieval now has bounded nested budgets:
  13 seconds per route, 32 seconds per page, and 60 seconds overall. A route
  timeout retires only that route when the page/search budgets still have room;
  already completed route results are retained and fused. The overall search
  remains finite.
- Search diagnostics now report requested/completed/failed pages and routes,
  raw and unique candidate counts, and retrieval duration without logging
  user queries or product payloads.
- If the first valid Gemini selection returns fewer than the requested count,
  the engine makes exactly one additional selection call over the still-
  unselected candidates, with the same hard requirements and the remaining
  selection time budget. Already selected IDs are excluded from that pass.
  There is no unbounded model loop and no deterministic padding of an
  under-filled AI selection with unreviewed candidates.
- The selection prompt now explicitly distinguishes soft use-case context from
  hard requirements, permits grounded cross-language synonyms, and asks Gemini
  to inspect all candidates and return all genuinely suitable items up to the
  limit. Evidence quotes, real candidate IDs, and deterministic hard gates
  remain mandatory.
- Semantic review IDs passed to Gemini are now intersected with the exact
  candidate IDs in that AI request. The live trace had 70 submitted candidates
  but 99 identity-review IDs; off-pool IDs were unnecessary prompt data and
  could confuse the model.
- Selection telemetry records input, requested, returned, validated and final
  card counts plus selection duration.

### Why the first AI pass could return only four

The selection schema has no `maxItems=4` restriction, and the application
caps accepted IDs at the requested maximum of 20. Therefore the observed four
IDs were not caused by a four-item schema limit. The model was allowed to
return a shorter semantically conservative list, and the engine previously
accepted that list as final without asking it to inspect the remaining
candidates. The live count mismatch (70 candidates versus 99 identity-review
IDs) is separately confirmed in the engine's data flow and is now corrected.
The exact relevance of the other 66 listings cannot be proven from aggregate
logs alone; the full candidate payload/raw model answer was not retained.
The new bounded continuation is intended to recover suitable candidates
without weakening hard gates or pretending every remaining item is relevant.

### AliExpress 405 interpretation

The existing client deliberately treats `resp_code=405` with
`resp_msg="The result is empty"` as a successful empty search result. Other
405 response messages remain API errors and are not retried. Therefore the
historical log fragment `resp_code=405` alone does not prove a broken route;
the exact `resp_msg` is required to distinguish a legitimate empty route from
a real API rejection. No blind retry or partner-link changes were made.

### Verification still required

- Run the focused offline tests for `tests/test_convsearch_engine.py` and
  `tests/test_search_service.py`, then the repository's conversational
  regression workflow.
- Confirm the five-page behavior and 60-second upper bound against real
  AliExpress latency without changing credentials or `.env`.
- Perform a live Telegram check for a search with more than 20 suitable
  candidates, a search with fewer than 20, and an alternatives follow-up.
- Revisit the AliExpress 405 diagnosis only with the matching safe
  `resp_msg`/error-class log; never record credentials, signatures, full user
  queries, or full product payloads.

## 2026-11-08 — Restored legacy deterministic query parser

A local regression run on `fix/conversational-search-completion-20` exposed that `services/ai_service.py` ended with a truncated `_to_search_request` implementation and omitted the class methods `_with_result_limit` and `_fallback_parse`. This broke both Gemini-unavailable fallback parsing and validation of Gemini's structured search request. The implementation was restored from the last known-good repository revision (`dec6289d55bc12f43c789020bccaa42f3859bcb2`), preserving the current branch's module-level parsing helpers and current model schema. The restored deterministic parser handles explicit prices/currencies, rating and order thresholds, unit quantity, AliExpress preference, required features, quality-only ranking preference, sort preference, and deterministic result-card count capture. The restored structured conversion validates model fields, preserves the user's exact original query, strips marketplace-irrelevant constraint phrases, and filters context/design/negation phrases out of literal required-feature filters.

**Fix commit:** `9602a7c823b9e19e0801d075d091798dc8268614` on `fix/conversational-search-completion-20`. The local user environment has not yet pulled or tested this commit. Required verification after pull: `tests/test_ai_service.py`, `tests/test_convsearch_engine.py`, and `tests/test_search_service.py`, followed by the full project regression suite if these pass. Existing user-local edits and backup folders must remain untouched; do not merge PR #9 without explicit approval.

### AIService parser restoration follow-up (2026-11-08)
- The first local validation after restoring `_to_search_request`, `_with_result_limit`, and `_fallback_parse` exposed a syntax regression: the `@staticmethod` decorator for `_with_result_limit` was accidentally placed at module scope while its method remained indented inside `AIService`.
- Corrected the decorator indentation in commit `15459d3828126562759419000883441798f46365`. The resulting branch snippet was re-fetched and verified to keep the decorator and method inside the class.
- The focused local pytest run had stopped during collection with `IndentationError`; no tests ran in that attempt. Re-run `py_compile` and the focused test set after pulling this commit. CI/live verification is still pending.

### Focused local regression verification (2026-11-08)
- After pulling commit `fcedc28`, the user confirmed `python -m py_compile services/ai_service.py` completed without errors.
- The focused suite `tests/test_ai_service.py tests/test_convsearch_engine.py tests/test_search_service.py` passed locally: **184 passed, 60 subtests passed** in 3.41 seconds.
- This confirms the restored deterministic AIService parser imports and works with the conversational engine and retrieval service in the focused test scope.
- The broader workflow regression set and live AliExpress/Telegram tests are still pending. GitHub Actions status was not returned by the available commit-status/workflow-run lookup for commit `fcedc28`.

### Broad regression triage and alignment (2026-11-08)
- User-reported expanded local run: **2,182 passed, 50 failed, 1 deselected, 746 subtests passed**. The failures clustered into outdated expectations and test fixtures, pre-Option-C merge tests, and a prompt/selector test mismatch; the run also emitted SQLite worker-thread warnings from the Gemini latency tests (`unable to open database file`), which were warnings rather than the reported test failures.
- Updated conversation selection prompt wording to explicitly fail closed when the main product type cannot be established from title/description: the unknown type must be skipped. This preserves the existing allowance for clear synonyms and does not treat missing exact wording as incompatibility.
- Updated tests to match the configured Gemini chain (`gemini-3.1-flash-lite` primary for text/vision roles; `gemini-3.8-flash` reserve). Conversation-turn fake responses now satisfy the complete `CONVERSATION_TURN_SCHEMA`; the old minimal `{"action":"refine"}` fixture is intentionally rejected by production validation.
- Updated retrieval-default tests to expect five pages, the approved setting on this task branch.
- Updated startup and callback tests for the actual registered Telegram command menu and current Gemini model configuration.
- Updated the selector-window test: deterministic identity filtering removes the clear welding-mask accessory before Gemini review, so only `welder-1` reaches the selector and `wanted=1`. This is not a model under-selection defect.
- The old `merge_search_requests` / `_merge_is_safe` API is absent from both this branch and its `feat/gemini-usage-analytics` base. The current bot setup builds `SearchService` and does not route through the old `services.conversation.resolve_request` merge path. The broad workflow therefore excludes the obsolete pre-Option-C merge test modules `test_conversation.py`, `test_stage3a_merge_feature_protection.py`, and `test_stage3c_merge_constraint_protection.py`, plus only the single Stage 3D test that directly calls the removed `_merge_is_safe` method. No legacy merge API was restored.
- Existing explicit exclusions for stale legacy test modules and `test_text_query_is_parsed_and_searched` remain documented in the workflow. The corrected focused and broad runs have **not yet been re-executed locally**; do not treat this triage as a passing test result. Live AliExpress/Telegram verification remains pending.

## PR #9 — follow-up regression correction (2026-11-08)

- The latest local broad test report exposed a remaining deterministic identity-gate leak: a listing titled «Сварочная маска автоматическая» shared the application modifier «сварочная» with the request «сварочный аппарат». Because the requested head noun «аппарат» was absent, the candidate should not have entered semantic review as a plausible machine. The generic accessory-role vocabulary now includes mask/маска, and an accessory-head mismatch is rejected even when a non-distinguishing requested modifier is shared. Requests whose own product type is a component (for example, a phone case) retain the bounded cross-language semantic-review path.
- Added direct RU/EN regression coverage for the shared-modifier / wrong-accessory-head case. The existing end-to-end turn regression verifies that only the actual welding machine reaches Gemini selection.
- Aligned one prompt test with the current explicit wording «Проверяй явно заданную совместимость отдельно»; this changes the assertion, not product behavior.
- The previously reported 2,161-pass / 2-failure local run is historical evidence only. Re-run focused and repository CI suites on the current branch head before claiming this correction is verified.
- resp_code=405 is classified as an empty successful result only when the API message explicitly says «The result is empty». Other 405 business responses remain errors. Existing evidence does not identify a single root cause for every 405; no request/signature/parameter fix is claimed. Capture the exact sanitized response code/message and route context for any other 405 before changing request construction.
- Live AliExpress catalog quality, actual 20-card selection, and Telegram delivery remain unverified until owner-run live testing.

## Gemini model-swap quality follow-up (2026-11-08)

- The intended model policy remains **Gemini 3.1 Flash-Lite primary / Gemini 3.8 Flash reserve**. The primary/reserve roles are not being globally reversed.
- Live Telegram logs exposed a gap in the selection chain: when Flash-Lite returned a valid but short selection (for example, 8 of the requested 20), the engine ran its one continuation pass using the same primary model. Because the response was valid, the error-based reserve fallback was not triggered. Thus the stronger reserve model was never consulted for an under-filled but valid result.
- The bounded continuation now explicitly asks AIService.select_conversation_candidates to use the configured reserve model. The first pass remains on Flash-Lite; only the second pass after a short or empty valid result is routed to Flash. This avoids treating a conservative empty Lite answer as a definitive no-match before the reserve model has reviewed the same remaining pool. The existing shared selection deadline, candidate-ID exclusion, hard requirements, evidence validation, and one-continuation limit remain in force.
- Corrected selection-returned diagnostics so the second-pass count is not overwritten by the first-pass count.
- Added tests for AIService model routing and engine continuation flag/diagnostics. No deterministic padding or relaxation of evidence/type/exclusion gates was introduced.
- New stacked branch: fix/gemini-quality-preserving-selection, based on PR #9 head 67f9cb8684504be0ddfbc2d1e3a0ef5f4edd98bd. Changes are not merged. Tests have not been executed in a local checkout; GitHub status must be checked after opening the stacked draft PR. Live AliExpress/Telegram verification is still required.


### Quality-preserving model-role adjustment (2026-11-08)

- The owner clarified that the model swap must not reduce the product-selection quality previously observed with Gemini 3.8 Flash.
- Therefore the quality-critical conversational product-selection stage is explicitly routed to the configured reserve model (Gemini 3.8 Flash) on its first pass as well as its bounded continuation. Gemini 3.1 Flash-Lite remains the primary fast model for conversation-turn interpretation and other lightweight text tasks.
- This deliberately prioritizes preserving selection quality over forcing Flash-Lite to perform the semantic product-selection stage. No product-type, feature-evidence, compatibility, exclusion, or ranking gate is relaxed. No result padding is introduced.
- PR #10 remains a draft and unmerged. Local and live verification have not been performed by the assistant; do not claim tests pass.

## Owner-confirmed Gemini model policy — corrected implementation (2026-11-08)

The owner's explicit requirement supersedes the earlier quality-follow-up notes above:

- **Gemini 3.1 Flash-Lite is the sole primary model for every AI task**: conversation interpretation, query parsing, product selection/ranking, product advice/questions, comparison evidence, and photo understanding.
- **Gemini 3.8 Flash is reserve-only.** It is never selected because Flash-Lite returns a valid short list, an empty valid selection, or a conservative answer. Those are model decisions, not proof of outage.
- The reserve is attempted only inside `AIService._generate` after a real primary request failure: model unavailable (404/503 or equivalent), server error, transport/network error, client timeout, or quota/rate-limit response (one reserve attempt because limits may be model-scoped). If the reserve is also limited, stop. Invalid credentials and invalid requests do not trigger a reserve because switching models cannot repair them.
- Every bounded product-selection continuation stays on Flash-Lite. A short/empty valid first answer may receive the existing single continuation on Flash-Lite; it must not route to 3.8.
- Existing product-type, identity evidence, mandatory-feature evidence, compatibility, exclusion, budget, deduplication and ranking gates remain unchanged. Do not loosen them to make the result count look larger.
- PR #10 branch `fix/gemini-quality-preserving-selection` is being corrected to this policy. Draft only; not merged. Local/live test execution is not claimed. The owner reported the focused local suite had one fixture assertion failure; do not broaden work into unrelated test rewrites.


### Evidence-backed product recommendations render as cards (2026-11-08)

When a user asks which previously found product is recommended, the product
assistant now carries the exact product IDs associated with its verified
listing quotes from Gemini's answer. A recommendation returns its one selected
ID; an evidence-based multi-product answer returns every referenced ID.
ProductAssistant maps those IDs only to the original Product objects it
was given. handlers.search.send_product_advice performs a final membership
check against the current cached/shown set, sends the factual explanation,
then renders the referenced real listing(s) through the existing
send_search_results card renderer (photo, marketplace data, price and
navigation). This is a presentation/delivery enhancement only: no new
AliExpress search, no model-created product, no weakened evidence checks,
and no change to the Flash-Lite primary / 3.8 reserve policy. The legacy
text-only AIService.answer_product_question facade remains available.


### Уточнение приветствия раздела «Умный поиск» (2026-11-08)
- В `handlers/start.py` обновлён `SEARCH_PROMPT`, который показывается при открытии раздела «Умный поиск». Текст теперь прямо представляет бота как ИИ-консультанта по товарам AliExpress и кратко объясняет его реальные задачи: поиск по обычному описанию, подбор по бюджету и пожеланиям, учёт характеристик/рейтинга/совместимости/ограничений, сравнение и ответы на вопросы о найденных товарах.
- Добавлен один более содержательный пример запроса и приглашение общаться обычным языком. HTML-разметка ограничена поддерживаемыми Telegram-тегами `<b>`.
- Одноразовое приветствие первого запуска (`WELCOME_MESSAGE`) и другие разделы не менялись. Это изменение касается только экрана раздела «Умный поиск».
- Изменение внесено в PR #10, ветка `fix/gemini-quality-preserving-selection`; локальный запуск бота и тесты после этого изменения не выполнялись.


## Project handoff update — monetization inventory and consultant model switch (2026-11-08)

This section consolidates the current monetization baseline and supersedes any older conflicting Gemini model-role notes below.

### 1. Monetization features already present in the project

The existing repository already contains several user-facing promotional mechanisms. They must be preserved and extended rather than rebuilt:

- **Product-level promo information:** the AliExpress product-processing/card pipeline extracts and displays the existing `promo_code_info`. This is separate from the standalone «🎟 Промокоды» screen.
- **Standalone «🎟 Промокоды» screen:** `config/promo.py` supplies one fixed `PROMO_REFERRAL_URL`; `handlers/promo_codes.py` renders the landing screen and its Telegram URL button. It is not a dynamic coupon catalogue. Legacy `pcamp:<index>` callbacks are handled safely; do not restore the removed campaign list or treat internal campaign IDs as coupon codes.
- **Sales events:** `config/sales.py` contains a reusable event registry (currently the configured Black Friday event). Enabled state and destination are stored in the existing `bot_config` key/value configuration (`sale.<event_id>.enabled`, `sale.<event_id>.url`). `handlers/sales.py` provides the landing screen, admin-aware visibility, server-side rejection of forged callbacks for disabled events, and safe HTTP(S) URL validation. It does not generate product feeds or claim historical discounts.
- **«🔥 Горячие скидки»:** `config/hot_discounts.py` defines four fixed partner destinations: Super discounts, Brand discounts, Product bundles and Coins discounts. This is a static partner-link section, not a live verified discount feed.
- **Existing analytics foundation:** `services/analytics.py` contains an event vocabulary distinguishing `affiliate_clicked` from `external_clicked`, with admin-facing labels and historical aggregation. However, direct Telegram «Купить» URL buttons do not report button presses to the bot, so the affiliate-click producer is dormant for that direct-button path. Card display or opening a Telegram URL button must not be counted as a confirmed click, order, sale or commission.
- **Existing admin panel:** `/admin` and its existing AI diagnostics/analytics screens are the place to extend; do not create a second admin interface. Admin-only checks and existing DB/config patterns must be retained.
- **Gemini cost analytics (separate from affiliate revenue analytics):** the Gemini usage analytics work added `services/gemini_pricing.py`, durable per-attempt `gemini_usage` persistence, request grouping, model/operation aggregation, admin Telegram commands (`/stats_today`, `/stats_week`, `/stats_month`, `/stats_models`, `/stats_requests`) and an optional read-only HTTP Basic dashboard at `http://127.0.0.1:8765` (disabled unless configured). The dashboard is loopback-bound by default and uses `ANALYTICS_DASHBOARD_PASSWORD`. Its focused CI was recorded as passing on PR #8's earlier head; verify current branch/PR CI before making claims about the latest head. These metrics measure AI usage/cost, not AliExpress orders or affiliate commission.

### 2. Monetization work not yet implemented

The monetization audit notes recorded in this file state that the targeted source audit was started/completed on 2026-11-06, but implementation of the new monetization workstream had **not** started at that handoff. The following remain future work unless separately verified in source:

- Dynamic cross-selling after the main consultation, with user consent before searching for add-on products.
- New reliable affiliate-click measurement/redirect tracking. Compatibility with Telegram and AliExpress attribution, open-redirect protection and signed-URL integrity must be assessed first.
- Verified Portals order, confirmed-purchase and commission reporting/import. Clicks must never be treated as sales.
- Dynamic coupon/deal retrieval from official AliExpress capabilities. Current credentials/permissions and response fields must be verified; no unsupported coupon API or seller-coupon availability may be assumed.
- Expanded admin partner-funnel, promo and cross-sell metrics/settings.
- Full commercial server deployment/update/backup/rollback work.
- Do not duplicate the existing promo, sales, hot-discount, analytics or admin mechanisms.

The following official/current baseline distinctions are important:
- Product-level promo data exists, but the standalone promo screen is a fixed referral destination, not a coupon database.
- Hot discounts are four configured partner destinations, not a dynamically checked discount feed.
- Black Friday is represented by reusable event configuration and a landing page, not by an automated discounted-product feed.
- Affiliate-click event names exist, but direct Telegram URL-button presses are not observable by the current handler flow.
- Partner orders and commissions are not established by click analytics.
- The new commercial workstream must continue from its agreed Stage A audit and implementation plan; do not jump directly into broad code changes.

### 3. Premium subscription — explicitly deferred

The owner clarified on 2026-11-08 that Premium subscriptions will be considered only after the bot gains meaningful popularity. For the current roadmap:
- Keep the bot useful and free for users.
- Do not implement subscription plans, paid limits, Telegram Stars, payment processing, renewal or subscription administration now.
- Do not spend current implementation effort on Free/Premium entitlement enforcement.
- Keep future extensibility in mind without building subscription infrastructure prematurely.

### 4. Consultant model switch — authoritative policy (2026-11-08)

The owner's latest instruction is to move the consultant's AI workload to **Gemini 3.1 Flash-Lite as the sole primary model**, while preserving the previous quality safeguards and keeping **Gemini 3.8 Flash as reserve-only**.

- Gemini 3.1 Flash-Lite is primary for every AI role: conversation-turn interpretation, query parsing, candidate selection/ranking, product advice/questions, comparisons/evidence review and photo understanding.
- Gemini 3.8 Flash is not a second-stage quality model and must not be invoked just because Flash-Lite returns a valid short list, an empty valid selection or a conservative answer.
- Gemini 3.8 Flash may be attempted only after a genuine primary request failure handled by `AIService._generate`: model unavailable/server error, transport or network error, client timeout, or quota/rate-limit failure. Invalid credentials and invalid requests are not recoverable by switching models and must not trigger reserve fallback. If reserve also fails/is rate-limited, stop.
- The one bounded continuation for a valid but short/empty candidate selection stays on Flash-Lite. No deterministic padding and no weakening of product identity, evidence quotes, required-feature, compatibility, exclusion, budget, deduplication or ranking gates.
- This corrected policy supersedes earlier notes in this file suggesting that the reserve model should perform first-pass candidate selection or the continuation pass. Those notes are historical and must not be treated as current instructions.
- The migration must preserve the Option C architecture, dialogue context, search/retrieval strategy, candidate pool and Telegram rendering. Do not restore the legacy ShoppingAgent loop or introduce category/keyword patches.
- The owner's quality requirement is that the model change must not silently reduce the recommendation quality previously observed with 3.8. This is an acceptance requirement, **not a verified result**: offline tests do not prove semantic parity, live catalogue quality, latency or cost. Do not claim parity until it has been evaluated honestly in owner-authorized live scenarios.

### 5. Additional latest consultant-facing improvements on PR #10

On branch `fix/gemini-quality-preserving-selection` (stacked on the conversational-search work; PR #10 remains draft/unmerged according to the latest recorded handoff):

- **Evidence-backed recommendations as product cards:** when the user asks which previously found item is recommended, `ProductAssistant` returns only IDs grounded in verified listing quotes and the original candidate set. `handlers.search.send_product_advice` checks those IDs against the currently shown/cached products, sends the explanation and renders the real existing product cards. It performs no new AliExpress search and does not let Gemini invent products.
- **Search-section welcome text:** `handlers/start.py::SEARCH_PROMPT` was rewritten to introduce the bot clearly as an AliExpress AI shopping consultant, describe its real capabilities and give a concrete natural-language example. The one-time first-start `WELCOME_MESSAGE` and other sections were not changed.
- These improvements do not constitute implementation of dynamic cross-selling or new affiliate monetization.

### 6. Verification and branch safety at this handoff

- Target branch for the latest consultant changes: `fix/gemini-quality-preserving-selection`; PR #10 is not approved for merge. Do not merge it, retarget it, or touch `main` without explicit owner approval.
- The local focused suite previously had a user-reported fixture/assertion failure after the model-policy correction. The assistant has not run the latest local checkout, the latest PR CI, or live Gemini/AliExpress/Telegram tests for the current head. Do not claim a green test suite or proven quality parity.
- The evidence-backed card and welcome-text changes were recorded as committed to PR #10; tests after those changes were not run in the recorded handoff.
- Preserve user-local edits, `.env`, local database, logs and backup folders. Never expose secrets or include them in repository documentation.
- Continue updating this file after every substantial implementation with exact files, commit SHA, verification actually performed, unresolved issues and current PR status.


---

## Project handoff update — AliExpress Portals API diagnostic preparation (2026-11-08)

### Branch and files

- A separate non-main branch was created from the current PR #10 head: `audit/aliexpress-portals-smoke-test`.
- Base commit: `8188fe3dc3de9d841cf203df45b4f46ca0835c12`.
- Added `scripts/diagnose_aliexpress_portals.py`.
- Latest diagnostic-script commit: `6081888e57429970630426ce6bcd32293f3459be`.
- PR #10 was not modified, merged, or retargeted. No changes were made to `main`.

### Diagnostic behavior

The script is a read-only live API probe and reuses the existing `config.settings.settings.aliexpress` credentials and `services.marketplaces.aliexpress_client.AliExpressClient` signer/gateway. It uses the existing Singapore endpoint `https://api-sg.aliexpress.com/sync`, disables retries for the diagnostic, does not open the database, and does not modify `.env`.

It probes:

1. `aliexpress.affiliate.featuredpromo.get` — lists featured campaign names/descriptions/product counts.
2. `aliexpress.affiliate.featuredpromo.products.get` — optional second-stage probe; requires exact campaign name plus exact start/end timestamps supplied by the operator. The official reference specifies these campaign timestamps in PST. No campaign name or dates are guessed.
3. `aliexpress.affiliate.order.list` — read-only query for the last 24 hours with status `Payment Completed`, first page only.

The script intentionally does not print API keys, app secrets, signatures, raw order rows, buyer details, order IDs, or full product records. It prints only method outcomes, response field names, campaign labels/counts, limited product metadata-presence flags, and aggregate order counts. Run from repository root with:

```powershell
python -m scripts.diagnose_aliexpress_portals
```

For the campaign-products probe, pass all three arguments:

```powershell
python -m scripts.diagnose_aliexpress_portals --promo-name "EXACT_NAME" --promo-start-time "YYYY-MM-DD HH:MM:SS" --promo-end-time "YYYY-MM-DD HH:MM:SS"
```

### Official documentation findings

- Official documentation exposes `aliexpress.affiliate.featuredpromo.get`, whose documented response contains `promo_name`, `promo_desc`, and `product_num`; it does not document campaign start/end dates in that response.
- Official documentation exposes `aliexpress.affiliate.featuredpromo.products.get`, which accepts `promotion_name`, `promotion_start_time`, and `promotion_end_time` (PST) and documents real product fields including product ID/title, sale/original prices, discount, image URL, promotion link, and `promo_code_info`.
- Official documentation exposes `aliexpress.affiliate.order.list`, with order status, tracking ID, product details, paid/finished amounts, and estimated paid/finished commissions.
- The JAQ documentation pages currently surfaced for these methods are grouped under a section labelled `Affiliate API（已废弃）` (deprecated Affiliate API) and show the legacy TOP gateway `eco.taobao.com/router/rest`. The project's existing app key has previously been verified against `api-sg.aliexpress.com/sync`, while the legacy gateway is documented in the code as rejecting this app key. Therefore, documentation alone is insufficient: live behavior on the project's current gateway and key is the deciding check.
- The featured-products documentation says campaign timestamps are PST. Do not pass Ukraine-local timestamps as if they were PST.

### Verification status and next action

- The diagnostic script was created and reviewed against the existing client response envelope; parsing handles the nested TOP `resp_result.result` structure.
- No live API calls have been run from this environment because the owner's local `.env` and API credentials are not accessible here.
- No local Python execution, pytest, CI run, or database operation was performed for this diagnostic branch.
- Next: owner fetches this branch locally, checks the working tree is clean before switching, runs the first diagnostic, and shares the sanitized output. If `featuredpromo.get` returns campaign names but not dates, the owner supplies the exact PST dates shown in Portals for the specific campaign before running the optional products probe.
- Do not implement campaign sync, order ingestion, or database changes until the live API results and permissions are confirmed and the audit findings are agreed.


---

## Master action plan — monetization audit and implementation (2026-11-08)

This is the authoritative recovery checklist for continuing the monetization project. If work is interrupted or context is lost, resume from the first unchecked item. Do not skip gates or start implementation ahead of the audit.

### Phase 0 — safe starting point

- [x] Preserve the monetization requirements and existing-system inventory in this file.
- [x] Keep the AliExpress diagnostic isolated on `audit/aliexpress-portals-smoke-test`, based on PR #10 HEAD.
- [x] Keep PR #10 and `main` untouched; do not merge or retarget either without explicit owner approval.
- [x] Add the read-only diagnostic script `scripts/diagnose_aliexpress_portals.py`.
- [ ] Owner checks local `git status --short --branch` before switching branches. If there are local edits/untracked files, stop and preserve them; do not stash, reset, clean, or overwrite.
- [ ] Owner fetches and checks out `origin/audit/aliexpress-portals-smoke-test`, if the local tree is clean.

### Phase 1 — verify current AliExpress Portals API access (current blocking step)

- [ ] Run `python -m scripts.diagnose_aliexpress_portals` locally using the existing `.env`.
- [ ] Record whether `aliexpress.affiliate.featuredpromo.get` succeeds on the current app key and Singapore gateway.
- [ ] Record whether `aliexpress.affiliate.order.list` succeeds and what aggregate data is returned.
- [ ] If featured campaigns are returned, identify the exact Black Friday campaign name from the API output; do not invent or normalize its identifier.
- [ ] Obtain exact campaign start/end timestamps from the owner's Portals calendar. Convert/enter them as PST values required by the documented featured-products method; do not pass Ukraine-local times as PST.
- [ ] Run the optional `featuredpromo.products.get` probe only with the exact campaign name and dates.
- [ ] Save sanitized outcomes, API error codes/subcodes and aggregate counts. Never paste API secrets, signatures, buyer details, order IDs, or raw order rows into chat or repository files.
- [ ] Distinguish verified success, unsupported method/permission, empty result and transient failure. The surfaced JAQ docs are marked deprecated, so documentation alone is not proof of current access.

### Phase 2 — complete the existing monetization audit

- [ ] Map current promo-code handling, fixed promo referral section, sales registry, hot-discount links, analytics events and admin controls.
- [ ] Verify which existing UI clicks actually emit analytics; specifically inspect direct external product buttons that bypass bot callbacks.
- [ ] Separate affiliate click analytics from confirmed orders, estimated commissions and finalized commissions.
- [ ] Inventory current database/configuration structures and migration constraints.
- [ ] Inventory deployment, backup, update and rollback procedures.
- [ ] Produce an audit report: already implemented, partially implemented, missing, API-dependent, and not currently possible.
- [ ] Present findings and obtain owner approval before adding monetization features or changing database schema.

### Phase 3 — agree implementation design

After Phase 1 and Phase 2, agree on a design that extends existing functionality rather than duplicating it:

1. Affiliate click/order/commission analytics with honest status distinctions.
2. Verified promo codes and discounts sourced from official Portals capabilities.
3. Internal "🔥 Большие распродажи" section driven by partner-side campaign information.
4. Hybrid campaign presentation: partner landing page by default; own product cards only when API confirms campaign participation; coupons only when verified.
5. Optional cross-sell recommendations only after user consent and only when relevant.
6. Extend the existing `/admin`; do not create a parallel admin panel.
7. Server deployment, backup, restore and rollback.

No fabricated discounts, campaign dates, orders, commissions, product participation or conversion claims. If an official API method is unavailable, document the limitation and use an honest manual/admin-managed fallback where agreed.

### Phase 4 — implement approved blocks incrementally

- [ ] Implement only the blocks approved after the audit.
- [ ] Reuse existing handlers, config, analytics and database patterns where suitable; do not add duplicate sections/services.
- [ ] Keep each change scoped and reviewable; record exact files and commit SHA.
- [ ] Verify the specific changed behavior and report only checks actually run.
- [ ] Update this handoff after each substantial block.

### Phase 5 — infrastructure

- [ ] Define a safe production update procedure.
- [ ] Configure automated or documented backups for database and required configuration (secrets must remain outside Git).
- [ ] Define and verify restore and rollback procedures.
- [ ] Ensure update/rollback cannot overwrite user data, local configuration or backups.

### Phase 6 — final verification and handoff

- [ ] Verify end-to-end user flows, affiliate links, analytics events and admin actions.
- [ ] Verify database backup/restore and deployment rollback.
- [ ] Update README and relevant operator instructions.
- [ ] Update this file with completed work, remaining risks, exact branch/commit and actual verification evidence.
- [ ] Provide a concise acceptance report.

### Explicitly deferred — Premium

Premium/subscription monetization is postponed until the bot has an established audience. Do not implement Telegram Stars, payments, subscriptions, paid limits, entitlement checks or free-tier restrictions now. Keep future extensibility possible without building the subscription system prematurely.

### Recovery rule

At every new session: read `AGENTS.md` and this `PROJECT_CONTEXT.md`; inspect the current branch/HEAD and working tree; locate the first unchecked item above; resume there. Never assume a local command, live API call, test, CI run or deployment succeeded unless its output is available and recorded.


---

## Project handoff update — first live Portals probe and diagnostic refinement (2026-11-08)

### Owner's first live run

The owner ran `python -m scripts.diagnose_aliexpress_portals` in the isolated audit worktree using the existing local `.env` and the existing project's Python environment.

Observed output:

- `aliexpress.affiliate.featuredpromo.get`: the gateway call completed without a reported client/API failure; the script printed `CAMPAIGN_COUNT: 0`.
- `aliexpress.affiliate.order.list`: the gateway call completed without a reported client/API failure; the script printed an empty response field list and `ORDER_COUNT_RETURNED: 0`.
- Product probe was correctly skipped because no exact campaign name and campaign dates were available.
- The diagnostic summary reported 2 successful calls, 0 failed calls.
- No database or configuration changes were reported.

**Interpretation limitation:** this first script version used `AliExpressClient.call()`, which intentionally normalizes the documented empty-result response (`resp_code=405`, “The result is empty”) to an empty dictionary. Consequently, the first output did not reveal the raw API `resp_code`, `resp_msg`, or nested response envelope. It is not yet sufficient to conclude that there are no campaigns or no orders, nor that these methods provide usable data on the current key.

### Diagnostic refinement committed

- Updated `scripts/diagnose_aliexpress_portals.py` to inspect the raw TOP response envelope for this standalone read-only diagnostic.
- Request parameters/signatures are still built by the existing `AliExpressClient.build_params()`; the current Singapore gateway and existing local credentials remain in use.
- The diagnostic now prints only HTTP status, response-envelope names, business-field names, API response code/message (message truncated), and aggregate counts. It does not print request parameters, secrets, signatures, raw orders, buyer data, order IDs or full product records.
- It distinguishes API-reported empty result (`resp_code=405`) from an empty/unrecognized payload and other non-200 business responses.
- It still does not open the database, write configuration, or use retries.
- This raw diagnostic path uses the client's internal HTTP session directly and is intentionally limited to this script; it does not alter production request behavior.
- Latest script commit: `2bae602bd923cb9fd4ef1d5bd6223d3aa516d2ff`.

### Current blocking next action

The owner must fetch the latest audit branch in the isolated worktree and rerun the diagnostic. The previous local copy is now behind the latest commits.

From `C:\\shorts-chill-shop-bot audit`:

```powershell
git pull --ff-only
git status --short --branch
& "C:\\shorts-chill-shop-bot рабочий\\.venv\\Scripts\\python.exe" -m scripts.diagnose_aliexpress_portals
```

Share only the sanitized console output. Never share `.env` contents.

Do not yet implement campaign sync, order ingestion, schema changes or monetization UI. First classify the raw responses as confirmed empty result, unexpected response shape, unsupported method/permission, or another API condition. Then continue the Phase 1 checklist above.


## Monetization audit update — GitHub-only workflow and live API results (2026-11-08)

### Workflow clarification
- Further work is performed directly against the GitHub branch `audit/aliexpress-portals-smoke-test`; do not ask the user to switch their main local worktree or run local Git commands for ordinary repository edits.
- The previously created local worktree `C:\\shorts-chill-shop-bot audit` was only an isolation mechanism. It is not required for GitHub-based work and must not be treated as the execution environment going forward.
- Do not touch PR #10, its branch, `main`, or the user's primary local working copy.

### Stage 1 — live AliExpress API probe (user executed with the current project key and gateway)
Gateway: `https://api-sg.aliexpress.com/sync`.

| Method | Observed result | What this proves |
|---|---|---|
| `aliexpress.affiliate.featuredpromo.get` | HTTP 200, `resp_code=200`, `resp_msg=Call succeeds`, 0 campaigns | The method is callable with the current key/gateway, but this request returned no campaign rows. It does not prove that the Portals calendar is unavailable or that no future campaign exists. |
| `aliexpress.affiliate.featuredpromo.products.get` | Not called | The campaign-list response supplied no exact `promo_name` and no campaign dates. Do not guess campaign name or start/end time. Run only after the exact campaign name and both PST-local dates/times are supplied from Portals. |
| `aliexpress.affiliate.order.list` | HTTP 200, `resp_code=405`, `resp_msg=The result is empty`; no order fields or total count | The API accepted the request, but the diagnostic used a GMT+8-derived last-24-hours range while order-query date semantics need to be checked against the documented PST convention. Treat this run as inconclusive: it does not prove there were no orders or that commission fields are unavailable. |

The first diagnostic reported 2 API calls received, 0 transport/API failures. It did not open the database or change configuration. Important correction: the first order query's date range was constructed in GMT+8; the order-list range is treated as PST by the API integration guidance, so its 405 response cannot be used as evidence of a genuinely empty last-24-hour window. The production client's empty-result normalization was inspected: `resp_code=405` is counted as a successful empty result, separately from real API errors.

### Stage 2 — existing monetization inventory and findings
Source inspection on this branch confirms:

1. **Product-level promo information exists.** The AliExpress product normalizer extracts a real listing's `promo_code_info` and product cards can display it. This is API-supplied per-product information, not a searchable coupon catalogue.
2. **The standalone «🎟 Промокоды» section is a static partner landing link.** `config/promo.py` defines one fixed `PROMO_REFERRAL_URL`; `handlers/promo_codes.py` renders one screen and one URL button. It does not fetch coupon codes, values, minimum spend or expiry dates.
3. **«🔥 Горячие скидки» is four fixed partner destinations.** `config/hot_discounts.py` contains Super discounts, Brand discounts, Product bundles and Coins discounts URLs. These are outbound landing pages, not a live feed of individually verified discounted products.
4. **Sales section exists as reusable landing-page infrastructure.** `config/sales.py` registers Black Friday; `handlers/sales.py` renders a landing screen using the administrator-configured URL stored in `bot_config`. The existing `/admin` «Акции» section toggles visibility. It does not synchronize a campaign calendar or show campaign-specific product cards.
5. **Partner click analytics are not currently collected reliably.** `services/analytics.py` retains event names such as `affiliate_clicked`, but its own comments state that the buy-click events have no producer because product «Купить» buttons are direct Telegram URL buttons. Telegram does not report URL-button presses to the bot. Historical event rows may remain, but current counts cannot be treated as current click totals.
6. **No order/commission ingestion is implemented.** The current `analytics_events` store records bot events and product metadata; it is not an affiliate-order ledger. The inspected admin analytics aggregate event counts and top products, not AliExpress order statuses, paid/finished amounts or commissions. The current provider does not implement `affiliate.order.list`.
7. **Existing `/admin` must be extended, not duplicated.** It already has sections for statistics, users, products, feed, discounts, sales, AI, AliExpress API, database, logs and system. It also has safe API diagnostics and a persistent sales enable/disable setting.
8. **Gemini usage analytics are separate.** Durable Gemini token/cost analytics and the optional read-only dashboard exist; they do not measure affiliate clicks, orders or commissions.
9. **Infrastructure is not yet a complete deployment/rollback system.** The repository has SQLite health/integrity diagnostics and deployment documentation, but no verified automated backup + atomic update + rollback workflow was established by this audit. Database schema changes must account for the current SQLite initialization approach; do not assume a migration framework exists.

### Current conclusions
- Verified working: existing AliExpress product search/detail methods (already live-verified by the project), featured-campaign list method returns a valid success envelope but currently no rows, fixed partner landing links, per-product promo metadata when supplied, existing admin and Gemini analytics.
- Not yet verified: featured-campaign product retrieval; order rows with real order/commission fields; current affiliate click tracking; any live campaign-product participation feed.
- Not implemented: affiliate order/commission synchronization and persistence; reliable product-link click producer; dynamic coupon catalogue; campaign calendar synchronization; verified sale-product catalogue; consent-based cross-sell workflow; production backup/update/rollback automation.
- No fake discounts, orders, commissions, campaign participation or click totals may be introduced.

### Stage gates / next action
- Stage 1 remains **partially blocked** only on the campaign-product probe and observing a non-empty order response with commission fields. The campaign-product probe requires the exact Portals campaign name and exact start/end timestamps in PST. Do not infer them from a calendar label or invent an end date. The order diagnostic has been corrected to use a bounded 30-day PST window and to query all four documented statuses (`Payment Completed`, `Buyer Confirmed Receipt`, `Completed Settlement`, `Invalid`); this corrected probe has not yet been executed with the user's key.
- Stage 2 source inventory is complete at code-inspection level; no live database contents were inspected and no user-level counts are claimed.
- Prepare Stage 3 architecture options from these findings and request user approval before implementing monetization features. Premium/subscriptions/Telegram Stars/paid limits remain explicitly deferred.

### Affiliate link-request analytics — first implementation block (2026-11-08)
- Product-card «🛒 Купить» buttons now use callback data `pbuy:<session_id>:<index>`; the callback resolves the exact cached product and refuses stale or non-affiliate links.
- On callback, the bot writes `affiliate_link_requested` with product identity and `is_affiliate=true`, then sends the exact API-provided affiliate URL in a separate Telegram URL button.
- Critical measurement boundary: Telegram does not notify the bot when the follow-up URL button is actually opened. The metric is therefore explicitly a link request, not a confirmed browser click, order or commission. Historical `affiliate_clicked` rows remain legacy data and are not mixed into the new metric.
- The new event is part of `KNOWN_EVENTS` and has an explicit Russian admin label. It is written through the existing analytics_events table and `handlers.common.record_event`; no duplicate analytics store was added.
- CI workflow `.github/workflows/conversational-regression.yml` now includes `audit/aliexpress-portals-smoke-test` as a push trigger so GitHub Actions can verify changes on this work branch.
- Follow-up needed: verify callback/card tests and GitHub Actions after the branch push. No tests are claimed as run from the GitHub file-edit operation alone.
### Affiliate analytics admin alignment (2026-11-08)
- The admin top-products aggregate now reads `affiliate_link_requested`, the new event emitted by the Buy callback. It no longer treats legacy `affiliate_clicked` rows as current click/request activity.
- Legacy `affiliate_clicked` / `external_clicked` labels are marked as historical formats to avoid presenting unproduced historical events as live measurements.
- Added offline tests for the callback keyboard shape, affiliate-only visibility, and the distinct event vocabulary/label. The tests have been committed; execution is pending an accessible GitHub Actions result.

### SQLite backup/restore utility (2026-11-08)
- Added `scripts/database_maintenance.py` with `backup`, `list`, and guarded `restore` commands. Backups use SQLite's online backup API (not a raw file copy), are verified with `PRAGMA integrity_check`, and retain the latest 14 standard backups by default.
- `DATABASE_BACKUP_DIR` may override the default `database/backups`. Restore requires `--confirm-restore`, should be run only with the bot service stopped, creates a pre-restore backup, stages the restored DB to a temporary file, and preserves existing WAL/SHM sidecars instead of deleting them.
- Added offline tests for backup consistency and restore rollback-copy behavior. Execution is not yet verified through an accessible Actions result.
- `.gitignore` now excludes local backup directories and `.sqlite3` files. README deployment guidance documents backup/restore.


### Scheduled server backups
- Added `deploy/shopbot-backup.service` and `deploy/shopbot-backup.timer`. On a systemd server, installing/enabling the timer schedules the verified SQLite backup daily at 03:15 server-local time, with `Persistent=true` and a small randomized delay.
- README now documents timer installation. The unit runs as the existing `shopbot` service account and uses `%h/shorts-chill-shop-bot`; adjust `WorkingDirectory`/`ExecStart` only if the deployment directory differs.
- No server was accessed or modified by this GitHub change; installation and a real server restore drill remain deployment-time checks.


### Order API diagnostic correction (2026-11-08)
- Review of the first live probe found that its order-list `start_time`/`end_time` were generated from GMT+8, not PST. The returned `405 The result is empty` therefore cannot be treated as evidence that the account had no orders in a correctly aligned period.
- Updated `scripts/diagnose_aliexpress_portals.py` to query a bounded 30-day PST window by default (configurable 1–90 days) and test all four AliExpress order statuses: `Payment Completed`, `Buyer Confirmed Receipt`, `Completed Settlement`, and `Invalid`. The script still prints only aggregate counts and never prints order rows, IDs, buyer data or amounts.
- Official order-list documentation confirms `order_id`, `sub_order_id`, `tracking_id`, `order_status`, `paid_amount`, `estimated_paid_commission`, `finished_amount`, `estimated_finished_commission`, `settled_currency`, and pagination fields. It labels the commission amounts as estimated; the implementation must preserve that distinction. <https://developer.alibaba.com/docs/api.htm?apiId=45802>
- Until the corrected probe returns a real record, order persistence/synchronization must remain gated; do not present the prior empty result as a confirmed zero-sales period.


### Affiliate order ledger and admin sync (2026-11-08)
- Added `affiliate_orders` SQLite table with a stable `(order_id, sub_order_id)` key and only allowlisted fields: order/status/tracking/product identifiers, commission rate, paid/finished amounts, estimated commissions, settlement currency and timestamps. Buyer/custom parameter fields and raw response bodies are not stored.
- Added idempotent upsert and status-count queries to the existing `Database` layer.
- Added `services/affiliate_orders.py`: bounded 30-day PST window, four known order statuses, page size 50, at most two pages per status per manual sync, error isolation per status, and verbatim storage of `estimated_*_commission` fields. It does not perform currency conversion or relabel estimates as actual payouts.
- Added `/admin → Монетизация` with link-request count, imported order counts by status, last sync time and a manual sync button. Callback is acknowledged before network calls to avoid Telegram callback timeouts.
- Added offline tests for normalization/privacy allowlist, nested response parsing, empty status responses, and database upsert behavior. Added the new service/database/admin files and test to the dedicated CI workflow.
- This is implementation against the documented schema; live order import is still unverified until the corrected 30-day PST probe is run against the user's current key. Do not present an empty table as proof of zero orders.


### GitHub branch/PR safety note (2026-11-08)
- Comparing `audit/aliexpress-portals-smoke-test` against `fix/gemini-quality-preserving-selection` shows the audit branch is based exactly on PR #10 HEAD (`8188fe3...`) and its diff is limited to the audit/monetization files. It is now 68 commits ahead of that base and not behind it; the current compare shows only the 26 monetization/audit files in the branch diff.
- A draft PR #11 was briefly created against `main`; comparison showed `main` is far behind this development base and the PR would have included unrelated Gemini/conversational changes. PR #11 was immediately closed without merging. Do not reopen it or target `main` while PR #10 is still separate. Keep the audit branch isolated; integrate only after the correct base is agreed.


### Admin API capability wording
- `/admin → AliExpress API` now explicitly distinguishes the callable-but-empty featured-campaign method, the unprobed campaign-products method, and the order-list implementation whose live data is still pending a corrected PST probe. No unverified capability is labelled as live-confirmed.


### Internal sales calendar date support (2026-11-08)
- Extended `SaleEvent` with optional `start_date`/`end_date`. Black Friday is seeded with the known start date `2026-11-19`; end date intentionally remains unknown.
- Added validated ISO date config keys and `manage_sales.py dates <event_id> <YYYY-MM-DD> [YYYY-MM-DD]` so an administrator can update dates in the existing `bot_config` store without creating another admin panel.
- `/admin → Акции` displays start/end dates; the public sale landing page displays configured dates while still opening only the admin-configured partner landing page. No campaign product cards or discounts are inferred.


### Monetization architecture document and current stage status (2026-11-08)
- Added root `MONETIZATION_ARCHITECTURE.md` defining measurement boundaries, order import, coupon/discount fallbacks, internal campaign calendar, opt-in cross-sell, admin scope, server operations and release gates.
- Stage 0: repository work remains isolated from PR #10 and `main`; local user worktree status was not inspected and must not be changed. The local audit worktree is no longer part of the required workflow.
- Stage 1: campaign-list call is live-success/zero rows; campaign-product method remains gated by exact campaign name and dates; order-list method has a corrected 30-day/four-status diagnostic but has not yet been live-run. The first 24-hour order response is inconclusive because of the date-zone issue.
- Stage 2: source-level inventory and report complete.
- Stage 3: architecture decision is documented in `MONETIZATION_ARCHITECTURE.md`; implementation uses the existing analytics, `bot_config`, `/admin` and SQLite layers.
- Stage 4: implemented link-request analytics, order ledger/manual sync, admin monetization section and campaign date metadata. Dynamic coupon catalogue, confirmed campaign product cards and opt-in cross-sell remain gated/not yet complete.
- Stage 5: verified-backup/restore utility and daily systemd timer files are in the repository. Production install, restore drill and safe automated code-update/rollback remain pending server access.
- Stage 6: focused tests and workflow are committed; GitHub Actions result is not exposed by the connected status API, so tests are not claimed as passing. README and this context are updated.
- Premium/Stars/subscriptions/paid limits remain deferred.


### Latest GitHub branch comparison (2026-11-08)
- Current audit branch HEAD: `f38a23abd46a9221d0b3ff70cfc055a9b322f549`.
- Compared with PR #10 branch `fix/gemini-quality-preserving-selection` (base `8188fe3...`): ahead 65, behind 0; diff contains only 26 audit/monetization files (including documentation, tests and workflow). No merge or PR to PR #10 was created.
- The connected GitHub status endpoint currently returns no status contexts for the latest commits; this is not evidence of a passing CI run. The dedicated workflow is committed and triggered by pushes to this branch, but its result still needs a visible Actions run.


### Final implementation snapshot (2026-11-08)
- Latest verified audit branch HEAD before this context update: `8841b3dcdf1126a92a842a46c7136e262db78354`.
- Current comparison against PR #10 branch: 68 commits ahead, 0 behind, 26 changed files; the diff contains no unrelated Gemini implementation files.
- The GitHub combined-status endpoint returned an empty status list for the latest branch commit. This connector cannot confirm whether Actions has started or passed; do not claim tests passed. Focused tests and `py_compile` steps are wired into `.github/workflows/monetization-audit.yml`.
- Draft PR #11 to `main` remains closed and unmerged; no code was merged into `main` or PR #10.


### Schema documentation correction (2026-11-08)
- Updated `database/models.py` inventory from ten to thirteen tables and documented the three additional runtime tables: `bot_config`, `gemini_usage`, and `affiliate_orders`. This is documentation-only; no schema or runtime behavior changed.


### Monetization audit documentation consistency pass (2026-11-08)
- Corrected stale product-keyboard and analytics comments: the Buy action is a callback that records `affiliate_link_requested`, then sends the API-provided URL in a separate Telegram URL button. Telegram cannot confirm that the URL was opened.
- Corrected the admin panel overview wording and updated the database schema inventory to thirteen tables. These changes align comments/docs with the implemented behavior; they do not change runtime logic.


### Restore rollback regression coverage (2026-11-08)
- Added a focused failure-path test for `restore_database`: simulate an integrity-check failure after the replacement file is installed, then assert the original database is restored and the failed replacement is retained for diagnosis.
- The existing monetization workflow already includes `tests/test_database_maintenance.py`, so this test is covered by the configured CI job when it runs. Execution is not claimed; the connected GitHub status endpoint still exposes no status contexts.


### Admin panel section-count regression fix (2026-11-08)
- The panel now contains 15 sections (including the added `Акции` and `Монетизация` sections) plus the main-menu escape button. A pre-existing test still expected 14 sections plus home; updated it to expect 16 total buttons.
- Updated stale 13-section comments/docstrings in `keyboards/admin.py` and `handlers/admin.py`.
- Added `tests/test_admin_panel.py` to `.github/workflows/monetization-audit.yml` so the focused workflow covers the expanded admin grid and screen rendering. CI execution remains unverified because no status contexts are exposed.


### Monetization workflow verification scope (2026-11-08)
- Confirmed the YAML now lists `tests/test_admin_panel.py` as a separate PR path and executes it in the focused pytest command alongside affiliate analytics, order sync, backup/restore and sales-calendar tests. (The first edit accidentally combined two path names into one YAML string; this was corrected before finalizing.)
- The workflow still runs automatically on pushes to `audit/aliexpress-portals-smoke-test`. No CI pass is claimed until an actual Actions run result is visible.


### Guarded Linux deployment and code rollback (2026-11-09)
- Added `scripts/deploy_update.sh` for the existing single-instance Linux/systemd layout. It requires a clean checkout on the configured deployment branch, fetches the exact remote-tracking ref, permits fast-forward only, creates/verifies a SQLite backup before changing code, installs pinned runtime + test dependencies, compiles modules, runs focused monetization/admin regressions, restarts the service, and verifies `systemctl is-active`.
- If a post-update step fails, the ERR trap resets only the server checkout to the prior commit, reinstalls that commit's runtime dependencies and attempts a verified service restart. It does not auto-restore SQLite: preserving new user data is safer; an incompatible schema rollback requires a deliberate offline restore from the pre-update backup.
- The `shopbot` account requires a narrow sudoers rule for only `systemctl restart shopbot` and `systemctl is-active --quiet shopbot`; README documents the setup. The script does not read or edit `.env`.
- `.github/workflows/monetization-audit.yml` validates shell syntax with `bash -n`. The script is not run against production here; server access is not available. CI status remains unverified.


### AliExpress Portals capability research (2026-11-09)
- Added `docs/ALIEXPRESS_PORTALS_CAPABILITY_MATRIX.md`: separates public method documentation from permissions/live evidence for this app.
- Affiliate Promotion Creatives includes featured-promotion list and campaign-product methods; the app-specific campaign-list probe returned a successful response with zero campaign rows. Campaign-product lookup remains gated until exact campaign name and start/end values are available.
- The public API catalog separately lists seller Marketing Management coupon/store-promotion methods. Those are not Portals Affiliate API evidence and are not wired into the bot; do not use them as a workaround with affiliate credentials.
- The current coupon display remains product-level `promo_code_info` only when actually returned by the affiliate product API. The standalone promo page remains the configured partner landing page.
- Corrected order diagnostic with the 30-day PST window has not yet been run with the live app credentials; order ingestion and commissions remain unverified live.


### Deployment test isolation (2026-11-09)
- Tightened `scripts/deploy_update.sh`: production `.venv` now receives only `requirements.txt`; pytest and `requirements-dev.txt` are installed in a temporary isolated venv under `${TMPDIR:-/tmp}`, removed by an EXIT trap. The temporary test environment is created only after the verified database backup and is used for focused pytest; compile and runtime install still use the production venv.
- This change has not been run on the production server. Workflow status and live deployment remain unverified.


### Affiliate link requests from favorites (2026-11-09)
- Added `fbuy:<marketplace>:<product_id>` callback to the favorites list. Buy is shown only when the saved product snapshot has a valid affiliate URL.
- Handler re-reads only the current user's persisted favorites and matches marketplace + product id; it does not trust list position, process-local cache or callback-supplied URL. It validates the affiliate URL, records `affiliate_link_requested`, then sends the exact saved URL as a Telegram URL button.
- Registered the callback in `bot/__init__.py`; added `tests/test_favorite_affiliate_link.py` for presence/absence and stable identity semantics. Added this test to monetization CI and guarded deployment focused tests.
- Telegram still does not report URL-button opens; this remains a link-request metric, not a click/purchase metric. GitHub CI result not yet verified.


### Monetization CI result (2026-11-09)
- GitHub Actions run `42579657986` for head `d0bd0549b025ec3070dc6454c50eef40f6e4fb03` completed successfully. Job `monetization-regressions`: dependency install, deployment shell syntax, monetization module compilation and focused pytest suite all passed, including the new favorites affiliate-link tests.
- This is branch CI evidence only. It does not validate the production server, live Portals credentials/order rows, backup timer installation or Telegram runtime interaction.


### Conversational regression CI observation (2026-11-09)
- The `Conversational regression tests` workflow on head `f089b75fdd001de6cb3b960d7c67ddebc48c0cce` completed with 3 failures, 438 passed, 1 deselected and 78 subtests passed. Failures: `tests/test_convsearch_turn_interpretation.py::...test_semantic_selector_empty_result_does_not_restore_irrelevant_items` (expected `semantic_no_match`, got `rejected_by_type_gate`); `tests/test_convsearch_engine.py::...test_partial_mandatory_feature_stays_hidden_if_gemini_times_out` (expected `unverified_required_features`, got `rejected_by_type_gate`); `tests/test_ai_operations.py::GeminiSelectionTimeoutFallbackTests::test_other_gemini_operations_do_not_retry_on_timeout` (mock awaited twice, expected once).
- These files/areas are outside the monetization branch's changed-file set relative to `fix/gemini-quality-preserving-selection`; no Gemini/conversational logic was changed to mask or alter these failures. Treat them as pre-existing baseline failures pending separate authorized work.
- The dedicated `Monetization audit regression` workflow on the same head completed successfully, including compile, deployment-script syntax and focused monetization tests.


## Monetization branch update — 2026-11-09

- Latest dedicated monetization workflow on commit `9d1b175460f6b534936cf01638cd568f85ba5c9a` completed successfully (run 42579896278).
- Implemented opt-in complementary-item follow-up in `handlers/cross_sell.py`: offered only after a user explicitly requests an affiliate link from a product card or favorites. Decline clears the offer; accept asks the user to describe what to find.
- The accepted text is consumed once in `handlers/search.py` and resets the previous conversational ledger before the normal search. No automatic second search, guessed accessory category, or hidden product injection.
- Added `tests/test_cross_sell.py` and included it in the dedicated monetization workflow. New workflow run is pending verification after these changes.
- Branch remains `audit/aliexpress-portals-smoke-test`; do not merge to main or PR #10. Production server deployment, backup-timer installation/restore drill, and live corrected AliExpress order diagnostic remain unverified and require server/.env access.

### User-facing AliExpress and cross-sell wording (2026-11-09)
- Updated product-link wording in `handlers/search.py` and `handlers/favorites.py`: the user-facing instruction says «Откройте товар на AliExpress:» rather than referring to an affiliate link. The affiliate URL itself and tracking behavior are unchanged.
- Updated the accepted cross-sell prompt in `handlers/cross_sell.py` to a product-agnostic instruction: «Напишите, какие дополнительные товары или аксессуары вас интересуют. Я подберу подходящие варианты.» Removed examples such as cases/cables/mounts because they were inappropriate for some product categories, and removed the redundant cancel instruction after explicit opt-in.
- Latest cross-sell wording commit: `39b7ff18350f240ddf462280ac4ca554743fa620`. Earlier user-facing wording commits: `ddcbad2d1c21a23c7a96561a5c30926d7e35d863` (`handlers/search.py`) and `b04fa0312b08a74188118a4c89415ccbe679e703` (`handlers/favorites.py`).
- No search, ranking, affiliate URL generation, attribution, or monetization logic was changed by these wording edits. No tests were run for the final text-only change.
- Local Windows checkout is `C:\\shorts-chill-shop-bot рабочий`; update it with `git pull --ff-only origin audit/aliexpress-portals-smoke-test`. Restart the bot after pulling. Do not run database maintenance/backup as part of this code-only update.

## Affiliate URL presentation in Telegram (2026-11-09)

- Keep the exact affiliate URL returned by AliExpress Portals API unchanged. Do not strip query parameters, rewrite the URL, or pass it through third-party URL shorteners: attribution parameters such as `aff_fcid` and `aff_trace_key` must remain intact unless a separately verified API-supported alternative is implemented.
- Telegram URL buttons hide the long destination URL behind their visible label. In the search and favorites link-delivery replies, the button label is now `🛒 Открыть на AliExpress`; the explanatory message remains `Откройте товар на AliExpress:`.
- The link is still the original `product.affiliate_url`; affiliate-link request analytics and the existing cross-sell offer flow are unchanged.
- Product-title links in favorites/tracking lists also remain Telegram URL buttons; their visible labels are product titles, not raw URLs.
- This is a presentation-only change. No affiliate generation, attribution, tracking, search, ranking, or database behavior was changed. No automated tests were run for this text-only label change.

## «✨ Находки» implementation — initial audit (2026-11-09)

### Scope and branch
- Active work remains isolated on `audit/aliexpress-portals-smoke-test`; do not merge into `main` or PR #10.
- Read `AGENTS.md` and this context before changes. Preserve Option C, honest real-product data, AliExpress Affiliate API only, and the existing Telegram card/navigation UX.
- This task is the approved implementation of the separate technical specification for an intelligent recommendation system in «✨ Находки». Premium subscriptions remain out of scope.

### Verified current architecture
- `handlers/discovery.py::show_feed` opens the feed, reads per-user `FLOW_FEED` history and persisted `popular_state` cursor, requests bounded AliExpress pages, merges page outcomes, advances the cursor, records the first shown card, and calls `handlers.search.send_search_results`.
- Runtime defaults are `feed_open_size=20` and `feed_max_pages_per_open=2`; each provider page can return up to 50 rows. The current handler stops fetching as soon as it has 20 technically valid unseen products, so the normal pool is often only the number needed for display, not a deeper candidate pool.
- `services/search_service.py::find_feed_page` calls `AliExpressProvider.fetch_feed_page` (keyword-less `aliexpress.affiliate.product.query`, without a sort), normalizes provider results, deduplicates canonical marketplace/product identity, requires a real image and affiliate URL, excludes the supplied per-user seen identities, and truncates to the passed limit. It does not currently run quality scoring, family selection, AI selection, or ranking.
- `handlers.discovery.show_feed` passes `pre_curated=True` to `send_search_results`, intentionally bypassing `CurationGate`. This is why the shared quality/history/mode/diversity pipeline is not applied to the feed.
- `services.feed_curation.py` already provides pure functions: title similarity, near-duplicate removal, AI-ID ranking/selection validation, diversity spreading, family selection, and recent-history penalty. These are available building blocks; do not create a parallel generic curation framework.
- `services.search_service.py` already has a bounded generic AI selector path (`select_feed_candidates`, max 20, timeout 8 seconds) for keyword-based product/discovery flows. It uses the shared strict search prompt and is not wired into `find_feed_page`; that prompt assumes a query-specific target and contains assumptions (e.g. rating thresholds) unsuitable for an unqualified broad «Находки» feed without verifying the live field distribution.
- `services.ai_service.py` already exposes `select_feed_candidates` and validates response shape to a list of IDs. `feed_curation.apply_selection` maps IDs back to the actual supplied candidate pool, so invented IDs are discarded.
- Product schema exposes real fields including `price`, `currency`, `old_price`, `discount_percent`, `rating`, `orders_count`, `positive_rate`, `promo_code`, `category_path`, image and affiliate URL. In particular, `positive_rate` is not a star rating; do not convert it to stars. Missing rating/orders are not proof of poor quality.
- Existing `CurationPipeline` recommendation profile is an impulse-buy policy ($1–$20, >=300 orders, >=4.5 stars, with a price fallback). It must NOT be blindly applied to the broad AliExpress feed: no live sample distribution has been obtained for this task, and AliExpress affiliate API may omit star ratings.
- Existing per-user feed history (`popular_shown`, `FLOW_FEED`) and cursor semantics must be preserved. Search history remains isolated. «Начать заново» clears feed history but intentionally continues from the next marketplace page.

### Evidence limitations
- Source code and project documentation have been inspected. The current conversation/runtime does not provide the user's local `.env`, production logs, or a live AliExpress response sample. No actual API candidate distribution (rating, orders, discount, category mix) has been claimed or fabricated.
- Before choosing hard commercial thresholds, inspect an anonymized live sample from the user's bot or a credential-safe diagnostic run. Until then, use structural validation and evidence actually present on each product; missing optional facts remain unknown.
- No code changes or live API calls have been made in this audit checkpoint.

### Next implementation steps
1. Inspect exact `AIService` prompt/payload and the existing feed-related tests/callers.
2. Implement a feed-specific Gemini selection policy for broad unqualified discovery, without changing the keyword-search selector semantics or model configuration. Keep model output limited to candidate IDs and validate all IDs against the exact pool.
3. Change the «Находки» refill to gather a bounded deeper pool from the existing maximum two documented pages, then curate once per open. Preserve cursor advancement, per-user exclusions, no-repeat history and the 20-card cap.
4. Add deterministic fallback using existing `services.feed_curation` helpers. A Gemini timeout/error/invalid answer must not produce invented items or an empty feed when real eligible candidates remain.
5. Record per-stage candidate counts, AI outcome/latency/tokens/cost using existing analytics/telemetry infrastructure; distinguish selected candidates from actually shown cards.
6. Run only focused offline tests for the touched behavior, then request a real user-side smoke test if live credentials/data are needed. Update this file after each meaningful implementation milestone.

### «✨ Находки» — first implementation milestone (2026-11-09)

- Added a dedicated `AIService.select_findings_candidates` policy and prompt. It evaluates a broad mixed catalogue (not a query for one target product), sees only real product IDs and allowlisted marketplace fields (title, category path, price/currency, old price, discount, rating, orders, positive-feedback percentage, promo-code presence), and returns IDs only. It explicitly does not invent hard minimum rating/orders or a fixed price ceiling; missing signals remain unknown; `positive_rate` is not converted into a star rating. Existing keyword-search selector and Gemini model configuration are unchanged.
- Added `SearchService.curate_findings`: exact identity dedup across the merged pool, near-duplicate title removal, one bounded Gemini selection (8-second ceiling), ID-to-pool validation through `feed_curation.apply_selection`, family limiting/diversity, and deterministic family/diversity fallback on unavailable Gemini, timeout, exception, empty or invalid output. It logs received/exact/near-duplicate/selected counts, AI status and elapsed time in `last_findings_stats` / application logs. Gemini token/cost telemetry continues through the existing `_generate` instrumentation under operation `findings_selection`.
- Updated `handlers/discovery.py::show_feed`: each open now gathers a deeper candidate pool (target 80, bounded by 50 rows per page and the existing configured page budget; default 2 pages) rather than stopping at the first 20 technically valid unseen products. The merged pool is curated once per open, then the existing 20-card default and Telegram card UI are retained.
- Preserved the feed's separate `FLOW_FEED` shown-history, page cursor advancement, per-page API bounds, per-user exclusion, and normal next-card display tracking. Corrected the first-card history write to record `outcome.products[0]` AFTER curation, not the first raw collected candidate.
- The shared generic `CurationGate` remains bypassed for this flow intentionally: the dedicated feed curation runs once, and the generic impulse-buy recommendation thresholds must not be applied a second time.
- Added focused tests in `tests/test_search_service.py` for real-ID validation, deterministic fallback with missing optional commercial data, and near-duplicate removal; added `FindingsSelectorTests` in `tests/test_ai_service.py`. Added that selector test class to the focused conversational regression workflow. CI has been triggered by branch commits; its result must be checked before claiming a pass.
- No live AliExpress API sample was available in this environment. Therefore no fixed rating/order/discount/price thresholds have been enabled. A live candidate-distribution review remains a required validation step before tightening quality rules.
- Latest implementation commit at this checkpoint: `411e9c40cbc9ddb5d7d7d9bead779a7815b43403`. Branch remains `audit/aliexpress-portals-smoke-test`; do not merge into `main` or PR #10.

### «✨ Находки» — analytics and fallback ranking refinement (2026-11-09)

- Applied the same deterministic family cap to AI-selected IDs, then spread similar titles so an AI preference cannot let one family occupy the whole opening set.
- Deterministic fallback now orders by actual marketplace signals only: existing `popularity_score` (orders + real star rating), then real `positive_rate`, then stated discount. Missing signals sort behind known signals but do not cause rejection; no fixed commercial thresholds were introduced.
- Added `analytics.EVENT_FEED_CURATED` to the known analytics vocabulary and record one event per completed feed curation with received/exact-duplicate/near-duplicate/selected counts, AI status and elapsed milliseconds. Gemini token/cost/latency remains recorded by the existing `gemini_usage` telemetry under `findings_selection`.
- Latest focused regression run observed on commit `01798becf48e5c44b7406529092012dfcfc1373d`: 443 passed, 3 failed, 1 deselected, 78 subtests passed. The three failures are the previously documented conversational baseline failures (type-gate reason mismatch twice and timeout fallback retry count); the new findings tests and `tests/test_search_service.py` passed in that run.
- The monetization workflow on that same commit had 43 passed and 1 unrelated failure in `tests/test_cross_sell.py`: the existing test still expects the earlier wording «Напишите, что именно подобрать», while the branch's earlier approved text change now says «Напишите, какие дополнительные товары или аксессуары вас интересуют…». This is unrelated to «Находки» and was not changed here.
- New runs have been triggered for the latest findings/analytics changes; verify their final results before claiming a clean workflow.

### «✨ Находки» — result cap and verification note (2026-11-09)

- The feed curation output is hard-capped at the existing AI feed limit of 20 cards, even if a deployment's legacy `FEED_OPEN_SIZE` setting is accidentally raised above that value. Default settings remain 20 cards and at most two API pages per open.
- Focused workflow on commit `70ff47ffe3a175a5967cf58fefd3653563173280` completed its focused offline step with 444 passed, 3 failed, 1 deselected and 78 subtests passed. All findings-specific tests and the full `tests/test_search_service.py` module passed. The three failures are the already-recorded conversational baseline failures, unrelated to the feed files.
- The latest cap-only follow-up is commit `d563ded3024888337a0581c1f4e8064774e77d6a`; its Actions runs are pending/completing. Recheck the run attached to the eventual context-update HEAD before reporting final CI status.

### «✨ Находки» — admin diagnostics (2026-11-09)

- Updated the admin «Находки» screen to show the latest API-page diagnostics alongside the curation summary: candidate count after technical validation, exact/near-duplicate counts, Gemini status, selected count and curation duration.
- Corrected the capability description: AliExpress still supplies the broad candidate listing; the bot now applies its own real-product selection after retrieval. The screen explains that Gemini returns only IDs and that deterministic fallback uses marketplace-provided commercial signals.
- Updated the startup/status wording from the retired «Популярное» ranking label to AI selection for «Находки».
- Added `tests/test_admin_panel.py::AdminScreenRenderTests::test_findings_screen_shows_curation_stage_metrics`.
- Follow-up verification is running on the branch; latest changes are limited to the admin diagnostics and their test. Keep the previously documented unrelated CI failures separate from findings regressions.

### Final verification snapshot for this work session (2026-11-09)

- Branch HEAD before this context-only update: `62738003e0587d4d0363fe085786cd90b6cd5a8a`.
- Conversational regression workflow: focused step reports **444 passed, 3 failed, 1 deselected, 78 subtests passed**. The three failures are unrelated pre-existing conversation regressions: two type-gate reason expectations and one timeout fallback retry-count expectation. The findings selector tests, findings curation tests and full `tests/test_search_service.py` passed. The admin screen test was also included in the monetization suite, where it passed.
- Monetization workflow: **44 passed, 1 failed, 45 subtests passed**. The only failure is the pre-existing cross-sell prompt wording assertion in `tests/test_cross_sell.py`; it is unrelated to findings.
- There is no claim of a clean full CI run. Findings-specific tests passed in both relevant workflow executions. No live AliExpress API request was possible from this environment because the local `.env` credentials are not accessible here.
- For local verification, pull this branch, start the bot, open «✨ Находки» once, then inspect Admin → «Находки». That screen now shows the candidate/curation metrics. A real-data review is still needed before any hard rating/orders/discount thresholds are considered.

## Findings feed cursor recovery (2026-11-09)

The «✨ Находки» feed cursor policy was corrected in
`handlers/discovery.py`:

- A provider/API exception no longer consumes the attempted page. The
  per-process and persisted cursor remain at the page where this open began,
  so the next open can retry instead of advancing into more pages after a
  failed request.
- A confirmed empty page ends the current pass and resets the next-open
  cursor to page 1. AliExpress Affiliate API's documented
  `resp_code=405` + `resp_msg="The result is empty"` is normalized by
  `AliExpressClient` as an ordinary empty result; this must not cause the
  feed cursor to grow forever (97, 98, 99, ...).
- The per-user `FLOW_FEED` shown-history is not cleared by this reset, so
  already displayed products remain excluded when the feed cycles back to
  page 1. Explicit «Начать заново» behavior, Gemini curation, product
  validation, and the normal search pipeline are unchanged.

This is a cursor-recovery change only. It does not establish that a live
AliExpress response at page 97 is always the end of the complete catalogue;
it treats the API's confirmed empty-page response as the end of the current
page sequence and safely rotates to the beginning. Real API behavior still
needs a local smoke check with the deployment's existing private credentials.

### Follow-up recovery for a stale persisted cursor (2026-11-09)

A further edge case was found during the local smoke check: after a bot restart,
`show_feed()` loads the persisted cursor before making its first API call.
Therefore the first recovery patch could only reset page 99 AFTER that opening
had already failed to return products. The handler now retries page 1 within
the same opening when all of these are true:

- the saved page is greater than 1;
- that page returned a confirmed empty result (not an API exception);
- no candidates have been collected in this opening;
- the one-time restart has not already been used.

The retry consumes the existing per-open page budget; it does not add an
unbounded retry loop. If the retry itself fails, the cursor is kept at the
actual page being retried (page 1), so the next opening does not return to the
stale high page. Gemini curation and the per-user shown history remain intact.


### Findings cursor reset when the per-open page budget is exhausted (2026-11-09)

- Local smoke logs showed that a stale saved cursor (for example page 99) could still survive the earlier recovery logic. Root cause: when the saved page returned a confirmed empty result, the handler set `restarted_from_first_page = True` and changed the loop variable to page 1, but the configured `feed_max_pages_per_open` budget could already be exhausted. The loop then exited without actually requesting page 1, and the final cursor calculation used `last_attempted + 1`, persisting page 100.
- Corrected the final cursor decision in `handlers/discovery.py`: whenever a confirmed empty stale page triggered recovery, the next persisted cursor is page 1 even if no request budget remains to retry page 1 during that same opening. The existing bounded retry still runs within the current opening when budget permits.
- This preserves the per-user shown history and does not alter Gemini selection, product curation, ordinary search, or other flows.
- Commit: `6db2de0c017459eee05ac982f893e49a69b1786a`. Offline tests and real AliExpress smoke verification have not yet been run for this follow-up.


## «✨ Находки» — разнообразная выборка каталога и безопасное заполнение (2026-11-10)

- Полное ТЗ: `docs/TECH_SPEC_FINDINGS_DIVERSITY.md`.
- Для стандартного бюджета двух запросов выборка теперь разнесённая: 1/40, 2/41, 3/42 и далее. Шаг 39 основан на ранее зафиксированной live-проверке отсутствия общих ID между страницами 1, 2 и 40.
- Курсор хранит базовую страницу и увеличивается на 1 за успешное открытие. При API-ошибке повторяется та же пара страниц. Пустая дальняя страница не считается концом каталога; stale cursor восстанавливается через page 1 в рамках оставшегося бюджета.
- Gemini prompt требует широкого охвата категорий/назначений и ровно 20 ID при наличии достаточного пула. Модель, таймаут, payload-поля и телеметрия не меняются.
- Если Gemini вернул короткий список, его реальные ID остаются в начале; остаток берётся только из того же реального пула, сортируется по marketplace-сигналам и проходит существующий family selector. Неизвестные ID не допускаются.
- Сохранены лимит 20, история `FLOW_FEED`, техническая проверка карточек, дедупликация, Telegram UI и остальные потоки.
- Добавлены офлайн-тесты разнесённых страниц, истории, заполнения короткого AI-списка и prompt. Локальные тесты и live smoke с пользовательским `.env` ещё требуется выполнить; не заявлять полную приёмку до этих проверок.


### Verification snapshot for the diversity fix (2026-11-10)

- Implementation commits: `7c955c1d768d1de94541bf5284d61f5b8ae81858`, `1544c7b02b598181092811f4f0b3a79c5f261a33`, and `630a101bfbff58f31d2848554303ab7f4b689f3d`.
- Conversational regression workflow on commit `1544c7b02b598181092811f4f0b3a79c5`: **445 passed, 3 failed, 1 deselected, 78 subtests passed**. The three failures are the existing unrelated conversation type-gate expectations (2) and timeout retry-count expectation (1). The new findings selector and short-selection fill tests passed.
- Monetization regression workflow on final code commit `630a101bfbff58f31d2848554303ab7f4b689f3d`: **44 passed, 1 failed, 45 subtests passed**. The one failure is the already documented unrelated cross-sell wording assertion in `tests/test_cross_sell.py`; the discovery acceptance tests, including spaced page sampling and history isolation, passed.
- Deployment script syntax validation and monetization-code compilation passed. No live AliExpress API smoke test was run here; credentials remain in the user's local `.env`.
- Local user verification remains required: pull the branch, open «Находки» several times, and inspect whether the 1/40, 2/41 sampling produces a wider category mix and up to 20 distinct cards. Do not claim live acceptance until that check is complete.


## Visual catalog rebuild — foundation (2026-11-12)

- Started the approved «Категории» redesign on the isolated branch
  `audit/aliexpress-portals-smoke-test`; no changes to `main`, PR #10,
  production configuration or local `.env`.
- Read current `AGENTS.md`, `PROJECT_CONTEXT.md`, catalog/keyboard/
  discovery handlers, category search service, SearchRequest, relevance
  logic and category regression tests before changing files.
- Added `config/subcategories.py` with a static, configuration-driven
  hierarchy: descriptions for all 12 existing category codes and 60
  subcategories (five per category). Each subcategory has a stable code,
  parent category, Russian UI title/description, bounded set of up to three
  English retrieval hypotheses and Russian relevance keywords.
- Added `tests/test_catalog_subcategories.py` for parent coverage, unique
  identifiers, stable lookup, bounded/unique routes and keyword presence.
- The query routes are initial hypotheses, NOT live-validated AliExpress
  queries. They must not be activated for marketplace calls until the shared
  SearchService integration, deterministic relevance behavior and bounded
  API budget are implemented and tested. Do not interpret configuration
  presence as proof of live product coverage.
- Existing category codes are preserved exactly (`electronics`, `home`,
  `auto`, `computers`, `games`, `clothing`, `beauty`, `tools`,
  `sport`, `pets`, `kids`, `garden`). No existing category handler,
  keyboard, SearchRequest, relevance, ranking or product-card behavior has
  been changed yet.
- Telegram inline buttons cannot contain images. The UI implementation must
  send cover media as Telegram messages and keep callback navigation separate;
  avoid a long image-per-message scroll. A Web App or external domain is not
  part of the first release.
- Commits created: `f277f2a` (subcategory configuration),
  `a93d721` (configuration tests). Test execution and CI status are not yet
  verified; do not report these tests as passing until an Actions/local run
  confirms them.
- Next: implement the catalog screen and category/subcategory navigation
  with stable callbacks; then connect subcategory requests to the existing
  SearchService via a bounded shared retrieval path. Preserve the category
  curation profile, history, country/currency propagation and common product
  card renderer.


## Visual catalog rebuild — navigation and retrieval foundation (2026-11-12)

- Added structured search fields to SearchRequest: subcategory_code and
  at most two retrieval_queries. Existing request payloads remain compatible
  because both fields are optional/defaulted.
- Added SearchService.find_by_subcategory. It builds a CATEGORY-mode request
  using the parent category code, the subcategory's narrower Russian relevance
  keywords, and up to three configured English query routes. Results still
  pass through the shared provider, deduplication, relevance, ranking,
  category curation and Telegram card pipeline.
- Subcategory requests do not also receive the legacy parent-category
  expansion keyword; the configured topic routes are the complete bounded
  route set for that request.
- A failed individual alternate route no longer prevents later configured
  subcategory routes from being attempted. Legacy category requests retain
  their existing failure-stop behavior. A regression covers a failed middle
  route while preserving primary results and attempting the final route.
- Main catalog category buttons now use new catview:<category> callbacks
  and open a subcategory directory. Existing cat:<category> callbacks are
  preserved as the legacy direct broad-category search path.
- Added sub:<subcategory> navigation, a two-column subcategory keyboard,
  parent-category return navigation, and a handler that passes user country,
  currency and shown-product history to find_by_subcategory. Product cards
  keep the existing CATEGORY curation mode and shared renderer.
- Updated the catalog landing copy to describe the new directory and
  subcategory structure.
- Added focused tests:
  tests/test_catalog_subcategories.py,
  tests/test_subcategory_search.py,
  tests/test_subcategory_navigation.py.
  The conversational regression workflow now runs these modules.
- CI on commit 26d18da7390192799d3f8e37c422c461f202516c ran the catalog
  tests successfully as part of 459 passed. The workflow still reports
  3 unrelated existing failures:
  two conversational type-gate reason expectation mismatches and one Gemini
  timeout fallback retry-count expectation. These failures are outside the
  catalog changes and were not modified. The monetization workflow also
  reports its previously documented unrelated cross-sell wording assertion.
- No live AliExpress query validation was run because the user's local .env
  is not accessible in this environment. The subcategory routes are therefore
  code-configured and offline-tested, but their real marketplace coverage
  still requires a limited live smoke test with the user's local credentials.
- No category image asset has been committed yet. Telegram inline keyboards
  cannot embed images; the next visual-UI step must add a real Telegram photo
  cover (or a small set of category cover media) with a text/keyboard fallback.
  Do not claim the image-based storefront is complete until this asset and
  media rendering are implemented.
- Current branch remains audit/aliexpress-portals-smoke-test; no changes to
  main, PR #10, .env, local files or production deployment.
- Next: complete image-backed catalog rendering, add explicit handler-level
  regression coverage for category/subcategory navigation, then run a bounded
  live search smoke across representative subcategories before considering
  the catalog ready for local use.


## Visual catalog rebuild — image-backed first version (2026-11-12)

- Added the Telegram catalog cover at assets/catalog_categories.jpg. It is a
  compact collage showing all 12 top-level categories; it is a catalog cover,
  not a live product listing or a set of individual category photos.
- The main catalog screen sends that cover as a Telegram photo with the
  existing 12-button inline keyboard and a short caption. If the cover is
  unavailable or photo sending fails, the handler falls back to a text screen.
- Selecting a category edits the caption of the cover message into the
  category's subcategory directory; text messages use edit_message_text.
  Returning to the catalog replaces the current screen with the cover again.
- Added handler-screen tests for the text fallback and photo-caption
  navigation. The fake bot is deliberately not treated as a real Telegram Bot,
  so tests do not attempt network/file uploads.
- The focused conversational CI run on commit
  fb852fad4b4469904ce1726c8a9f3fc7cf0bb579 completed with **461 passed**,
  **3 unrelated pre-existing failures**, 1 deselected and 330 subtests passed.
  All catalog configuration, subcategory retrieval, keyboard and handler
  screen tests passed. The three failures are two existing conversational
  type-gate reason mismatches and one existing Gemini timeout retry-count
  assertion; no catalog test failed.
- The full legacy discovery acceptance module was briefly added to the
  focused CI list, then removed because it exposes unrelated pre-existing
  failures in other discovery/feed/photo tests. It is not a reliable
  acceptance gate for this isolated catalog change. Targeted catalog handler
  coverage is maintained in test_subcategory_navigation.py instead.
- Live AliExpress searches have not been run: the user's local .env is not
  accessible here. The initial query routes are offline-tested only; their
  actual coverage, result relevance, response time and API call count require
  a limited local live smoke test.
- Not implemented in this first version: analytics event expansion and
  thematic collections. They remain second-queue enhancements from the
  approved specification and should follow after live catalog validation.
- Code changes last tested at fb852fad4b4469904ce1726c8a9f3fc7cf0bb579;
  this final context update is documentation-only. No changes to main, PR #10,
  local working copy, .env or production.
- Next action: the user pulls this branch and starts the bot locally; then
  run a limited live check across representative subcategories, review the
  category cover on an actual Telegram screen, and tune only the routes whose
  marketplace results prove weak or irrelevant.


## Catalog quality and subcategory expansion — implementation in progress (2026-11-12)

- Work is isolated on `feat/catalog-quality-and-subcategories`, based on
  `audit/aliexpress-portals-smoke-test`. No changes to `main`, PR #10,
  local `.env`, production or the user's local working copy.
- Expanded the visual catalog from 60 to 96 subcategories: exactly 8 under
  each of the 12 existing parent categories. All existing subcategory codes
  remain unchanged; 36 new shelves are defined in
  `config/subcategory_expansion.py`.
- Extended `Subcategory` with `product_types` and `exclude_terms`.
  All 60 original shelves now have curated Russian/English product identity
  aliases (57 in `config/subcategory_type_overrides.py`, plus targeted
  overrides for cleaning, lighting and game controllers); all 36 new shelves
  carry their own explicit identity phrases. Focused shelves also declare
  excluded title concepts where live tests exposed likely category leakage.
- Added `services/subcategory_quality.py`: a deterministic title-evidence
  gate applied only to curated subcategory requests after the shared
  relevance filter and before constraints/ranking. It rejects candidates
  without a declared product-type match and honors configured excluded title
  concepts. It does not add product data or make claims about missing fields.
- Added a dedicated bounded Gemini subcategory selector in
  `services/ai_service.py`. It replaces (does not add to) the existing
  single category/feed selection call for subcategory screens. The prompt
  includes the shelf description, allowed product types and exclusions, and
  explicitly asks for attractive, useful and varied candidates without
  padding. A valid empty ID list is treated as a no-match; unavailable or
  malformed responses use deterministic fallback.
- Updated `services/search_service.py` to apply the deterministic gate,
  route subcategory screens through the specialized selector, cap subcategory
  output at 20, and run final exact/near-duplicate and product-family checks.
  The final family pass has no fill-up behavior: if there are not enough
  distinct candidates, the screen stays shorter rather than restoring twins.
  The normal search/feed selector path remains unchanged.
- Added/extended offline tests:
  `tests/test_catalog_subcategories.py` (96 shelves, 8 per parent, metadata),
  `tests/test_subcategory_quality.py` (cleaning leaks, controller/router
  separation, lighting identity, family diversity), and
  `tests/test_subcategory_search.py` (strict gate in shared search, dedicated
  selector, explicit empty selection). Added the new quality test to the
  focused conversational CI workflow and enabled push runs for this branch.
- Test execution and CI status are NOT YET VERIFIED from this environment.
  The user's local AliExpress credentials are unavailable here, so no live
  marketplace smoke tests or cost/latency measurements have been run. Do not
  report live validation or test success until confirmed.
- Next: inspect the updated diff, run the focused offline catalog tests and
  the repository's existing focused regression workflow, correct any failures,
  then ask the owner to pull the branch and run a limited live smoke across
  representative categories. Recheck the three previously documented
  unrelated CI failures without modifying them.


- Follow-up hardening: the expansion data module now uses an independent
  `ExtraSubcategory` dataclass, avoiding a circular import with the base
  catalog. The original 57 non-special shelves receive explicit bilingual
  identity aliases from the override map; the three previously broad shelves
  keep their targeted aliases/exclusions. AI-selection telemetry now reports
  the final post-deduplication product count.


### First focused local test feedback and corrections (2026-11-12)

- The user's first focused run reported 24 passed, 15 failed and 491 subtests
  passed. The 15 failures reduced to three causes: 13 expanded shelves had
  only two Russian relevance keywords; the cleaning unit test expected two
  exclusions although the configured policy explicitly excludes drain tools,
  fireplace kits and kitchen aprons (three); and the shared generic query-word
  gate rejected English-title products such as "Electric Spin Scrubber" before
  the new subcategory product-type aliases could validate them.
- Corrected all 13 missing Russian keyword sets.
- Added `filter_subcategory_relevant`: it retains the shared invalid-URL and
  empty-title checks and all other shared rejection reasons, but allows the
  generic `no_query_term_in_title` case only when the candidate independently
  matches an explicit product-type alias for the selected shelf. The strict
  shelf exclusion and positive product-type checks still run; this does not
  weaken the gate for unrelated items.
- Updated the cleaning assertion to reflect the three configured exclusions.
- The corrected code has NOT yet been re-run by the user or confirmed by CI.
  Next: pull the latest branch commit and rerun the same four focused test
  modules. Do not proceed to live marketplace checks until this passes.



## Subcategory 20-card selection and balanced reserve — implementation update (2026-11-12)

This section supersedes the earlier note above that the final subcategory family
pass has no fill-up behavior.

- The root cause from live Telegram logs was confirmed in
  `services/search_service.py`: Gemini was asked for at most 20 IDs, and the
  final exact/near/family curation ran only on those IDs. When curation removed
  similar products, no reserve candidates were available to restore the shelf
  to 20 even when the retrieval pool contained many more candidates.
- Subcategory Gemini selection now requests up to 40 ranked IDs: the first 20
  are the primary shelf and up to 20 more are a reserve. The selector prompt
  explicitly requires at least 20 IDs when 20 or more candidates meet the
  subcategory identity criteria; it no longer treats broad product-family
  variety as a reason to return only 10–12 cards. This remains one Gemini call.
- The deterministic finalizer still removes exact and near-identical listings.
  It now uses a lower, category-agnostic title-family similarity threshold and
  prefers at most two candidates per family. If that preference leaves empty
  slots, it refills in round-robin family order from the Gemini-approved
  reserve, so one family does not monopolize the refill.
- AI-unavailable/error fallback now curates the complete already-filtered
  subcategory pool instead of truncating it to 20 before duplicate/family
  checks. This avoids losing replacement candidates before final curation.
- Non-subcategory selection behavior, the 30-card general browse cap,
  conversational Option C, provider retrieval and the AliExpress-only policy
  remain unchanged. No extra per-product Gemini calls were introduced.
- Added a focused offline regression test asserting that a subcategory selector
  receives a 40-ID budget and the deterministic family refill returns 20
  distinct real candidates from a larger pool.
- Validation status: source changes and test were committed to this branch,
  but tests have NOT yet been executed in a local checkout or CI from this
  environment. No live AliExpress or Telegram validation has been performed.



## Catalog subcategory quality — unified subtype balancing and adaptive retrieval (2026-11-12)

This update supersedes the previous subcategory-quality implementation note above.

### Implemented on `feat/catalog-quality-and-subcategories`

- Subcategory Gemini selection remains a single bounded call, but may return up to
  40 ranked IDs (20 primary + up to 20 reserve). The prompt now explicitly asks
  for the full reserve when enough suitable candidates exist. The selector's
  ID-cleaning cap accepts the requested bound; the general 20-card feed cap is
  unchanged.
- After Gemini selection, the deterministic finalizer now appends the rest of
  the already-filtered candidate pool as a reserve, ranked by the existing
  deterministic `value_score` (real discount, rating, orders and availability
  signals). This allows replacement candidates when Gemini returns only 20 IDs
  or duplicate/family curation removes some of them. It does not create products
  or make another Gemini call.
- Product-type aliases already configured for every subcategory now serve as a
  shared subtype taxonomy. The most specific matched alias is selected using
  alias-word specificity (e.g. `neck massager` instead of generic `massager`).
  The finalizer distributes primary candidates across represented subtypes with
  an adaptive quota, limits close title families to two primary cards per
  subtype, then prefers other distinct families before reusing deferred family
  members. It records the final per-type counts, family deferrals and refill
  counts.
- Family similarity is applied within a configured subtype rather than across
  the entire shelf, so a MIG welder is not grouped with a TIG welder merely
  because both belong to welding equipment. Near-identical listings are still
  removed by the existing near-duplicate gate.
- Subcategory title validation now uses the leading product-title clause
  (before comma/semicolon/pipe) as the product identity. Multiword aliases must
  occur within one local title window; scattered words from unrelated title
  fragments no longer combine into a false product-type match. Exclusion terms
  use the same primary-identity boundary.
- Expanded the configured product-type aliases for welding equipment, massage
  devices and hand tools. Welding aliases include MIG/TIG/MMA, plasma, spot and
  laser welding variants; dent-puller concepts are excluded from the welding
  shelf. Massage aliases include body-part-specific massagers, massage guns,
  rollers and other declared forms.
- When the filtered subcategory pool remains below the 80-candidate target,
  retrieval now continues the same bounded configured routes on later pages,
  up to the deployment's existing `max_search_pages` limit. It stops early
  once the filtered pool is full. No new endpoint or marketplace is introduced.
- Added concise subcategory diagnostics: code, route count, pages scanned, raw
  count, filtered pool size, final count, shortfall reason, subtype distribution,
  family deferrals and refill counts.
- The Gemini ranking payload now includes the real positive-feedback percentage
  and a boolean indicating whether an image exists. No image URL or affiliate
  URL is sent to Gemini.

### Focused regression coverage added

- 40-ID Gemini selector reserve handling.
- Refill to 20 when Gemini returns only the first 20 and the rest of the pool
  contains additional families.
- Balanced selection across four welding subtypes.
- Specific subtype alias preference (neck/facial massager).
- Rejection of a welding mention in a secondary cross-sell clause and rejection
  of a dent-puller identity.
- Subtype classification coverage across the full configured subcategory list.
- Multi-route subcategory pagination onto the next page when the filtered pool
  remains short.
- Rank payload includes actual positive feedback and image-presence signals.

### Validation status

- Changes and focused tests are committed to this branch.
- Tests have NOT yet been executed in a local checkout or CI from this session.
  The GitHub workflow file is configured to run on pushes to this branch, but
  the available repository status endpoint does not expose push-triggered run
  results. No live AliExpress/Telegram validation has been performed yet.
- Do not merge or apply these changes to `main` or PR #10 without explicit
  approval. Local checkout still requires a fast-forward pull of this branch
  before testing the updated implementation.



### Follow-up hardening — CI coverage and fallback behavior (2026-11-12)

- The adaptive subtype quota is now an equal-share cap based on the number of
  represented configured product types (one type may use the full 20 slots;
  two types receive up to 10 each, three up to 7 each, etc.). If some types do
  not have enough distinct candidates, remaining distinct families are used
  before deferred family repeats.
- Added deterministic fallback coverage for the case where Gemini is
  unavailable: a filtered pool with at least 20 distinct candidates still
  produces 20 cards.
- Added a four-way welding subtype distribution test and a full-catalog test
  confirming that every configured subcategory has at least one classifiable
  product-type alias.
- Added a primary-title identity test: a requested product type appearing only
  after a comma/semicolon/pipe cross-sell separator cannot qualify the listing.
- The conversational regression workflow now explicitly includes the new
  `SubcategorySelectorReserveTests` and `CandidatePayloadTests` classes from
  `tests/test_ai_service.py`, in addition to the existing subcategory/search
  test modules.
- Validation is still pending: no test command has been executed from this
  session. The changes are on the feature branch only; `main` and PR #10 remain
  untouched.



### First local test feedback and focused corrections (2026-11-12)

The first local run after pulling commit `e491204` executed:

```powershell
python -m pytest tests/test_subcategory_quality.py tests/test_search_service.py tests/test_ai_service.py -q
```

Result: **134 passed, 5 failed, 6 subtests passed**. Three failures were in
new/affected catalog coverage and have been corrected in subsequent commits:

- Family-dedup test now checks the actual contract (same-family duplicate is
  removed; remaining candidates survive) without asserting an arbitrary order
  across distinct subtype buckets.
- Subtype specificity now prefers explicit, longer product-type identities
  (e.g. `facial massager`) over generic phrases that happen to match by
  inflection (e.g. `massage device`).
- Pagination regression fixture now returns six genuinely different massage
  subtypes. The previous fixture returned six near-identical neck massagers,
  so a final result count of one was correct deduplication, not failed route
  expansion.

The two remaining failures concern the pre-existing return contract of
`AIService.answer_product_question_with_recommendation`: its implementation
returns a single product ID string for a recommendation, while two existing
tests expect a list. The same tests and the exact method body are present on
baseline branch `audit/aliexpress-portals-smoke-test`; this behavior was not
introduced by the catalog changes. It is intentionally left untouched because
it is outside this task.

Latest focused fixes are committed on this feature branch. They have not yet
been executed locally; ask for a fresh fast-forward pull and rerun the same
three focused test modules before live AliExpress validation.


## Catalog quality branch — live AliExpress smoke follow-up (2026-11-12)

The local focused regression run after pulling the catalog-quality fixes is
**137 passed, 2 failed, 6 subtests passed**. All catalog-quality and search
service tests pass. The two remaining failures are the pre-existing
`GroundedProductQuestionTests` return-contract mismatches in
`AIService.answer_product_question_with_recommendation` (one expects a list
instead of the existing scalar ID; the other expects a list instead of the
existing `None` for claims-only answers). This method was compared with the
baseline branch and is outside the catalog TЗ; do not change it here.

Live `python smoke_aliexpress.py` returned valid normalized products and
affiliate links for all eight broad query families, with **0 API errors and
0 retries**. Its keyword-search pagination probe saw one product ID repeated
between page 1 and page 2 for `wireless headphones` (5 products on each page).
A single cross-page overlap is not itself an API failure: live keyword ranking
may shift between calls, and the search pipeline deduplicates product IDs.
The smoke assertion was therefore corrected to report overlap and require
page 2 to contain at least one previously unseen ID; duplicate IDs within
page 2 remain a failure. This change is diagnostic only and does not alter
provider requests or search ranking. Pull commit
`755555dfa0ce575ba102a579cc1325c9d0909599` and rerun the live smoke before
proceeding with category/subcategory quality validation.


---

## 2026-11-12 — Catalog-wide quality gate and subcategory repeat search

Implementation branch: `feat/catalog-quality-and-subcategories`.

### Scope and current changes

The category shelf pipeline remains the shared `SearchService.find_by_subcategory`
flow for the complete catalog (currently 96 configured subcategories). The
first implementation pass strengthens the common identity boundary rather than
adding special cases for the two live examples (window cleaners and hand tools).

- `services/subcategory_quality.py::subcategory_match_reason` still requires
  positive evidence for a configured product-type alias in the primary title,
  then additionally checks whether the listing is the principal requested
  product rather than an accessory/part that merely mentions its host device.
  It reuses the category-general
  `services.convsearch.type_validation.validate_product_type` logic.
- A rejected role relation is diagnosed as
  `accessory_or_component`, separately from a missing product-type match.
- `SearchService._finalize_subcategory_selection` repeats this identity gate
  over the entire selected + reserve stream before exact/near duplicate removal,
  family balancing and the 20-card cap. Gemini-approved IDs and reserve
  candidates therefore cannot bypass the final check.
- The Telegram card browser exposes `🔄 Новый поиск` only on the final card
  of a subcategory result set. The callback reuses the same configured
  subcategory and country/currency, excludes previous batches plus the
  user's persisted shown history, prevents concurrent requests and marks a
  successfully replaced session as consumed so an old card cannot replay the
  same next batch.
- Favorites, price tracking, ordinary text search and non-category card
  keyboards are not changed by the new-search action.

### Tests added (not yet executed locally)

- Window-cleaner accessory/spare-part rejection.
- Hand-tool holder rejection while retaining a legitimate hand-tool set.
- Finalizer revalidation of selected and reserve candidates.
- New-search button visibility on the last card only.

### Remaining acceptance work

This is an implementation increment, not a claim that the complete TЗ is
finished. The user must run the focused local test commands after pulling the
branch. Then continue with:
1. Fix any test or integration failures.
2. Audit the shared subtype taxonomy and diversity behavior, including
   cross-language aliases that currently split one real subtype into multiple
   log buckets.
3. Verify the new-search callback end-to-end in Telegram, including retries,
   stale callbacks and successive batches.
4. Run real AliExpress Affiliate API checks for all 96 subcategories and
   record per-subcategory quality/shortfall evidence. Offline fixtures do not
   satisfy this live acceptance requirement.
5. Complete the broader regression suite and report the exact commands/results.

Do not mark the TЗ complete until every configured subcategory has real
provider validation and the remaining acceptance work above is closed.

### Live validation runner

`scripts/validate_catalog_subcategories.py` runs the real shared
`find_by_subcategory` pipeline against every configured subcategory, with
bounded documented page depth (default two pages per route), the configured
Gemini selector/fallback, per-shelf type counts and final product titles.
It writes a timestamped JSON report under `reports/`. Run explicitly after
pulling; it is not part of offline pytest and may make real AliExpress API and
Gemini requests. Example:

```powershell
.venv\\Scripts\\python.exe scripts\\validate_catalog_subcategories.py --max-pages 2
```



### Strict Gemini approval boundary correction (2026-11-12)

A focused local run after the first implementation reported 28 passed / 1
failed in the three catalog test modules. The remaining failure showed that
`_finalize_subcategory_selection` was receiving every unselected pool
candidate appended after Gemini's approved IDs. This made the deterministic
refill reintroduce products that Gemini had explicitly omitted.

Corrected on the feature branch: successful subcategory selection now passes
only the IDs returned by Gemini into the deterministic finalizer. The selector
is already requested to return up to `SUBCATEGORY_SELECTION_MAX`
(40) IDs, so its approved tail remains the reserve for the final 20-card
quality/diversity refill. Candidates outside that approved set are never
reintroduced. Full-pool deterministic fallback remains available only when
the selector is unavailable, fails, or returns no usable selection. This
preserves the one-call selector and avoids additional model calls.

Latest correction commit: `4417cc5e4f94470dc06b4634a78ff354ff78a816`.
Local execution of the tests after this commit is still required.

### Subcategory reserve-test contract (2026-11-12)

The subcategory selector test fixture must model the production contract: when
it is asked for up to 40 IDs, it must actually return a representative approved
set of primary and reserve candidates. A fixture that returns only the first
20 family-clustered IDs cannot test reserve refill or ten-family diversity;
the production finalizer must not silently reintroduce IDs omitted by Gemini.

### Discovery acceptance test-double refresh (2026-11-12)

The legacy discovery acceptance fixtures must follow current production
contracts without constraining them to old call shapes: the conversation
retrieval adapter forwards raw/broaden/continuation kwargs to
SearchService.find_products; the photo handler may execute both a primary
hypothesis and its bounded fallback-category search when the primary pool is
small; and the popular feed performs one curate_findings call after gathering
its pages. Recording fakes should accept the current kwargs and provide that
feed curation hook. These are test-double updates only; production discovery
behavior is unchanged.


### Discovery acceptance follow-up — final stale assertions (2026-11-12)

The local run after the fixture refresh passed 48 tests and exposed two
remaining test expectation issues; production behavior is unchanged:

- The engine intentionally emits multiple INFO diagnostics per turn. The
  instrumentation test now selects the specific `convsearch action=` line
  instead of the first arbitrary line containing `convsearch` (which is the
  semantic-selection metrics line).
- The popular-feed test now expects the second sampled page to exclude both
  the pre-existing feed history and the product collected from the first page.
  This is the production no-repeat guarantee across pages within one opening;
  expecting the exact same exclusion set on both calls contradicted that
  behavior.

These are test-only assertion corrections. Re-run
`tests/test_bot_startup.py` and `tests/test_discovery_acceptance.py` locally
before proceeding to the 96-subcategory live validation.


### Live catalog report follow-up (2026-11-12)

The first 96-subcategory provider run returned 1,452/1,920 cards (59 shelves
at 20, 33 short, 3 empty, 1 full provider error). Review found that subtype
classification counted matching title aliases without applying the principal
product validator, so add-ons (for example, a microphone bundled with a
speaker) could be counted as the shelf's main subtype. It now requires the
shared validator to approve each alias and prefers the earliest validated
product identity in the primary title clause.

Catalog configuration was tightened for the clearest false-positive shelves:
storage now uses household-specific organizer/container aliases and excludes
known unrelated title concepts; cleaning now uses concrete cleaning-tool
aliases and excludes pet-care, food-preparation and industrial-cleaning
concepts. These are data-driven catalog policies; do not add query-specific
branches to shared code.

The live validation JSON now reports provider_status independently from
shelf card-count status, so partial route/provider failures are visible even
when another route returned rows. It also includes positive_rate and
documents that the AliExpress Affiliate API's evaluate_rate is not a 0-5
star rating; rating: null is expected unless a distinct rating source is
implemented.

Focused regression coverage was added for speaker/microphone principal-type
classification, household storage intent and cleaning false positives.
Re-run the focused catalog tests locally before another live 96-shelf run.


### Subcategory selection diagnostics (2026-11-12)

The live acceptance runner now captures the per-shelf
last_subcategory_selection_stats from SearchService. For each shelf it records
selector status/latency, requested and returned IDs, IDs matched to the real
pool and approved by Gemini, selection source/fallback reason, finalizer
input, identity-gate drop reasons, exact/near duplicate drops, subtype/family
counts and deferrals/refills, final count and shortfall. These are measurement
only: the one-call selector, 8-second timeout, strict Gemini approval boundary
and deterministic fallback policy are unchanged.

Focused tests assert both successful 40-ID reserve selection and the no-AI
deterministic fallback diagnostics. The next live report can now distinguish
a short Gemini-approved set from losses in the finalizer instead of inferring
the cause from the final card count alone.

---

## 2026-11-12 — Final catalog completion pass (in progress)

### Baseline live acceptance

Latest real AliExpress/Gemini report before this pass:
- 96 subcategories checked; 57 returned 20 cards, 37 were short, 1 empty,
  and 1 had a full provider error.
- 1,450 / 1,920 cards (75.5%); 92 provider successes, 3 partial failures,
  1 full failure.
- In 36 of 39 short shelves, the filtered candidate pool was below 20.
- Many shelves had 200–290 raw rows but only 1–18 candidates after the
  subcategory identity gate. The primary problem is catalog identity coverage
  and classification, not raw API row volume alone.

### First implementation pass on feat/catalog-quality-and-subcategories

- Tightened catalog-local Russian token matching from a four-character to a
  five-character stem. This prevents unrelated identities sharing «вело»
  from being merged (for example, «велосипедный» vs «велофонарь»).
  The shared conversational type validator was deliberately not changed.
- Expanded concrete product-type aliases for weak broad shelves, including
  portable gadgets, bathroom, car interior/electronics, computer components,
  console accessories, cycling, outdoor, power tools, fishing, fitness,
  camping, streaming and outdoor leisure.
- Expanded configured routes for those shelves and for console controllers,
  network equipment, PC controllers, woodworking tools, water sports,
  sports electronics, pet training and outdoor furniture.
- Added explicit cycling light aliases so Russian titles such as
  «велосипедный фонарь» remain valid after the stricter stem rule.
- SearchOutcome now retains the exact relevance rejection breakdown.
  The live runner reports routes, route count, rejection reasons and a
  shortfall reason separately from provider health and Gemini diagnostics.
- Added offline regressions for cycling subtype identity, concrete portable
  gadget identity, bathroom product coverage, console controller/accessory
  separation, sports heart-rate strap identity and rejection diagnostics.

### Validation status

- Source and test updates are committed to this feature branch only.
- Offline pytest and real AliExpress/Gemini live validation have NOT yet been
  executed after this pass. Do not mark the catalog task complete.
- Next required actions: pull the feature branch locally, run focused catalog
  tests, correct any failures, then run the 96-subcategory live validator and
  compare its JSON with the baseline above.
- Preserve main, PR #10, .env and unrelated local files. Do not relax the
  Gemini-approved-ID boundary, selector call count, timeout or deterministic
  fallback to inflate shelf counts.

---

## 2026-11-12 — Consolidated handoff: work from the last week and next direction

This section consolidates the recent work and decisions so a new session can resume
without relying on chat history. It supplements, and does not replace, the detailed
architecture and historical implementation notes above.

### Hard product/UI boundaries — MUST PRESERVE

- The existing main-menu **«Умный поиск»** and **«✨ Находки»** are protected,
  production features. They took substantial effort to stabilize.
- Do not rename, move, redesign, replace, refactor, reroute, change callbacks,
  alter search/feed behavior, or otherwise touch either feature while working
  on catalog navigation or new discovery ideas.
- Any change that could affect either feature requires the user's explicit,
  separate approval first. A broad approval to work on catalog/categories does
  NOT authorize changes to these two main-menu features.
- The only currently discussed extension is an independent, optional
  **«Тематические подборки»** area. It must be additive and isolated; it must
  not be implemented by repurposing «Умный поиск» or «Находки».
- Candidate themes (not yet approved as a final taxonomy or implementation):
  1. «Необычные полезные устройства»
  2. «Умные решения для дома»
  3. «Интересные автомобильные аксессуары»
  4. «Гаджеты, о которых мало кто знает»
  5. «Полезные мелочи до определённого бюджета»
- These are editorial/product-discovery collections, NOT new AliExpress taxonomy
  categories. Each needs explicit inclusion/exclusion criteria, concrete product
  types and real-provider validation before UI implementation. Do not create
  broad queries and hope that they produce relevant results. Do not fill a
  collection with irrelevant products to reach a target count.
- Budget collections must use current/verified price data and must not present
  stale or missing prices as current.
- Collections may overlap naturally (a product can qualify for more than one),
  but each collection must be independently useful and avoid repetitive
  near-identical products.
- Do not start implementation until the user approves the proposed collection
  definitions and the acceptance criteria.

### Mandatory workflow for EVERY future task / TZ

1. Read the current `AGENTS.md` and `PROJECT_CONTEXT.md` before making changes.
2. Check the actual current branch, HEAD, worktree state where available,
   and inspect relevant source/call sites. Never assume a historical branch
   or commit is still current.
3. Translate the user's TZ into explicit scope, protected areas, acceptance
   criteria and non-goals before editing.
4. Make minimal, task-scoped changes only. Do not touch `main`, PR #10,
   `.env`, user backups, reports, temporary JSON files or unrelated features
   without explicit authorization.
5. Run the exact relevant offline tests; clearly distinguish tests actually
   executed from tests merely added or recommended. Never claim CI/live checks
   passed without evidence.
6. For live AliExpress/Gemini checks, use the user's local configured
   environment and report the exact run/report. Offline tests do not replace
   live validation.
7. After every completed TZ, update this existing `PROJECT_CONTEXT.md` with
   scope, decisions, changed files, commit/branch, exact test commands/results,
   live evidence and remaining work. Preserve prior useful history; do not
   replace the file with a short summary.
8. Review the final diff and confirm protected features and unrelated files
   were not changed.
9. If local execution or credentials are unavailable to the assistant, state
   that plainly and provide the smallest necessary user-side verification step.

### Recent work areas (approximately 2026-11-05 through 2026-11-12)

#### 1. Conversational search / reliability work

- The authoritative architecture is hybrid **Option C**:
  Gemini interprets the user's conversational turn and returns a validated
  structured decision; deterministic state, retrieval, validation, constraints,
  ranking and Telegram delivery remain authoritative.
- The removed full Gemini ShoppingAgent/tool loop must not return.
- Retrieval uses AliExpress Affiliate API only, bounded grounded routes and
  deterministic RRF. Product identity, hard budget, required features,
  exclusions and accessory compatibility are validated deterministically.
- Gemini candidate selection is bounded and may approve only real candidate
  IDs. Timeouts/errors use the documented deterministic fallback; no invented
  products/specifications/links/prices.
- Recent regressions addressed contextual refinements, explicit product
  switches, stale conversational slots, material/use-case follow-ups, product
  identity versus accessories/genitive mentions, and candidate relevance.
- Relevant historical branches/commits in the wider recent work include
  `work/consultant-reliability`, `fix/gemini-quality-preserving-selection`,
  and the conversational Option C changes documented earlier in this file.
  These are historical references, not an assertion about the user's current
  local checkout. Always inspect the actual current branch before proceeding.
- Previous local test evidence from the reliability pass included 2,210 tests
  plus 730 subtests in a filtered regression run, and a separate
  2,029-test/668-subtest run in the later work history. These counts belong to
  their respective historical runs only; do not treat them as validation of
  the current catalog branch.
- A separate focused catalog run previously reported 137 passed, 2 failed,
  6 subtests passed; the two failures were identified as pre-existing
  `GroundedProductQuestionTests` return-contract mismatches in
  `AIService.answer_product_question_with_recommendation`, also present on
  the baseline branch. Do not change that unrelated method as part of catalog
  work without a separate TZ.

#### 2. AliExpress provider / Affiliate API smoke and diagnostics

- The project uses AliExpress Affiliate API / Portals, not ordinary-page
  scraping. Do not introduce scraping, another marketplace, Alibaba or Temu.
- A live provider smoke previously returned normalized products and affiliate
  links across eight broad query families with 0 API errors and 0 retries.
  One product ID overlapped between page 1 and page 2 for wireless headphones;
  this was treated as a cross-page overlap handled by pipeline deduplication,
  not as an API failure. Page 2 must contain at least one unseen ID; duplicates
  within the same page remain a failure.
- A separate official Portals monetization diagnostic (gateway
  `https://api-sg.aliexpress.com/sync`) found:
  - featured campaigns call succeeded but returned 0 campaigns;
  - product lookup was skipped because no exact campaign name/date range was
    available;
  - order-list call succeeded but returned 0 orders / empty response fields;
  - 2 calls succeeded, 0 failed;
  - the client normalized HTTP 405 "The result is empty" to an empty object,
    hiding some response structure. Improve safe diagnostics (response code,
    message, nested field shape and empty-result counters) before treating an
    empty response as a usable campaign/order result.
- No monetization implementation was authorized from that diagnostic alone.
  The bot remains free; Premium, subscriptions, Telegram Stars and paid query
  limits remain deferred. Monetization roadmap is a separate workstream and
  must not be mixed into catalog changes.

#### 3. Gemini cost / latency / usage analytics

- A separate benchmark compared Gemini Flash and Flash-Lite:
  - Flash-Lite: 43 requests / 32 accepted (74.4%);
  - Flash: 43 / 19 accepted (44.2%) in the measured sample;
  - measured conversation-turn p50: approximately 970 ms for Lite versus
    3,628 ms for Flash;
  - chat-action p50: approximately 826 ms for Lite versus 3,849 ms for Flash.
- Flash-Lite was materially faster and had better acceptance in that sample.
  These are historical benchmark observations, not a universal model-quality
  claim or a current production setting guarantee.
- Usage analytics/dashboard work tracks request ID, operation, model, prompt
  size/tokens, candidate counts, thoughts, total tokens, cost status and
  latency. The local dashboard was previously configured at
  `http://127.0.0.1:8765` with HTTP Basic authentication.
- Gemini quota exhaustion / RESOURCE_EXHAUSTED occurred during earlier testing;
  the user later added funds. Preserve fallback and cost visibility. Do not
  weaken search quality or change model routing/limits during catalog tasks.

#### 4. Catalog categories / subcategories — current active workstream

- The current feature branch identified in the recent project context is
  `feat/catalog-quality-and-subcategories`. The last explicitly documented
  commit in the earlier handoff was
  `db6e3912357ceeeeabf371954220b4206d0c370a`; this must be rechecked against
  the live branch HEAD before any pull, update or new commit.
- Catalog work has included:
  - a shared principal-product/accessory identity gate at retrieval and
    finalizer boundaries;
  - subtype aliases, routes, family balancing and deterministic dedup/refill;
  - a 40-ID Gemini approval/reserve contract, with strict approval boundary;
  - repeat-search button on the final card of a subcategory shelf, excluding
    prior batches and persisted shown history and preventing replay/concurrency;
  - provider health separated from shelf card-count status;
  - positive_rate reporting without misrepresenting AliExpress evaluate_rate
    as a 0–5 star rating;
  - live acceptance diagnostics for selection, finalizer, subtype/family and
    relevance rejection reasons.
- A first 96-subcategory live report returned 1,452/1,920 products (75.6%):
  59 shelves at 20, 33 short, 3 empty, 1 provider error.
- The latest baseline report before the final completion pass returned
  1,450/1,920 (75.5%): 57 shelves at 20, 37 short, 1 empty, 1 full provider
  error; provider status 92 ok, 3 partial_failure, 1 failed.
- In 36 of 39 short shelves, the filtered candidate pool was below 20. This
  points mainly to taxonomy/identity/retrieval coverage rather than simply
  increasing raw page counts. Examples of problematic shelves and quality
  observations are documented in the catalog sections above and in the
  timestamped JSON reports.
- Latest known baseline report:
  `catalog_subcategory_live_20261112_132853.json`
  (conversation file reference `turn1file0`). Earlier report:
  `catalog_subcategory_live_20261112_123423.json`.
- The subsequent final catalog completion pass expanded routes/aliases and
  added diagnostics/tests, but no post-change local pytest run or 96-shelf
  live run has been verified. Treat the branch as **unverified/in progress**,
  not accepted.
- Outstanding catalog acceptance:
  1. inspect current branch diff and confirm latest HEAD;
  2. pull/fast-forward locally without overwriting local work, `.env`,
     backups or reports;
  3. run focused catalog tests and resolve only task-related failures;
  4. inspect real product relevance/diversity, not just card counts;
  5. validate the repeat-search flow in Telegram;
  6. rerun the real 96-shelf validator and compare with the baseline;
  7. complete regression checks and document the exact outcome.
- Do not force every shelf to 20 if only fewer relevant products exist. Never
  fill with irrelevant or fabricated products. Do not weaken the Gemini
  approved-ID boundary, selector call count, 8-second timeout, identity gate,
  duplicate/family rules or deterministic fallback just to inflate counts.

#### 5. Catalog strategy decision reached on 2026-11-12

- The user questioned whether the original 96 subcategories were correctly
  designed. The agreed direction is to reassess the taxonomy before more
  search-route tuning: compare AliExpress's real category tree with current
  shelves, identify clear mappings, duplicates, overly broad/ambiguous groups,
  and categories with insufficient relevant supply.
- Prefer recognizable AliExpress-style product taxonomy, adapted to a curated
  bot catalog. Do not copy the entire AliExpress tree blindly and do not add
  categories merely to make the menu look full.
- A proposed 12-section top-level map was discussed (electronics/phones;
  computers/office; home/garden; tools/repair; auto/moto; sports/recreation;
  beauty/personal care; baby/kids; pets; clothing/accessories; toys/hobbies;
  jewelry/watches). This is only a discussion draft, not an approved final
  category structure.
- Before coding taxonomy changes, prepare a mapping table for each existing
  subcategory: current name, exact product intent, AliExpress category/ID
  mapping if available, overlap/conflicts, actual relevant supply, quality,
  and proposed action (keep, rename, merge, remove). Present the proposed
  structure to the user for approval first.
- Verify what category IDs/fields are actually available through the current
  AliExpress Affiliate API. Do not assume the public AliExpress website's
  navigation tree is directly queryable by this bot's API.

### Current next project direction: additive thematic collections (not started)

The user is considering a separate «Тематические подборки» feature with these
candidate editorial shelves:
- unusual useful devices;
- smart home solutions;
- interesting car accessories;
- little-known gadgets;
- useful small items under a chosen budget.

These are not yet a committed implementation task. First define precise product
inclusion/exclusion criteria, product-type routes, minimum evidence of live
assortment, price freshness rules for budget collections, diversity and
no-repeat behavior. Validate candidate shelves against real AliExpress
Affiliate API data before proposing UI or writing code.

**Critical:** these collections must be a new, separate feature. Do not alter
the existing main-menu «Умный поиск» or «✨ Находки» for any reason without
explicit separate permission. Reuse shared low-level provider/validation
services only if doing so does not change the behavior or public contracts of
the protected features; add regression tests proving their behavior is
unchanged.

### Honest status at this handoff

- No thematic collections have been implemented or approved yet.
- The AliExpress-inspired category map is a proposal only.
- The 96-subcategory catalog quality task is not complete; the latest changes
  still require local and live verification.
- Do not merge to `main`, merge PR #10, or claim final acceptance.
- Keep the current task scope explicit and ask for approval before moving from
  taxonomy analysis to implementation.

---

## 2026-11-12 — Strategic direction: discounts and multi-marketplace price comparison

This section records the product direction discussed on 2026-11-12. It is a
strategic roadmap note, NOT authorization to start implementation and NOT a
claim that these features already exist.

### Current implementation — facts from the repository at this handoff

- The production bot is still **AliExpress-only**.
- There is exactly one concrete provider module:
  `services/marketplaces/aliexpress.py`, built on the generic
  `services/marketplaces/base.py::MarketplaceProvider`.
- `models/product.py::Product` is already marketplace-independent:
  it carries marketplace identity, product ID, title, URL, price/currency,
  old price/discount, rating/orders, shipping, availability and affiliate data.
- `models/product.py::Marketplace` currently contains only
  `ALIEXPRESS`. Its documentation says Temu and Alibaba were deliberately
  removed; do not silently re-add them.
- `models/search_request.py::SearchRequest` already has
  `preferred_marketplaces`, so the request contract can express a marketplace
  preference.
- `services/search_service.py::SearchService` accepts a list of providers,
  filters by `preferred_marketplaces`, runs active providers concurrently,
  and records searched/failed/unavailable marketplace names. This is a
  multi-provider orchestration seam, not proof that multiple providers are
  wired into production.
- `bot/__init__.py::build_providers()` currently returns only
  `[AliExpressProvider(settings.aliexpress)]`. `Settings` currently has
  only AliExpress API credentials. There is no second marketplace adapter.
- There is no implemented cross-marketplace offer-matching / same-model
  comparison UI. `services/comparison_service.py` filters, ranks and scores
  normalized products; it is not yet a complete "where is this exact item
  cheapest?" engine.
- Main menu currently has «Умный поиск», «Найти по фото», «✨ Находки»,
  «Категории», «Избранное», «Отслеживание цен», «Валюта», «Настройки»,
  «🔥 Горячие скидки» and «🎟 Промокоды», plus the Shorts Chill channel link.
  Configured sale-event buttons can be appended separately.
- «🔥 Горячие скидки» currently opens four fixed AliExpress partner
  destinations (super discounts, brand discounts, bundles, coins). It is NOT
  a verified product-level discount feed.
- «🎟 Промокоды» currently opens one configured AliExpress partner/referral
  destination. It is NOT a live coupon catalogue.
- Product-level coupon information can be displayed only when
  `promo_code_info` is actually returned for a product. Do not invent coupon
  codes, discount percentages, minimum spends or expiry dates.
- «Умный поиск» and «✨ Находки» are protected production features. They must
  remain untouched unless the user separately authorizes a change.

### Multi-marketplace comparison — user-confirmed strategic idea, not implemented

The intended user value is broader than adding several search APIs:
when a person asks for a product, the bot should help answer **where it is
better to buy that product**, by searching supported marketplaces and
comparing trustworthy offers.

Target experience to evaluate:
1. User names/describes a product (possibly with model, required specs and
   budget).
2. Search the enabled marketplaces in parallel using their own official,
   permitted APIs / feeds.
3. Normalize real listings into the common `Product` model.
4. Identify the same exact product/model across sources. Compare exact matches
   separately from merely similar alternatives; never present different
   models as the same item.
5. Compare the best available price on a genuinely comparable basis:
   currency conversion using a fresh, documented rate; shipping and destination
   costs when the source exposes them; stock; delivery estimate; seller/store
   quality; and timestamp/freshness of the quote.
6. Show a clear per-marketplace offer, source and direct/affiliate URL. Explain
   what is unknown (for example shipping, taxes, delivery or missing seller
   data) instead of claiming an unverified "cheapest" offer.
7. Rank by the user's goal: lowest verified total cost, fastest delivery,
   trusted seller, or best overall balance. "Best" must be explainable and
   grounded in actual source fields.

Important design distinction:
- **Exact-product comparison**: same brand/model/variant and compatible
  configuration. Only these offers can be called prices for the same item.
- **Alternative-product comparison**: different models that meet the user's
  requirements. Show as alternatives, not as the same product.
- A lower item price alone is not enough to claim a better deal if shipping,
  destination, currency or other material cost fields are unknown.

### Candidate marketplace policy

- No additional marketplace has been integrated; Amazon and eBay are the historical next candidates for feasibility review, not yet approved implementations.
- Historical discussion recovered from 2026-10-06 confirms that Alibaba and
  Temu had already been tried/evaluated and were dropped because of API access
  problems. The user explicitly said not to recommend them again as new options.
  Their enum values were subsequently removed from the normalized
  `Marketplace` model; do not silently re-add them.
- The agreed next candidates in that discussion were **Amazon first, eBay
  second**. Walmart, Banggood and Etsy were mentioned as later alternatives,
  not as the immediate implementation sequence.
- Historical Amazon notes (2026-10-06): Amazon Associates did not require the
  user to have a Seller account; the discussion cited 3 qualifying sales in
  180 days for the Associates review. The then-discussed Creators API access
  required an accepted Associates account and 10 qualifying sales in 30 days.
  These are historical notes, NOT verified current requirements; re-check
  official current eligibility, API access and Ukraine availability before
  planning implementation.
- Historical eBay notes (2026-10-06): eBay Partner Network did not require a
  seller account; production Buy API access required separate approval. Again,
  verify current official EPN/API requirements and Ukraine coverage before
  implementation.
- Amazon and eBay are the remembered next candidates for a feasibility audit,
  not already approved integrations. Do not replace them with an unrelated
  shortlist without discussing it with the user.
- For each candidate, first verify official product-search access, API terms,
  affiliate/partner eligibility, price/stock/shipping fields, Ukraine delivery
  coverage, rate limits, attribution requirements and operational cost.
- Do not scrape ordinary marketplace pages as a shortcut. Do not use a
  marketplace's API without documented permission. Preserve the historical
  exclusions of Temu and Alibaba unless the user explicitly reopens them.
- Some sources may not offer an affiliate program/API suitable for this bot.
  The product comparison value and affiliate monetization must be treated as
  separate questions: a direct-link source may still be useful, but its link
  must be labelled honestly and its economics assessed separately.

### Architecture implications and likely future work

The existing provider abstraction is a useful starting point, but adding a
provider alone is insufficient. Before implementation, design and validate:
- provider-specific credentials/configuration and a provider registry;
- normalized source identity and stable product identifiers;
- marketplace-specific query translation and pagination/rate limits;
- cross-source product identity / variant matching with confidence and
  "unknown" outcomes;
- comparable currency and destination-aware total-cost calculation;
- freshness timestamps and cache policy for volatile prices/stock;
- provider-specific affiliate/direct-link handling and analytics;
- partial-provider failure behavior and per-source diagnostics;
- UI for source selection and comparison without changing protected features.

Do not change the existing AliExpress retrieval pipeline or the protected
«Умный поиск» / «✨ Находки» contracts as a side effect. A future multi-source
comparison feature should first be prototyped behind a separate, explicitly
approved flow, with regression coverage proving existing AliExpress behavior
is unchanged.

### Discounts and thematic discovery — separate but complementary layers

The user and assistant discussed keeping the ordinary category catalogue AND
adding separate editorial «Тематические подборки». A discount surface may
complement both:
- «Выгодные предложения» could become a dedicated product-level deals feed
  only after reliable source data and freshness rules are verified.
- Thematic shelves can optionally highlight a verified discounted item, but
  their primary purpose remains discovery and relevance, not chasing the
  largest advertised percentage.
- Existing «🔥 Горячие скидки» and «🎟 Промокоды» must not be described as
  verified deal/coupon feeds; they are currently partner landing destinations.
- Do not trust a crossed-out old price or headline percentage by itself.
  Validate current price, old-price basis, discount source, product relevance,
  seller quality, shipping/currency context and timestamp. If the evidence is
  incomplete, do not call the item a genuine bargain.
- First audit the current AliExpress discount/coupon capabilities and actual
  account permissions. The earlier Portals diagnostic returned zero featured
  campaigns and zero order rows; empty/ambiguous responses are not proof that
  an operational campaign or coupon feed is available.
- Do not build a new discount feed until a live sample proves sufficient,
  relevant and fresh inventory. A smaller honest feed is preferable to a full
  screen of weak deals.

### Agreed sequencing / current status

1. Finish the current catalogue taxonomy review and verify the latest
   subcategory quality work. Do not divert the active task into provider
   expansion.
2. Separately prepare a marketplace feasibility matrix and compare potential
   sources for API access, affiliate terms, Ukraine delivery, data quality,
   coverage and cost.
3. Prototype a small cross-marketplace comparison for a narrow product set
   only after the user approves the provider shortlist and acceptance rules.
4. Validate exact-model matching, currency/landed-cost comparison, source
   freshness, provider failures and affiliate/direct links with real permitted
   data.
5. Only then decide whether to expose a new menu entry or integrate the
   comparison as an additional mode. Preserve all protected existing sections.
6. Audit AliExpress discounts/coupons separately; implement a verified
   «Выгодные предложения» feed only if live evidence supports it.

At this handoff: no second marketplace provider, no cross-marketplace
comparison UI, no live multi-marketplace benchmark and no product-level
discount feed have been implemented or accepted. This note records the user's
strategic direction and the actual code seams discovered; it does not authorize
code changes or select vendors.



## 2026-11-12 — Stage 1 catalog taxonomy audit completed (documentation only)

- Completed a read-only review of all **12 current parent categories and 96 configured subcategories** on `feat/catalog-quality-and-subcategories`.
- Added the detailed 96-row audit, per-shelf live metrics, product-type distribution, preliminary recommendations, overlap analysis, API findings and draft taxonomy direction to `docs/catalog-taxonomy-audit-2026-11-12.md`.
- Live baseline used: `catalog_subcategory_live_20261112_132853.json` (96 shelves; 57 full 20-card shelves, 37 short, 1 empty, 1 provider failure; 1,450/1,920 cards; provider health 92 ok / 3 partial failures / 1 failed). Previous run at 12:34 returned 1,452 cards, so the later run is not a net improvement by total count.
- Confirmed official AliExpress Affiliate documentation for `aliexpress.affiliate.category.get` and `product.query.category_ids`. The repository only declares `METHOD_CATEGORY_GET`; it does not call or normalize that method. The current provider path does not use category_ids, and the normalized product category path stores category names rather than category IDs.
- No live category.get call was possible in this environment because the user's local credentials are not available. No category IDs have been guessed or written into configuration.
- Preliminary structural issues: duplicated audio/headphone/speaker/microphone and streaming/webcam boundaries; home lighting/table lamps; overlapping controllers/console accessories; watches/jewelry duplicated under fashion accessories; pet-by-species shelves overlap with pet beds/toys; camping/outdoor and outdoor leisure/furniture; hand tools/woodworking and power tools/garden power tools/lawn care.
- Preliminary hierarchy proposal (14 areas) is recorded in the audit document only. It is not approved and does not authorize moving, deleting or renaming catalog entries.
- **No code/configuration/navigation changes were made.** Protected «Умный поиск» and «✨ Находки», photo search, favorites, price tracking, currencies, Option C and Gemini limits remain untouched. `main`, PR #10, `.env` and unrelated files were not changed.
- Offline pytest was not run because this stage is read-only. No live API calls were made in this environment.
- Next gate: present and obtain approval for the proposed category hierarchy (Stage 2). Do not edit catalog configuration or navigation before approval. After approval, perform the real category.get check with the user's local credentials, then proceed to catalog quality validation.
- Audit commit: `6ba7d44bccb5967db8995173af0260d50c7917b2`; the context update follows as a separate documentation-only commit.


## 2026-11-12 — Stage 2 taxonomy proposal prepared (awaiting exact hierarchy approval)

- User approved the proposed **15-parent-category direction** as the basis for the new catalogue. This approval is for the top-level direction only; it is not yet approval to change config, callbacks or Telegram navigation.
- Added `docs/catalog-taxonomy-proposal-stage-2-2026-11-12.md` with the proposed hierarchy, definitions/boundaries, mapping recommendations for all 96 current codes, candidate new shelves, seven disputed decisions and implementation gates.
- The proposed 15 parents are: Smartphones & accessories; Computers & office; Electronics & gadgets; Home/kitchen/household; Tools & repair; Auto & moto; Sports/tourism/recreation; Beauty & personal care; Kids; Pets; Clothing & shoes; Gaming devices; Toys/hobbies/creativity; Watches & jewelry; Garden & dacha.
- Key structural recommendations: split generic audio into distinct headphones/speakers and consolidate microphones under streaming/recording in Computers; move gaming keyboards/mice to Computers while gamepads/console accessories/VR stay in Gaming; move watches/jewelry out of fashion accessories; move sportswear to Clothing; move general toys/creative kits to Toys & hobbies; replace broad dog/cat shelves with functional pet needs; consolidate home/table lighting, dash cameras under auto electronics by default, outdoor leisure/furniture, and woodworking under principal tool roles; retire broad `portable_gadgets` and `outdoor` shelves after preserving only concrete relevant routes.
- New smartphone shelves, full game consoles, hobby/model making, board games and moto accessories are candidates only; they must pass live inventory/quality validation before being enabled.
- This is a proposal, not an accepted final list. No code, config, callback, UI or menu changes were made. Protected «Умный поиск» and «✨ Находки» remain untouched.
- The earlier Stage 1 audit document remains unchanged. Its preliminary 14-area hypothesis is superseded as a working direction by the user's approval of 15 parent areas; the detailed subcategory proposal is in the new Stage 2 document.
- Next gate: user review/approval or corrections to the exact hierarchy and disputed decisions. After that, run the local-credential live category.get check and Stage 3 catalog quality validation before implementing the catalogue.
- Proposal commit: `f45301b61d24d81c635ea7274ec8fc9dbfbf4d90`; this context update is a separate documentation-only commit.


## 2026-11-12 — Stage 2 taxonomy explicitly approved; Stage 3 validation started

- User explicitly approved the complete 15-parent hierarchy in `docs/catalog-taxonomy-proposal-stage-2-2026-11-12.md` and all seven disputed decisions in its Section 5. This supersedes the previous 'awaiting exact hierarchy approval' status.
- Approval authorizes validation and subsequent implementation only after the defined quality gates. It does not authorize guessed AliExpress category IDs, bypassing live inventory checks, weakening identity/diversity filters, or changing protected features.
- Stage 3 initial GitHub review confirms the current feature branch still contains the legacy 12-parent catalogue (`config/catalog.py`) and the existing 96-shelf configuration (`config/subcategories.py` plus expansion/override modules). The approved 15-parent hierarchy has not yet been implemented in code.
- The branch's `.github/workflows/conversational-regression.yml` includes the catalog tests and a filtered repository suite. This workflow is an offline CI path; it does not perform the credentialed live AliExpress run.
- Environment limitation: the GitHub-connected workspace cannot access the user's local `.env` or execute the repository's Windows `.venv`. Therefore the official `aliexpress.affiliate.category.get` call and `scripts/validate_catalog_subcategories.py` live run cannot honestly be reported as completed here. Do not infer category IDs or live shelf quality from docs or prior reports. A credential-safe local execution/report handoff is required for that gate.
- The initial Stage 3 source review has begun; no catalog code, callbacks, menu, or UI have been changed yet. «Умный поиск» and «✨ Находки» remain strictly protected.
- Approval record was appended to the Stage 2 proposal in commit `c8bb778b7433ccf8cf469f1c096ceee42be914ff`.


- Stage 3 source-review report added at `docs/catalog-stage-3-validation-2026-11-12.md` (commit `30eed60a751c155ca7f50afe402a0375dae38d8b`). It records the legacy 12-parent/96-shelf implementation, existing offline test coverage, the category.get integration gap, the available live shelf runner, and the exact local validation gates. No code/config/UI changes and no tests/live calls were made. Stage 3 remains open pending local offline and live results plus a credential-safe category-tree diagnostic.


## 2026-11-12 — User clarification and catalog navigation correction

- User clarified that the automotive editorial collection must not look like a rigid marketplace category among discovery-oriented themes. Use the collection title **«Автонаходки и полезные штуки»** (replacing «Интересные автомобильные аксессуары»). Its scope remains unusual/practical driver and vehicle solutions, not a duplicate of the full «Авто и мото» catalogue.
- User reported a live Telegram UX regression: tapping a category caused the current screen to disappear and a new message/window to be sent. User explicitly requires catalog navigation to remain in one message: category/subcategory/back navigation must edit the current message/caption in place.
- Corrected `handlers/discovery.py::show_categories` on this branch. Callback entry now answers the callback and edits the existing text message or photo caption. The catalog cover is sent only when the screen is opened as a fresh command (not as a replacement for an active callback message). Removed the callback-path delete-and-send behavior.
- Change commit: `d308d653230b793e5dcf8c487c303591cf5fb130`.
- User explicitly requested that implementation continue through the specification but **no tests be run**; the user will run tests personally. No pytest, CI workflow, or live validation command was run for this correction.
- Current branch head after the correction: `d308d653230b793e5dcf8c487c303591cf5fb130` (branch `feat/catalog-quality-and-subcategories`).
- Important remaining execution constraint: this GitHub-connected workspace has repository access but not the user's local Windows `.env`, AliExpress credentials, or local runtime. Therefore it cannot perform the credentialed `aliexpress.affiliate.category.get` diagnostic or the existing live 96-shelf runner. Do not invent category IDs, claim live inventory validation, or enable new shelves whose minimum live quality is unconfirmed. Continue source-level implementation only where it can be done safely, and leave those live-dependent gates explicitly pending.
- The approved 15-parent taxonomy and seven disputed decisions remain the accepted target. Existing «Умный поиск» and «✨ Находки» remain protected; do not touch them. Keep existing catalogue/search infrastructure and Option C contracts intact. The thematic collections must have independent configuration, history, routing and diagnostics.


## 2026-11-12 — Catalog implementation started; final collection decisions

- User instructed to execute the approved full catalog/thematic-collections specification.
- The final thematic collection set is exactly four: «Необычные полезные устройства», «Умные решения для дома», «Гаджеты, о которых мало кто знает», and «Полезные мелочи до определённого бюджета» (price tiers: under $5, under $10, under $20). The proposed automotive editorial collection is fully cancelled and must not be renamed or replaced. Automotive goods remain in the ordinary «Авто и мото» catalogue only.
- «Гаджеты, о которых мало кто знает» is an independent collection, not a synonym or automatic merge with «Необычные полезные устройства». The former emphasizes low awareness of a device/technology; the latter emphasizes an unusual solution to an everyday task. Avoid repeatedly showing the same product in both.
- The user requires catalog and thematic-collection navigation to remain in one Telegram message, including parent category, subcategory, back, home, collection entry, and «Показать ещё». Prefer Telegram edit methods; only use a replacement fallback when the message type/API makes editing objectively impossible.
- This implementation session is not permitted to run tests, CI, or live checks. The user will run validation personally.
- The 15-parent taxonomy and seven disputed decisions were explicitly approved earlier. New shelves requiring live inventory proof remain disabled until the user runs the credentialed checks. The GitHub workspace has no local .env/AliExpress credentials; do not invent category IDs or claim live validation.
- Existing «Умный поиск» and «✨ Находки» are protected and must not be changed. Preserve Option C and the existing search service contracts. Do not touch .env, local backups, main, PR #10, or unrelated code.


## 2026-11-12 — Implementation progress and remaining live gates

- The current implementation is on `feat/catalog-quality-and-subcategories`. The approved 15-parent taxonomy is now represented in `config/catalog.py` and existing subcategories have been reparented to the approved internal codes. The 15th (smartphone) domain remains hidden from the category keyboard because it has no verified live shelves yet; the keyboard only shows parent domains with at least one enabled shelf.
- Latest user-supplied live report reviewed: `catalog_subcategory_live_20261112_132853.json` (96 total: 57 full 20-card results, 37 short, 1 provider error, 1 empty). 69 shelves had at least 12 final products, but 20 cards alone did not prove diversity or correct family. The visible menu currently exposes 60 shelves after removing merged/duplicate shelves and withholding weak/misclassified output. A fresh run is still needed after the configuration changes.
- Final editorial collections are exactly four: «Необычные полезные устройства», «Умные решения для дома», «Гаджеты, о которых мало кто знает», «Полезные мелочи до определённого бюджета» with $5/$10/$20 tiers. No automotive thematic collection is configured. Budget results require a confirmed USD price at or below the chosen cap and all collections require at least 12 eligible products.
- Thematic collections have their own callbacks, retrieval config, shared theme-only product history and diagnostics source. They reuse the existing subcategory search/identity pipeline; no parallel marketplace or ShoppingAgent architecture was added. Thematic history is stored under `FLOW_THEMES` and does not use ordinary catalog history or curation novelty history.
- Catalog and thematic navigation is being changed to edit the same Telegram message across category, subcategory, back, home, theme menu, budget tier and product-card browsing. The only replacement fallback is for an edit Telegram cannot perform because of the current message/media type or an API rejection; replacement is sent before old message deletion.
- Added the read-only client method and `scripts/diagnose_aliexpress_categories.py` for the documented Affiliate `category.get` call. It is deliberately not run here; inspect its sanitized report locally before any category ID mapping. No guessed AliExpress IDs were added.
- No tests, CI, or new live API calls were run in this session. User will run validation. Existing live JSON was reviewed only as historical evidence.
- Latest implementation commits include: `d308d653230b793e5dcf8c487c303591cf5fb130` (single-message category entry fix), `d327413b3c7766b9e809e25b7848afa86d2dd174` (thematic search handler), `4a70890d718ed0edbfa2fba2a6d66b375b84dea1` (category diagnostic), and subsequent taxonomy/navigation/history refinements on this branch. Always fetch the current branch head before continuing.


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


## 2026-11-13 — Catalog and thematic collections continuation checkpoint

- Current work remains on feat/catalog-quality-and-subcategories. The catalog source contains 96 unique ordinary shelves (60 base + 36 expanded), with 96 explicit parent assignments across the approved 15 parent domains. The current quality/merge holds hide 37 shelves; 59 are visible across 14 domains. Smartphones remain hidden pending live inventory proof.
- The approved editorial navigation is exactly four collections: «Необычные полезные устройства», «Умные решения для дома», «Гаджеты, о которых мало кто знает», and «Полезные мелочи до определённого бюджета» with $5/$10/$20 tiers. The automotive editorial collection was explicitly removed and must not be recreated or renamed into another automotive theme.
- Thematic searches use dedicated theme configuration and FLOW_THEMES history. They exclude previously shown theme products across the four collections, isolate ordinary catalog/curation history, run MODE_CATEGORY curation, enforce a 12-product minimum after curation, and require a confirmed USD price within the selected ceiling for budget tiers.
- Navigation correction: handlers/catalog_navigation.py is a catalog/theme-only Telegram edit helper. It attempts replacement only for recognized BadRequest reasons that explicitly prevent editing; transient/unclassified errors leave the active message intact. handlers/discovery.py and handlers/thematic_collections.py use this helper. The shared search/feed helper and protected «Умный поиск» / «✨ Находки» paths were not changed by this correction.
- Added a per-theme in-flight guard for item/tier callbacks; «Показать ещё» retains its independent replay/busy guard.
- Full 96-shelf audit data is present in docs/catalog-taxonomy-audit-2026-11-12.md. That document is a historical audit baseline; its recorded HEAD and pre-redesign taxonomy description must not be mistaken for the current branch state. Current implementation status and validation gates are appended to docs/catalog-stage-3-validation-2026-11-12.md.
- No tests, pytest, CI, Telegram, AliExpress, or Gemini calls were run in this continuation. This respects the user's standing instruction not to run validation without explicit permission.
- Final acceptance is still blocked on local credentialed category.get diagnostic, a fresh live 96-shelf validation after the latest configuration, product-level review of all enabled shelves and theme diversity/cross-theme repetition, and user-authorized offline regression tests. No real AliExpress category IDs have been verified or added. Do not claim the full TZ is complete until those gates and the remaining navigation/runtime acceptance are satisfied.


## 2026-11-13 — Final navigation audit continuation

- Catalog/theme message editing is now strict through the entire path: category and theme screens, home/back navigation, first product card, and previous/next card browsing. A dedicated helper only attempts send-then-delete replacement for recognized Telegram BadRequest responses that explicitly prohibit editing. Text/photo media transitions that Telegram cannot perform are handled as the documented exception. Transient/unclassified edit errors leave the existing message in place.
- The shared handlers/search.py delivery layer has an opt-in strict_navigation flag defaulting to false. Only catalog category and thematic collection callers pass true; other search/feed callers retain the previous behavior. handlers/common.py was not changed.
- handlers/start.py catalog_home_callback now uses the catalog-only helper. Callback registration for catalog home, theme menu/tier/item, category, subcategory and category repeat-search was checked in bot/__init__.py.
- Catalog/theme card browsing sessions persist strict_navigation metadata; previous/next navigation does not silently create a duplicate message on a transient Telegram error. Per-theme item/tier callbacks are serialized to prevent duplicate concurrent searches.
- This is not final TZ acceptance. Still required: local credentialed category.get diagnostic, fresh live validation after the latest catalog changes, product-level review of the enabled shelves and cross-theme diversity/repetition, and explicit user permission to run offline regression tests. No tests, CI, syntax checks, Telegram, AliExpress or Gemini requests were run.


## 2026-11-13 — Independent thematic collections main-menu entry

- Added «✨ Тематические подборки» as a dedicated top-level main-menu callback (menu:themes) and routed it directly to the existing thematic collection screen. The category keyboard no longer contains a duplicate thematic entry; catalog and thematic navigation remain separate.
- The main-menu dispatcher already acknowledges callbacks before dispatch. The thematic menu renderer now accepts answer_callback=False for this entry path, preventing a second Telegram callback answer; its existing internal theme:menu route retains the default acknowledgement behavior.
- Updated the help text to explain the independent thematic discovery section. The four approved collections and three USD budget tiers are unchanged; no automotive collection was introduced.
- Files changed in this increment: keyboards/main.py, handlers/start.py, handlers/thematic_collections.py, and this context file.
- No tests, pytest, CI, syntax checks, Telegram, AliExpress, or Gemini requests were run, per the user's explicit instruction. The user will perform all validation after implementation.
- Remaining acceptance gates are unchanged: local credentialed category.get diagnostic, fresh live catalog and thematic collection runs, product-level relevance/diversity/cross-theme repetition review, and user-run offline regression tests. No AliExpress category IDs are claimed or added.


## 2026-11-13 — Live validation tooling and audit matrix continuation

- Added the complete 96-row audit matrix to docs/catalog-taxonomy-audit-2026-11-12.md. Each row now explicitly carries the AliExpress category ID status (unverified, not guessed), recorded overlap group, historical raw/pool/final counts, quality status, loss reasons, recommendation and current 15-parent destination. The original audit and historical baseline remain intact.
- Current official Affiliate documentation confirms category.get response fields (category_id, category_name, parent_category_id) and product.query category_ids support. This confirms the documented API surface, not this application's entitlement or a live response. The signed client method and sanitized local diagnostic script are present; no category ID has been added to production config.
- Added scripts/validate_thematic_collections.py to check the four editorial collections and all three USD budget tiers through the production SearchService and Gemini selector, then run the same category-mode CurationGate as Telegram delivery. The JSON includes product IDs/titles/prices/affiliate link presence, provider state, search/selection/curation stage counts, type distribution, cross-collection duplicates, budget-tier duplicates and price retrieval timestamps. It does not call Telegram or mutate shown-product history.
- Updated scripts/validate_catalog_subcategories.py to accept optional country/currency and to include the final category-mode curation stage in its final counts and diagnostics. Both live runners write reports under reports/ and have not been executed.
- Added the independent top-level menu callback menu:themes; removed the duplicate thematic button from the category list; added direct dispatch and avoided duplicate Telegram callback acknowledgements. Updated the focused UI test source to cover the new main-menu entry and catalog/theme separation; the test was not run.
- Catalog main-menu entry callback acknowledgement was also made single-answer through the catalog-only show_categories path. Protected «Умный поиск» and «✨ Находки» handlers and shared delivery defaults were not changed.
- Files changed in this continuation: keyboards/main.py, handlers/start.py, handlers/thematic_collections.py, handlers/discovery.py, tests/test_handlers_ui.py, scripts/validate_catalog_subcategories.py, new scripts/validate_thematic_collections.py, docs/catalog-taxonomy-audit-2026-11-12.md, docs/catalog-stage-3-validation-2026-11-12.md, and PROJECT_CONTEXT.md.
- No tests, pytest, CI, syntax checks, Telegram, AliExpress, or Gemini calls were run. The user's explicit no-test instruction remains in force.
- Remaining acceptance: user-run scripts/diagnose_aliexpress_categories.py, fresh catalog and thematic live reports with the intended country/currency, manual product-level review, and user-run offline regression suites. Do not claim final acceptance before these are completed.


## 2026-11-13 — Thematic offline contracts and final report update

- Added tests/test_thematic_collections.py with offline-only contracts for the exact four approved editorial collections, the $5/$10/$20 USD tiers, six independent thematic shelves, concrete route/product-type configuration, the FLOW_THEMES history scope, and separate main-menu navigation. The tests were not executed.
- Updated tests/test_handlers_ui.py to check the new top-level theme callback, ensure the catalog menu no longer contains the theme entry, and assert the current catview/catalog:home callback contract. No test command was run.
- scripts/validate_catalog_subcategories.py now mirrors Telegram's final MODE_CATEGORY CurationGate before reporting final counts, including per-stage curation drops and the 12-product minimum status. scripts/validate_thematic_collections.py also uses canonical get_subcategory objects and reports final curated results.
- The live catalog runner supports --country and --currency; the thematic runner supports --country and --currency for non-budget themes and forces USD for budget tiers. Both are live API/Gemini diagnostics and remain unexecuted.
- Current implementation branch remains feat/catalog-quality-and-subcategories. The latest source/documentation changes are recorded in the commit history; fetch the branch head before continuing. No unrelated or protected Smart Search/Findings implementation files were changed by this continuation.
- No tests, pytest, CI, syntax checks, Telegram, AliExpress or Gemini calls were run. The user retains all final validation.


## 2026-11-13 — Main-menu layout preservation checkpoint

- Kept the new standalone «✨ Тематические подборки» entry on its own row immediately after Categories while restoring the exact original pairings/order of all existing main-menu sections. «Умный поиск» and «✨ Находки» remain in their original first two rows and retain their existing callbacks.
- Extended the offline UI callback fake to include an existing Telegram message and assert the thematic menu edits it without sending another message. The thematic contract test now also asserts all original main-menu row pairings. Neither test was executed.
- The latest stage-3 report documents the menu preservation and focused test coverage. No tests, pytest, CI, syntax checks, Telegram, AliExpress or Gemini calls were run.
- Current working branch: feat/catalog-quality-and-subcategories. Latest prior documentation checkpoint: c41fab40c297e3e8d4509282ef51bf350c4e0da4; fetch the current branch head after this context update before continuing.


## 2026-11-13 — Final menu preservation and offline navigation contract checkpoint

- The standalone thematic menu row is placed immediately after the original Categories row. All original main-menu pairings and their relative order are preserved: Search/Photo, Findings/Categories, Favorites/Tracking, Currency/Settings, Hot Discounts/Promo Codes. No existing section was renamed or moved into a different pair.
- Updated offline UI test fakes to supply a callback message, allowing strict edit-in-place navigation to be asserted. Thematic entry must edit the existing message and must not send a replacement. tests/test_thematic_collections.py also checks exact existing menu pairings and the four approved collections.
- The stage-3 handoff now documents the above and the focused offline test command. These test files have not been run.
- Latest pre-context source/report commit: bb0f355e8bc0197728e5548288f7499ce0e75778 (UI fake), preceded by af83a960591e99c5f60462accfed87a158342646 (main-menu pairing contract) and 33d4c99c0c7991f4f3463df19d94c7df4f09887a (preserve original menu rows). Fetch current branch head after this context write.
- No tests, pytest, CI, syntax checks, Telegram, AliExpress or Gemini calls were run.


## 2026-11-13 — Thematic collection retrieval fix

- User reported that thematic collections return the empty-state message. Their live log for theme_unusual_devices showed all three routes repeatedly receiving AliExpress product.query resp_code=405; raw=0, filtered_pool=0, final=0. No products reached the relevance filter. The ordinary Smart Search was separately confirmed working by the user.
- Static source review identified the immediate query-design problem: thematic query_routes were broad editorial phrases (e.g. “clever practical everyday gadgets”, “lesser known electronic gadgets devices”) instead of concrete product names. AliExpress 405 here is treated by the client as a legitimate empty search result, not a general API/authentication failure.
- Created fix branch fix/thematic-collection-retrieval from feat/catalog-quality-and-subcategories at the user's current checkpoint. Updated config/thematic_collections.py so each of the six theme shelves uses three concrete search phrases that exactly match its own declared product_types: jar opener/bag sealer/cleaning brush; smart plug/smart switch/motion sensor; digital microscope/USB tester/endoscope camera; and concrete small-item queries for $5/$10/$20.
- Updated tests/test_thematic_collections.py with an offline contract requiring each route to exactly match a declared product type. Updated docs/catalog-stage-3-validation-2026-11-12.md with diagnosis and validation status.
- No retrieval, relevance, product-type, USD budget, curation, 12-product minimum, history, Telegram navigation, Smart Search, Findings, photo search, or other protected behavior was changed. In particular, no filters were weakened to make the screen appear populated.
- No tests, pytest, syntax checks, CI, Telegram calls, AliExpress calls or Gemini calls were run. The fix is source-level only; the user must perform the live validation after pulling the branch. Current working branch: fix/thematic-collection-retrieval. Latest source/docs commits should be fetched from this branch head before continuing.


## 2026-11-13 — Thematic collections diversity correction

- User live-tested the retrieval correction. Thematic searches returned real AliExpress candidates: «Необычные полезные устройства» raw=295, filtered_pool=46, Gemini-approved=20, Telegram-delivered=19; «Умные решения для дома» raw=287, filtered_pool=33, Gemini-approved/final=17. Retrieval is no longer empty, but live output showed repeated product types/families.
- Static diagnosis in SearchService._finalize_subcategory_selection: the first selection pass enforced adaptive type_cap and limited each detected title family to two primary listings; the second overflow refill ignored type_cap, and the final deferred-family refill could restore later variants from already represented families. English/Russian aliases also counted as separate types.
- Added explicit product_type_groups to all six thematic shelves (three editorial themes plus USD $5/$10/$20 shelves). Thematic aliases map to canonical type keys; ordinary catalog subcategories have no groups and preserve their previous alias-level classification.
- Thematic finalization now stops after the first balanced pass: it does not overflow the per-type cap (maximum six per canonical type) and does not use deferred same-family variants to pad the result. The shelf may return fewer than 20 cards when approved candidates do not support a sufficiently diverse result. No unapproved reserve candidates or invented products are introduced.
- Ordinary catalog subcategories retain their existing refill semantics. The change is gated by explicit thematic product_type_groups. No retrieval routes, product relevance gates, accessory rules, USD ceilings, 12-product minimum policy, Gemini approved-ID boundary, curation history, navigation, Smart Search, Findings, photo search, favorites, tracking, currency settings or Option C behavior were intentionally changed.
- Added offline regression source for canonical alias mapping, non-overlapping alias configuration, thematic type-cap enforcement and no deferred-family padding. No tests, pytest, CI, syntax checks, Telegram, AliExpress or Gemini calls were run. User retains all live validation.
- Working branch: fix/thematic-collection-retrieval. This correction extends PR #12; do not merge until the user reviews live results and explicitly approves.

- Smart-home component/module candidates observed in the live sample were not blanket-excluded by a generic `module` term: some relay/sensor modules are sold as standalone usable products. A broad ban could remove legitimate devices. If fresh live results still show non-consumer components, classify the exact title patterns/product roles before tightening the shelf gate; do not weaken the shared principal-product validator.


## 2026-11-13 — Full thematic retrieval and shelf-isolated history implementation

- Began implementing the user's full thematic-collections TZ on `fix/thematic-collection-retrieval` / PR #12. The PR remains open and unmerged.
- SearchService now composes thematic routes from the configured priority phrases plus one canonical route per `product_type_groups` entry (up to 20 total). `SearchRequest.retrieval_queries` allows 19 alternates; SearchService preserves the original three-route cap for all ordinary catalog subcategories.
- Thematic searches may perform one additional bounded page even if Gemini is unavailable, but only when the first page has not reached the eligible pool target. Ordinary catalog page behavior is unchanged. Per-route result counts are retained for the thematic live diagnostic.
- Thematic shown history now uses `themes:<shelf_code>` as its persistent flow key. Existing SQLite `popular_shown` uniqueness already scopes by user and flow, so no migration is needed. New thematic screens no longer exclude products globally across all shelves. Legacy `FLOW_THEMES="themes"` records are retained, not deleted.
- Removed the thematic 12-product gate. Both initial and “more” thematic handlers show any positive number of validated results and pass `minimum_products=1` to the shared sender; zero remains an honest empty result. Other search callers keep their existing minimum behavior.
- Gemini's thematic selection prompt now includes canonical type groups and explicitly prioritizes cross-type selection, treating aliases/translations in one group as the same type. The deterministic post-selection identity check and existing Gemini-approved-ID boundary remain unchanged.
- Updated the live thematic diagnostic to accept optional internal `users.id` via `--user-id`, read (never write) that user's per-shelf history, compare base vs post-history availability, report route counts, and use a one-product display threshold. Its history-adjusted result is a post-curation estimate; it does not make a second marketplace/Gemini pass.
- Added source-level tests for route coverage/budget, per-shelf history scopes and the 19-alternate SearchRequest bound. No tests, syntax checks, CI, Telegram, AliExpress, Gemini or bot startup were run, as required by the TZ. The user retains local test and live validation.
- Still pending before acceptance: deeper route-level loss diagnostics and source-level verification of the updated branch, full test coverage for the new route/history/display behavior, and the user's local offline/live checks. Do not claim the TZ is complete until those checks pass.


## 2026-11-13 — Diagnostic and regression coverage extension

- SearchOutcome now exposes a measurement-only `products_dropped_by_constraints` count. The thematic live diagnostic includes this as a budget-constraint rejection count alongside post-search price/currency checks.
- The thematic diagnostic retains the raw result count per route, distinct candidates before relevance filtering, duplicate counts across route rows, candidate-to-route provenance, rejection-title samples, finalizer duplicate/family/type statistics, CurationGate stage counts, Gemini selector latency/usage/cost and baseline vs shelf-history availability.
- Added offline test source for all canonical routes running within the finite 20-route budget, empty routes not consuming the extra page, per-user/per-shelf SQLite history isolation and persistence after opening a new Database object, and the removal of the artificial 12-card display threshold.
- No tests, syntax checks, CI, bot, Telegram, AliExpress or Gemini calls were run. These are source changes only; user-run validation remains pending.


## 2026-11-13 — Staged thematic route execution checkpoint

- Thematic routes now execute in priority order. The three configured priority phrases run first; the remaining canonical product-type routes are requested only while the eligible pool is below target or fewer than five canonical types are represented. The search stops when both conditions are met, or when the finite route list is exhausted.
- The optional second page is still bounded to one page and now continues only routes that returned rows on page one. This prevents empty routes from consuming additional Affiliate API requests.
- Added offline regression source for both route exhaustion (all canonical routes are attempted when the pool remains empty) and early stopping (a sufficiently large, diverse pool from the priority route skips all extra phrases). Tests were not executed.


## 2026-11-13 — Type-coverage-aware deeper-page refinement

- A thematic raw pool reaching its numeric target no longer suppresses the one optional deeper page if fewer than five canonical product types are represented. The page loop now checks both pool size and type coverage before stopping.
- The second page still runs only productive page-one routes and remains the only extra page for thematic shelves. It stops once both pool size and type coverage are satisfied, or when the route/page budget is exhausted.
- Added regression-test source for a full but single-type pool to verify that the bounded second page is still used. Tests were not executed.


## 2026-11-13 — Ordinary request-contract isolation

- Corrected the route expansion boundary so the existing `SearchRequest.retrieval_queries` field retains its original `max_length=2`. Thematic shelves use a new dedicated `thematic_retrieval_queries` field with `max_length=19`; SearchService chooses the appropriate field only after resolving the subcategory configuration.
- This keeps ordinary category request validation and the three-total-route behavior unchanged, not merely its runtime slicing. Updated the model regression to assert the separate 19-route thematic field and the original two-alternate ordinary field.


## 2026-11-13 — Parser-schema isolation for thematic routes

- Replaced the temporary public `thematic_retrieval_queries` field with a Pydantic `PrivateAttr`, `SearchRequest._thematic_retrieval_queries`. The 19-alternate thematic route list is now internal to SearchService, excluded from Gemini's SearchRequest schema and excluded from persisted request JSON.
- The public `retrieval_queries` field remains exactly as before (`max_length=2`), and ordinary catalog searches continue using that field. Thematic route metadata is set only by `find_by_subcategory` for `theme_*` shelf codes and is preserved by Pydantic `model_copy` during route execution.


## 2026-11-13 — Adaptive route type-coverage target

- Replaced the fixed five-type route stop with an adaptive coverage target: `min(6, max(3, ceil(configured_type_groups / 3)))`. The six shelves therefore scale search continuation with their declared type breadth.
- This target controls whether to spend the one additional page; it is not a minimum display count. If the route/page budget is exhausted with fewer types, the finalizer returns only validated distinct products.
- Updated the staged-route regression fixture to provide six canonical types for the broad $5 shelf's early-stop case. Tests were not run.


## 2026-11-13 — Gemini candidate context and route-plan reporting

- The thematic-only Gemini candidate payload now includes the deterministic `canonical_product_type` assigned by `subcategory_product_type`, plus `shelf_title` and `shelf_purpose`. The same deterministic identity gate runs again after AI selection. Ordinary subcategory payloads and their previous prompt diversity wording are unchanged.
- The diagnostic reports the complete planned route list separately from the actually executed route counts, includes candidate route provenance, and states the actual thematic route/page budget (maximum 20 routes and two pages).
- Added an offline AI-service regression source verifying the thematic Gemini prompt contains the canonical product type and shelf purpose. No tests or live calls were run.
