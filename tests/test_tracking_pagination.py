from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from handlers import settings
from keyboards.products import CB_TRACK_PAGE


class FakeDatabase:
    def __init__(self, count=25):
        self.rows = [
            {
                "id": index,
                "title": f"Товар {index}",
                "product_url": f"https://www.aliexpress.com/item/{index}.html",
                "last_checked_price": float(index),
                "initial_price": float(index),
                "currency": "USD",
                "marketplace": "aliexpress",
            }
            for index in range(1, count + 1)
        ]

    async def get_or_create_user(self, telegram_id, username, first_name):
        return 1

    async def get_tracked_products(self, user_id):
        return list(reversed(self.rows))


class FakeQuery:
    def __init__(self, data=""):
        self.data = data
        self.answers = []
        self.edits = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class FakeUpdate:
    def __init__(self, query):
        self.callback_query = query
        self.effective_user = SimpleNamespace(id=555, username="tester", first_name="Test")
        self.effective_chat = SimpleNamespace(id=47)
        self.effective_message = None


class TrackingPaginationTests(unittest.TestCase):
    def setUp(self):
        self.database = FakeDatabase(25)
        self.context = SimpleNamespace(
            user_data={},
            application=SimpleNamespace(
                bot_data={
                    "database": self.database,
                    "price_tracker": object(),
                    "price_tracking_scheduled": True,
                    "settings": SimpleNamespace(tracking_interval_minutes=60),
                }
            ),
        )

    def test_tracking_screen_renders_only_one_page_and_navigation(self):
        query = FakeQuery()
        asyncio.run(settings.show_tracking(FakeUpdate(query), self.context, page=1))

        text, kwargs = query.edits[-1]
        self.assertIn("Страница 2/3", text)
        self.assertIn("Товар 15", text)
        self.assertIn("Товар 6", text)
        self.assertNotIn("Товар 5", text)
        buttons = [button for row in kwargs["reply_markup"].inline_keyboard for button in row]
        callbacks = [button.callback_data for button in buttons if button.callback_data]
        self.assertIn(f"{CB_TRACK_PAGE}:0", callbacks)
        self.assertIn(f"{CB_TRACK_PAGE}:2", callbacks)

    def test_invalidated_currency_baseline_does_not_show_old_initial_price(self):
        self.database.rows[-1]["last_checked_price"] = None
        self.database.rows[-1]["currency"] = None
        query = FakeQuery()
        asyncio.run(settings.show_tracking(FakeUpdate(query), self.context, page=0))

        text, kwargs = query.edits[-1]
        self.assertIn("Товар 25", text)
        self.assertNotIn("25.00", text)
        buttons = [button for row in kwargs["reply_markup"].inline_keyboard for button in row]
        self.assertTrue(any("—" in (button.text or "") for button in buttons))


    def test_last_page_has_no_next_button(self):
        query = FakeQuery()
        asyncio.run(settings.show_tracking(FakeUpdate(query), self.context, page=99))

        text, kwargs = query.edits[-1]
        self.assertIn("Страница 3/3", text)
        buttons = [button for row in kwargs["reply_markup"].inline_keyboard for button in row]
        callbacks = [button.callback_data for button in buttons if button.callback_data]
        self.assertIn(f"{CB_TRACK_PAGE}:1", callbacks)
        self.assertNotIn(f"{CB_TRACK_PAGE}:3", callbacks)

    def test_page_callback_acknowledges_and_renders_requested_page(self):
        query = FakeQuery(f"{CB_TRACK_PAGE}:2")
        asyncio.run(settings.tracking_page_callback(FakeUpdate(query), self.context))

        self.assertEqual(query.answers, [(None, False)])
        self.assertIn("Страница 3/3", query.edits[-1][0])


if __name__ == "__main__":
    unittest.main()
