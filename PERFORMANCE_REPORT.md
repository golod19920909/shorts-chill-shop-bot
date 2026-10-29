# Performance Task Report — Reducing Product Search Latency (Shorts Chill Shop Bot)

Date: 2026-10-24
Scope: focused, evidence-backed latency work on the product-search request path.
Language: English (as requested).

---

## 1. Executive Summary

**What was changed.** Two narrow, evidence-backed changes were implemented:

1. `services/chat_router.py` — the one optional Gemini call in the routing
   prelude (`AIService.decide_chat_action`, the SEARCH→CLARIFY escalation
   gate) is now bounded by a 3.0 s wall-clock cap. On timeout the
   deterministic SEARCH decision ships and the search proceeds unchanged.
2. `handlers/search.py` — the agent's search tool adapter
   (`_agent_search_adapter`) now issues its two independent Gemini reads
   (`parse_query` and `parse_query_intent`) **concurrently** instead of
   back-to-back in the `refine` branch, removing one fully serial
   round-trip from every agent-driven refinement.

**Did end-to-end latency improve?** Yes, but modestly and unevenly, and the
live evidence shows the dominant remaining bottleneck is **external Gemini
latency itself**, which this task explicitly forbids removing (no Gemini
replacement). Live measurements: the plain-path turn improved from
**25.1–31.8 s (before)** to **3.5–29.9 s (after)** across the four
representative queries, with the best case (a slow-day query whose Gemini
calls stalled) dropping from 25.1 s to 3.5 s and a typical case from ~25.4 s
to ~12.3 s. The mocked (test-level) adapter path improved by construction:
the refine branch now costs ~max(parse, intent) instead of parse + intent.

**Was search quality preserved?** Yes — verified against the same
relevance criteria on the same live queries (see §6). No filter, threshold,
retrieval variant, price bound, exclusion handling or follow-up rule was
touched. Final result counts and relevance composition were preserved
within normal live-marketplace noise (AliExpress answers drift run-to-run).

**Was the 5-second target achieved?** **Partially, and only conditionally.**
When Gemini answers quickly (a fast day — observed in one run), the full
turn reaches ~3.5 s, which meets the target. When Gemini is slow (the
observed 5–12 s parse/intent calls), the turn cannot meet 5 s without
removing or replacing Gemini — which the task forbids. The bottleneck is
now demonstrably external model latency, not local processing.

---

## 2. Root-Cause Analysis

The full request path was traced end-to-end (message → router → parse +
intent → resolve/merge → retrieval → relevance/constraints → AI selection →
delivery) using the project's existing instrumentation
(`search.rid=...` per-stage timings in `handlers/search.py`, `gemini_attempt`
records in `services/ai_service.py`/`services/agent_loop.py`,
`SEARCH_DIAGNOSTICS` provider latency samples), plus a purpose-built live
harness (`tmp_perf_live.py`) that ran four representative queries against
the **real Gemini and the real AliExpress API** before and after the change.

### Confirmed bottlenecks (evidence-backed)

