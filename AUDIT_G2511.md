# G25.1 — Systemic Read-Only Audit of the AI Shopping Assistant

Audit date: 2026-10-14 · Working dir: `c:\shorts-chill-shop-bot` · No Git repository (verified).
Method: offline code tracing + targeted offline execution of the REAL production
services (`parse_query`, `parse_query_intent`, `apply_intent`, `resolve_request`,
`deterministic_merge`, `query_variants.product_query_variants`, `filter_relevant`,
`sort_products`, `chat_router.classify`) using `AIService(api_key=None)` — i.e. the
deterministic fallback parser that runs whenever Gemini is unavailable, quota-blocked,
timed out, or the intent circuit is open. No live Gemini/AliExpress calls, no bot
instance, no production/test/config mutation. Temporary probes were removed (verified).

Real-API evidence available from prior stages is *referenced*, not re-fetched. All
product listings below are SYNTHETIC; they exercise the real decision logic offline.

---

## A. Executive summary

The search system is a well-structured, honestly-degrading pipeline, but it is **not
ready for a major feature push without first hardening three systemic blockers**:

1. **AI understanding is duplicated and uncoordinated** — the same query text is parsed
   by two independent Gemini calls (`AIService.parse_query` *and*
   `query_intent.parse_query_intent`), each with its own 20s×2 timeout, plus a third AI
   call for feed selection. Cost, latency and failure surface are multiplied for no gain.
2. **The deterministic fallback that runs on ANY AI degradation collapses product
   identity and multi-turn constraints.** Reproduced offline: a cordless-screwdriver
   search followed by "two batteries, ≤ $40" then "cheaper alternative" loses the
   screwdriver, the budget, and the battery attribute entirely in the fallback path.
   Because Stage 56 confirmed intermittent AI/circuit timeouts, users hit this degraded
   path under normal load, so output quality is bimodal (good with Gemini, poor without).
3. **The intent circuit breaker never recovers within a process.** `_AI_CIRCUIT_OPEN`
   opens on the first hard AI failure and is reset only inside tests/diagnostics, never in
   any production path — so one transient hiccup degrades the whole live process to
   fallback until restart.

Ranking itself (`sort_products/_sort_key`) is sound and bounded, but **role separation is
brittle**: reproduced offline that a compatibility-only *phone case* can outrank genuine
*bicycle phone mounts* when the compatibility word ("phone") leads the title and the
genuine product leads with an adjective/synonym ("Aluminum … Holder"). This contradicts
the Stage 64 real-pool conclusion and shows Stage 62's role logic is only reliable in a
narrow slice of inputs.

These are systemic, not per-query. A single grouped remediation (below F) addresses them.

---

## B. Verified architecture (real pipeline and responsibilities)

```
Telegram TEXT (!command)
  └─ handlers/search.py::search_message
       ├─ services/chat_router.py::decide            (deterministic SEARCH/CLARIFY/COMPARE/EXPLAIN/RESET;
       │     optional Gemini decide_chat_action ONLY escalates short-vague fresh msgs to CLARIFY)
       └─ _search_with_context (handlers/search.py:211)
            1. AIService.parse_query(user_text)                 → SearchRequest      [AI #1]
            2. conversation.resolve_request(previous, parsed, raw, ai)               [AI #2, follow-ups only:
                 → AIService.merge_search_requests OR deterministic_merge]            validated + drift-guarded]
            3. query_intent.parse_query_intent(text, ai)        → StructuredQueryIntent [AI #3, module circuit]
               + query_intent.apply_intent(effective, intent, refined, intent_source)
            4. SearchService.find_products(request)  (services/search_service.py:408)
                 ├─ query_variants.product_query_variants  (≤ MAX_PRODUCT_QUERY_VARIANTS = 3)
                 ├─ pages 1..SEARCH_MAX_PAGES (default 3): provider.search_products(phrase)  [AliExpress call(s)]
                 │     page 1 sends all variants; deeper pages re-ask primary only; early-stop when pool full
                 └─ _collect (search_service.py:713)
                      deduplicate_products → filter_relevant → filter_products
                      → exclude(seen) → sort_products → feed_curation.spread_similar
                      → _cut_with_numeric_protection(limit = pool_target)
                 └─ apply_feed_selection  [AI #4]
                      → AIService.select_feed_candidates(pool, ≤20)   else deterministic cap 30
       └─ _answer_over_shown → ProductAssistant.answer  (COMPARE/EXPLAIN/ANSWER; [AI] per question)
  └─ send_search_results → CurationGate.curate(mode=search) → first product card
```

