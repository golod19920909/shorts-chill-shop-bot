"""Tests for ``services.search_service``.

The search service is the piece that must guarantee:
- only registered providers are queried, never anything else;
- one failing (or unconfigured) marketplace never breaks the search;
- numeric filtering and sorting happen programmatically, not in the LLM;
- results come only from providers - never fabricated locally.

Everything here uses stub providers: no network, no credentials.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

import httpx

from config.settings import MarketplaceCredentials
from models.product import Marketplace, Product
from models.search_request import SearchRequest, SortPreference
from services.ai_service import AIService
from services.marketplaces.base import MarketplaceProvider
from services.search_service import SearchService

EMPTY_CREDENTIALS = MarketplaceCredentials(None, None)
CONFIGURED_CREDENTIALS = MarketplaceCredentials("key", "secret")


def make_product(
    marketplace: Marketplace,
    product_id: str,
    price: float | None,
    **overrides,
) -> Product:
    """A listing shaped like a real AliExpress answer for this project.

    The product text is requested in Russian (the provider asks for
    ``target_language=RU``) and the URL is a real item link shape, so the
    fixtures also exercise the relevance rules the live bot applies.
    """

    data = {
        "marketplace": marketplace,
        "product_id": product_id,
        "title": f"Товар {product_id} из каталога",
        "product_url": f"https://www.aliexpress.com/item/{product_id}.html",
        # Every real AliExpress answer carries a main image; the G24 feed
        # additionally REQUIRES one (a card without a picture cannot be
        # shown), so the fixture provides it like the live API does.
        "image_url": f"https://ae01.alicdn.com/kf/{product_id}.jpg",
        "price": price,
        "currency": "USD",
    }
    data.update(overrides)
    return Product(**data)


class StubProvider(MarketplaceProvider):
    """A provider that returns pre-set data or raises on demand."""

    def __init__(
        self,
        marketplace: Marketplace,
        display_name: str,
        products: list[Product] | None = None,
        credentials: MarketplaceCredentials = EMPTY_CREDENTIALS,
        error: Exception | None = None,
    ) -> None:
        super().__init__(credentials, httpx.AsyncClient())
        self.marketplace = marketplace
        self.display_name = display_name
        self._products = products or []
        self._error = error
        self.search_calls = 0
        self.search_queries: list[str] = []

    async def search_products(self, request: SearchRequest) -> list[Product]:
        self.search_calls += 1
        self.search_queries.append(request.normalized_query or request.original_query)
        if self._error is not None:
            raise self._error
        return list(self._products)

    async def get_product(self, product_id: str) -> Product | None:
        return next(
            (p for p in self._products if p.product_id == product_id), None
        )

    def generate_affiliate_link(self, product: Product) -> str | None:
        return None


class SearchServiceTestCase(unittest.TestCase):
    """Shared setup: build providers and always close their HTTP clients."""

    def run_async(self, coro):
        return asyncio.run(coro)

    def setUp(self) -> None:
        self.providers: list[StubProvider] = []

    def tearDown(self) -> None:
        async def close_all() -> None:
            for provider in self.providers:
                await provider.aclose()

        if self.providers:
            asyncio.run(close_all())

    def stub(self, **kwargs) -> StubProvider:
        provider = StubProvider(**kwargs)
        self.providers.append(provider)
        return provider

    def ali_express(self, products, **kwargs) -> StubProvider:
        return self.stub(
            marketplace=Marketplace.ALIEXPRESS,
            display_name="AliExpress",
            products=products,
            credentials=CONFIGURED_CREDENTIALS,
            **kwargs,
        )


class RawConversationalRetrievalTests(SearchServiceTestCase):
    def test_api_error_result_is_not_reported_as_a_valid_empty_answer(self) -> None:
        from services.marketplaces.aliexpress import AliExpressSearchResults

        provider = self.ali_express([])
        async def api_error(_request):
            return AliExpressSearchResults(provider_error="AliExpressApiError:405")

        provider.search_products = api_error
        service = SearchService([provider])
        request = SearchRequest(
            original_query="welding machine",
            normalized_query="welding machine",
        )

        outcome = self.run_async(
            service.find_products(request, raw=True, broaden=False)
        )

        self.assertEqual(outcome.products, [])
        self.assertEqual(outcome.failed_marketplaces, ["AliExpress"])
        self.assertEqual(outcome.searched_marketplaces, [])

    def test_raw_conversation_can_use_bounded_grounded_variants(self) -> None:
        products = [make_product(Marketplace.ALIEXPRESS, "ali-1", 19.99)]
        provider = self.ali_express(products)
        service = SearchService([provider])
        request = SearchRequest(
            original_query="wireless headphones for running",
            normalized_query="wireless headphones for running",
        )

        with patch(
            "services.query_variants.product_query_variants",
            return_value=["wireless headphones for running", "wireless headphones"],
        ):
            outcome = self.run_async(
                service.find_products(request, raw=True, broaden=True, raw_broaden=True)
            )

        self.assertEqual(provider.search_calls, 2)
        self.assertEqual(
            provider.search_queries,
            ["wireless headphones for running", "wireless headphones"],
        )
        self.assertEqual([item.product_id for item in outcome.products], ["ali-1"])

    def test_raw_conversation_fuses_all_routes_even_when_first_pool_is_full(self) -> None:
        a = make_product(Marketplace.ALIEXPRESS, "route-a", 19.0)
        b = make_product(Marketplace.ALIEXPRESS, "route-b", 18.0)
        c = make_product(Marketplace.ALIEXPRESS, "route-c", 17.0)
        provider = self.ali_express([a, b, c])
        route_results = [[a, b, c], [b, a], [b]]

        async def sequenced_search(request):
            index = provider.search_calls
            provider.search_calls += 1
            provider.search_queries.append(
                request.normalized_query or request.original_query
            )
            return route_results[index]

        provider.search_products = sequenced_search
        # The first route already fills the raw pool. Conversational RRF must
        # still collect the remaining bounded routes before choosing the head.
        service = SearchService([provider], max_results=3)
        request = SearchRequest(
            original_query="wireless headphones for running",
            normalized_query="wireless headphones for running",
        )

        with patch(
            "services.query_variants.product_query_variants",
            return_value=[
                "wireless headphones for running",
                "wireless headphones",
                "running bluetooth headphones",
            ],
        ):
            outcome = self.run_async(
                service.find_products(request, raw=True, broaden=True, raw_broaden=True)
            )

        self.assertEqual(provider.search_calls, 3)
        self.assertEqual(
            [item.product_id for item in outcome.products],
            ["route-b", "route-a", "route-c"],
        )

    def test_raw_retrieval_stays_single_phrase_without_explicit_opt_in(self) -> None:
        provider = self.ali_express([make_product(Marketplace.ALIEXPRESS, "ali-1", 19.99)])
        service = SearchService([provider])
        request = SearchRequest(
            original_query="wireless headphones for running",
            normalized_query="wireless headphones for running",
        )

        with patch(
            "services.query_variants.product_query_variants",
            return_value=["wireless headphones for running", "wireless headphones"],
        ):
            self.run_async(service.find_products(request, raw=True))

        self.assertEqual(provider.search_calls, 1)
        self.assertEqual(provider.search_queries, ["wireless headphones for running"])


class ProviderSelectionTests(SearchServiceTestCase):
    """AliExpress is the only marketplace, so selection is preference-based."""

    def test_registered_provider_serves_a_consumer_search(self) -> None:
        service = SearchService([self.ali_express([])])
        request = SearchRequest(original_query="wireless headphones")

        selected = service.applicable_providers(request)

        self.assertEqual([p.marketplace for p in selected], [Marketplace.ALIEXPRESS])

    def test_matching_marketplace_preference_is_respected(self) -> None:
        service = SearchService([self.ali_express([])])
        request = SearchRequest(
            original_query="mouse", preferred_marketplaces=[Marketplace.ALIEXPRESS]
        )

        selected = service.applicable_providers(request)

        self.assertEqual([p.marketplace for p in selected], [Marketplace.ALIEXPRESS])

    def test_unconfigured_provider_is_not_queried(self) -> None:
        unconfigured = self.stub(
            marketplace=Marketplace.ALIEXPRESS, display_name="AliExpress", products=[]
        )
        service = SearchService([unconfigured])

        outcome = self.run_async(
            service.find_products(SearchRequest(original_query="mouse"))
        )

        self.assertEqual(unconfigured.search_calls, 0)
        self.assertEqual(outcome.unavailable_marketplaces, ["AliExpress"])


class ResilienceTests(SearchServiceTestCase):
    """A failing marketplace must not break the whole search."""

    def test_one_failing_provider_still_returns_other_results(self) -> None:
        """A provider whose API call fails must not hide good results.

        The second stub stands for a failing marketplace call: the error
        is collected (``asyncio.gather(return_exceptions=True)``) while
        the products of the working provider are still shown.
        """

        products = [make_product(Marketplace.ALIEXPRESS, "ali-1", 19.99)]
        failing = self.stub(
            marketplace=Marketplace.ALIEXPRESS,
            display_name="AliExpress (сбой запроса)",
            products=[],
            credentials=CONFIGURED_CREDENTIALS,
            error=httpx.ConnectError("AliExpress API is down"),
        )
        service = SearchService([self.ali_express(products), failing])

        outcome = self.run_async(
            service.find_products(SearchRequest(original_query="headphones"))
        )

        self.assertEqual([p.product_id for p in outcome.products], ["ali-1"])
        self.assertEqual(outcome.searched_marketplaces, ["AliExpress"])
        self.assertEqual(outcome.failed_marketplaces, ["AliExpress (сбой запроса)"])
        self.assertTrue(outcome.has_partial_failures)

    def test_no_configured_provider_returns_empty_outcome(self) -> None:
        unconfigured = self.stub(
            marketplace=Marketplace.ALIEXPRESS, display_name="AliExpress", products=[]
        )
        service = SearchService([unconfigured])

        outcome = self.run_async(
            service.find_products(SearchRequest(original_query="headphones"))
        )

        self.assertEqual(outcome.products, [])
        self.assertEqual(outcome.found_count, 0)
        self.assertEqual(outcome.unavailable_marketplaces, ["AliExpress"])
        self.assertEqual(outcome.searched_marketplaces, [])

    def test_duplicate_marketplace_and_id_are_collapsed(self) -> None:
        """The same listing must never be shown twice.

        Deduplication is keyed on the marketplace+product_id pair, so two
        providers that surface the same item (a listing is often reachable
        through more than one API entry point) produce a single card.
        """

        duplicate = make_product(Marketplace.ALIEXPRESS, "ali-1", 10.0)
        first = self.ali_express([duplicate])
        second = self.ali_express([duplicate])

        collected = SearchService._deduplicate(
            [(first, [duplicate]), (second, [duplicate])]
        )

        self.assertEqual(len(collected), 1)
        self.assertEqual(collected[0].product_id, "ali-1")


class FilteringAndSortingTests(SearchServiceTestCase):
    """Numeric filtering/ranking is programmatic - never Gemini's job."""

    def build_service(self, products: list[Product]) -> SearchService:
        return SearchService([self.ali_express(products)])

    def test_max_price_filter_is_applied(self) -> None:
        service = self.build_service(
            [
                make_product(Marketplace.ALIEXPRESS, "cheap", 19.99),
                make_product(Marketplace.ALIEXPRESS, "expensive", 49.99),
            ]
        )

        outcome = self.run_async(
            service.find_products(
                SearchRequest(original_query="headphones under $30", max_price=30.0)
            )
        )

        self.assertEqual([p.product_id for p in outcome.products], ["cheap"])
        self.assertEqual(outcome.products_before_filtering, 2)

    def test_product_with_unknown_price_is_filtered_out_when_limit_set(self) -> None:
        service = self.build_service(
            [
                make_product(Marketplace.ALIEXPRESS, "known", 10.0),
                make_product(Marketplace.ALIEXPRESS, "unknown", None),
            ]
        )

        outcome = self.run_async(
            service.find_products(
                SearchRequest(original_query="x under $30", max_price=30.0)
            )
        )

        # Unknown price cannot be verified -> rejected instead of guessed.
        self.assertEqual([p.product_id for p in outcome.products], ["known"])

    def test_min_rating_and_min_orders_filters_are_applied(self) -> None:
        service = self.build_service(
            [
                make_product(
                    Marketplace.ALIEXPRESS, "good", 10.0, rating=4.8, orders_count=900
                ),
                make_product(
                    Marketplace.ALIEXPRESS, "bad-rating", 10.0, rating=3.9, orders_count=900
                ),
                make_product(
                    Marketplace.ALIEXPRESS, "few-orders", 10.0, rating=4.8, orders_count=10
                ),
            ]
        )

        outcome = self.run_async(
            service.find_products(
                SearchRequest(
                    original_query="good rated", min_rating=4.5, min_orders=100
                )
            )
        )

        self.assertEqual([p.product_id for p in outcome.products], ["good"])

    def test_quantity_is_informational_and_never_hides_products(self) -> None:
        """A quantity in the query must not filter results.

        AliExpress has no minimum order quantity, so naming a number of
        pieces cannot invalidate an otherwise matching product.
        """

        service = self.build_service(
            [
                make_product(Marketplace.ALIEXPRESS, "first", 10.0),
                make_product(Marketplace.ALIEXPRESS, "second", 12.0),
            ]
        )

        outcome = self.run_async(
            service.find_products(
                SearchRequest(original_query="cheap mouse, 20 pieces", quantity=20)
            )
        )

        self.assertEqual([p.product_id for p in outcome.products], ["first", "second"])

    def test_results_are_sorted_by_price_when_requested(self) -> None:
        service = self.build_service(
            [
                make_product(Marketplace.ALIEXPRESS, "mid", 25.0),
                make_product(Marketplace.ALIEXPRESS, "low", 5.0),
                make_product(Marketplace.ALIEXPRESS, "high", 45.0),
            ]
        )

        outcome = self.run_async(
            service.find_products(
                SearchRequest(
                    original_query="cheapest", sort_preference=SortPreference.PRICE_ASC
                )
            )
        )

        self.assertEqual(
            [p.product_id for p in outcome.products], ["low", "mid", "high"]
        )

    def test_results_are_sorted_by_rating_when_requested(self) -> None:
        service = self.build_service(
            [
                make_product(Marketplace.ALIEXPRESS, "a", 10.0, rating=4.1),
                make_product(Marketplace.ALIEXPRESS, "b", 10.0, rating=4.9),
            ]
        )

        outcome = self.run_async(
            service.find_products(
                SearchRequest(
                    original_query="best rating",
                    sort_preference=SortPreference.RATING_DESC,
                )
            )
        )

        self.assertEqual([p.product_id for p in outcome.products], ["b", "a"])

    def test_max_results_limit_is_applied(self) -> None:
        products = [
            make_product(Marketplace.ALIEXPRESS, f"p{i}", float(i)) for i in range(20)
        ]
        service = SearchService([self.ali_express(products)], max_results=3)

        outcome = self.run_async(
            service.find_products(SearchRequest(original_query="anything"))
        )

        self.assertEqual(len(outcome.products), 3)
        self.assertEqual(outcome.products_before_filtering, 20)

    def test_comparisons_are_built_only_for_shown_products(self) -> None:
        service = SearchService(
            [
                self.ali_express(
                    [
                        make_product(Marketplace.ALIEXPRESS, "a", 10.0, shipping_cost=2.0),
                        make_product(Marketplace.ALIEXPRESS, "b", 12.0),
                    ]
                )
            ],
            max_results=1,
        )

        outcome = self.run_async(
            service.find_products(SearchRequest(original_query="anything"))
        )

        self.assertEqual(len(outcome.comparisons), 1)
        self.assertEqual(outcome.comparisons[0].total_known_cost, 12.0)