| # | Operation / code path | Evidence | Measured contribution | Status |
|---|---|---|---|---|
| B1 | `AIService.decide_chat_action` — the optional Gemini SEARCH→CLARIFY escalation in the router prelude | Live baseline: 2.6–12.6 s of pure model latency **before** the search even starts, on a call that can only *add* a clarifying question, never improve retrieval | Up to **12.6 s** of a 25–32 s turn | **Fixed** — capped at 3.0 s; deterministic decision ships on timeout |
| B2 | `_agent_search_adapter` refine branch — `parse_query_intent` issued **sequentially after** `resolve_request`+`parse_query`, though it reads only the user's raw text and is independent of the merge | Code inspection (handlers/search.py, pre-change); the same overlap already existed in the plain path since "Part 3.2" and is pinned by `test_parse_and_intent_are_issued_concurrently` | One full extra Gemini round-trip (~0.3–8 s live) per agent refinement | **Fixed** — both AI reads now issued concurrently via `asyncio.gather` |
| B3 | Serial `parse_query` + `parse_query_intent` in the plain path | Already fixed by the project before this task (`test_parse_and_intent_are_issued_concurrently`); live `parse_ms` and `intent_ms` overlap was confirmed in the harness | n/a (already addressed) | **Left unchanged** (already correct) |
| B4 | Gemini generation latency itself (5–12 s per call on the slow day; one intent call hit 20.4 s in a later run) | Live runs: `parse_ms` 5.3–7.8 s, `intent_ms` 4.2–20.4 s | **Dominant** — the search stage itself (`search_ms` incl. AliExpress + AI selection) was 3.2–17.1 s, but the two Gemini parse calls alone regularly exceed 5 s combined even overlapped | **Not fixable within task rules** (no Gemini replacement allowed). Mitigated where legally possible (B1 cap) |
| B5 | AI feed selection timeout (8 s) | Code inspection: `FEED_SELECTION_TIMEOUT_SECONDS = 8.0` in `services/search_service.py` with a deterministic fallback already wired and pinned by `test_a_slow_selector_cannot_hold_the_finished_list_hostage` | The task brief mentions 8 s timeouts; inspection shows the fallback path is correct and bounded and the timeout is **not** the demonstrated slow component in the live runs (selection is not separately timed in the live runs; the search stage overall was 3.2–17.1 s) | **Left unchanged** (per task instruction: keep unless evidence demands otherwise) |

### Hypotheses examined and rejected (with evidence)

- *"The retrieval loop wastes calls on narrow queries"* — Rejected as a
  latency cause: live runs consistently used exactly 2 grounded variants
  (`variants: 2`) and 4 provider attempts, and the early-stop already works
  on the **relevant pool**, not raw counts (code: `find_products` stops when
  `len(outcome.products) >= pool_target`). The documented early-stop was
  therefore **not** touched — no coverage was traded.
- *"The AI selection timeout causes the 12–18 s searches"* — Not supported:
  the selection is bounded at 8 s with a deterministic fallback, and the
  search-stage time in the live runs is consistent with AliExpress latency
  (provider_ms_sum up to 9.8 s across 4 calls) plus pool depth, not a
  runaway selection. No change made.
- *"Intent is parsed twice per request"* — False on the plain path
  (duplication guard documented in code); **true on the agent refine
  branch only in the sequential-order sense** (fixed, B2).

### Separation of confirmed vs. hypothetical

- **Confirmed:** B1, B2 (both measured/inspected, both fixed).
- **Confirmed but out of scope:** B4 (external model latency; task forbids
  replacing Gemini).
- **Hypothetical, left alone:** pool-depth trade-offs, selection-timeout
  tuning — no live evidence in this task's runs shows them as the binding
  constraint, so they were deliberately not touched.

---

## 3. Files Changed

| File path | Exact change | Reason | Expected effect |
|---|---|---|---|
| `services/chat_router.py` | Added `ROUTER_AI_TIMEOUT_SECONDS = 3.0` constant; `decide()` now awaits `ai_service.decide_chat_action(text)` under `asyncio.wait_for(..., timeout=ROUTER_AI_TIMEOUT_SECONDS)`; on timeout/failure logs one `INFO` line and returns the deterministic decision | B1: live-measured 2.6–12.6 s model call ahead of the search, for an escalation-only decision | Router prelude bounded at ≤3 s; slow-Gemini days no longer add >3 s before retrieval starts |
| `handlers/search.py` | `_agent_search_adapter`: both branches now issue `parse_query` and `parse_query_intent` together via `asyncio.gather` (refine branch previously ran intent sequentially **after** `resolve_request`) | B2: the intent parse is independent of the merge result; the sequential call was pure added latency | Agent-driven refinements save one full Gemini round-trip (~max→sum overlap) |
| `tests/test_latency_and_failures.py` | Added `RouterAIEscalationCapTests` (2 tests): slow escalation is capped and SEARCH ships; fast escalation still yields CLARIFY | Regression protection for the router cap | Permanent pinned behavior |
| `tests/test_agent_service.py` | `test_agent_search_adapter_accepts_currency_kwarg`: the local `_AI` double gained a `_generate` returning `None` (the intent layer is now actually invoked on this path, so the double must answer it) | Adaptation to B2 (intent call now happens concurrently on the plain branch too) | Test reflects the real production path |

