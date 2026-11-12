"""Offline tests for subcategory retrieval through the shared search service."""
from __future__ import annotations

import asyncio
import unittest

import httpx

from config.catalog import CATEGORIES
from config.settings import MarketplaceCredentials
from config.subcategories import get_subcategory
from models.product import Marketplace, Product
from models.search_request import SearchMode
from services.marketplaces.base import MarketplaceProvider
from services.search_service import SearchService

CONFIGURED = MarketplaceCredentials("key", "secret")


def run(coro):
    return asyncio.run(coro)


def listing(product_id: str, title: str) -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=product_id,
        title=title,
        product_url=f"https://www.aliexpress.com/item/{product_id}.html",
        image_url=f"https://ae01.alicdn.com/kf/{product_id}.jpg",
        price=10.0,
        currency="USD",
        orders_count=100,
    )


class PhraseProvider(MarketplaceProvider):
    marketplace = Marketplace.ALIEXPRESS
    display_name = "AliExpress"
    supports_feed = True

    def __init__(self, pools, failures=()):
        super().__init__(CONFIGURED, httpx.AsyncClient())
        self.pools = {key: list(value) for key, value in pools.items()}
        self.failures = set(failures)
        self.requests = []

    async def search_products(self, request):
        phrase = request.normalized_query or request.original_query
        self.requests.append(request)
        if phrase in self.failures:
            raise RuntimeError("route unavailable")
        return list(self.pools.get(phrase, []))

    async def get_product(self, product_id):
        return None

    def generate_affiliate_link(self, product):
        return None

    async def fetch_feed_page(self, **kwargs):
        return []


class SubcategorySearchTests(unittest.TestCase):
    def test_catalog_covers_all_existing_parent_categories(self):
        sub = get_subcategory("smart_home")
        self.assertIsNotNone(sub)
        self.assertIn(sub.category_code, {item.code for item in CATEGORIES})

    def test_subcategory_search_uses_all_bounded_routes_and_shared_request(self):
        sub = get_subcategory("audio")
        self.assertIsNotNone(sub)
        products = [
            listing("headphones", "Наушники беспроводные Bluetooth"),
            listing("speaker", "Колонка портативная Bluetooth"),
            listing("earbuds", "Наушники TWS беспроводные"),
        ]
        provider = PhraseProvider(dict(zip(sub.query_routes, [[products[0]], [products[1]], [products[2]]])))
        service = SearchService([provider], ai_service=None)
        outcome = run(service.find_by_subcategory(sub, country="DE", currency="EUR"))
        run(provider.aclose())

        self.assertEqual(
            [request.normalized_query for request in provider.requests],
            list(sub.query_routes),
        )
        self.assertEqual(len(provider.requests), 3)
        for request in provider.requests:
            self.assertIs(request.mode, SearchMode.CATEGORY)
            self.assertEqual(request.category, "electronics")
            self.assertEqual(request.subcategory_code, "audio")
            self.assertEqual(request.keywords, list(sub.keywords))
            self.assertEqual(request.country, "DE")
            self.assertEqual(request.currency, "EUR")
        self.assertEqual(
            {item.product_id for item in outcome.products},
            {"headphones", "speaker", "earbuds"},
        )

    def test_failed_alternate_route_does_not_discard_primary_results(self):
        sub = get_subcategory("kitchen")
        primary = listing("kitchen-tool", "Кухонная принадлежность для кухни")
        provider = PhraseProvider(
            {sub.query_routes[0]: [primary]},
            failures={sub.query_routes[1]},
        )
        service = SearchService([provider], ai_service=None)
        outcome = run(service.find_by_subcategory(sub))
        run(provider.aclose())

        self.assertEqual(len(provider.requests), len(sub.query_routes))
        self.assertIn("kitchen-tool", {item.product_id for item in outcome.products})

    def test_subcategory_routes_do_not_inherit_parent_category_expansion(self):
        sub = get_subcategory("kitchen")
        provider = PhraseProvider({sub.query_routes[0]: []})
        service = SearchService([provider], ai_service=None)
        run(service.find_by_subcategory(sub))
        run(provider.aclose())

        phrases = [request.normalized_query for request in provider.requests]
        self.assertEqual(phrases, list(sub.query_routes))
        self.assertNotIn("быт", phrases)


if __name__ == "__main__":
    unittest.main()
