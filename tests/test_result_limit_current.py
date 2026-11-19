from __future__ import annotations

import asyncio
import unittest

import httpx

from config.settings import MarketplaceCredentials
from models.product import Marketplace, Product
from models.search_request import SearchRequest
from services.search_service import SearchOutcome
from services.ai_service import AIService
from services.marketplaces.base import MarketplaceProvider
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


class ResultLimitParseTests(unittest.TestCase):
    def test_fallback_parse_attaches_result_limit(self):
        request = asyncio.run(
            AIService(api_key=None).parse_query("покажи 3 варианта шуруповерта")
        )
        self.assertEqual(request.result_limit, 3)
        self.assertNotIn("3", request.normalized_query or "")
        self.assertIn("шуруповерт", (request.normalized_query or "").lower())

    def test_parse_without_count_keeps_default(self):
        request = asyncio.run(
            AIService(api_key=None).parse_query("беспроводные наушники")
        )
        self.assertIsNone(request.result_limit)
