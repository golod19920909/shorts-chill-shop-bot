"""Candidate pool, grounded retrieval variants and quality ranking.

Regression coverage for the three behaviours the manual comparison with the
AliExpress website exposed (§3, §4, §11):

- ONE narrow query is a weak way to look for a product a human described
  in several words, so a product search may ask a small bounded set of
  GROUNDED variants - every word of which the user actually wrote;
- all candidates (from every variant) are merged and judged by the same
  relevance gate, and the loop stops as soon as the pool is full;
- «с хорошим рейтингом» is a WISH about the order of real products, never
  a filter and never a fabricated number.

Everything runs offline against stub providers.
"""

from __future__ import annotations

import asyncio
import time
import unittest

from handlers import discovery
from models.product import Marketplace, Product
from models.search_request import SearchMode, SearchRequest
from services import feed_curation, query_variants
from services.ai_service import AIService
from services.comparison_service import sort_products
from services.relevance import tokenize
from services.search_service import (
    AI_FEED_RESULTS,
    DEFAULT_MAX_RESULTS,
    FEED_SELECTION_TIMEOUT_SECONDS,
    MAX_SEARCH_PAGES,
    RAW_POOL_TARGET,
    SearchOutcome,
    SearchService,
)

from tests.test_photo_and_discovery import (
    FakeSearchService,
    PhotoMessage,
    make_context as photo_context,
    photo_update,
)
from tests.test_search_quality import StructuredVisionAI, TEA_PHOTO
from tests.test_search_service import CONFIGURED_CREDENTIALS, StubProvider


def listing(title: str, product_id: str, **overrides) -> Product:
    """A real-shaped Russian AliExpress listing."""

    data = {
        "marketplace": Marketplace.ALIEXPRESS,
        "product_id": product_id,
        "title": title,
        "product_url": f"https://www.aliexpress.com/item/{product_id}.html",
        "price": 10.0,
        "currency": "USD",
    }
    data.update(overrides)
    return Product(**data)


def run(coro):
    return asyncio.run(coro)