class FeatureFilterTests(SearchServiceTestCase):
    """Parsed required/excluded features must filter - never crash."""

    def test_required_feature_keeps_only_matching_products(self) -> None:
        service = SearchService(
            [
                self.ali_express(
                    [
                        make_product(
                            Marketplace.ALIEXPRESS,
                            "with-anc",
                            39.99,
                            title="Наушники беспроводные с шумоподавлением",
                        ),
                        make_product(
                            Marketplace.ALIEXPRESS,
                            "without-anc",
                            19.99,
                            title="Наушники беспроводные накладные",
                        ),
                    ]
                )
            ]
        )

        outcome = self.run_async(
            service.find_products(
                SearchRequest(
                    original_query="наушники с шумоподавлением",
                    normalized_query="Наушники беспроводные",
                    required_features=["шумоподавление"],
                )
            )
        )

        self.assertEqual([p.product_id for p in outcome.products], ["with-anc"])


class CurrencyCoherenceTests(SearchServiceTestCase):
    """A price bound is compared only when both sides share one currency.

    Stage 2: the project performs no currency conversion, so a product
    whose price currency cannot be reconciled with the request currency
    must not be treated as satisfying an explicit price constraint.
    Without any price constraint, currency alone never rejects a product.
    Every test asserts the actual ``find_products`` outcome, not a helper.
    """

    def search_ids(
        self, products: list[Product], request: SearchRequest
    ) -> list[str]:
        service = SearchService([self.ali_express(products)])
        outcome = self.run_async(service.find_products(request))
        return [product.product_id for product in outcome.products]

    def test_matching_currency_within_budget_is_kept(self) -> None:
        ids = self.search_ids(
            [make_product(Marketplace.ALIEXPRESS, "ok", 19.99, currency="USD")],
            SearchRequest(
                original_query="headphones under $30",
                max_price=30.0,
                currency="USD",
            ),
        )

        self.assertEqual(ids, ["ok"])

    def test_matching_currency_above_max_is_dropped(self) -> None:
        ids = self.search_ids(
            [make_product(Marketplace.ALIEXPRESS, "pricey", 49.99, currency="USD")],
            SearchRequest(
                original_query="headphones under $30",
                max_price=30.0,
                currency="USD",
            ),
        )

        self.assertEqual(ids, [])

    def test_matching_currency_below_min_is_dropped(self) -> None:
        ids = self.search_ids(
            [make_product(Marketplace.ALIEXPRESS, "cheap", 10.0, currency="USD")],
            SearchRequest(
                original_query="headphones over $20",
                min_price=20.0,
                currency="USD",
            ),
        )

        self.assertEqual(ids, [])

    def test_different_currency_with_equal_number_does_not_bypass_budget(
        self,
    ) -> None:
        # 30 EUR is numerically equal to the $30 bound - comparing the raw
        # numbers would wrongly keep it. Currencies differ, so it is out.
        ids = self.search_ids(
            [make_product(Marketplace.ALIEXPRESS, "eur", 30.0, currency="EUR")],
            SearchRequest(
                original_query="headphones under $30",
                max_price=30.0,
                currency="USD",
            ),
        )

        self.assertEqual(ids, [])

    def test_missing_product_currency_with_explicit_max_is_dropped(self) -> None:
        ids = self.search_ids(
            [make_product(Marketplace.ALIEXPRESS, "unknown", 10.0, currency=None)],
            SearchRequest(
                original_query="headphones under $30",
                max_price=30.0,
                currency="USD",
            ),
        )

        self.assertEqual(ids, [])

    def test_unrecognized_product_currency_with_explicit_max_is_dropped(
        self,
    ) -> None:
        ids = self.search_ids(
            [make_product(Marketplace.ALIEXPRESS, "odd", 10.0, currency="XYZ")],
            SearchRequest(
                original_query="headphones under $30",
                max_price=30.0,
                currency="USD",
            ),
        )

        self.assertEqual(ids, [])

    def test_mixed_currencies_keep_only_reconcilable_products(self) -> None:
        ids = self.search_ids(
            [
                make_product(Marketplace.ALIEXPRESS, "ok", 10.0, currency="USD"),
                make_product(Marketplace.ALIEXPRESS, "eur", 10.0, currency="EUR"),
                make_product(Marketplace.ALIEXPRESS, "unknown", 10.0, currency=None),
                make_product(Marketplace.ALIEXPRESS, "pricey", 99.0, currency="USD"),
            ],
            SearchRequest(
                original_query="headphones under $30",
                max_price=30.0,
                currency="USD",
            ),
        )

        # One incompatible product must not disable filtering for the rest:
        # the comparable in-budget product survives, every incompatible or
        # over-budget product is gone.
        self.assertEqual(ids, ["ok"])

    def test_no_price_constraint_ignores_currency_alone(self) -> None:
        ids = self.search_ids(
            [
                make_product(Marketplace.ALIEXPRESS, "eur", 10.0, currency="EUR"),
                make_product(Marketplace.ALIEXPRESS, "unknown", 10.0, currency=None),
                make_product(Marketplace.ALIEXPRESS, "odd", 10.0, currency="XYZ"),
            ],
            SearchRequest(original_query="headphones"),
        )

        self.assertEqual(ids, ["eur", "unknown", "odd"])

    def test_price_boundaries_stay_inclusive(self) -> None:
        ids = self.search_ids(
            [
                make_product(Marketplace.ALIEXPRESS, "at-max", 30.0, currency="USD"),
                make_product(Marketplace.ALIEXPRESS, "at-min", 20.0, currency="USD"),
            ],
            SearchRequest(
                original_query="headphones $20-$30",
                min_price=20.0,
                max_price=30.0,
                currency="USD",
            ),
        )

        self.assertEqual(ids, ["at-max", "at-min"])

    def test_request_without_currency_keeps_legacy_numeric_behavior(self) -> None:
        # No budget currency: nothing to reconcile against, so the existing
        # numeric comparison still applies (and case is normalized).
        ids = self.search_ids(
            [
                make_product(Marketplace.ALIEXPRESS, "cheap", 19.99, currency="USD"),
                make_product(Marketplace.ALIEXPRESS, "pricey", 49.99, currency="USD"),
            ],
            SearchRequest(original_query="headphones under $30", max_price=30.0),
        )

        self.assertEqual(ids, ["cheap"])

    def test_currency_codes_compare_case_insensitively(self) -> None:
        ids = self.search_ids(
            [make_product(Marketplace.ALIEXPRESS, "ok", 19.99, currency="USD")],
            SearchRequest(
                original_query="headphones under $30",
                max_price=30.0,
                currency="usd",
            ),
        )

        self.assertEqual(ids, ["ok"])


