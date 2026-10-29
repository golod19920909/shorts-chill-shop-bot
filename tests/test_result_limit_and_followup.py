"""Regression tests for the two confirmed delivery problems.

1. A requested product count («покажи 3 варианта», «show 5 earbuds») must
   survive parsing and cap the delivered card list; with no count the
   existing default (the browse cap / AI feed 20) stays untouched, and a
   smaller relevant pool is never padded.

2. After the product cards the bot must NOT send the model's own text
   (which may list different products); it sends a short follow-up built
   from what was actually delivered.

Everything is offline: stub providers, no Gemini, no Telegram.
"""

from __future__ import annotations

import asyncio
import unittest

import httpx

from config.settings import MarketplaceCredentials
from handlers import search as search_handlers
from models.product import Marketplace, Product
from models.search_request import SearchMode, SearchRequest
from services.ai_service import AIService
from services.marketplaces.base import MarketplaceProvider
from services.search_service import DEFAULT_MAX_RESULTS, SearchOutcome, SearchService
from services.text_matching import extract_requested_result_count

CONFIGURED = MarketplaceCredentials("key", "secret")
EMPTY = MarketplaceCredentials(None, None)


def make_product(product_id: str, price: float = 10.0) -> Product:
    """A fixture shaped like the ones the existing search tests use."""

    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=product_id,
        title=f"Шуруповерт аккумуляторный {product_id}",
        product_url=f"https://www.aliexpress.com/item/{product_id}.html",
        image_url=f"https://ae01.alicdn.com/kf/{product_id}.jpg",
        price=price,
        currency="USD",
        orders_count=100,
        rating=4.7,
        affiliate_url=f"https://s.click.aliexpress.com/e/_{product_id}",
    )


class StubProvider(MarketplaceProvider):
    marketplace = Marketplace.ALIEXPRESS
    display_name = "AliExpress"

    def __init__(self, products, credentials=CONFIGURED):
        super().__init__(credentials, httpx.AsyncClient())
        self._products = products

    async def search_products(self, request):
        return list(self._products)

    async def get_product(self, product_id):
        return next((p for p in self._products if p.product_id == product_id), None)

    def generate_affiliate_link(self, product):
        return None


class CountExtractionTests(unittest.TestCase):
    """The deterministic count capture (``extract_requested_result_count``)."""

    def test_option_counts_are_captured(self) -> None:
        self.assertEqual(extract_requested_result_count("покажи 3 варианта шуруповерта"), 3)
        self.assertEqual(extract_requested_result_count("дай 3 варианта шуруповерта"), 3)
        self.assertEqual(extract_requested_result_count("покажи 5 наушников"), 5)
        self.assertEqual(extract_requested_result_count("show 5 earbuds"), 5)
        self.assertEqual(extract_requested_result_count("найди 2 вариантов ноутбука"), 2)

    def test_units_per_item_are_not_a_result_count(self) -> None:
        # «3 шт» / «100 pieces» is UNITS in a product, never how many cards.
        self.assertIsNone(extract_requested_result_count("наушники 3 шт"))
        self.assertIsNone(extract_requested_result_count("Find 100 pieces of earbuds"))

    def test_no_number_no_count(self) -> None:
        self.assertIsNone(extract_requested_result_count("беспроводные наушники"))
        self.assertIsNone(extract_requested_result_count("шуруповерт, несколько вариантов"))
        self.assertIsNone(extract_requested_result_count(""))


class ParseTests(unittest.TestCase):
    """``parse_query`` attaches the count on every path, model and fallback."""

    def test_fallback_parse_attaches_result_limit(self) -> None:
        request = asyncio.run(AIService(api_key=None).parse_query("покажи 3 варианта шуруповерта"))
        self.assertEqual(request.result_limit, 3)
        # The count never leaks into the retrieval phrase.
        self.assertNotIn("3", request.normalized_query or "")
        self.assertIn("шуруповерт", (request.normalized_query or "").lower())

    def test_no_count_keeps_default(self) -> None:
        request = asyncio.run(AIService(api_key=None).parse_query("беспроводные наушники"))
        self.assertIsNone(request.result_limit)


class DeliveryCapTests(unittest.TestCase):
    """The cap is applied to the delivered list, never padded."""

    def setUp(self) -> None:
        self.products = [make_product(f"p{i}") for i in range(30)]

    def _search(self, request: SearchRequest, pool=None):
        provider = StubProvider(pool if pool is not None else self.products)
        service = SearchService([provider], ai_service=None)
        try:
            return asyncio.run(service.find_products(request))
        finally:
            asyncio.run(provider.aclose())

    def test_requested_count_caps_delivered_cards(self) -> None:
        for wanted in (3, 5):
            with self.subTest(wanted=wanted):
                request = SearchRequest(
                    original_query="шуруповерт",
                    normalized_query="шуруповерт",
                    result_limit=wanted,
                )
                outcome = self._search(request)
                self.assertEqual(len(outcome.products), wanted)

    def test_no_count_keeps_default_cap_unchanged(self) -> None:
        request = SearchRequest(original_query="шуруповерт", normalized_query="шуруповерт")
        outcome = self._search(request)
        self.assertEqual(len(outcome.products), DEFAULT_MAX_RESULTS)

    def test_smaller_pool_is_never_padded(self) -> None:
        request = SearchRequest(
            original_query="шуруповерт", normalized_query="шуруповерт", result_limit=5
        )
        outcome = self._search(request, self.products[:2])
        self.assertEqual(len(outcome.products), 2)

    def test_units_quantity_never_caps_delivery(self) -> None:
        # quantity=20 is units per item - it must NOT cap the card list.
        request = SearchRequest(
            original_query="mouse, 20 pieces", normalized_query="mouse", quantity=20
        )
        outcome = self._search(request)
        self.assertEqual(len(outcome.products), DEFAULT_MAX_RESULTS)


class FollowUpTests(unittest.TestCase):
    """The post-card follow-up is deterministic and consistent."""

    def test_follow_up_matches_delivered_count(self) -> None:
        request = SearchRequest(original_query="q")
        two = SearchOutcome(request=request, products=[make_product("a"), make_product("b")])
        text = search_handlers._agent_follow_up_text(two)
        self.assertIn("2", text)
        self.assertNotIn(make_product("a").title, text)
        one = SearchOutcome(request=request, products=[make_product("a")])
        self.assertIn("один", search_handlers._agent_follow_up_text(one))
        self.assertIsNone(search_handlers._agent_follow_up_text(
            SearchOutcome(request=request)))

    def test_follow_up_names_the_real_delivered_count(self) -> None:
        request = SearchRequest(original_query="q")
        five = SearchOutcome(request=request, products=[make_product(str(i)) for i in range(5)])
        text = search_handlers._agent_follow_up_text(five)
        self.assertIn("Нашёл 5 вариантов", text)
        self.assertIn("листайте карточки кнопкой «➡️»", text)
        self.assertIn("Могу сравнить их или поискать подешевле.", text)


if __name__ == "__main__":
    unittest.main()