class GroundedVariantTests(unittest.TestCase):
    """§4: extra retrieval phrases come from the user's own words only."""

    def variants(self, **fields) -> list[str]:
        request = SearchRequest(**fields)
        return query_variants.product_query_variants(request)

    def test_a_single_product_query_costs_exactly_one_query(self) -> None:
        # The common case must not spend a second API call.
        self.assertEqual(
            self.variants(original_query="электрическая зубная щетка"),
            ["электрическая зубная щетка"],
        )
        self.assertEqual(
            self.variants(
                original_query="электрическая зубная щетка",
                normalized_query="electric toothbrush",
            ),
            ["electric toothbrush"],
        )

    def test_a_one_word_query_is_also_asked_in_the_users_own_words(self) -> None:
        """§4: a translated/inflated parser phrase is not the user's product word."""

        variants = self.variants(
            original_query="наушники",
            normalized_query="wireless bluetooth headphones pro",
        )

        self.assertEqual(variants[0], "wireless bluetooth headphones pro")
        self.assertIn("наушники", variants)
        self.assertLessEqual(len(variants), query_variants.MAX_PRODUCT_QUERY_VARIANTS)

    def test_a_more_detailed_own_wording_is_not_broadening(self) -> None:
        """The user wrote MORE words than the parser sent: the same lookup."""

        self.assertEqual(
            self.variants(
                original_query="термос для чая с крышкой",
                normalized_query="thermos",
            ),
            ["thermos"],
        )

    def test_a_descriptive_query_adds_the_users_own_product_phrase(self) -> None:
        variants = self.variants(
            original_query="брелок в виде строительного инструмента",
            normalized_query="keychain construction tool",
        )

        self.assertEqual(variants[0], "keychain construction tool")
        self.assertIn("брелок инструмента", variants)

    def test_no_variant_contains_a_word_the_user_did_not_write(self) -> None:
        original = "брелок в виде строительного инструмента"
        variants = self.variants(original_query=original)

        own_words = set(tokenize(original))
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertTrue(set(tokenize(variant)) <= own_words, variant)

    def test_a_variant_never_collapses_to_one_generic_noun(self) -> None:
        # «полотно» alone means fabric - the design that keeps the user's
        # actual product words in every shortened phrase.
        variants = self.variants(
            original_query="полотно для лобзика, с хорошим рейтингом"
        )

        self.assertGreater(len(variants), 1)
        for variant in variants[1:]:
            with self.subTest(variant=variant):
                words = set(tokenize(variant))
                self.assertIn("полотно", words)
                self.assertIn("лобзика", words)

    def test_the_core_noun_pair_is_always_generated(self) -> None:
        # A request naming four or more product nouns used to spend the whole
        # variant budget on near-copies of the parser's own phrase, so the
        # broad CORE pair - the head noun plus the noun that follows it, which
        # is the broadest phrase that still names a product - was never asked.
        variants = self.variants(
            original_query="чехол коврик держатель кабель сумка кожаный"
        )

        self.assertIn("чехол коврик", variants)
        self.assertLessEqual(len(variants), query_variants.MAX_PRODUCT_QUERY_VARIANTS)
        for variant in variants[1:]:
            with self.subTest(variant=variant):
                # The head noun still leads every generated phrase.
                self.assertEqual(variant.split()[0], "чехол")

    def test_price_and_quality_words_never_enter_a_retrieval_phrase(self) -> None:
        # The FIRST element is the parser's own phrase (already cleaned by
        # ``ai_service._strip_constraint_phrases`` in production); the
        # ADDED phrases are built here and must stay product-only.
        variants = self.variants(
            original_query="зубная щетка для чехла до 10 долларов хорошим рейтингом"
        )

        self.assertGreater(len(variants), 1)
        for variant in variants[1:]:
            for noise in ("долларов", "рейтингом", "хорошим", "щетка"):
                with self.subTest(variant=variant, noise=noise):
                    if noise == "щетка":
                        self.assertIn(noise, tokenize(variant))
                        continue
                    self.assertNotIn(noise, tokenize(variant))

    def test_the_variant_list_is_bounded(self) -> None:
        variants = self.variants(
            original_query="чехол коврик держатель кабель сумка кожаный"
        )

        self.assertLessEqual(len(variants), query_variants.MAX_PRODUCT_QUERY_VARIANTS)

    def test_a_mixed_script_query_is_also_asked_homogeneously(self) -> None:
        """A keywords string that mixes two scripts is a weak index question.

        Live (2026, this app key): «led лампа» answers 6/50 product hits,
        the homogeneous «лампа» answers 49/50. The dominant-script content
        words of the phrase become ONE extra homogeneous question - and
        only those words: nothing is translated, nothing is invented.
        """
        self.assertEqual(
            self.variants(original_query="LED лампа"), ["LED лампа", "лампа"]
        )
        # A translated parser phrase plus a mixed original still costs at
        # most the bounded list and asks BOTH languages homogeneously.
        variants = self.variants(
            original_query="LED лампа", normalized_query="LED lamp"
        )
        self.assertEqual(variants[0], "LED lamp")
        self.assertIn("лампа", variants)
        self.assertLessEqual(len(variants), query_variants.MAX_PRODUCT_QUERY_VARIANTS)

    def test_a_single_script_query_never_spends_the_homogeneous_call(self) -> None:
        # The homogeneous variant exists only for a REAL script mix, so
        # ordinary single-language queries keep their exact call count.
        for query in (
            "небулайзер",
            "наушники",
            "portable nebulizer",
            "phone accessories",
        ):
            with self.subTest(query=query):
                self.assertEqual(self.variants(original_query=query), [query])

    def test_non_search_modes_keep_their_single_query(self) -> None:
        for mode in (
            SearchMode.CATEGORY,
            SearchMode.POPULAR,
            SearchMode.DISCOVERY,
            SearchMode.IMAGE,
        ):
            with self.subTest(mode=mode):
                variants = self.variants(
                    original_query="брелок инструмента",
                    normalized_query="keychain",
                    mode=mode,
                )
                self.assertEqual(variants, ["keychain"])


class VariantAwareProvider(StubProvider):
    """Answers with a DIFFERENT set per query phrase, like a search index."""

    def __init__(self, by_query: dict[str, list[Product]]) -> None:
        super().__init__(
            marketplace=Marketplace.ALIEXPRESS,
            display_name="AliExpress",
            products=[],
            credentials=CONFIGURED_CREDENTIALS,
        )
        self.by_query = by_query
        self.queries: list[str] = []

    async def search_products(self, request: SearchRequest) -> list[Product]:
        phrase = request.normalized_query or request.original_query
        self.queries.append(phrase)
        return list(self.by_query.get(phrase, []))


class PageAwareProvider(StubProvider):
    """Answers with a DIFFERENT set per (phrase, documented page).

    Stands for a real search index: page 1 of a narrow phrase holds its
    best rows, page 2 and 3 hold further real rows of the SAME query.
    """

    def __init__(self, by_page: dict[tuple[str, int], list[Product]]) -> None:
        super().__init__(
            marketplace=Marketplace.ALIEXPRESS,
            display_name="AliExpress",
            products=[],
            credentials=CONFIGURED_CREDENTIALS,
        )
        self.by_page = by_page
        self.calls: list[tuple[str, int]] = []

    async def search_products(self, request: SearchRequest) -> list[Product]:
        phrase = request.normalized_query or request.original_query
        self.calls.append((phrase, request.page_no))
        return list(self.by_page.get((phrase, request.page_no), []))


