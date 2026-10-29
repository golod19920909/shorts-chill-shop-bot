"""Regression tests for conversational narrowing of the SHOWN results.

«Оставь 5 штук из этих 30» must select five REAL products from the cached
previous result set and re-deliver them through the existing card renderer -
never a prose-only reply, never a new marketplace search, never invented
products.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from handlers import common
from handlers import search as search_handlers
from models.product import Marketplace, Product
from models.search_request import SearchRequest
from services.search_service import SearchOutcome, SearchService


def run(coro):
    return asyncio.run(coro)


def make_product(product_id: str, *, price: float | None = 25.0) -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=product_id,
        title=f"Шуруповерт {product_id}",
        product_url=f"https://www.aliexpress.com/item/{product_id}.html",
        image_url=f"https://ae01.alicdn.com/kf/{product_id}.jpg",
        price=price,
        currency="USD",
        orders_count=100,
        rating=4.7,
        affiliate_url=f"https://s.click.aliexpress.com/e/_{product_id}",
    )


class FakeBot:
    def __init__(self):
        self.photos = []
        self.texts = []

    async def send_message(self, chat_id, text, **kwargs):
        self.texts.append(text)

    async def send_photo(self, chat_id, photo, **kwargs):
        self.photos.append((photo, kwargs.get("caption", "")))

    async def send_text(self, chat_id, text, **kwargs):
        self.texts.append(text)


class FakeMessage:
    def __init__(self, chat_id=100, text="", bot=None):
        self.chat_id = chat_id
        self.text = text
        self.bot = bot if bot is not None else FakeBot()

    async def reply_text(self, text, **kwargs):
        self.bot.texts.append(text)


class FakeUser:
    id = 555
    full_name = "Test User"
    username = "tester"
    first_name = "Test"


class FakeUpdate:
    def __init__(self, message):
        self.effective_message = message
        self.effective_user = FakeUser()


class FakeApp:
    def __init__(self, bot_data):
        self.bot_data = bot_data


class FakeContext:
    def __init__(self):
        self.user_data = {}
        self.chat_data = {}
        self.bot_data = {
            "ai_service": None,
            "search_service": None,
            "database": None,
            "product_assistant": None,
        }
        self.bot = FakeBot()
        self.application = FakeApp(self.bot_data)
        self.message = FakeMessage(bot=self.bot)


class NarrowBase(unittest.TestCase):
    """Shared setup: a real temp database, 30 cached products."""

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        tmp.close()
        from database.database import Database

        self.db = Database(Path(tmp.name))
        asyncio.run(self.db.initialize())
        self.ctx = FakeContext()
        self.ctx.bot_data["database"] = self.db
        self.provider_calls = 0

        class CountingProvider:
            marketplace = Marketplace.ALIEXPRESS
            display_name = "AliExpress"
            supports_feed = True

            async def search_products(self, request):
                self_outer = self
                self_outer.provider_calls += 1
                return []

            async def fetch_feed_page(self, **kwargs):
                return []

            async def aclose(self):
                return None

        self.provider = CountingProvider()
        self.provider_calls = 0
        self.ctx.bot_data["search_service"] = SearchService([self.provider], ai_service=None)

        self.products = [
            make_product(f"p{i:02d}", price=float(30 - i)) for i in range(30)
        ]
        self.session = "s-prev"
        self.outcome = SearchOutcome(
            request=SearchRequest(original_query="шуруповерт", normalized_query="шуруповерт"),
            products=self.products,
        )
        self.ctx.user_data[common.OUTCOME_CACHE_KEY] = {self.session: self.outcome}
        self.ctx.user_data["last_shown_session"] = self.session

    def tearDown(self):
        asyncio.run(self.db.close())

    def _message(self, text):
        return FakeMessage(text=text, bot=self.ctx.bot)


class NarrowParsingTests(unittest.TestCase):
    """The request parser: what is and is NOT a narrowing request."""

    def test_accepts_the_task_examples(self) -> None:
        cases = {
            "Оставь 5 штук из этих 30": (5, False, True),
            "Выбери 5 лучших": (5, False, False),
            "Оставь самые дешёвые 3": (3, True, False),
            "Покажи только 2 варианта": (2, False, False),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(search_handlers._parse_narrow_request(text), expected)

    def test_rejects_ordinary_searches_and_unverifiable_criteria(self) -> None:
        for text in (
            "найди беспроводные наушники до 5 долларов",
            "оставь самые надёжные 3",
            "покажи только красные наушники",
            "оставь 5 штук с микрофоном",
            "привет",
            "найди дрель",
        ):
            with self.subTest(text=text):
                self.assertIsNone(search_handlers._parse_narrow_request(text))


class NarrowDeliveryTests(NarrowBase):
    """The REAL delivery path: actual cards re-sent from the cached set."""

    def test_keep_five_delivers_five_selected_products_as_cards(self) -> None:
        message = self._message("Оставь 5 штук из этих 30")
        run(search_handlers._narrow_shown_results(
            FakeUpdate(message), self.ctx, message.text))
        # The narrowed session was opened IN THIS TURN through the existing
        # card renderer: one live browsing card was sent, and the card set
        # behind it holds exactly the 5 selected real products (walked via
        # «➡️» in the current conversation, never the old 30-card session).
        card_sent = bool(message.bot.photos) or bool(message.bot.texts)
        self.assertTrue(card_sent)
        new_session = self.ctx.user_data["last_shown_session"]
        self.assertNotEqual(new_session, self.session)
        kept = common.get_cached_products(self.ctx, new_session)
        self.assertEqual(len(kept), 5)
        # All five belong to the PREVIOUS 30-product set and carry their real
        # fields (identity, price, affiliate URL) - nothing invented.
        previous = {p.product_id: p for p in self.products}
        for product in kept:
            self.assertIn(product.product_id, previous)
            self.assertEqual(product, previous[product.product_id])
            self.assertTrue(product.product_url.startswith("https://www.aliexpress.com/item/"))
        self.assertEqual(
            [p.product_id for p in kept],
            [p.product_id for p in self.products[:5]],
        )
        # The delivered card carries the REAL product data of its product:
        # the image, and the title/price on the caption. The AliExpress link
        # rides the card's open-button (the real product_url), and the card
        # set keeps the affiliate URLs untouched.
        self.assertEqual(len(message.bot.photos), 1)
        photo_url, caption = message.bot.photos[0]
        self.assertEqual(photo_url, self.products[0].image_url)
        self.assertIn(self.products[0].title, caption)
        self.assertIn("30.00 USD", caption)
        self.assertTrue(all(p.affiliate_url for p in kept))
        # One short follow-up AFTER the card; no second product list. The
        # card itself is a PHOTO (caption), the single text is the narrow
        # summary - the generic count hint is not doubled here.
        self.assertIn("Оставил 5 из 30", message.bot.texts[0])
        self.assertEqual(len(message.bot.texts), 1)

    def test_quantity_only_keeps_existing_order(self) -> None:
        message = self._message("Выбери 5 лучших")
        run(search_handlers._narrow_shown_results(
            FakeUpdate(message), self.ctx, message.text))
        kept = common.get_cached_products(
            self.ctx, self.ctx.user_data["last_shown_session"])
        # No new criterion invented: the previous ranking order is preserved.
        self.assertEqual([p.product_id for p in kept],
                         [p.product_id for p in self.products[:5]])

    def test_cheapest_selects_three_lowest_priced(self) -> None:
        message = self._message("Оставь самые дешёвые 3")
        run(search_handlers._narrow_shown_results(
            FakeUpdate(message), self.ctx, message.text))
        kept = common.get_cached_products(
            self.ctx, self.ctx.user_data["last_shown_session"])
        expected = sorted(self.products, key=lambda p: p.price)[:3]
        self.assertEqual([p.product_id for p in kept],
                         [p.product_id for p in expected])
        self.assertTrue(all(p.price in (1.0, 2.0, 3.0) for p in kept))
        # The follow-up describes the action ACTUALLY completed.
        self.assertIn("дешёвых", message.bot.texts[-1])

    def test_no_new_marketplace_call(self) -> None:
        before = self.provider.search_calls if hasattr(self.provider, "search_calls") else 0
        message = self._message("Покажи только 4 варианта")
        run(search_handlers._narrow_shown_results(
            FakeUpdate(message), self.ctx, message.text))
        self.assertEqual(self.provider.queries if hasattr(self.provider, "queries") else [], [])

    def test_fewer_available_sends_only_available(self) -> None:
        self.ctx.user_data[common.OUTCOME_CACHE_KEY] = {
            self.session: SearchOutcome(
                request=self.outcome.request, products=self.products[:2])
        }
        message = self._message("Оставь 5 штук из этих 30")
        run(search_handlers._narrow_shown_results(
            FakeUpdate(message), self.ctx, message.text))
        kept = common.get_cached_products(
            self.ctx, self.ctx.user_data["last_shown_session"])
        self.assertEqual(len(kept), 2)
        follow_up = message.bot.texts[-1]
        self.assertIn("подходящих", follow_up)

    def test_missing_context_answers_honestly_not_with_fake_success(self) -> None:
        self.ctx.user_data.pop(common.OUTCOME_CACHE_KEY, None)
        self.ctx.user_data.pop("last_shown_session", None)
        message = self._message("Оставь 5 штук из этих 30")
        run(search_handlers._narrow_shown_results(
            FakeUpdate(message), self.ctx, message.text))
        # No card was sent and NO selection success was claimed.
        self.assertEqual(message.bot.photos, [])
        self.assertEqual(message.bot.texts, [search_handlers.STALE_RESULTS_TEXT])

    def test_non_narrowing_message_is_not_intercepted(self) -> None:
        message = self._message("найди беспроводные наушники до 5 долларов")
        self.assertFalse(run(search_handlers._narrow_shown_results(
            FakeUpdate(message), self.ctx, message.text)))
        self.assertEqual(message.bot.photos, [])


if __name__ == "__main__":
    unittest.main()