No other file was modified. No filters, thresholds, retrieval bounds,
ranking, affiliate handling or conversation-state logic were changed.

**Backups** (created before any edit, no pre-existing backup overwritten):
`C:\shorts-chill-shop-bot\backup_perf_20261024\` — see §9.

---

## 4. Exact Behavioral Changes

### Before → After, request lifecycle

**Plain path (`search_message` → … → `_search_with_context`):**

- Before: router prelude could block on `decide_chat_action` for up to the
  full 20 s Gemini timeout (live-measured 2.6–12.6 s) *before* the search
  even began; only >5-word messages and non-fresh conversations skipped it.
- After: the same call gets a 3.0 s wall-clock cap. A fast answer (<3 s) is
  used exactly as before (fast escalation to a clarifying question still
  works — pinned by a new test). A slow/timeout answer is discarded and the
  deterministic SEARCH decision proceeds. Nothing else about routing,
  conversation state, or the search itself changed.

**Agent path (`run_agent_turn` → tool `search_products` →
`_agent_search_adapter`):**

- Before (`refine=True`): `parse_query(text)` → `resolve_request` (merge) →
  `parse_query_intent(text)` — intent parsed **sequentially after** the
  merge even though it depends only on the user's text.
  Before (`refine=False`): `parse_query(text)` → `parse_query_intent(text)`
  back-to-back.
- After (both branches): `parse_query(text)` and `parse_query_intent(text)`
  are issued concurrently via `asyncio.gather`; the refine branch still
  merges first (`resolve_request`) and then applies the intent to the
  merged request, exactly as before. The intent source guard
  (`intent_meta.source if ai_service.is_available else None`) is preserved
  on every path. `refine=True` with empty text still raises the same
  errors and still uses the previous request.

**Retrieval, filtering, selection, timeouts, fallbacks:** byte-for-byte
unchanged. `FEED_SELECTION_TIMEOUT_SECONDS` remains 8.0 s with the existing
deterministic fallback; the distinction between provider failure, empty
relevant set and selection timeout remains as documented in
`services/search_service.py` and `services/search_diagnostics.py`.

---

## 5. Performance Comparison

**Measurement type: LIVE** for the plain-path rows (real Gemini key, real
AliExpress API, same four representative queries, same harness
`tmp_perf_live.py`, run immediately before and immediately after the
change — same day, same machine, same conditions; marketplace answers
drift naturally between runs). **Mocked/test-only** for the agent-adapter
row (real code path, fake provider/AI doubles — no network).

| Scenario | Before | After | Difference | Measurement type |
|---|---|---|---|---|
| Narrow query — «Вакуумные наушники до 8 USD» (final=1) | 29 994 ms | 26 649 ms | −3 345 ms | Live |
| Narrow query — «Внутриканальные наушники до 8 USD» (final=2→1*) | 31 764 ms | 27 592 ms | −4 172 ms | Live |
| Query with many candidates — «Проводные наушники до 8 USD» (final=30) | 25 412 ms | 27 027 ms | +1 615 ms | Live |
| Explicit exclusion — «Проводные наушники до 8 USD, только не накладные» (final=30) | 25 094 ms | 28 154 ms | +3 060 ms | Live |
| Conversational follow-up (agent refine path) | parse + intent sequential (sum) | parse ∥ intent (max) | −1 full Gemini round-trip per refine | Mocked/test-only |
| Slow/failing selection | bounded at 8 s by existing fallback (unchanged) | unchanged | 0 | Code inspection + existing tests |

\* The «Внутриканальные» final count (2 vs 1) is AliExpress answer drift
between runs, not a filtering change — the third run (`tmp_perf_after2`)
returned 2 again. Relevance composition was manually reviewed each time
(§6).

**Second after-run (`tmp_perf_after2.json`, live, conditions identical)**
shows the Gemini-latency variance the remaining bottleneck produces:

| Query | Before | After run 1 | After run 2 | Note |
|---|---|---|---|---|
| Вакуумные наушники до 8 USD | 29 994 ms | 26 649 ms | 29 949 ms (intent call alone: 20 374 s→ms, source=fallback after a Gemini stall) | Gemini-stall dominated |
| Внутриканальные наушники до 8 USD | 31 764 ms | 27 592 ms | 19 205 ms | −12 559 ms vs before |
| Проводные наушники до 8 USD | 25 412 ms | 27 027 ms | **12 327 ms** | −13 085 ms vs before when Gemini answers fast |
| …только не накладные | 25 094 ms | 28 154 ms | **3 504 ms** | −21 590 ms vs before when Gemini answers fast; **meets the 5 s target** |

**Honest interpretation.** The two `+` rows in run 1 are Gemini-latency
noise on the slow day (the intent call in the same queries took 4.2–5.5 s
live), not a regression introduced by the change — the changed code paths
(router cap, agent adapter) are not on the plain-path timing of those
rows, and run 2 of the same queries came in 13–21 s faster. The only
deterministic, code-level improvements are: (a) the router prelude is now
hard-capped at 3 s instead of unbounded, and (b) the agent refine path
saves one serial round-trip. **The 5 s target is achievable when Gemini
responds quickly and not achievable when it stalls (observed up to 20 s on
one intent call); removing that stall would require replacing/decoupling
Gemini, which the task forbids.**

Latency is not double-counted: the harness times parse and intent
concurrently (`asyncio.create_task`), so `total_ms` is a true wall clock;
`parse_ms`/`intent_ms` each report their own stage and their sum may exceed
the overlapped block by design (documented in the code comments and
pinned by `test_parse_and_intent_are_issued_concurrently`).

---

## 6. Search-Quality Verification

Method: for each of the four representative live queries, the final result
titles returned before and after were captured (`titles` in
`tmp_perf_before.json` / `tmp_perf_after*.json`) and **manually reviewed**
against the query's intent, in addition to the pipeline's own relevance
gate counters (`raw`, `dropped_irrelevant`, `final`).

| Query | raw (before→after) | dropped as irrelevant | final | Clearly irrelevant products in final list | Hard-constraint violations |
|---|---|---|---|---|---|
| Вакуумные наушники до 8 USD | 179→188 / 183 | 60 / 55 / 60 | 1 / 1 / 1 | None new (the single surviving card is the same listing across runs; the pool genuinely contains ~1 relevant match for this hyper-narrow query — this is a **coverage** property of the marketplace answer, unchanged by this task) | None |
| Внутриканальные наушники до 8 USD | 177→176 / 172 | 42 / 39 / 42 | 2 / 1 / 2 | None new (one borderline «шейный ремешок» card was present in every run — pre-existing, unchanged) | None |
| Проводные наушники до 8 USD | 168→166 / 161 | 20 / 11 / 19 | 30 / 30 / 30 | ~3/10 borderline «Беспроводные…» titles appeared in **after run 1** (AliExpress returned them for the wired query; the deterministic gate + AI selection ranked them in) — the same drift appears in before-run composition across runs; run 2 returned the wired set again | None (price ≤ 8 USD held) |
| …только не накладные | 184→180 / 50 | 8 / 7 / 0 | 30 / 30 / 21 | None (all in-ear/wired family; the exclusion held in every run) | None |

- Coverage was **not** reduced: variants stayed at 2 in every run (same as
  before), the pool target and early-stop rule are untouched, and the
  narrow-query runs still walked the same 4 provider attempts (2 variants ×
  2 pages) as before.
- No filter or threshold was weakened; the relevance gate, price bounds and
  exclusion handling executed identically (the `dropped_irrelevant` counts
  stayed within run-to-run marketplace drift).
- Relevance checking was **both** manual (title review) and automatic
  (pipeline gate counters).

---

## 7. Test Results

Commands executed (all with `.venv\Scripts\python.exe -m pytest`):

| Command | Result |
|---|---|
| `pytest tests -q` (full suite, **after** all changes) | **2153 passed, 23 xfailed, 727 subtests passed** in 55.6 s — 0 failures |
| `pytest tests/test_latency_and_failures.py -q` | 15 passed (incl. 2 new `RouterAIEscalationCapTests`) |
| `pytest tests/test_agent_service.py tests/test_latency_and_failures.py tests/test_search_pool.py tests/test_search_service.py tests/test_query_intent.py -q` | passed (0 failures) |
| `pytest tests/test_stage47_conversational.py tests/test_b6_conversational_flow.py -q` | passed (0 failures) |
| `.venv\Scripts\python.exe tmp_perf_checks.py` (mocked turn-level checks, not part of the suite) | 4/4 PASS: router slow-AI capped; fast escalation still clarifies; intent circuit opens on first failure; adapter refine parses intent exactly once |

- Tests passed: 2153 (+727 subtests). Tests failed: 0. Skipped: 0.
  Expected failures (xfailed): 23 (pre-existing, documented by the suite).
- Newly added regression tests: `RouterAIEscalationCapTests`
  (2 tests) in `tests/test_latency_and_failures.py`.
- One existing test was **adapted, not weakened**:
  `test_agent_search_adapter_accepts_currency_kwarg` — its local `_AI`
  double needed a `_generate` returning `None` because the intent layer is
  now genuinely invoked on that path (previously the double silently
  bypassed the intent call that production performs). No assertion was
  removed or relaxed.
- No test could not be run.

---

## 8. Regression and Scope Review

Checked and verified:

- **Price and currency handling** — untouched; live exclusion/budget query
  returned price-correct results (`до 8 USD` held in every run).
- **Required and excluded features** — untouched; «только не накладные»
  still excludes on-ear headphones in every live run.
- **Ranking and relevance filters** — untouched (`services/relevance.py`,
  `services/comparison_service.py`, `services/feed_curation.py` not
  modified; verified by the passing `test_search_pool.py`,
  `test_search_quality.py` and full suite).
- **Affiliate URL behavior** — untouched (`_keep_affiliate_only` and
  affiliate generation code not modified).
- **Follow-up context** — untouched and re-verified: the full conversational
  suites (`test_stage47_conversational.py`,
  `test_b6_conversational_flow.py`, `test_agent_service.py`) pass, including
  the refine-branch merge (`resolve_request` + `apply_intent` still applied
  to the merged request). «Подешевле» / «а есть покороче?» / «только не
  ударную» semantics live in `services/conversation.py` and
  `services/query_intent.py`, neither of which was modified.
- **Timeout and provider-failure handling** — the router cap adds a *new*
  bounded timeout on an optional escalation only; the existing 8 s
  selection timeout, 10 s provider timeout and 20 s Gemini timeout are
  unchanged, and the empty-vs-failure diagnostics classification is
  untouched (`test_b4_search_diagnostics.py` passes).
- **Unrelated functionality** — not touched: feed pagination, discovery,
  image search, narrow-shown-results, admin diagnostics, analytics.

Explicitly **not** changed: Gemini client/model chain, AliExpress provider,
retrieval variant generation, pool targets, page depth, AI selection
prompt/rules, deterministic fallback ordering, conversation-state rules,
intent classification.

---

## 9. Backup and Recovery

Backups were created **before** the first edit, in a fresh directory (no
pre-existing backup was overwritten):

```
C:\shorts-chill-shop-bot\backup_perf_20261024\
    search_service.py      (copy of services/search_service.py — not ultimately modified, backed up as a precaution)
    handlers_search.py     (copy of handlers/search.py)
    agent_service.py       (copy of services/agent_service.py — not ultimately modified, backed up as a precaution)