class SearchBroadeningTests(unittest.TestCase):
    """§4: broader retrieval, same relevance gate, bounded API cost.

    Covers both halves of the breadth: extra GROUNDED PHRASES and, when the
    relevant pool is still thin, the next documented PAGE of the phrase the
    user actually asked with.
    """

    def setUp(self) -> None:
        self.providers: list[StubProvider] = []

    def tearDown(self) -> None:
        async def close_all() -> None:
            for provider in self.providers:
                await provider.aclose()

        if self.providers:
            run(close_all())

    def provider(self, by_query: dict[str, list[Product]]) -> VariantAwareProvider:
        provider = VariantAwareProvider(by_query)
        self.providers.append(provider)
        return provider

    def page_provider(
        self, by_page: dict[tuple[str, int], list[Product]]
    ) -> PageAwareProvider:
        provider = PageAwareProvider(by_page)
        self.providers.append(provider)
        return provider

    def test_a_thin_first_answer_asks_one_more_grounded_variant(self) -> None:
        keychain = listing("Брелок строительный инструмент молоток", "tool-keychain")
        noise = listing("Настольная лампа светодиодная", "lamp")
        provider = self.provider(
            {
                "keychain construction tool": [noise],
                "брелок инструмента": [keychain, noise],
            }
        )
        service = SearchService([provider])
        request = SearchRequest(
            original_query="брелок в виде строительного инструмента",
            normalized_query="keychain construction tool",
        )

        outcome = run(service.find_products(request))

        self.assertEqual(
            provider.queries[:2],
            ["keychain construction tool", "брелок инструмента"],
            "A thin relevant pool must trigger exactly one extra grounded query",
        )
        # Once every grounded phrase has been asked, the PRIMARY phrase - the
        # words the user actually wrote - is asked for its next DOCUMENTED
        # page instead of an invented query, and never deeper than
        # ``MAX_SEARCH_PAGES``.
        self.assertEqual(
            provider.queries[2:],
            ["keychain construction tool"] * (MAX_SEARCH_PAGES - 1),
        )
        # Every candidate of every variant went through the SAME gate.
        self.assertEqual([p.product_id for p in outcome.products], ["tool-keychain"])
        self.assertGreaterEqual(outcome.products_dropped_as_irrelevant, 1)
        self.assertEqual(outcome.searched_marketplaces, ["AliExpress"])

    def test_a_full_pool_stops_after_the_first_query(self) -> None:
        full = [
            listing(f"Брелок инструмент {index} металлический", f"kc-{index}")
            for index in range(DEFAULT_MAX_RESULTS)
        ]
        provider = self.provider({"keychain": full})
        service = SearchService([provider])
        request = SearchRequest(
            original_query="брелок в виде инструмента",
            normalized_query="keychain",
        )

        outcome = run(service.find_products(request))

        self.assertEqual(provider.queries, ["keychain"])
        self.assertEqual(len(outcome.products), DEFAULT_MAX_RESULTS)

    def test_variants_never_add_words_to_the_marketplace_query(self) -> None:
        provider = self.provider({})
        service = SearchService([provider])
        request = SearchRequest(
            original_query="брелок в виде строительного инструмента",
            normalized_query="keychain construction tool",
        )

        run(service.find_products(request))

        own = set(tokenize(request.original_query)) | set(
            tokenize(request.normalized_query)
        )
        for phrase in provider.queries:
            with self.subTest(phrase=phrase):
                self.assertTrue(set(tokenize(phrase)) <= own, phrase)

    def test_a_full_result_set_is_capped_at_the_documented_pool(self) -> None:
        products = [
            listing(f"Брелок инструмент {index} набор", f"many-{index}")
            for index in range(DEFAULT_MAX_RESULTS + 25)
        ]
        provider = self.provider({"брелок инструмента": products})
        service = SearchService([provider])

        outcome = run(
            service.find_products(
                SearchRequest(
                    original_query="брелок в виде строительного инструмента",
                    normalized_query="брелок инструмента",
                )
            )
        )

        self.assertEqual(len(outcome.products), DEFAULT_MAX_RESULTS)

    def test_a_thin_first_page_is_topped_up_from_the_same_phrase_deeper(self) -> None:
        """§3: one page of one narrow phrase is not the whole answer."""

        page_one = [
            listing("Брелок инструмент набор маленький", "p1a"),
            listing("Брелок инструмент набор средний", "p1b"),
        ]
        page_two = [
            listing("Брелок инструмент молоток титановый", "p2a"),
            listing("Брелок инструмент отвёртка стальная", "p2b"),
        ]
        provider = self.page_provider(
            {
                ("брелок инструмента", 1): page_one,
                ("брелок инструмента", 2): page_two,
            }
        )
        service = SearchService([provider])

        outcome = run(
            service.find_products(SearchRequest(original_query="брелок инструмента"))
        )

        self.assertEqual(provider.calls[0], ("брелок инструмента", 1))
        self.assertEqual(provider.calls[1], ("брелок инструмента", 2))
        # The deeper page added real rows of the SAME phrase...
        self.assertEqual(
            [p.product_id for p in outcome.products], ["p1a", "p1b", "p2a", "p2b"]
        )
        # ...and the search never left the documented depth.
        self.assertLessEqual(
            max(page for _, page in provider.calls), MAX_SEARCH_PAGES
        )

    def test_the_retrieval_stops_at_the_documented_page(self) -> None:
        """Depth is bounded: the documented pages, then the pool is what it is."""

        by_page = {
            ("брелок", page): [
                listing(f"Брелок металлический вариант {page}", f"p{page}")
            ]
            for page in range(1, 10)
        }
        provider = self.page_provider(by_page)
        service = SearchService([provider])

        run(service.find_products(SearchRequest(original_query="брелок")))

        self.assertEqual(
            provider.calls,
            [("брелок", page) for page in range(1, MAX_SEARCH_PAGES + 1)],
        )

    def test_a_full_pool_never_asks_a_deeper_page(self) -> None:
        full = [
            listing(f"Брелок инструмент {index} металлический", f"kc-{index}")
            for index in range(DEFAULT_MAX_RESULTS)
        ]
        provider = self.page_provider({("брелок инструмента", 1): full})
        service = SearchService([provider])

        outcome = run(
            service.find_products(SearchRequest(original_query="брелок инструмента"))
        )

        self.assertEqual(provider.calls, [("брелок инструмента", 1)])
        self.assertEqual(len(outcome.products), DEFAULT_MAX_RESULTS)

    def test_the_retrieval_depth_is_configurable(self) -> None:
        """``SEARCH_MAX_PAGES``: how deep ONE search walks is a deployment choice."""

        by_page = {
            ("брелок", page): [
                listing(f"Брелок металлический вариант {page}", f"p{page}")
            ]
            for page in range(1, 10)
        }

        shallow = self.page_provider(by_page)
        run(
            SearchService([shallow], max_search_pages=1).find_products(
                SearchRequest(original_query="брелок")
            )
        )
        self.assertEqual(shallow.calls, [("брелок", 1)])

        deep = self.page_provider(by_page)
        run(
            SearchService([deep], max_search_pages=5).find_products(
                SearchRequest(original_query="брелок")
            )
        )
        self.assertEqual(deep.calls, [("брелок", page) for page in range(1, 6)])

    def test_a_broken_depth_setting_still_asks_the_first_page(self) -> None:
        # A number that makes no sense must not produce a search with no call
        # at all: the user asked a question and deserves page 1.
        provider = self.page_provider(
            {("брелок", 1): [listing("Брелок металлический", "b1")]}
        )

        run(
            SearchService([provider], max_search_pages=0).find_products(
                SearchRequest(original_query="брелок")
            )
        )

        self.assertEqual(provider.calls, [("брелок", 1)])

    def test_a_request_starting_on_a_deeper_page_walks_on_from_there(self) -> None:
        """A caller that passes ``page_no`` continues the SAME bounded walk."""

        by_page = {
            ("брелок", page): [
                listing(f"Брелок металлический вариант {page}", f"p{page}")
            ]
            for page in range(1, 10)
        }
        provider = self.page_provider(by_page)

        run(
            SearchService([provider], max_search_pages=3).find_products(
                SearchRequest(original_query="брелок", page_no=2)
            )
        )

        self.assertEqual(provider.calls, [("брелок", 2), ("брелок", 3)])

    def test_a_photo_or_category_search_never_goes_past_the_first_page(self) -> None:
        # These flows take their breadth from their own pipeline (the photo
        # hypotheses, the catalogue feed), so depth is not added on top of it:
        # their number of marketplace calls stays bounded by that pipeline.
        provider = self.page_provider({})
        service = SearchService([provider])

        run(service.find_products(SearchRequest(original_query="x"), broaden=False))
        run(
            service.find_products(
                SearchRequest(
                    original_query="🔧 Инструменты",
                    normalized_query="hand tools hardware",
                    mode=SearchMode.CATEGORY,
                    keywords=["инструмент"],
                )
            )
        )

        self.assertEqual(provider.calls, [("x", 1), ("hand tools hardware", 1)])

    def test_a_category_query_costs_exactly_one_query(self) -> None:
        provider = self.provider({})
        service = SearchService([provider])
        request = SearchRequest(
            original_query="🔧 Инструменты",
            normalized_query="hand tools hardware",
            mode=SearchMode.CATEGORY,
            keywords=["инструмент", "ключ"],
        )

        run(service.find_products(request))

        self.assertEqual(provider.queries, ["hand tools hardware"])

    def test_broaden_false_keeps_the_single_primary_query(self) -> None:
        provider = self.provider({})
        service = SearchService([provider])
        request = SearchRequest(
            original_query="брелок в виде строительного инструмента",
            normalized_query="keychain construction tool",
        )

        run(service.find_products(request, broaden=False))

        self.assertEqual(provider.queries, ["keychain construction tool"])