class FeatureExclusionTests(SearchServiceTestCase):
    """Parsed excluded features must filter - never crash."""

    def test_excluded_feature_removes_only_matching_products(self) -> None:
        service = SearchService(
            [
                self.ali_express(
                    [
                        make_product(
                            Marketplace.ALIEXPRESS,
                            "wired",
                            9.99,
                            title="Наушники проводные с микрофоном",
                        ),
                        make_product(
                            Marketplace.ALIEXPRESS,
                            "wireless",
                            29.99,
                            title="Наушники накладные с микрофоном",
                        ),
                    ]
                )
            ]
        )

        outcome = self.run_async(
            service.find_products(
                SearchRequest(
                    original_query="беспроводные наушники без провода",
                    normalized_query="Наушники с микрофоном",
                    excluded_features=["проводные"],
                )
            )
        )

        self.assertEqual([p.product_id for p in outcome.products], ["wireless"])

    def test_features_that_match_nothing_yield_an_empty_page_not_a_crash(
        self,
    ) -> None:
        """A feature no listing carries hides everything, honestly.

        Before the fix ``_apply_feature_filters`` returned ``None``, so
        ``filter_products`` crashed with ``TypeError`` and the whole search
        failed with an internal-error message for every request whose
        parsed features were non-empty (verified live, not a theory).
        """

        service = SearchService(
            [self.ali_express([make_product(Marketplace.ALIEXPRESS, "a", 10.0, title="Наушники беспроводные")])]
        )

        outcome = self.run_async(
            service.find_products(
                SearchRequest(
                    original_query="наушники с шумоподавлением",
                    normalized_query="Наушники беспроводные",
                    required_features=["шумоподавление"],
                )
            )
        )

        self.assertEqual(outcome.products, [])
        self.assertEqual(outcome.found_count, 0)
        self.assertEqual(outcome.products_before_filtering, 1)

    def test_products_are_kept_when_no_features_were_parsed(self) -> None:
        service = SearchService(
            [
                self.ali_express(
                    [
                        make_product(
                            Marketplace.ALIEXPRESS, "a", 10.0, title="Наушники беспроводные"
                        ),
                        make_product(
                            Marketplace.ALIEXPRESS, "b", 12.0, title="Наушники накладные"
                        ),
                    ]
                )
            ]
        )

        outcome = self.run_async(
            service.find_products(
                SearchRequest(
                    original_query="наушники", normalized_query="Наушники"
                )
            )
        )

        self.assertEqual(len(outcome.products), 2)