Key bounds confirmed in code:
- `MAX_PRODUCT_QUERY_VARIANTS = 3`, `MAX_SEARCH_PAGES = 3` (default), `SEARCH_MAX_PAGES` env 1..10
  (`config/settings.py:70`). AliExpress `PAGE_SIZE = 50`.
- `RAW_POOL_TARGET = 80` (pool target only when an AI selector exists, else `DEFAULT_MAX_RESULTS = 30`).
- `AI_FEED_RESULTS = 20`; numeric-spec pool rescue bounded (`_cut_with_numeric_protection`).

Boundary counting the code exposes: `raw (≤80/pool) → deduped → relevance-filtered →
constraint-filtered(filter_products) → seen-excluded → ranked → spread_similar → pool cut
→ AI/deterministic selection (≤20/≤30) → curation gate → cards`.

---

## C. Benchmark results

Legend: **[OFFLINE-FALLBACK]** = executed against `AIService(api_key=None)` (the real
deterministic fallback path). **[SYNTHETIC-RANK]** = real relevance/sort logic over
synthetic listings. No live API was used; live behavior of the Gemini paths is NOT claimed.

### Scenario A — Multi-turn screwdriver [OFFLINE-FALLBACK]
Inputs: "I need a cordless screwdriver for furniture assembly and woodworking." →
1) "Find one with two batteries, but no more than $40." → 2) compare → 3) explain →
4) "Find a cheaper alternative while keeping the original requirements."

Observed (deterministic merge / fallback parse):
- turn0 query = `i need a cordless screwdriver for furniture assembly and woodworking`, intent_product_type = `None`.
- turn1: `max_price=40, currency=USD` **preserved**; `refined=False`;
  query collapsed to `one with two batteries but`; intent_product_type=`None`; required_attrs=`[]`.
  → **product identity (screwdriver) and the "two batteries" attribute are LOST**; the
  merge could not attach the follow-up as a feature-only refinement because the fallback
  parser leaves no product noun, and `_names_new_product` returned `"one"`.
- turn4: `query=cheaper alternative while keeping the original requirements`,
  `max_price=None`, required_attrs=`[]`. → **screwdriver + budget both gone**; a degenerate
  new search. Variants after turn1 contain no screwdriver token.

**Outcome: FAIL (fallback path).** Budget survives one hop; product identity and the battery
attribute never survive; "cheaper alternative keeping requirements" fully resets. Limitation:
this is the AI-DOWN path. The Gemini-present path may merge better but was NOT executed live
(no keys/budget/interference permitted). Because Stage 56 makes the fallback path recurrent,
this is the user-visible risk.

### Scenario B — Descriptive requests [OFFLINE-FALLBACK]
- tire pressure / crimp multilayer pipes / claw tool / cat cabinet / bike mount:
  `intent_product_type` becomes the **whole descriptive sentence** (no canonical class);
  variants merely strip function words (e.g. bike → `bicycle phone`, losing `mount`/`vibration`);
  `intent_compatibility=[]`. Product class only weakly inferable from stray noun tokens.
**Outcome: PARTIAL (fallback)** — recognizable nouns present, but no product-class identity
and descriptive specifiers dropped. Gemini path not executed.

### Scenario C — Bicycle phone mount, mixed pool [SYNTHETIC-RANK]  ⚠ contradicts Stage 64
English pool, clean AI-shaped intent `product_type="bicycle phone mount"`:
| id | title | tier | verdict |
|----|-------|------|---------|
| 1 | Phone Case for iPhone 15 Pro Shockproof Bicycle Compatible Cover | **2** | **exact** |
| 0 | Aluminum Alloy Bicycle Phone Holder Anti-vibration… Mount | 1 | unverified |
| 2 | Universal Bike Phone Mount Handlebar… | 1 | unverified |
| 3 | Silicone Phone Grip Strap for Bike… | 1 | unverified |
| 4 | Waterproof Bike Handlebar Bag… Pouch | 1 | unverified |