class QualityWishRankingTests(unittest.TestCase):
    """§11: «с хорошим рейтингом» reorders REAL data, never filters."""

    def request(self, **overrides) -> SearchRequest:
        data: dict = {"original_query": "брелок с хорошим рейтингом"}
        data.update(overrides)
        return SearchRequest(**data)

    def test_the_wish_orders_by_the_real_quality_field_within_the_tier(self) -> None:
        popular = listing(
            "Брелок инструмент металлический",
            "popular",
            orders_count=5000,
            positive_rate=81.0,
        )
        quality = listing(
            "Брелок инструмент стальной",
            "quality",
            orders_count=12,
            positive_rate=97.0,
        )

        with_wish = sort_products(
            [popular, quality], self.request(prefers_good_rating=True)
        )
        # Both are the SAME product type (one relevance tier), so the real
        # quality field the marketplace returned decides - orders cannot
        # buy the top spot once the user asked for quality.
        self.assertEqual([p.product_id for p in with_wish], ["quality", "popular"])

        without_wish = sort_products([popular, quality], self.request())
        self.assertEqual([p.product_id for p in without_wish], ["popular", "quality"])

    def test_an_unrated_product_sorts_after_a_rated_one(self) -> None:
        rated = listing(
            "Брелок инструмент латунный", "rated", positive_rate=70.0, orders_count=3
        )
        unrated = listing(
            "Брелок инструмент стальной", "unrated", positive_rate=None, orders_count=9
        )

        ordered = sort_products(
            [unrated, rated], self.request(prefers_good_rating=True)
        )

        self.assertEqual([p.product_id for p in ordered], ["rated", "unrated"])

    def test_the_wish_removes_nothing_and_invents_no_rating(self) -> None:
        products = [
            listing("Брелок инструмент латунный", "rated", positive_rate=93.0),
            listing("Брелок инструмент стальной", "unrated", positive_rate=None),
            listing("Брелок инструмент титановый", "titanium", positive_rate=55.0),
        ]

        ordered = sort_products(products, self.request(prefers_good_rating=True))

        self.assertEqual(len(ordered), len(products))
        self.assertEqual(
            {p.product_id for p in ordered}, {p.product_id for p in products}
        )
        # A share of positive feedback is never displayed as stars.
        for product in ordered:
            with self.subTest(product_id=product.product_id):
                self.assertIsNone(product.rating)

    def test_a_fuzzy_quality_phrase_becomes_a_ranking_wish_only(self) -> None:
        request = AIService._fallback_parse("брелок с хорошим рейтингом")

        self.assertTrue(request.prefers_good_rating)
        # Never a fabricated numeric filter («рейтинг от 4.0» would be a lie).
        self.assertIsNone(request.min_rating)

    def test_a_numeric_rating_stays_a_filter_and_not_a_wish(self) -> None:
        request = AIService._fallback_parse(
            "electric toothbrush rating above 4.5"
        )

        self.assertEqual(request.min_rating, 4.5)
        self.assertFalse(request.prefers_good_rating)


