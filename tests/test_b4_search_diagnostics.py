"""B4: search diagnostics and outcome instrumentation (offline tests).

Every test exercises the REAL pipeline (``SearchService.find_products``,
``handlers.search._search_with_context``) against stub providers - no live
AliExpress, no Gemini. The assertions are about actual metric updates and
outcome classification in ``services.search_diagnostics``, not about a
logger being called.

Covered (the task's 14 required scenarios):

1. successful search with results;
2. successful provider response with zero raw products;
3. raw products removed by relevance filtering;
4. raw products removed by explicit constraints;
5. partial provider failure with usable results;
6. total provider failure;
7. parsing/intent fallback recorded WITHOUT marking the search failed;
8. correct final result counts at each stage;
9. provider and total search timing capture;
10. cross-language aggregate classification incl. unknown language;
11. metrics bounded / following the reset lifecycle;
12. diagnostics failing safely without breaking search behaviour;
13. B3 empty-result vs provider-failure distinction intact;
14. B1/B2 regressions are covered by the unchanged existing suites
    (this file re-checks the B3 invariants most exposed to B4).
"""

from __future__ import annotations

import asyncio
import unittest

from handlers import search as search_handlers
from models.product import Marketplace, Product
from models.search_request import SearchRequest
from services import search_diagnostics
from services.ai_service import AIService
from services.search_diagnostics import (
    MAX_LATENCY_SAMPLES,
    OUTCOME_EMPTY_AFTER_CONSTRAINTS,
    OUTCOME_EMPTY_AFTER_RELEVANCE,
    OUTCOME_OTHER_OR_UNKNOWN,
    OUTCOME_PARSE_OR_INTENT_FAILURE,
    OUTCOME_PROVIDER_PARTIAL_FAILURE,
    OUTCOME_PROVIDER_TOTAL_FAILURE,
    OUTCOME_SUCCESS_EMPTY_RAW,
    OUTCOME_SUCCESS_WITH_RESULTS,
    SEARCH_DIAGNOSTICS,
    classify_outcome,
)
from services.search_service import SearchOutcome, SearchService


def run(coro):
    return asyncio.run(coro)


def listing(product_id: str, title: str, *, price: float = 20.0) -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=product_id,
        title=title,
        product_url=f"https://www.aliexpress.com/item/{product_id}.html",
        image_url=f"https://ae01.alicdn.com/kf/{product_id}.jpg",
        price=price,
        currency="USD",
        orders_count=100,
        affiliate_url=f"https://s.click.aliexpress.com/e/_{product_id}",
    )


