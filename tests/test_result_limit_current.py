from __future__ import annotations

import asyncio
import unittest

import httpx

from config.settings import MarketplaceCredentials
from handlers import search as search_handlers
from models.product import Marketplace, Product
from models.search_request import SearchRequest
from services.search_service import DEFAULT_MAX_RESULTS, SearchService
from services.text_matching import extract_requested_result_count, strip_option_count


CONFIGURED = MarketplaceCredentials("key", "secret")


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


class ResultLimitWiringTests(unittest.TestCase):
    def test_count_is_attached_to_current_search_request(self):
        request = SearchRequest(
            original_query="покажи 3 варианта шуруповерта",
            normalized_query="шуруповерт",
        )
        updated = search_handlers._with_result_limit(request, 3)
        self.assertEqual(updated.result_limit, 3)
        self.assertEqual(updated.normalized_query, "шуруповерт")
        self.assertIsNone(request.result_limit)

    def test_option_count_is_removed_from_query_phrase(self):
        self.assertEqual(
            strip_option_count("покажи 3 варианта шуруповерта"),
            "шуруповерта",
        )



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


if __name__ == "__main__":
    unittest.main()