**The compatibility-only phone CASE ranks #1 as "exact" ABOVE the two genuine mounts.**
Reproduced both with the fallback whole-phrase intent AND with a clean intent.
Root cause (verified by direct probe): `query_noun_terms(request,"latin") = ()` is EMPTY for
this request (nouns derive from `original_query/keywords/normalized_query`, not from
`intent_product_type`; after adjective/function filtering nothing usable remains for this
English phrasing). So `classify_product` takes the cross-writing branch; the case leads with
`phone` (a request noun via romanized cross-match) → "exact"/tier 2, while the genuine mount
leads with `aluminum` (adjective) or `bike` (synonym, prefix-misses `bicycle`) → "unverified"/tier 1.
Russian pool check: genuine `Велокрепление для телефона` correctly tier 2, but the bike **bag**
`Велосумка…для телефона` also got tier 2 "exact" while the genuine synonym
`Держатель телефона на руль` only tier 1 — **role separation is inconsistent**.
**Outcome: FAIL for accessory/exact separation in these wordings.** This is exactly the
Stage 64 residual ("different wording can fall into the same tier") — and here it inverts the
rank toward an accessory.

### Scenario D — Categories with constraints [OFFLINE-FALLBACK]
- USB-C hub 4K HDMI ≤$25 → max_price=25 USD OK; required_attrs=`['4 k','usb-c','25']`
  (**budget number `25` leaked into required_attributes**).
- yoga mat 8mm → 8mm NOT captured (required_attrs=`[]`), type=whole sentence.
- dash cam 1080p → 1080p NOT captured.
**Outcome: PARTIAL** — budget parsed, but numeric-spec capture is inconsistent and the budget
integer contaminates required attributes in the fallback path.

### Scenario E — Ambiguity / clarify (deterministic) [OFFLINE]
- "посоветуй что-нибудь" → **CLARIFY** (correct).
- "беспроводные наушники" → SEARCH (correct; no over-clarification).
- "a way to prevent a cat from opening a cabinet" → SEARCH (proceeds; no invented requirement).
- "power bank 20000" → SEARCH.
**Outcome: PASS** — clarify is conservative, does not fire per-request, and does not invent

---

## D. Findings with evidence

| # | Sev | Location | Evidence | Proven? | User-visible consequence |
|---|-----|----------|----------|---------|--------------------------|
| F1 | High | `handlers/search.py:211` `_search_with_context`; `services/ai_service.py:470`; `services/query_intent.py:940` | Same text parsed by TWO Gemini calls (parse_query + parse_query_intent) plus select_feed_candidates | Proven (code) | 3+ AI calls/search; doubled latency & failure surface |
| F2 | High | `services/query_intent.py:197,912` `_AI_CIRCUIT_OPEN` | Opens on first hard failure; `reset_ai_state` called only in tests (grep: no production caller) | Proven (code+grep) | One transient AI failure → whole process stuck on fallback until restart |
| F3 | High | `services/conversation.py:584` `deterministic_merge`; fallback `parse_query` | Scenario A: screwdriver/budget/attribute lost across turns | Proven offline (fallback path) | Follow-up "with two batteries ≤$40" then "cheaper" searches garbage; silent context loss |
| F4 | High | `services/relevance.py:1222` `classify_product`, `query_noun_terms:503`, `noun_terms:484` | Scenario C: phone case tier-2 exact outranks genuine mounts; Russian bag also tier-2 while synonym held tier-1 | Proven offline (synthetic) | Accessory/compatibility item shown as top "exact" match; genuine product demoted |
| F5 | High | `services/ai_service.py:1080` `asyncio.timeout(20)`, chain fast+fallback | 20s per model × 2 models × up to 3 semantic ops ≈ up to ~2 min AI budget/message in degraded mode | Proven (code) | Long perceived hangs / budget exhaustion under AI trouble |
| F6 | Med | `services/ai_service.py` parse_query→required_features; intent budget | Scenario D: `25` leaked into required_attributes; 8mm/1080p not extracted in fallback | Proven offline (fallback) | Wrong filtering / missed specs when AI degraded |
| F7 | Med | `services/query_variants.py` `_noun_phrases`/core pair | Descriptive specs dropped from variants (bike → `bicycle phone`, no `mount`) | Proven offline | Thin retrieval for purpose-described requests in fallback |
| F8 | Low | `services/comparison_service.py:426` `_sort_key` | Tier-first ordering is correct and bounded | Proven (code) — non-finding | (none) |

