"""Offline tests for the shared product-card refresh action."""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from bot import build_application
from config.settings import MarketplaceCredentials, Settings
from handlers import search as search_handlers
from handlers.refresh import refresh_results_callback
from handlers.search import send_search_results
from keyboards.products import CB_PREFIX_REFRESH, LABEL_REFRESH
from models.product import Marketplace, Product
from models.search_request import SearchMode, SearchRequest
from services.product_identity import identity_pair
from services.search_service import SearchOutcome


class FakeDatabase:
    async def get_or_create_user(self, telegram_id, username, first_name):
        return 1

    async def get_shown_products(self, user_id, *, flow=None):
        return []

    async def add_shown_product(self, **kwargs):
        return None

    async def get_tracked_products(self, user_id):
        return []

    async def get_user_region(self, user_id):
        return {"currency": None}

    async def set_feed_page(self, user_id, page):
        self.feed_page = page

    async def record_event(self, event_type, **kwargs):
        return None


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text, kwargs))


class FakeMessage:
    def __init__(self):
        self.chat_id = 47
        self.message_id = 10
        self.photo = []
        self.texts = []

    async def reply_text(self, text, **kwargs):
        self.texts.append((text, kwargs))


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.message = FakeMessage()
        self.answers = []
        self.edits = []

    async def answer(self, text=None, show_alert=False):
        if text:
            self.answers.append(text)

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class FakeUpdate:
    def __init__(self, query):
        self.callback_query = query
        self.effective_chat = SimpleNamespace(id=47)
        self.effective_user = SimpleNamespace(id=555, username="tester", first_name="Tester")
        self.effective_message = None


class FakeContext:
    def __init__(self, **bot_data):
        self.bot = FakeBot()
        self.user_data = {}
        self.application = SimpleNamespace(
            bot_data={"database": FakeDatabase(), **bot_data}
        )


def make_product(index: int) -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=f"p{index}",
        title=f"Товар {index}",
        product_url=f"https://example.com/p{index}",
        price=10.0 + index,
        currency="USD",
    )


def callbacks_of(rows):
    return [
        button.callback_data
        for row in rows
        for button in row
        if button.callback_data is not None
    ]


def labels_of(rows):
    return [button.text for row in rows for button in row]


def run(coro):
    return asyncio.run(coro)


class QueueSearchService:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def find_products(self, request, *, exclude=None, broaden=True):
        self.calls.append(
            {
                "request": request,
                "exclude": set(exclude or ()),
                "broaden": broaden,
            }
        )
        return self.outcomes.pop(0)


class QueueFeedService:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.last_findings_stats = {}

    async def find_feed_page(self, **kwargs):
        self.calls.append(dict(kwargs))
        return self.outcomes.pop(0)

    async def curate_findings(self, outcome, *, wanted=20):
        return None


