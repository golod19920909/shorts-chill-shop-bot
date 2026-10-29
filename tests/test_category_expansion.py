"""G24.8 P1.9: validated CATEGORY multi-query expansion.

The patch gives a MAPPED category (HOME/ELECTRONICS/GARDEN) exactly ONE
extra marketplace question - its P1.8-C validated keyword - whose raw rows
join the primary pool BEFORE the single existing pipeline (identity dedup →
relevance → category ranking → spread → curation gate). Everything here
runs with stub providers: no network, no credentials, no live API.

Covered, per the P1.9 test contract:

1. HOME expansion keyword is exactly «быт»;
2. ELECTRONICS expansion keyword is exactly «колонка»;
3. GARDEN expansion keyword is exactly «шланг»;
4. TOOLS has no expansion (and so does every other unmapped category);
5. the CATEGORY primary query is still executed;
6. the expansion query runs exactly ONCE per validated category;
7. non-target categories execute only the primary query;
8. the expansion request receives the same country;
9. the expansion request receives the same currency;
10. expansion results are unioned with the primary results;
11. exact duplicates are not duplicated downstream (A,B + B,C → A,B,C);
12. an expansion API failure never destroys the primary result;
13. an empty expansion result never destroys the primary result;
14. SEARCH mode does NOT use category expansion;
15. POPULAR/FEED/legacy modes do NOT use category expansion;
16. the existing category ranking stays in control of the final order.

RECOMMENDATION is a CURATION mode (``curation.pipeline.MODE_RECOMMENDATION``)
that runs after ``find_products``; no curation file is touched by this patch,
and none of the request modes feeding a curation flow (POPULAR, FEED,
DISCOVERY, DEALS) produces an expansion request either - proven below.
"""

from __future__ import annotations

import asyncio
import unittest

import httpx

from config.catalog import CATEGORIES, Category
from config.settings import MarketplaceCredentials
from models.product import Marketplace, Product
from models.search_request import SearchMode, SearchRequest
from services import feed_curation
from services.comparison_service import sort_products
from services.marketplaces.base import MarketplaceProvider
from services.product_identity import deduplicate_products
from services.relevance import filter_relevant
from services.search_service import (
    CATEGORY_EXPANSION_KEYWORDS,
    SearchService,
    category_expansion_phrase,
)

CONFIGURED = MarketplaceCredentials("key", "secret")

#: The three candidates G24.8 P1.8-C validated - the ONLY ones this patch
#: may add («P1.8-CANDIDATE-VALIDATION PASS — PRODUCTION PATCH READY»).
VALIDATED = {"home": "быт", "electronics": "колонка", "garden": "шланг"}


def run(coro):
    return asyncio.run(coro)


def category(code: str) -> Category:
    return next(cat for cat in CATEGORIES if cat.code == code)


def category_request(code: str, **overrides) -> SearchRequest:
    """Exactly the request ``SearchService.find_by_category`` builds."""

    cat = category(code)
    data = {
        "original_query": cat.title,
        "normalized_query": cat.query,
        "mode": SearchMode.CATEGORY,
        "category": cat.code,
        "keywords": list(cat.keywords),
    }
    data.update(overrides)
    return SearchRequest(**data)


def listing(product_id: str, title: str, *, orders: int = 100) -> Product:
    """A real-shaped Russian AliExpress listing for a category fixture."""

    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=product_id,
        title=title,
        product_url=f"https://www.aliexpress.com/item/{product_id}.html",
        image_url=f"https://ae01.alicdn.com/kf/{product_id}.jpg",
        price=10.0,
        currency="USD",
        orders_count=orders,
    )


#: Fixture titles - every word a declared keyword of its own category, so
#: the UNCHANGED relevance gate keeps them (the patch must not be judged on
#: products the production gate would reject anyway).
HOME_TITLES = {
    "a": "Органайзер кухонный для хранения дома",
    "b": "Полотенце кухонное хлопковое для дома",
    "c": "Термос стальной для хранения на кухне",
}
ELECTRONICS_TITLES = {
    "a": "Наушники беспроводные TWS с кейсом",
    "b": "Кабель зарядки USB для смартфона",
    "c": "Портативная колонка Bluetooth для дома",
}
GARDEN_TITLES = {
    "a": "Шланг садовый для полива дача",
    "b": "Секатор садовый для обрезки дача",
    "c": "Лейка садовая для полива дача",
}