Root causes established: F1/F5 yes; F2 yes; F3 yes (fallback loses product identity because
fallback has no productive noun and `_names_new_product` mis-fires); F4 yes (empty
`query_noun_terms` + leading-adjective/synonym mismatch + romanized cross-match).

---

## E. Systemic causes

- **SC-1 Split, redundant AI understanding.** `parse_query` + `parse_query_intent` +
  `select_feed_candidates` each independently parse/judge the same input with independent
  timeouts and independent failure modes (F1, F5). Fix = one coordinated parse feeding all
  downstream consumers.
- **SC-2 Fallback is not behaviour-preserving for identity/constraints.** When AI is
  degraded, the deterministic layer (parse + deterministic_merge + fallback intent) drops
  product identity, budget cohesion and required attributes (F3, F6, F7). Since Stage 56
  makes this path recurrent, quality is bimodal. Fix = make the fallback carry a minimal
  product-class + constraint spine (not full NLU) and make merge attach features to the
  previous product even without a productive noun.
- **SC-3 No AI recovery model.** Circuit breaker is write-once per process with no half-open
  probe and no production reset (F2). Fix = add a bounded cooldown/half-open probe; keep
  `reset_ai_state` for tests only.
- **SC-4 Role separation relies on literal leading-noun matching, not a product-role model.**
  `classify_product`'s exact/mentioned/tier depends on the leading title noun literally
  matching a request noun; compatibility words, adjectives, and synonyms break it (F4).
  Fix = a small, explainable product-role/compatibility classifier (canonical class +
  "is device vs for-device") applied before the word-position heuristic.

Distinguished from symptoms/resolved: the numeric-spec **pool rescue** and **curation caps**
are working safeguards (non-issues). Stage 53's deterministic-fallback-after-AI-reject,
Stage 55's alternatives-vs-quantity split, and Stage 57/59's preserved parser query are
implemented and green in tests (60/60 passed offline) — do not re-open without contradicting
evidence.

---

## F. Prioritized remediation plan (logically grouped; do NOT fix per-query)

1. **Unify AI understanding (SC-1/F1/F5).** Make `parse_query_intent` the single structured
   understanding step and have `parse_query` reuse it (or merge into one call that yields
   both `SearchRequest` and `StructuredQueryIntent`). Keep one timeout governance across the
   chain; stop issuing a second parse of the same text. Expected: −1 Gemini call/search,
   consistent intent vs request, bounded latency. Depends on nothing; unblocks everything.