class ResultSpreadingTests(unittest.TestCase):
    """§3: relevance decides membership, diversity only SPREADS near-copies."""

    def setUp(self) -> None:
        self.providers: list[StubProvider] = []

    def tearDown(self) -> None:
        async def close_all() -> None:
            for provider in self.providers:
                await provider.aclose()

        if self.providers:
            run(close_all())

    def provider(self, by_query: dict[str, list[Product]]) -> VariantAwareProvider:
        provider = VariantAwareProvider(by_query)
        self.providers.append(provider)
        return provider

    def test_a_near_copy_is_moved_behind_the_different_products(self) -> None:
        products = [
            listing("Брелок инструмент набор молоток", "copy-1"),
            listing("Брелок инструмент набор молоток", "copy-2"),
            listing("Брелок инструмент отвёртка стальная", "other"),
        ]

        spread = feed_curation.spread_similar(products)

        self.assertEqual(
            [p.product_id for p in spread], ["copy-1", "other", "copy-2"]
        )

    def test_the_spread_removes_nothing_and_invents_nothing(self) -> None:
        products = [
            listing(f"Брелок инструмент набор молоток {index}", f"copy-{index}")
            for index in range(5)
        ]
        products.append(listing("Брелок инструмент отвёртка стальная", "other"))

        spread = feed_curation.spread_similar(products)

        self.assertEqual(len(spread), len(products))
        self.assertEqual(
            {p.product_id for p in spread}, {p.product_id for p in products}
        )
        for product in spread:
            with self.subTest(product_id=product.product_id):
                # The same objects come back, only in another order.
                self.assertIn(product, products)

    def test_a_single_product_is_returned_untouched(self) -> None:
        products = [listing("Брелок инструмент", "only")]

        self.assertEqual(feed_curation.spread_similar(products), products)

    def test_a_real_search_result_set_is_spread_the_same_way(self) -> None:
        provider = self.provider(
            {
                "брелок инструмента": [
                    listing("Брелок инструмент набор молоток", "copy-1"),
                    listing("Брелок инструмент набор молоток второй", "copy-2"),
                    listing("Брелок инструмент отвёртка стальная", "other"),
                ]
            }
        )
        service = SearchService([provider])

        outcome = run(
            service.find_products(
                SearchRequest(original_query="брелок инструмента"), broaden=False
            )
        )

        # Every relevant listing is still in the pool the user browses...
        self.assertEqual(
            {p.product_id for p in outcome.products}, {"copy-1", "copy-2", "other"}
        )
        # ...but the near-copy no longer sits next to its own twin on the first
        # screen of cards (the order itself stays the ranking's).
        self.assertLess(
            feed_curation.similarity(
                outcome.products[0].title, outcome.products[1].title
            ),
            feed_curation.NEAR_DUPLICATE_THRESHOLD,
        )