def titles_for(code: str) -> dict[str, str]:
    return {
        "home": HOME_TITLES,
        "electronics": ELECTRONICS_TITLES,
        "garden": GARDEN_TITLES,
    }[code]


def primary_title(code: str) -> str:
    """One valid title for ANY category (its first declared keyword)."""

    if code in VALIDATED:
        return titles_for(code)["a"]
    return f"Товар {category(code).keywords[0]} для дома и хранения"


class PhraseStubProvider(MarketplaceProvider):
    """Provider answering per retrieval phrase (no network).

    ``pools`` maps a phrase to the products that phrase returns; a phrase in
    ``fail_phrases`` raises, exactly like a real API failure (the search
    service captures it with ``return_exceptions=True``).
    """

    marketplace = Marketplace.ALIEXPRESS
    display_name = "AliExpress"
    supports_feed = True

    def __init__(
        self,
        pools: dict[str, list[Product]],
        *,
        fail_phrases: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(CONFIGURED, httpx.AsyncClient())
        self.pools = {phrase: list(items) for phrase, items in pools.items()}
        self.fail_phrases = set(fail_phrases)
        self.requests: list[SearchRequest] = []

    async def search_products(self, request: SearchRequest) -> list[Product]:
        self.requests.append(request)
        phrase = request.normalized_query or request.original_query or ""
        if phrase in self.fail_phrases:
            raise RuntimeError("expansion endpoint down")
        return list(self.pools.get(phrase, []))

    async def get_product(self, product_id: str) -> Product | None:
        return None

    def generate_affiliate_link(self, product: Product) -> str | None:
        return None

    async def fetch_feed_page(self, **kwargs):
        return []


def asked_phrases(provider: PhraseStubProvider) -> list[str]:
    return [
        (request.normalized_query or request.original_query or "")
        for request in provider.requests
    ]


def expected_final(products: list[Product], request: SearchRequest) -> list[Product]:
    """The UNCHANGED production chain over a pool, minus no-op steps.

    identity dedup → relevance → sort (category ranking) → spread - the
    same functions ``SearchService._collect`` runs (``filter_products`` is
    a no-op for these fixture requests: no price/rating constraints; the
    history ``exclude`` is empty; the cap of 30 never binds here).
    """

    unique = deduplicate_products(list(products))
    relevant, _dropped = filter_relevant(unique, request)
    ranked = sort_products(list(relevant), request)
    return list(feed_curation.spread_similar(ranked))


class ExpansionMappingTests(unittest.TestCase):
    """1-4: the exact, deterministic, catalog-consistent mapping."""

    def test_home_expansion_keyword_is_exactly_byt(self) -> None:
        self.assertEqual(CATEGORY_EXPANSION_KEYWORDS["home"], "быт")
        self.assertEqual(category_expansion_phrase(category_request("home")), "быт")

    def test_electronics_expansion_keyword_is_exactly_kolonka(self) -> None:
        self.assertEqual(CATEGORY_EXPANSION_KEYWORDS["electronics"], "колонка")
        self.assertEqual(
            category_expansion_phrase(category_request("electronics")), "колонка"
        )

    def test_garden_expansion_keyword_is_exactly_shlang(self) -> None:
        self.assertEqual(CATEGORY_EXPANSION_KEYWORDS["garden"], "шланг")
        self.assertEqual(category_expansion_phrase(category_request("garden")), "шланг")

    def test_tools_has_no_expansion(self) -> None:
        self.assertNotIn("tools", CATEGORY_EXPANSION_KEYWORDS)
        self.assertIsNone(category_expansion_phrase(category_request("tools")))

    def test_only_the_three_validated_categories_are_mapped(self) -> None:
        self.assertEqual(set(CATEGORY_EXPANSION_KEYWORDS), set(VALIDATED))

    def test_every_mapped_keyword_is_already_a_catalog_keyword(self) -> None:
        # No new wording enters the catalog: the expansion reuses the
        # keyword the category itself already declares.
        for code, keyword in VALIDATED.items():
            with self.subTest(code=code):
                self.assertIn(keyword, category(code).keywords)

    def test_unmapped_category_requests_have_no_expansion_phrase(self) -> None:
        for cat in CATEGORIES:
            if cat.code in VALIDATED:
                continue
            with self.subTest(code=cat.code):
                self.assertIsNone(category_expansion_phrase(category_request(cat.code)))


class CategoryExpansionCallTests(unittest.TestCase):
    """5-7: the primary always runs; the expansion runs exactly once."""

    def test_primary_query_is_still_executed_for_validated_categories(
        self,
    ) -> None:
        for code in VALIDATED:
            with self.subTest(code=code):
                cat = category(code)
                provider = PhraseStubProvider(
                    {cat.query: [listing("p1", primary_title(code))]}
                )
                service = SearchService([provider], ai_service=None)
                run(service.find_by_category(cat))
                run(provider.aclose())
                self.assertEqual(asked_phrases(provider)[0], cat.query)

    def test_expansion_runs_exactly_once_per_validated_category(self) -> None:
        # 5 + 6 together: primary once, expansion once, exactly TWO API
        # calls per validated category open (the P1.9 budget: 1 → 2).
        for code, keyword in VALIDATED.items():
            with self.subTest(code=code):
                cat = category(code)
                titles = titles_for(code)
                provider = PhraseStubProvider(
                    {
                        cat.query: [
                            listing("p1", titles["a"]),
                            listing("p2", titles["b"]),
                        ],
                        keyword: [listing("p3", titles["c"])],
                    }
                )
                service = SearchService([provider], ai_service=None)
                run(service.find_by_category(cat))
                run(provider.aclose())
                phrases = asked_phrases(provider)
                self.assertEqual(len(provider.requests), 2)
                self.assertEqual(phrases.count(cat.query), 1)
                self.assertEqual(phrases.count(keyword), 1)

    def test_unmapped_categories_execute_only_the_primary_query(self) -> None:
        # 7: every category without a validated candidate - TOOLS included -
        # costs exactly ONE call, exactly as before the patch.
        for cat in CATEGORIES:
            if cat.code in VALIDATED:
                continue
            with self.subTest(code=cat.code):
                provider = PhraseStubProvider(
                    {cat.query: [listing("p1", primary_title(cat.code))]}
                )
                service = SearchService([provider], ai_service=None)
                run(service.find_by_category(cat))
                run(provider.aclose())
                self.assertEqual(len(provider.requests), 1)
                self.assertEqual(asked_phrases(provider), [cat.query])

    def test_a_full_primary_pool_still_asks_the_expansion(self) -> None:
        # The early stop must never skip the expansion: its value is the
        # DIVERSITY of the union, not volume. A primary pool that already
        # fills the 30-card cap still gets its one extra keyword asked, and
        # the final cut is made from the RANKED union.
        cat = category("home")
        crowd = [
            listing(f"c{i}", f"Органайзер кухонный для хранения дома {i}")
            for i in range(35)
        ]
        provider = PhraseStubProvider(
            {cat.query: crowd, "быт": [listing("x1", HOME_TITLES["c"])]}
        )
        service = SearchService([provider], ai_service=None)
        run(service.find_by_category(cat))
        run(provider.aclose())

        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(asked_phrases(provider).count("быт"), 1)


class PropagationTests(unittest.TestCase):
    """8-9: the expansion rides the SAME request (country + currency)."""

    def test_expansion_request_receives_the_same_country(self) -> None:
        for code in VALIDATED:
            for country in ("DE", "US"):
                with self.subTest(code=code, country=country):
                    cat = category(code)
                    provider = PhraseStubProvider(
                        {
                            cat.query: [listing("p1", primary_title(code))],
                            VALIDATED[code]: [
                                listing("p2", titles_for(code)["c"])
                            ],
                        }
                    )
                    service = SearchService([provider], ai_service=None)
                    run(service.find_by_category(cat, country=country))
                    run(provider.aclose())
                    self.assertEqual(
                        [request.country for request in provider.requests],
                        [country, country],
                    )

    def test_expansion_request_receives_the_same_currency(self) -> None:
        cat = category("home")
        for currency in (None, "USD", "UAH"):
            with self.subTest(currency=currency):
                provider = PhraseStubProvider(
                    {
                        cat.query: [listing("p1", HOME_TITLES["a"])],
                        "быт": [listing("p2", HOME_TITLES["c"])],
                    }
                )
                service = SearchService([provider], ai_service=None)
                run(service.find_by_category(cat, currency=currency))
                run(provider.aclose())
                self.assertEqual(
                    [request.currency for request in provider.requests],
                    [currency, currency],
                )
                # The primary keeps its own request object; the expansion
                # is a copy with ONLY the phrase replaced - every other
                # field (keywords, category, mode, country) travels with it.
                self.assertEqual(
                    provider.requests[1].normalized_query, "быт"
                )
                self.assertEqual(
                    provider.requests[1].keywords, provider.requests[0].keywords
                )
                self.assertEqual(provider.requests[1].category, "home")
                self.assertIs(provider.requests[1].mode, SearchMode.CATEGORY)


class UnionTests(unittest.TestCase):
    """10-11: union by EXISTING identity - not a concatenation."""

    def test_home_union_is_a_b_b_c_becomes_a_b_c(self) -> None:
        # The mandated regression: primary [A, B], expansion [B, C] (B is
        # the SAME product - same id, different tracking URL) → combined
        # pool [A, B, C], B exactly once downstream.
        cat = category("home")
        a = listing("a", HOME_TITLES["a"])
        b_primary = listing("b", HOME_TITLES["b"])
        b_expansion = Product(
            marketplace=Marketplace.ALIEXPRESS,
            product_id="b",
            title=HOME_TITLES["b"],
            product_url="https://www.aliexpress.com/item/b.html?spm=track",
            image_url="https://ae01.alicdn.com/kf/b.jpg",
            price=10.0,
            currency="USD",
            orders_count=100,
        )
        c = listing("c", HOME_TITLES["c"])
        provider = PhraseStubProvider(
            {cat.query: [a, b_primary], "быт": [b_expansion, c]}
        )
        service = SearchService([provider], ai_service=None)
        outcome = run(service.find_by_category(cat))
        run(provider.aclose())

        ids = [product.product_id for product in outcome.products]
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(sorted(ids), ["a", "b", "c"])
        self.assertEqual(ids.count("b"), 1)
        # ...and the order is the EXISTING chain over the deduped union,
        # not the raw concatenation [a, b, b, c].
        pool = [a, b_primary, b_expansion, c]
        self.assertEqual(
            ids,
            [p.product_id for p in expected_final(pool, outcome.request)],
        )

    def test_exact_duplicates_never_reach_downstream(self) -> None:
        # Both pools return the SAME products: the union stays the same
        # size, and no card is doubled.
        cat = category("electronics")
        titles = titles_for("electronics")
        products = [listing("p1", titles["a"]), listing("p2", titles["b"])]
        provider = PhraseStubProvider(
            {cat.query: products, "колонка": list(products)}
        )
        service = SearchService([provider], ai_service=None)
        outcome = run(service.find_by_category(cat))
        run(provider.aclose())

        ids = [product.product_id for product in outcome.products]
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(sorted(ids), ["p1", "p2"])
        self.assertEqual(len(ids), len(set(ids)))


class ResilienceTests(unittest.TestCase):
    """12-13: the optional expansion can fail or return nothing."""

    def test_expansion_failure_keeps_the_primary_result(self) -> None:
        # The primary query succeeds, the expansion query raises: the
        # primary products still reach the existing downstream pipeline,
        # and the search as a whole does NOT fail.
        cat = category("home")
        a = listing("a", HOME_TITLES["a"])
        b = listing("b", HOME_TITLES["b"])
        provider = PhraseStubProvider(
            {cat.query: [a, b]},
            fail_phrases=frozenset({"быт"}),
        )
        service = SearchService([provider], ai_service=None)
        outcome = run(service.find_by_category(cat))
        run(provider.aclose())

        ids = [product.product_id for product in outcome.products]
        self.assertEqual(len(provider.requests), 2)  # the call was attempted
        self.assertEqual(sorted(ids), ["a", "b"])
        self.assertEqual(
            ids, [p.product_id for p in expected_final([a, b], outcome.request)]
        )

    def test_empty_expansion_result_keeps_the_primary_result(self) -> None:
        # Zero products from the expansion behaves exactly like before.
        cat = category("garden")
        a = listing("a", GARDEN_TITLES["a"])
        b = listing("b", GARDEN_TITLES["b"])
        provider = PhraseStubProvider({cat.query: [a, b], "шланг": []})
        service = SearchService([provider], ai_service=None)
        outcome = run(service.find_by_category(cat))
        run(provider.aclose())

        ids = [product.product_id for product in outcome.products]
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(
            ids, [p.product_id for p in expected_final([a, b], outcome.request)]
        )


class ModeIsolationTests(unittest.TestCase):
    """14-15: ONLY CATEGORY mode may receive the additional query."""

    def test_category_mode_is_the_only_one_with_an_expansion(self) -> None:
        # Paired inputs: identical request except the mode - the helper
        # answers a phrase for CATEGORY and NOTHING for any other mode
        # (SEARCH, POPULAR, FEED, DISCOVERY, DEALS, IMAGE, PROMO).
        for mode in SearchMode:
            with self.subTest(mode=mode):
                phrase = category_expansion_phrase(
                    category_request("home", mode=mode)
                )
                if mode is SearchMode.CATEGORY:
                    self.assertEqual(phrase, "быт")
                else:
                    self.assertIsNone(phrase)

    def test_search_mode_never_asks_the_expansion_keyword(self) -> None:
        # 14: even a SEARCH request that (hypothetically) carries the home
        # category code never asks «быт» - the mode gate holds.
        cat = category("home")
        request = category_request("home", mode=SearchMode.SEARCH)
        provider = PhraseStubProvider({cat.query: [listing("p1", HOME_TITLES["a"])]})
        service = SearchService([provider], ai_service=None)
        run(service.find_products(request))
        run(provider.aclose())

        self.assertNotIn("быт", [p.casefold() for p in asked_phrases(provider)])

    def test_popular_and_feed_modes_never_ask_the_expansion(self) -> None:
        # 15: POPULAR (the legacy recommendation flow) and FEED - the modes
        # behind the curation RECOMMENDATION mode - never expand.
        for mode in (SearchMode.POPULAR, SearchMode.FEED, SearchMode.DISCOVERY,
                     SearchMode.DEALS):
            with self.subTest(mode=mode):
                cat = category("home")
                provider = PhraseStubProvider(
                    {cat.query: [listing("p1", HOME_TITLES["a"])]}
                )
                service = SearchService([provider], ai_service=None)
                run(service.find_products(category_request("home", mode=mode)))
                run(provider.aclose())
                self.assertNotIn(
                    "быт", [p.casefold() for p in asked_phrases(provider)]
                )

    def test_the_feed_page_flow_never_calls_product_search(self) -> None:
        # The «Находки» feed builds its own FEED request and talks to
        # ``fetch_feed_page`` only - category expansion cannot reach it.
        provider = PhraseStubProvider({})
        service = SearchService([provider], ai_service=None)
        run(service.find_feed_page())
        run(provider.aclose())
        self.assertEqual(provider.requests, [])

    def test_broaden_false_flow_keeps_its_single_query(self) -> None:
        # A flow that owns its breadth (photo hypotheses pass
        # ``broaden=False``) is left completely alone.
        cat = category("home")
        provider = PhraseStubProvider(
            {cat.query: [listing("p1", HOME_TITLES["a"])]}
        )
        service = SearchService([provider], ai_service=None)
        run(service.find_products(category_request("home"), broaden=False))
        run(provider.aclose())
        self.assertEqual(asked_phrases(provider), [cat.query])


class RankingControlTests(unittest.TestCase):
    """16: the EXISTING category ranking orders the final cards."""

    def test_ranking_over_the_union_beats_pool_concatenation(self) -> None:
        # Primary pool [A (weak popularity)], expansion pool [B (strong
        # popularity)], same title/tier/word-share - only the real quality
        # tie-break differs. The final order must be the EXISTING category
        # ranking over the union (B first), NOT the raw primary→expansion
        # concatenation (A first).
        cat = category("home")
        a = listing("a", "Органайзер кухонный для хранения дома", orders=10)
        b = listing("b", "Органайзер кухонный для хранения дома", orders=9999)
        provider = PhraseStubProvider({cat.query: [a], "быт": [b]})
        service = SearchService([provider], ai_service=None)
        outcome = run(service.find_by_category(cat))
        run(provider.aclose())

        ids = [product.product_id for product in outcome.products]
        expected = [p.product_id for p in expected_final([a, b], outcome.request)]
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(ids, expected)
        # The union, ranked, is not the concatenation of the pools:
        self.assertNotEqual(ids, ["a", "b"])
        self.assertEqual(sorted(ids), ["a", "b"])

    def test_expansion_products_pass_the_unchanged_relevance_gate(self) -> None:
        # The validated keyword's own product type is kept by the SAME
        # relevance functions - no second relevance pipeline exists.
        cat = category("electronics")
        expansion_product = listing("x1", ELECTRONICS_TITLES["c"])
        provider = PhraseStubProvider(
            {cat.query: [listing("p1", ELECTRONICS_TITLES["a"])],
             "колонка": [expansion_product]}
        )
        service = SearchService([provider], ai_service=None)
        outcome = run(service.find_by_category(cat))
        run(provider.aclose())

        self.assertIn("x1", [p.product_id for p in outcome.products])


if __name__ == "__main__":  # pragma: no cover - pytest is the entry point
    unittest.main()