class TextSearchAndAIRankingTests(SearchServiceTestCase):
    """``find_products_from_text`` works without Gemini (fallback parser)."""

    def test_text_query_is_parsed_and_searched(self) -> None:
        service = SearchService(
            [self.ali_express([make_product(Marketplace.ALIEXPRESS, "a", 19.99)])],
            ai_service=AIService(api_key=None),
        )

        outcome = self.run_async(
            service.find_products_from_text("wireless headphones under $30")
        )

        self.assertEqual(outcome.found_count, 1)
        self.assertEqual(outcome.request.max_price, 30.0)

    def test_text_search_requires_an_ai_service(self) -> None:
        service = SearchService([self.ali_express([])])

        with self.assertRaises(RuntimeError):
            self.run_async(service.find_products_from_text("headphones"))

    def test_ai_ranking_is_skipped_without_gemini(self) -> None:
        service = SearchService(
            [self.ali_express([make_product(Marketplace.ALIEXPRESS, "a", 19.99)])],
            ai_service=AIService(api_key=None),
        )

        outcome = self.run_async(
            service.find_products(SearchRequest(original_query="headphones"))
        )
        # No Gemini key: ranking quietly returns None and the feed keeps
        # the deterministic order.
        self.assertIsNone(
            self.run_async(service.ai_service.rank_candidates(outcome.products))
        )
        self.assertEqual(outcome.found_count, 1)