class DeepPoolProvider(StubProvider):
    """Answers page after page of ONE broad phrase, like a real search index.

    Page 1 holds the first ``PAGE`` listings, page 2 the next ones: this is what
    makes a deep raw pool (60-80 candidates) possible at all, and it is exactly
    the documented way the affiliate search answers a broad keyword.
    """

    PAGE = 50

    def __init__(self, phrase: str, products: list[Product]) -> None:
        super().__init__(
            marketplace=Marketplace.ALIEXPRESS,
            display_name="AliExpress",
            products=[],
            credentials=CONFIGURED_CREDENTIALS,
        )
        self.phrase = phrase
        self.products = list(products)
        self.calls: list[tuple[str, int]] = []

    async def search_products(self, request: SearchRequest) -> list[Product]:
        phrase = request.normalized_query or request.original_query
        page = max(1, int(request.page_no or 1))
        self.calls.append((phrase, page))
        if phrase != self.phrase:
            return []
        start = (page - 1) * self.PAGE
        return self.products[start : start + self.PAGE]


class SelectingAI:
    """A minimal AI double: the two selection seams, no network, no quota.

    ``is_available`` is True on purpose - the service asks the ASSEMBLED service
    whether a selection can really run (a deployment without a key must not pay
    for a deep pool), so the double has to answer that question too.
    """

    is_available = True

    def __init__(self, *, ids=None, order=None) -> None:
        self.ids = ids
        self.order = order
        self.calls: list[dict] = []

    async def select_feed_candidates(self, candidates, *, wanted=20, query=None):
        self.calls.append(
            {"seam": "feed", "pool": len(candidates), "wanted": wanted, "query": query}
        )
        return list(self.ids) if self.ids is not None else None

    async def rank_candidates(self, candidates, *, query=None):
        self.calls.append({"seam": "rank", "pool": len(candidates), "query": query})
        return list(self.order) if self.order is not None else None