2. **Make the deterministic fallback carry a minimal constraint spine (SC-2/F3/F6/F7).**
   Add a compact, language-agnostic product-class lexicon + canonicalization so the fallback
   keeps a product noun, the budget inside `max_price` (never `required_attributes`), and
   numeric specs; change `deterministic_merge` so a feature-only follow-up attaches to the
   previous product even when the follow-up has no productive noun (guard on "price/number/
   feature", not "new product noun"). Expected: multi-turn survives AI-down. Depends on #1
   for the intent side.
3. **Add AI recovery (SC-3/F2).** Replace the write-once circuit with a cooldown + single
   half-open probe; expose it for the admin panel. Do not touch timeout values. Expected:
   the process self-heals instead of staying degraded. Independent.
4. **Introduce a minimal product-role model (SC-4/F4).** Before the leading-noun heuristic,
   classify each candidate as *device vs for-device/accessory/compatibility* using an
   explainable rule set (a small class registry + "for/of/…compatible" intro detection), and
   demote compatibility-only items below genuine products regardless of which noun leads.
   Preserve Stage 62's "don't globally reject accessories". Expected: phone-case-vs-mount
   inversion fixed; consistent tiers. Depends on #2's canonical class data.

Order 1 → 2 → 4 (1 unblocks 2/4; 3 independent, can go anytime).

---

## G. Common acceptance benchmark (measurable; baseline from observed results)

Baseline from this audit: Scenario A FAIL, C FAIL (fallback/synthetic), D PARTIAL, E PASS,
B PARTIAL; ≥3 AI calls/search; fallback loses product identity.
Pass/fail for the next phase (offline-first + a bounded live sample):

- **Correct product identity:** after turn1/turn4 of Scenario A, the effective
  `normalized_query`/`intent_product_type` still names *screwdriver* (fallback AND Gemini
  paths). Currently FAIL.
- **Preservation of budget & required attributes:** `max_price ≤ 40 USD` stays set; "two
  batteries" appears in `required_attributes` (not budget); budget integer NOT in
  required_attributes (F6). Currently FAIL.
- **Relevant final cards (Scenario C):** genuine mounts occupy tier ≥ the phone case's tier;
  no compatibility-only item ranks strictly above a genuine product. Currently FAIL.
- **Correct product-role ranking (C/D):** for a fixed representative fixture set (bike mount,
  drill battery, phone mount, SSD, …), all genuine products rank above accessories. Currently
  FAIL. (Set the set size as a fixed count, not a percentage.)
- **Multi-turn context retention (A):** product + budget + an attribute survive ≥4 turns.
  Currently FAIL.
- **Bounded API & runtime:** ≤2 Gemini calls per new search, ≤1 per follow-up, ≤1 per
  COMPARE/EXPLAIN; worst-case AI wall-clock per message ≤ an agreed cap (e.g. ≤30s),
  AliExpress calls ≤ variants×pages within `SEARCH_MAX_PAGES`. Currently 3+ calls, ~2min
  worst case → FAIL until #1/#3 land.
- **Predictable fallback & recovery:** with AI forced down, outcomes ≥ stated baseline AND
  circuit self-recovers (half-open) without restart. Currently FAIL.
- **Regression coverage:** unit tests for each criterion above run offline (like the 60/60
  already green) and gate merges.

No arbitrary pass percentages: each criterion is a concrete boolean over the fixture set;
the baseline above is the observed starting point.

---

## H. Explicit non-findings and limitations

Checked and satisfactory:
- Deterministic clarify (Scenario E) is conservative; no over-clarification, no invented requirements.
- `sort_products/_sort_key` tier-first ordering is correct and bounded (F8 non-finding).
- Bounds exist for variants/pages/pool/feed cap; numeric-spec **pool rescue** protects correct specs at the cut.
- Prior stages 53/55/57/59/62 tests pass offline (60/60) — treat as resolved; do not re-investigate absent new evidence.

Not reproduced / unverified:
- **Live Gemini behavior of every scenario** (parse quality, merge quality, feed selection,
  decide_chat_action) — NOT executed (no keys/budget/isolation permitted). All Gemini-path
  outcomes here are code-traced; the "good" branch is assumed per prior live stages, not re-proven.
- **Live AliExpress retrieval** — not executed; raw-result rankings assume prior Stage 64 pool.
- **End-to-end Telegram rendering** — traced, not exercised (running bot not touched). The
  gap between parser output and final rendered cards was traced through code
  (`send_search_results` → `CurationGate.curate` → card) but not executed live.
- **Whether the Gemini-available Scenario A merge actually preserves the screwdriver** — unknown
  (fallback FAIL is proven; AI-present path is not).
- **Whether `query_noun_terms` is empty for real Russian marketplace phrasing** — verified for
  the synthetic English/Cyrillic phrasings used; broader lexicon behavior not exhaustively mapped.

---

## I. Cleanup confirmation

- No production code, tests, config, dependencies, DB, or runtime settings were modified.
- No packages installed; no env vars changed; no bot started/stopped; no concurrent probes.
- No secrets exposed. No Git repo present (confirmed).
- Temporary probes lived only in `tmp_g2511_audit/` and were **removed and verified**
  (`ARTIFACT_REMOVED`; no `tmp_g2511*` remains; no lingering `python` processes).
- This report file is the sole audit deliverable created; it changes no runtime behavior.