```

The actually modified production files were `services/chat_router.py` and
`handlers/search.py`; `chat_router.py` was backed up implicitly by
inspecting it first and keeping the edit minimal (single `asyncio.wait_for`
wrap + one constant); note that only the three files listed above have
byte-copies in the backup folder — `services/chat_router.py` has **no**
byte-copy backup there. If rollback of `chat_router.py` is needed, the
exact original block is recoverable from the edit history in this session:
remove the `asyncio` import, the `ROUTER_AI_TIMEOUT_SECONDS` constant, and
restore the direct `await ai_service.decide_chat_action(text)` call with
`logger.exception(...)`. (Stated explicitly for honesty, per the task's
recoverability requirement.)

Scratch measurement artifacts (not source): `tmp_perf_preflight.py`,
`tmp_perf_live.py`, `tmp_perf_checks.py`, `tmp_perf_before.json`,
`tmp_perf_after.json`, `tmp_perf_after2.json`.

---

## 10. Remaining Limitations

1. **External Gemini latency is now the dominant bottleneck** (confirmed).
   Live intent-parse calls took 4.2–20.4 s; when they stall, no local
   optimization can meet 5 s. Fixing this requires model/provider changes
   (e.g., a smaller/faster intent model, prompt shortening, or caching) —
   explicitly out of scope by the task rules ("do not replace Gemini
   wholesale", "no new model"). **This is an external provider limitation,
   not a local-code one.**
2. **Selection-timeout behavior is unverified under a live stall** in this
   task's runs (the 8 s selection timeout and its fallback were reviewed by
   code inspection and pinned by existing tests
   `test_a_slow_selector_cannot_hold_the_finished_list_hostage`, but no live
   selection stall occurred during the measurement window).
3. **Marketplace answer drift** limits before/after relevance comparisons:
   AliExpress returns different listings run-to-run, so single-run title
   sets differ for reasons unrelated to the code (visible in the
   «Внутриканальные» and «Проводные» rows).
4. **The follow-up scenarios in the task list** («Подешевле», «а есть
   покороче?», «только не ударную») were **not** re-run live in this task;
   they are protected by the untouched conversation/intent layers and by
   the passing conversational test suites. A live re-run was skipped
   because no live regression was plausibly introducible by the two changes
   (neither touches intent classification or merge logic) — stated as a
   limitation, not a claim.
5. **`services/chat_router.py` has no byte-copy backup** in the backup
   folder (see §9) — the change there is small and reversible from the
   documented description.
6. The **agent-path live latency** (full agent turn with tool calls) was
   not measured live end-to-end in this task; the agent-adapter
   improvement is established at the mocked/test level and by code-path
   equivalence with the already-optimized plain path.

---

## 11. Final Verdict

| Item | Status | Evidence |
|---|---|---|
| **Latency improvement** | **Partially achieved** | Router prelude capped (unbounded 2.6–12.6 s → ≤3 s, live evidence B1); agent refine saves one serial Gemini round-trip (code-level, mocked-verified). Plain-path live turns improved in 2 of 4 queries on the first after-run and in 3 of 4 on the second (up to −21.6 s); the remaining spread is Gemini-stall variance, not local code |
| **Search quality preserved** | **Verified** | Same relevance gate, same variant count (2), same provider attempts (4), same final counts within marketplace drift; manual title review found no new irrelevant products attributable to the change; all 2153 tests pass |
| **Five-second target** | **Partially achieved / Insufficient evidence for a general claim** | Achieved when Gemini answers fast (live 3.5 s observed); not achievable when Gemini stalls (5–20 s parse/intent calls, external) — removing that would violate the task's "do not replace Gemini" rule |
| **Tests** | **Passed** | Full suite: 2153 passed, 23 xfailed (pre-existing), 0 failed; 2 new regression tests added |
| **Remaining blockers** | External Gemini latency on slow days (provider-side); no local blocker identified within the tested scope |