class FeedSelectionTests(unittest.TestCase):
    """§2-§3: a deep raw pool, then AI junk-filtering down to 20 cards.

    Two promises are checked together, because one is useless without the other:
    the retrieval really gathers 60-80 RAW listings (a model cannot choose from a
    list that was already cut to 20), and the ids it returns are a SELECTION - the
    junk it left out never reaches a card, and a broken AI never empties a feed.
    """

    def setUp(self) -> None:
        self.providers: list[StubProvider] = []

    def tearDown(self) -> None:
        async def close_all() -> None:
            for provider in self.providers:
                await provider.aclose()

        if self.providers:
            run(close_all())

    @staticmethod
    def _raw_pool(count: int) -> list[Product]:
        """``count`` genuinely DIFFERENT real listings of one broad phrase.

        The titles differ by more than a two-digit number on purpose: the
        curation layer's title comparison ignores tokens shorter than three
        characters, so numbered copies of one title ARE the same product for it
        (by design) - and a pool holding one product is not a pool.
        """

        adjectives = ("спортивные", "студийные", "детские")
        nouns = ("вкладыши", "накладки", "затычки")
        return [
            listing(
                f"Наушники {adjectives[index % 3]} {nouns[index // 3 % 3]} "
                f"bluetooth {100 + index}",
                f"np-{index}",
                rating=4.7,
                orders_count=900 + index,
            )
            for index in range(count)
        ]

    def _service(self, products, ai) -> tuple[SearchService, DeepPoolProvider]:
        provider = DeepPoolProvider("наушники", products)
        self.providers.append(provider)
        return SearchService([provider], ai_service=ai), provider

    def test_a_search_with_a_selector_gathers_a_deep_raw_pool(self) -> None:
        ai = SelectingAI(ids=[f"np-{index}" for index in range(AI_FEED_RESULTS)])
        service, provider = self._service(self._raw_pool(80), ai)

        outcome = run(service.find_products(SearchRequest(original_query="наушники")))

        # Two documented pages: one page of 50 cannot hold a pool of 80.
        self.assertEqual(provider.calls, [("наушники", 1), ("наушники", 2)])
        # The model saw the DEEP pool, at the agreed ceiling and with the user's
        # own wording - not a pre-cut list of 20 that leaves nothing to reject.
        self.assertGreaterEqual(ai.calls[0]["pool"], 60)
        self.assertEqual(ai.calls[0]["wanted"], AI_FEED_RESULTS)
        self.assertEqual(ai.calls[0]["query"], "наушники")
        # ...and the carousel walks exactly what the selection approved.
        self.assertEqual(
            [product.product_id for product in outcome.products],
            [f"np-{index}" for index in range(AI_FEED_RESULTS)],
        )

    def test_the_selection_is_a_selection_not_a_reorder(self) -> None:
        chosen = [f"np-{index}" for index in range(5, 25)]
        ai = SelectingAI(ids=chosen)
        service, _provider = self._service(self._raw_pool(80), ai)

        outcome = run(service.find_products(SearchRequest(original_query="наушники")))

        self.assertEqual([product.product_id for product in outcome.products], chosen)
        # The derived views were rebuilt for the new list, not left from the pool.
        self.assertEqual(len(outcome.comparisons), len(chosen))

    def test_an_id_the_model_invented_cannot_reach_a_card(self) -> None:
        ai = SelectingAI(ids=["np-0", "нет-такого-товара", "np-1"])
        service, _provider = self._service(self._raw_pool(80), ai)

        outcome = run(service.find_products(SearchRequest(original_query="наушники")))

        self.assertEqual(
            [product.product_id for product in outcome.products], ["np-0", "np-1"]
        )

    def test_a_chatty_selection_is_capped_at_twenty(self) -> None:
        # The model lists the WHOLE pool: the cap, not the model, decides.
        ai = SelectingAI(ids=[f"np-{index}" for index in range(80)])
        service, _provider = self._service(self._raw_pool(80), ai)

        outcome = run(service.find_products(SearchRequest(original_query="наушники")))

        self.assertEqual(len(outcome.products), AI_FEED_RESULTS)

    def test_a_declined_selection_keeps_the_deterministic_feed(self) -> None:
        # ``None`` is what the AI answers on quota, outage or unusable JSON.
        ai = SelectingAI(ids=None)
        service, _provider = self._service(self._raw_pool(80), ai)

        outcome = run(service.find_products(SearchRequest(original_query="наушники")))

        self.assertEqual(len(outcome.products), DEFAULT_MAX_RESULTS)

    def test_a_selector_that_raises_does_not_break_the_search(self) -> None:
        class BrokenAI:
            is_available = True

            async def select_feed_candidates(self, candidates, **kwargs):
                raise RuntimeError("boom")

        service, _provider = self._service(self._raw_pool(80), BrokenAI())

        outcome = run(service.find_products(SearchRequest(original_query="наушники")))

        self.assertEqual(len(outcome.products), DEFAULT_MAX_RESULTS)

    def test_a_slow_selector_cannot_hold_the_finished_list_hostage(self) -> None:
        # A hung Gemini answer used to block the finished pool for the FULL
        # ``AIService.timeout_seconds`` (20s in production). The selection is
        # bounded now: past the documented bound the deterministic list ships
        # immediately - same promise as a failed or unusable answer.
        class SlowAI:
            is_available = True

            async def select_feed_candidates(self, candidates, **kwargs):
                # The real selector is an async network call: it parks on I/O,
                # it does not block the loop - await instead of time.sleep.
                await asyncio.sleep(FEED_SELECTION_TIMEOUT_SECONDS + 10.0)
                return [product.product_id for product in candidates]

        service, _provider = self._service(self._raw_pool(80), SlowAI())

        started = time.perf_counter()
        outcome = run(service.find_products(SearchRequest(original_query="наушники")))
        elapsed = time.perf_counter() - started

        # Bounded: well under the sleep the slow selector asked for.
        self.assertLess(elapsed, FEED_SELECTION_TIMEOUT_SECONDS + 1.0)
        self.assertGreaterEqual(elapsed, FEED_SELECTION_TIMEOUT_SECONDS * 0.5)
        # ...and the deterministic list shipped, not an empty screen.
        self.assertEqual(len(outcome.products), DEFAULT_MAX_RESULTS)

    def test_without_a_selector_nothing_about_the_search_changes(self) -> None:
        # No AI service at all: the browse cap is the target, ONE page is asked
        # and the deterministic list is returned - the previous behaviour.
        provider = DeepPoolProvider("наушники", self._raw_pool(80))
        self.providers.append(provider)
        service = SearchService([provider])

        self.assertEqual(service.pool_target, DEFAULT_MAX_RESULTS)
        outcome = run(service.find_products(SearchRequest(original_query="наушники")))

        self.assertEqual(provider.calls, [("наушники", 1)])
        self.assertEqual(len(outcome.products), DEFAULT_MAX_RESULTS)

    def test_a_service_without_a_client_does_not_pay_for_a_deep_pool(self) -> None:
        # No key configured: the selector exists but can never answer, so the
        # deep pool (and its extra API call) would be pure waste.
        provider = DeepPoolProvider("наушники", self._raw_pool(80))
        self.providers.append(provider)
        service = SearchService([provider], ai_service=AIService(api_key=None))

        self.assertFalse(service.has_feed_selector)
        self.assertEqual(service.pool_target, DEFAULT_MAX_RESULTS)

    def test_the_pool_target_is_never_below_the_browse_cap(self) -> None:
        # A deployment that raised its own cap keeps it: the AI pool is a floor.
        service, _provider = self._service(self._raw_pool(10), SelectingAI(ids=[]))

        self.assertGreaterEqual(service.pool_target, service.max_results)
        self.assertGreaterEqual(service.pool_target, RAW_POOL_TARGET)

    def test_a_flow_that_owns_its_breadth_selects_nothing_per_call(self) -> None:
        ai = SelectingAI(ids=["np-0"])
        service, provider = self._service(self._raw_pool(80), ai)

        outcome = run(
            service.find_products(
                SearchRequest(original_query="наушники"), broaden=False
            )
        )

        # One page, NO AI request and the browse cap: the flow that owns its
        # breadth decides once, on the pool it has finished building.
        self.assertEqual(provider.calls, [("наушники", 1)])
        self.assertEqual(ai.calls, [])
        self.assertEqual(len(outcome.products), DEFAULT_MAX_RESULTS)

    def test_the_finished_pool_of_a_flow_can_be_selected_once(self) -> None:
        ai = SelectingAI(ids=[f"np-{index}" for index in range(3)])
        service, _provider = self._service(self._raw_pool(80), ai)
        outcome = SearchOutcome(
            request=SearchRequest(original_query="наушники"),
            products=self._raw_pool(80),
        )

        run(service.apply_feed_selection(outcome))

        self.assertEqual(len(ai.calls), 1)
        self.assertEqual(
            [product.product_id for product in outcome.products],
            ["np-0", "np-1", "np-2"],
        )
        self.assertEqual(len(outcome.comparisons), 3)

    def test_a_discovery_feed_hides_what_the_model_rejected(self) -> None:
        provider = VariantAwareProvider({"наушники": self._raw_pool(30)})
        self.providers.append(provider)
        chosen = [f"np-{index}" for index in range(10)]
        ai = SelectingAI(order=chosen)
        service = SearchService([provider], ai_service=ai)

        outcome = run(service.find_discovery("прикольные наушники"))

        shown = [product.product_id for product in outcome.products]
        self.assertEqual(set(shown), set(chosen))
        self.assertNotIn("np-25", shown)
        # The user's own wording travelled with the pool.
        self.assertIn("наушники", ai.calls[0]["query"])