class FeedStubProvider(StubProvider):
    """A provider that can answer the keyword-less «✨ Находки» feed."""

    supports_feed = True

    async def fetch_feed_page(
        self, *, page: int = 1, currency=None, country=None
    ) -> list[Product]:
        # The service applies dedup/validity/history/limit itself, so the
        # provider only delivers its fixed pool - exactly like the real
        # AliExpress page would.
        return list(self._products)


class DeduplicationAndRelevanceTests(SearchServiceTestCase):
    """Centralised dedup and relevance filtering, applied by the service."""

    def feed_provider(self, products, **kwargs) -> FeedStubProvider:
        provider = FeedStubProvider(
            marketplace=Marketplace.ALIEXPRESS,
            display_name="AliExpress",
            products=products,
            credentials=CONFIGURED_CREDENTIALS,
            **kwargs,
        )
        self.providers.append(provider)
        return provider

    def test_one_listing_reached_through_two_tracking_links_is_shown_once(self):
        plain = make_product(
            Marketplace.ALIEXPRESS,
            "1005001",
            19.99,
            title="Беспроводные наушники TWS",
        )
        tracked = make_product(
            Marketplace.ALIEXPRESS,
            "1005001",
            19.99,
            title="Беспроводные наушники TWS",
            product_url="https://aliexpress.com/item/1005001.html?aff_trace_key=x",
            affiliate_url="https://s.click.aliexpress.com/e/_AFF",
        )
        request = SearchRequest(
            original_query="наушники", normalized_query="наушники"
        )
        service = SearchService(
            [
                self.ali_express([plain]),
                self.ali_express([tracked]),
            ]
        )

        outcome = self.run_async(service.find_products(request))

        self.assertEqual(outcome.found_count, 1)
        self.assertEqual(outcome.products_before_filtering, 1)

    def test_irrelevant_result_is_removed_and_reported(self):
        good = make_product(
            Marketplace.ALIEXPRESS,
            "good",
            15.0,
            title="Беспроводные наушники TWS",
        )
        cross = make_product(
            Marketplace.ALIEXPRESS, "cross", 15.0, title="Крест нательный"
        )
        service = SearchService([self.ali_express([good, cross])])
        request = SearchRequest(
            original_query="беспроводные наушники",
            normalized_query="беспроводные наушники",
        )

        outcome = self.run_async(service.find_products(request))

        self.assertEqual([p.product_id for p in outcome.products], ["good"])
        self.assertEqual(outcome.products_dropped_as_irrelevant, 1)

    def test_popular_feed_keeps_only_affiliate_products(self):
        with_affiliate = make_product(
            Marketplace.ALIEXPRESS,
            "with",
            20.0,
            affiliate_url="https://s.click.aliexpress.com/e/_1",
        )
        without_affiliate = make_product(Marketplace.ALIEXPRESS, "without", 20.0)
        service = SearchService([self.feed_provider([with_affiliate, without_affiliate])])

        outcome = self.run_async(service.find_feed_page())

        self.assertEqual([p.product_id for p in outcome.products], ["with"])
        self.assertEqual(outcome.products_without_affiliate_url, 1)

    def test_popular_feed_has_no_duplicates(self):
        product = make_product(
            Marketplace.ALIEXPRESS,
            "dup",
            9.99,
            affiliate_url="https://s.click.aliexpress.com/e/_2",
        )
        service = SearchService([self.feed_provider([product, product, product])])

        outcome = self.run_async(service.find_feed_page())

        self.assertEqual(outcome.found_count, 1)

    def test_feed_page_skips_what_the_user_has_already_seen(self):
        fresh = make_product(
            Marketplace.ALIEXPRESS,
            "fresh",
            10.0,
            title="Свежий товар из каталога",
            affiliate_url="https://s.click.aliexpress.com/e/_f",
        )
        seen = make_product(
            Marketplace.ALIEXPRESS,
            "seen",
            11.0,
            title="Уже показанный товар",
            affiliate_url="https://s.click.aliexpress.com/e/_s",
        )
        # The provider returns them in this order...
        service = SearchService([self.feed_provider([seen, fresh])])

        # ...but the user's shown-history drops the already-shown id.
        from services.product_identity import identity_pair

        outcome = self.run_async(
            service.find_feed_page(exclude={identity_pair(seen)})
        )

        self.assertEqual([p.product_id for p in outcome.products], ["fresh"])
        self.assertEqual(outcome.products_hidden_as_seen, 1)

    def test_relevance_outranks_popularity_in_a_product_search(self):
        """A verified product type leads; a neighbouring-type bestseller never shows.

        Two requirements hold at once:

        * a bestseller of a NEIGHBOURING product type that shares only the
          adjective («беспроводные колонки» for «беспроводные наушники»)
          is dropped by the product-type gate - popularity must not
          smuggle an unrelated product into the feed at all;
        * among the products that ARE the requested type, the precise
          attribute match leads even against a much more popular listing.
        """

        precise = make_product(
            Marketplace.ALIEXPRESS,
            "precise",
            19.0,
            title="Беспроводные наушники TWS",
            orders_count=12,
        )
        loose = make_product(
            Marketplace.ALIEXPRESS,
            "loose",
            9.0,
            title="Беспроводные колонки портативные",
            orders_count=900000,
            rating=4.9,
        )
        popular_sibling = make_product(
            Marketplace.ALIEXPRESS,
            "sibling",
            9.0,
            title="Проводные наушники офисные",
            orders_count=900000,
            rating=4.9,
        )
        # The provider returns the bestseller first...
        service = SearchService(
            [self.ali_express([loose, popular_sibling, precise])]
        )
        request = SearchRequest(
            original_query="беспроводные наушники",
            normalized_query="беспроводные наушники",
        )

        outcome = self.run_async(service.find_products(request))

        # ...but the neighbouring product TYPE never reaches the user...
        self.assertNotIn(
            "loose", [product.product_id for product in outcome.products]
        )
        self.assertGreater(outcome.products_dropped_as_irrelevant, 0)
        # ...and of the surviving same-type candidates the one matching
        # every requested attribute leads, however popular the other is.
        self.assertEqual(
            [p.product_id for p in outcome.products], ["precise", "sibling"]
        )

    def test_feed_keeps_the_marketplace_order_untouched(self):
        """G24: the bot must NOT re-rank AliExpress' own feed.

        The provider returns the candidates in its own order; the service
        delivers them exactly as they came - no popularity score, no local
        ranking, no quality judgement. The quiet listing leads only because
        AliExpress put it first.
        """

        quiet = make_product(
            Marketplace.ALIEXPRESS,
            "quiet",
            5.0,
            title="Подставка для ноутбука алюминиевая",
            orders_count=350,
            affiliate_url="https://s.click.aliexpress.com/e/_c",
        )
        popular = make_product(
            Marketplace.ALIEXPRESS,
            "popular",
            50.0,
            title="Беспроводные наушники TWS с кейсом",
            orders_count=9000,
            affiliate_url="https://s.click.aliexpress.com/e/_g",
        )
        service = SearchService([self.feed_provider([quiet, popular])])

        outcome = self.run_async(service.find_feed_page())

        self.assertEqual(
            [p.product_id for p in outcome.products], ["quiet", "popular"]
        )

    def test_feed_page_is_reproducible(self):
        """The same page of the same listing must always yield the same cards."""

        titles = (
            "Беспроводные наушники TWS",
            "Умные часы спортивные",
            "Рюкзак городской водостойкий",
            "Лампа настольная LED",
        )
        products = [
            make_product(
                Marketplace.ALIEXPRESS,
                f"p{index}",
                10.0 + index,
                title=title,
                orders_count=orders,
                affiliate_url=f"https://s.click.aliexpress.com/e/_{index}",
            )
            for index, (orders, title) in enumerate(
                zip((500, 9000, 30, 2500), titles)
            )
        ]
        service = SearchService([self.feed_provider(products)])

        first = self.run_async(service.find_feed_page())
        second = self.run_async(service.find_feed_page())

        # G24: every valid card of the page survives (no quality floor, no
        # ranking) and the order is the provider's own - twice the same.
        self.assertEqual(
            [p.product_id for p in first.products],
            ["p0", "p1", "p2", "p3"],
        )
        self.assertEqual(
            [p.product_id for p in first.products],
            [p.product_id for p in second.products],
        )


if __name__ == "__main__":
    unittest.main()