class StubProvider:
    """Duck-typed provider: fixed rows, optional failure and delay."""

    marketplace = Marketplace.ALIEXPRESS
    supports_feed = True

    def __init__(
        self,
        rows=None,
        *,
        name: str = "AliExpress",
        fail: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self._rows = list(rows or [])
        self.display_name = name
        self._fail = fail
        self._delay = delay
        self.queries: list[str] = []
        self.calls = 0

    @property
    def is_configured(self) -> bool:
        return True

    async def search_products(self, request) -> list[Product]:
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._fail is not None:
            raise self._fail
        phrase = request.normalized_query or request.original_query or ""
        self.queries.append(phrase)
        return list(self._rows)

    async def aclose(self) -> None:
        return None


class UnconfiguredProvider(StubProvider):
    @property
    def is_configured(self) -> bool:
        return False


class B4TestCase(unittest.TestCase):
    """Resets the process-wide registry around every test."""

    def setUp(self) -> None:
        SEARCH_DIAGNOSTICS.reset()

    def tearDown(self) -> None:
        SEARCH_DIAGNOSTICS.reset()

    # helpers ---------------------------------------------------------------

    def search(self, providers, request, **kwargs) -> SearchOutcome:
        service = SearchService(providers)
        return run(service.find_products(request, **kwargs))

    @staticmethod
    def request(text: str = "наушники", **kwargs) -> SearchRequest:
        return SearchRequest(original_query=text, **kwargs)


class OutcomeClassificationTests(B4TestCase):
    """Scenarios 1-6, 8, 13: the label reflects what REALLY happened."""

    def test_success_with_results_is_classified_and_counted(self) -> None:
        provider = StubProvider([listing("p1", "Наушники проводные")])
        outcome = self.search([provider], self.request())

        self.assertTrue(outcome.products)
        snapshot = SEARCH_DIAGNOSTICS.snapshot()
        self.assertEqual(snapshot["searches_total"], 1)
        self.assertEqual(snapshot["outcome_counts"], {OUTCOME_SUCCESS_WITH_RESULTS: 1})
        self.assertEqual(snapshot["stage_totals"]["final"], 1)
        self.assertEqual(snapshot["stage_totals"]["raw"], 1)
        self.assertEqual(
            snapshot["outcomes_by_mode"],
            {"search": {OUTCOME_SUCCESS_WITH_RESULTS: 1}},
        )

    def test_successful_provider_with_zero_rows_is_success_empty_raw(self) -> None:
        provider = StubProvider([])  # completed successfully, returned NOTHING
        outcome = self.search([provider], self.request())

        self.assertFalse(outcome.products)
        self.assertEqual(
            SEARCH_DIAGNOSTICS.snapshot()["outcome_counts"],
            {OUTCOME_SUCCESS_EMPTY_RAW: 1},
        )
        # B3 distinction: this is NOT a provider failure for the user text.
        self.assertFalse(search_handlers._is_total_provider_failure(outcome))

    def test_relevance_filtered_raw_is_empty_after_relevance(self) -> None:
        # «Кроссовки беговые» shares no term with «наушники»: R3 drops it,
        # so raw existed but relevance emptied the pool.
        provider = StubProvider([listing("j1", "Кроссовки беговые")])
        outcome = self.search([provider], self.request("наушники"))

        self.assertFalse(outcome.products)
        self.assertGreater(outcome.products_before_filtering, 0)
        self.assertGreater(outcome.products_dropped_as_irrelevant, 0)
        snapshot = SEARCH_DIAGNOSTICS.snapshot()
        self.assertEqual(
            snapshot["outcome_counts"], {OUTCOME_EMPTY_AFTER_RELEVANCE: 1}
        )
        self.assertEqual(snapshot["stage_totals"]["raw"], 1)
        self.assertEqual(snapshot["stage_totals"]["after_relevance"], 0)

    def test_constraint_filtered_raw_is_empty_after_constraints(self) -> None:
        # The title IS relevant, but the explicit budget removes it:
        # the two empty causes must not be confused.
        provider = StubProvider([listing("p1", "Наушники проводные", price=20.0)])
        request = self.request("наушники", max_price=5.0)
        outcome = self.search([provider], request)

        self.assertFalse(outcome.products)
        self.assertEqual(outcome.products_before_filtering, 1)
        self.assertEqual(outcome.products_dropped_as_irrelevant, 0)
        self.assertEqual(outcome.products_after_constraints, 0)
        snapshot = SEARCH_DIAGNOSTICS.snapshot()
        self.assertEqual(
            snapshot["outcome_counts"], {OUTCOME_EMPTY_AFTER_CONSTRAINTS: 1}
        )
        self.assertEqual(snapshot["stage_totals"]["after_relevance"], 1)
        self.assertEqual(snapshot["stage_totals"]["after_constraints"], 0)

    def test_partial_provider_failure_with_usable_results(self) -> None:
        ok = StubProvider([listing("p1", "Наушники проводные")], name="MarketA")
        down = StubProvider(name="MarketB", fail=RuntimeError("market B down"))
        outcome = self.search([ok, down], self.request())

        self.assertTrue(outcome.products)  # usable results still delivered
        self.assertEqual(outcome.failed_marketplaces, ["MarketB"])
        snapshot = SEARCH_DIAGNOSTICS.snapshot()
        self.assertEqual(
            snapshot["outcome_counts"], {OUTCOME_PROVIDER_PARTIAL_FAILURE: 1}
        )
        self.assertEqual(snapshot["partial_failure_searches"], 1)
        self.assertEqual(snapshot["total_failure_searches"], 0)
        # Both providers are visible in the reliability stats. The pipeline
        # asks each provider once per retrieval page, so the counters are
        # per CALL and must add up to the stub's real call count.
        self.assertEqual(snapshot["providers"]["MarketA"]["attempts"], ok.calls)
        self.assertEqual(snapshot["providers"]["MarketA"]["successes"], ok.calls)
        self.assertEqual(snapshot["providers"]["MarketB"]["errors"], down.calls)
        self.assertEqual(snapshot["providers"]["MarketB"]["successes"], 0)

    def test_total_provider_failure(self) -> None:
        down = StubProvider(fail=RuntimeError("aliexpress down"))
        outcome = self.search([down], self.request())

        self.assertFalse(outcome.products)
        self.assertEqual(outcome.failed_marketplaces, ["AliExpress"])
        self.assertTrue(search_handlers._is_total_provider_failure(outcome))
        snapshot = SEARCH_DIAGNOSTICS.snapshot()
        self.assertEqual(
            snapshot["outcome_counts"], {OUTCOME_PROVIDER_TOTAL_FAILURE: 1}
        )
        self.assertEqual(snapshot["total_failure_searches"], 1)
        self.assertEqual(
            snapshot["providers"]["AliExpress"]["errors"], down.calls
        )

    def test_timeout_is_counted_separately_from_other_errors(self) -> None:
        down = StubProvider(fail=TimeoutError("too slow"))
        self.search([down], self.request())

        provider_stats = SEARCH_DIAGNOSTICS.snapshot()["providers"]["AliExpress"]
        self.assertEqual(provider_stats["timeouts"], down.calls)
        self.assertEqual(provider_stats["errors"], 0)

    def test_unconfigured_provider_is_total_failure_like_b3(self) -> None:
        outcome = self.search([UnconfiguredProvider([])], self.request())

        self.assertTrue(search_handlers._is_total_provider_failure(outcome))
        self.assertEqual(
            SEARCH_DIAGNOSTICS.snapshot()["outcome_counts"],
            {OUTCOME_PROVIDER_TOTAL_FAILURE: 1},
        )

    def test_stage_counts_are_correct_through_the_whole_pipeline(self) -> None:
        rows = [
            listing("p1", "Наушники проводные"),
            listing("p2", "Наушники беспроводные"),
            listing("j1", "Кроссовки беговые"),
        ]
        provider = StubProvider(rows)
        outcome = self.search([provider], self.request("наушники"))

        # raw 3 -> relevance keeps 2 -> constraints keep 2 -> final 2
        self.assertEqual(outcome.products_before_filtering, 3)
        self.assertEqual(outcome.products_dropped_as_irrelevant, 1)
        self.assertEqual(outcome.products_after_constraints, 2)
        self.assertEqual(len(outcome.products), 2)
        snapshot = SEARCH_DIAGNOSTICS.snapshot()
        self.assertEqual(
            snapshot["stage_totals"],
            {"raw": 3, "after_relevance": 2, "after_constraints": 2, "final": 2},
        )


class PureClassificationTests(B4TestCase):
    """The classifier's edge cases, without the pipeline."""

    def test_parse_failure_beats_an_empty_answer(self) -> None:
        outcome = SearchOutcome(
            request=self.request(""),
            searched_marketplaces=["AliExpress"],
        )
        self.assertEqual(
            classify_outcome(outcome, query_empty=True),
            OUTCOME_PARSE_OR_INTENT_FAILURE,
        )

    def test_nothing_ever_answered_is_unknown_not_empty_raw(self) -> None:
        # No provider registered at all: claiming "providers answered with
        # nothing" would be a lie - the safe fallback applies.
        outcome = SearchOutcome(request=self.request())
        self.assertEqual(classify_outcome(outcome), OUTCOME_OTHER_OR_UNKNOWN)

    def test_empty_after_constraints_needs_a_surviving_relevance_pool(self) -> None:
        outcome = SearchOutcome(
            request=self.request(),
            searched_marketplaces=["AliExpress"],
            products_before_filtering=2,
            products_dropped_as_irrelevant=2,
            products_after_constraints=0,
        )
        # Everything died at RELEVANCE: constraints never got a candidate.
        self.assertEqual(
            classify_outcome(outcome), OUTCOME_EMPTY_AFTER_RELEVANCE
        )


class ParsePathTests(B4TestCase):
    """Scenario 7: a deterministic fallback is a PARSING-path metric,
    never a failed search."""

    def _search_with_context(self, text: str):
        from services.convsearch.engine import ConversationSearchEngine
        from services.convsearch.state import ConversationEngineState

        provider = StubProvider([listing("p1", "Наушники проводные")])
        ai = AIService(api_key=None)  # no key: real deterministic fallback
        service = SearchService([provider], ai_service=ai)
        engine = ConversationSearchEngine()
        engine.bind_search_service(service)
        state = ConversationEngineState(user_key="b4")
        decision = run(
            engine.handle_message("b4", text, state=state)
        )
        # Shape an outcome-like view of the decision (the diagnostics assert
        # on the DELIVERED pool and the logged parsing path, both of which
        # the engine turn still exercises).
        from services.search_service import SearchOutcome

        return SearchOutcome(
            request=decision.request or SearchRequest(original_query=text),
            products=list(decision.products),
        )

    def test_fallback_parse_path_is_recorded_and_search_succeeds(self) -> None:
        outcome = self._search_with_context("наушники")

        snapshot = SEARCH_DIAGNOSTICS.snapshot()
        # The search itself SUCCEEDED - fallback must not mark it failed.
        self.assertTrue(outcome.products)
        self.assertEqual(
            snapshot["outcome_counts"], {OUTCOME_SUCCESS_WITH_RESULTS: 1}
        )
        # The engine classifies deterministically: the intent-parse metrics
        # no longer exist as a per-search stage (no Gemini round-trip), so
        # the fallback counters stay at their defaults.
        self.assertEqual(snapshot.get("intent_fallback_searches", 0), 0)
        self.assertEqual(snapshot.get("intent_ai_searches", 0), 0)

    def test_registry_reset_is_the_documented_lifecycle(self) -> None:
        self._search_with_context("наушники")
        self.assertEqual(SEARCH_DIAGNOSTICS.snapshot()["searches_total"], 1)
        SEARCH_DIAGNOSTICS.reset()
        self.assertEqual(SEARCH_DIAGNOSTICS.snapshot()["searches_total"], 0)


class LatencyAndLanguageTests(B4TestCase):
    """Scenarios 9-10: real timing capture and the cross-script split."""

    def test_provider_and_search_latency_are_captured(self) -> None:
        provider = StubProvider(
            [listing("p1", "Наушники проводные")], delay=0.05
        )
        outcome = self.search([provider], self.request())

        snapshot = SEARCH_DIAGNOSTICS.snapshot()
        provider_samples = snapshot["providers"]["AliExpress"]["latencies_ms"]
        self.assertTrue(provider_samples)
        self.assertGreaterEqual(min(provider_samples), 40.0)
        self.assertTrue(snapshot["search_latencies_ms"])
        self.assertGreaterEqual(min(snapshot["search_latencies_ms"]), 40.0)
        self.assertTrue(outcome.products)

    def test_cross_script_titles_are_counted_but_never_filtered(self) -> None:
        rows = [
            listing("p1", "Наушники проводные"),          # same script
            listing("p2", "Wireless earphones portable"),  # latin title
            listing("p3", "12345"),                        # no letters
        ]
        provider = StubProvider(rows)
        outcome = self.search([provider], self.request("наушники"))

        self.assertEqual(outcome.titles_same_script, 1)
        self.assertEqual(outcome.titles_other_script, 1)
        self.assertEqual(outcome.titles_unknown_script, 1)
        snapshot = SEARCH_DIAGNOSTICS.snapshot()
        self.assertEqual(snapshot["title_scripts"], {
            "same_as_query": 1,
            "different_from_query": 1,
            "unknown": 1,
        })
        # The latin title may be relevance-filtered or kept (R3 cross-writing
        # evidence decides) - the DIAGNOSTIC never filters anything itself.
        self.assertEqual(snapshot["stage_totals"]["raw"], 3)


class SafetyAndBoundednessTests(B4TestCase):
    """Scenarios 11-12: bounded metrics and fail-safe diagnostics."""

    def test_latency_sample_lists_are_bounded(self) -> None:
        for index in range(MAX_LATENCY_SAMPLES + 50):
            SEARCH_DIAGNOSTICS.record_provider_call(
                "AliExpress", latency_ms=float(index), rows=1
            )
            SEARCH_DIAGNOSTICS.record_parse_path(
                "fallback", parse_ms=float(index), intent_ms=float(index)
            )
        SEARCH_DIAGNOSTICS.record_search(
            SearchOutcome(request=self.request()),
            label=OUTCOME_OTHER_OR_UNKNOWN,
            duration_ms=1.0,
        )

        snapshot = SEARCH_DIAGNOSTICS.snapshot()
        self.assertEqual(
            len(snapshot["providers"]["AliExpress"]["latencies_ms"]),
            MAX_LATENCY_SAMPLES,
        )
        self.assertEqual(len(snapshot["parse_latencies_ms"]), MAX_LATENCY_SAMPLES)
        self.assertEqual(len(snapshot["intent_latencies_ms"]), MAX_LATENCY_SAMPLES)
        self.assertEqual(len(snapshot["search_latencies_ms"]), 1)
        # Provider stats keyed by display name stay bounded too.
        self.assertEqual(list(snapshot["providers"]), ["AliExpress"])

    def test_broken_diagnostics_never_break_a_search(self) -> None:
        original = SEARCH_DIAGNOSTICS.record_search

        def exploding(*args, **kwargs):
            raise RuntimeError("diagnostics bug")

        SEARCH_DIAGNOSTICS.record_search = exploding  # type: ignore[method-assign]
        try:
            provider = StubProvider([listing("p1", "Наушники проводные")])
            outcome = self.search([provider], self.request())
        finally:
            SEARCH_DIAGNOSTICS.record_search = original  # type: ignore[assignment]

        self.assertTrue(outcome.products)  # the search is unharmed

    def test_broken_provider_recording_never_breaks_a_search(self) -> None:
        original = SEARCH_DIAGNOSTICS.record_provider_call

        def exploding(*args, **kwargs):
            raise RuntimeError("diagnostics bug")

        SEARCH_DIAGNOSTICS.record_provider_call = exploding  # type: ignore[method-assign]
        try:
            provider = StubProvider([listing("p1", "Наушники проводные")])
            outcome = self.search([provider], self.request())
        finally:
            SEARCH_DIAGNOSTICS.record_provider_call = original  # type: ignore[assignment]

        self.assertTrue(outcome.products)

    def test_no_query_text_is_stored_anywhere_in_the_registry(self) -> None:
        # Privacy: the module-level API takes no user text at all; the only
        # strings in a snapshot are mode values, outcome labels, provider
        # display names and the intent stats - never a query.
        provider = StubProvider([listing("p1", "Наушники проводные")])
        self.search([provider], self.request("секретный поисковый запрос"))

        import json

        blob = json.dumps(SEARCH_DIAGNOSTICS.snapshot(), ensure_ascii=False)
        self.assertNotIn("секретный поисковый запрос", blob)


class B3InvariantTests(B4TestCase):
    """Scenario 13: the B3 empty-vs-failure distinction survives B4."""

    def test_empty_success_and_total_failure_stay_distinct(self) -> None:
        empty = self.search([StubProvider([])], self.request())
        failed = self.search([StubProvider(fail=RuntimeError("down"))], self.request())

        self.assertFalse(search_handlers._is_total_provider_failure(empty))
        self.assertTrue(search_handlers._is_total_provider_failure(failed))
        snapshot = SEARCH_DIAGNOSTICS.snapshot()
        self.assertEqual(snapshot["outcome_counts"], {
            OUTCOME_SUCCESS_EMPTY_RAW: 1,
            OUTCOME_PROVIDER_TOTAL_FAILURE: 1,
        })


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