class PhotoPoolTests(unittest.TestCase):
    """§21: a photo opens the same pool every other flow can browse."""

    def test_the_photo_pool_matches_the_documented_browse_cap(self) -> None:
        self.assertEqual(discovery.PHOTO_ENOUGH_RESULTS, DEFAULT_MAX_RESULTS)
        self.assertEqual(DEFAULT_MAX_RESULTS, 30)

    def test_the_photo_flow_asks_for_one_selection_on_its_merged_pool(self) -> None:
        """§3: one photo costs ONE selection, on the pool it has finished.

        The flow owns its breadth (several hypothesised searches), so a selection
        per hypothesis would be several requests for a list that is not final.
        """

        class SelectingFake(FakeSearchService):
            def __init__(self, outcome) -> None:
                super().__init__(outcome)
                self.selected: list[int] = []

            async def apply_feed_selection(self, outcome) -> None:
                self.selected.append(len(outcome.products))

        products = [
            listing(f"Спиннинг телескопический {index} карбон", f"rp-{index}")
            for index in range(3)
        ]
        search = SelectingFake(
            SearchOutcome(
                request=SearchRequest(original_query="спиннинг"),
                products=products,
            )
        )
        context = photo_context(StructuredVisionAI(TEA_PHOTO), search)

        run(discovery.photo_message(photo_update(PhotoMessage()), context))

        self.assertEqual(len(search.selected), 1, search.selected)
        self.assertGreater(search.selected[0], 0)

    def test_photo_search_adds_no_text_level_broadening(self) -> None:
        """Its breadth is the bounded hypothesis loop, nothing else."""

        search = FakeSearchService()
        context = photo_context(StructuredVisionAI(TEA_PHOTO), search)

        run(discovery.photo_message(photo_update(PhotoMessage()), context))

        self.assertTrue(search.broadened)
        self.assertFalse(any(search.broadened), search.broadened)

    def test_the_photo_flow_never_stops_after_two_products(self) -> None:
        # A pool that is not full keeps the loop running through the
        # grounded hypotheses instead of stopping at the first few cards.
        self.assertGreater(discovery.PHOTO_ENOUGH_RESULTS, 2)
        self.assertLessEqual(discovery.MAX_PHOTO_ATTEMPTS, 6)


if __name__ == "__main__":
    unittest.main()