class ProductCardRefreshTests(unittest.TestCase):
    def test_refresh_button_is_on_every_browsed_card_and_callback_is_registered(self):
        context = FakeContext()
        outcome = SearchOutcome(
            request=SearchRequest(original_query="игровой ноутбук"),
            products=[make_product(1), make_product(2)],
            searched_marketplaces=["AliExpress"],
        )
        session_id = run(
            send_search_results(
                context, 47, outcome, pre_curated=True, follow_up=False
            )
        )
        first_rows = context.bot.messages[0][2]["reply_markup"].inline_keyboard
        self.assertIn(LABEL_REFRESH, labels_of(first_rows))
        self.assertIn(f"{CB_PREFIX_REFRESH}:{session_id}", callbacks_of(first_rows))

        middle_query = FakeQuery(f"pnav:{session_id}:1")
        run(search_handlers.browse_callback(FakeUpdate(middle_query), context))
        middle_rows = middle_query.edits[0][1]["reply_markup"].inline_keyboard
        self.assertIn(LABEL_REFRESH, labels_of(middle_rows))
        self.assertIn(f"{CB_PREFIX_REFRESH}:{session_id}", callbacks_of(middle_rows))

        settings = Settings(
            bot_token="123:ABC",
            gemini_api_key=None,
            database_path=":memory:",
            log_level="CRITICAL",
            aliexpress=MarketplaceCredentials(),
        )
        application = build_application(settings)
        patterns = [
            handler.pattern
            for handler in application.handlers[0]
            if getattr(handler, "pattern", None) is not None
        ]
        self.assertTrue(
            any(pattern.search(f"{CB_PREFIX_REFRESH}:{session_id}") for pattern in patterns)
        )

    def test_refresh_reuses_request_and_excludes_every_prior_batch(self):
        old = make_product(1)
        first_new = make_product(2)
        second_new = make_product(3)
        request = SearchRequest(
            original_query="игровой ноутбук до 500 долларов",
            normalized_query="игровой ноутбук",
            max_price=500,
            currency="USD",
        )
        first_outcome = SearchOutcome(
            request=request,
            products=[old],
            searched_marketplaces=["AliExpress"],
        )
        second_outcome = SearchOutcome(
            request=request,
            products=[first_new],
            searched_marketplaces=["AliExpress"],
        )
        third_outcome = SearchOutcome(
            request=request,
            products=[second_new],
            searched_marketplaces=["AliExpress"],
        )
        service = QueueSearchService([second_outcome, third_outcome])
        context = FakeContext(search_service=service)
        session_id = run(
            send_search_results(
                context,
                47,
                first_outcome,
                pre_curated=True,
                follow_up=False,
                refresh_broaden=False,
            )
        )

        query = FakeQuery(f"{CB_PREFIX_REFRESH}:{session_id}")
        run(refresh_results_callback(FakeUpdate(query), context))

        self.assertEqual(len(service.calls), 1)
        self.assertIs(service.calls[0]["request"], request)
        self.assertIn(identity_pair(old), service.calls[0]["exclude"])
        self.assertFalse(service.calls[0]["broaden"])
        self.assertIn("Товар 2", query.edits[0][0])

        new_keyboard = query.edits[0][1]["reply_markup"].inline_keyboard
        new_refresh = next(
            callback
            for callback in callbacks_of(new_keyboard)
            if callback.startswith(f"{CB_PREFIX_REFRESH}:")
        )
        new_session_id = new_refresh.split(":", 1)[1]
        self.assertNotEqual(new_session_id, session_id)

        next_query = FakeQuery(new_refresh)
        run(refresh_results_callback(FakeUpdate(next_query), context))
        self.assertEqual(len(service.calls), 2)
        self.assertIn(identity_pair(old), service.calls[1]["exclude"])
        self.assertIn(identity_pair(first_new), service.calls[1]["exclude"])
        self.assertIn("Товар 3", next_query.edits[0][0])

        stale_query = FakeQuery(f"{CB_PREFIX_REFRESH}:{session_id}")
        run(refresh_results_callback(FakeUpdate(stale_query), context))
        self.assertEqual(len(service.calls), 2)
        self.assertTrue(stale_query.answers)
        self.assertIn("уже обновлена", stale_query.answers[0])

    def test_findings_refresh_uses_the_official_feed_cursor(self):
        old = make_product(10)
        fresh = make_product(11)
        request = SearchRequest(
            original_query="находки AliExpress",
            mode=SearchMode.FEED,
            page_no=1,
        )
        initial = SearchOutcome(
            request=request,
            products=[old],
            searched_marketplaces=["AliExpress"],
        )
        page_two = SearchOutcome(
            request=request.model_copy(update={"page_no": 2}),
            products=[fresh],
            products_before_filtering=1,
            searched_marketplaces=["AliExpress"],
        )
        page_forty_one = SearchOutcome(
            request=request.model_copy(update={"page_no": 41}),
            products=[],
            products_before_filtering=0,
            searched_marketplaces=["AliExpress"],
        )
        service = QueueFeedService([page_two, page_forty_one])
        context = FakeContext(search_service=service)
        context.user_data["feed_page"] = 2
        session_id = run(
            send_search_results(
                context,
                47,
                initial,
                pre_curated=True,
                track_shown=True,
                follow_up=False,
            )
        )

        query = FakeQuery(f"{CB_PREFIX_REFRESH}:{session_id}")
        run(refresh_results_callback(FakeUpdate(query), context))

        self.assertEqual([call["page"] for call in service.calls], [2, 41])
        self.assertTrue(
            all(identity_pair(old) in call["exclude"] for call in service.calls)
        )
        self.assertIn("Товар 11", query.edits[0][0])
        self.assertEqual(context.user_data["feed_page"], 3)


if __name__ == "__main__":
    unittest.main()
