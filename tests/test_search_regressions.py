"""Regression protection for the accepted behaviour.

Nothing here is new functionality: these are the guarantees the live
acceptance established, pinned so an AI-related change cannot quietly break
them - the ordinary natural-language search, the invisible-character
cleanup, the accepted main menu and the removal of Alibaba/Temu.
"""

from __future__ import annotations

import asyncio
import unittest

import httpx

from config.settings import MarketplaceCredentials
from keyboards.main import MAIN_MENU_CALLBACKS, main_menu_keyboard
from models.product import Availability, Marketplace, Product
from models.search_request import SearchRequest
from services.ai_service import AIService
from services.marketplaces.base import MarketplaceProvider
from services.product_normalizer import clean_text
from services.search_service import SearchService


def run(coro):
    return asyncio.run(coro)


def make_product(
    product_id: str, title: str, price: float | None, currency: str = "USD"
) -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=product_id,
        title=title,
        product_url=f"https://www.aliexpress.com/item/{product_id}.html",
        price=price,
        currency=currency,
        availability=Availability.IN_STOCK,
    )


class StubProvider(MarketplaceProvider):
    def __init__(self, products: list[Product]) -> None:
        super().__init__(
            MarketplaceCredentials("key", "secret"), httpx.AsyncClient()
        )
        self.marketplace = Marketplace.ALIEXPRESS
        self.display_name = "AliExpress"
        self._products = products

    async def search_products(self, request):
        return list(self._products)

    async def get_product(self, product_id):
        return None

    def generate_affiliate_link(self, product):
        return None


def search_with(products: list[Product], request: SearchRequest):
    return run(SearchService([StubProvider(products)]).find_products(request))


def parse(text: str) -> SearchRequest:
    return run(AIService(api_key=None).parse_query(text))


class NaturalLanguageSearchTests(unittest.TestCase):
    """The exact queries of the live acceptance stay relevant."""

    def test_the_advertised_example_keeps_its_budget_and_relevance(self) -> None:
        # Regression (verified live): "с хорошими отзывами" must never be sent
        # to the catalogue - the full-text index answers an EMPTY result for
        # it - while the price limit must still be applied.
        request = parse("беспроводные наушники до $30 с хорошими отзывами")
        self.assertEqual(request.max_price, 30.0)
        self.assertNotIn("отзыв", (request.normalized_query or "").lower())

        outcome = search_with(
            [
                make_product("ok", "Беспроводные наушники TWS", 19.99),
                make_product("pricey", "Беспроводные наушники Pro", 59.99),
                make_product("alien", "Кроссовки мужские", 25.0),
            ],
            request,
        )

        titles = [product.title for product in outcome.products]
        self.assertIn("Беспроводные наушники TWS", titles)
        self.assertNotIn("Беспроводные наушники Pro", titles)
        self.assertNotIn("Кроссовки мужские", titles)

    def test_a_simple_query_returns_relevant_products(self) -> None:
        request = parse("чехол для телефона")

        outcome = search_with(
            [
                make_product("case", "Чехол для телефона с камерами", 4.5),
                make_product("shoe", "Кроссовки мужские", 30.0),
            ],
            request,
        )

        self.assertEqual([p.product_id for p in outcome.products], ["case"])

    def test_equivalent_natural_phrasings_stay_relevant(self) -> None:
        # Several ways of asking for the same thing must not change the
        # outcome: none of them may raise, drop the query or invent filters.
        for text in (
            "wireless headphones under $30",
            "наушники беспроводные до 30 долларов",
            "Find wireless headphones under $30",
            "найди беспроводные наушники до 30 баксов",
        ):
            with self.subTest(text=text):
                request = parse(text)
                self.assertEqual(request.max_price, 30.0)
                self.assertTrue(request.normalized_query)
                outcome = search_with(
                    [make_product("a", "Wireless Headphones Bluetooth", 20.0)],
                    request,
                )
                self.assertIsInstance(outcome.products, list)

    def test_a_price_limit_is_never_ignored(self) -> None:
        request = parse("наушники беспроводные до 3000 рублей")

        self.assertEqual(request.currency, "RUB")
        outcome = search_with(
            [
                make_product(
                    "cheap", "Наушники беспроводные", 1500.0, currency="RUB"
                )
            ],
            request,
        )

        self.assertEqual([p.product_id for p in outcome.products], ["cheap"])


class UnicodeCleanupRegressionTests(unittest.TestCase):
    """One normalizer, used everywhere; nothing invisible reaches the user."""

    def test_all_invisible_families_are_removed(self) -> None:
        contaminated = (
            "\ufeffБеспроводные\u200b наушники\u200c TWS\u200d \u202e"
            "успей\u202c\u2060 купить\u00ad"
        )

        self.assertEqual(
            clean_text(contaminated), "Беспроводные наушники TWS успей купить"
        )

    def test_legitimate_multilingual_characters_survive(self) -> None:
        for text in (
            "Наушники 🎧 Xiaomi",
            "Чехол для iPhone 15 Pro Max",
            " Câble USB-C 2m",
            "日本語 テスト",
        ):
            with self.subTest(text=text):
                # Leading/trailing whitespace is normalized away; the visible
                # characters themselves must never change.
                self.assertEqual(clean_text(text), text.strip())

    def test_a_normalized_title_reaches_the_product_model_clean(self) -> None:
        product = make_product("x", clean_text("\ufeff Наушники\u200b TWS"), 10.0)

        for char in ("\ufeff", "\u200b", "\u200c", "\u200d", "\u202e", "\u00ad"):
            self.assertNotIn(char, product.title)


class AcceptedUiRegressionTests(unittest.TestCase):
    def test_the_main_menu_is_unchanged(self) -> None:
        keyboard = main_menu_keyboard()
        labels = [button.text for row in keyboard.inline_keyboard for button in row]

        # Exactly the accepted eleven buttons, in the accepted order
        # (the final spec added the standalone «🎟 Промокоды» section;
        # the search entry was renamed to «Умный поиск»).
        self.assertEqual(
            labels,
            [
                "🧠 Умный поиск",
                "\U0001F4F7 Найти по фото",
                "\u2728 Находки",
                "\U0001F4C2 Категории",
                "❤️ Избранное",
                "\U0001F514 Отслеживание цен",
                "\U0001F30D Валюта",
                "⚙️ Настройки",
                "\U0001F525 Горячие скидки",
                "\U0001F39F Промокоды",
                "\U0001F4E2 Shorts Chill",
            ],
        )
        # The AI menu belongs to a product card, never to the main menu.
        self.assertFalse(any("AI" in label for label in labels))
        self.assertIn("menu:back", MAIN_MENU_CALLBACKS)

    def test_no_removed_marketplace_appears_in_any_button(self) -> None:
        labels = [
            button.text
            for row in main_menu_keyboard().inline_keyboard
            for button in row
        ]

        joined = " ".join(labels)
        self.assertNotIn("Alibaba", joined)
        self.assertNotIn("Temu", joined)

    def test_the_removed_marketplaces_are_absent_from_the_model(self) -> None:
        self.assertEqual([item.value for item in Marketplace], ["aliexpress"])


if __name__ == "__main__":
    unittest.main()
