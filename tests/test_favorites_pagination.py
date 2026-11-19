from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace

from handlers import favorites
from keyboards.products import CB_FAVORITE_PAGE
from models.product import Marketplace, Product


class FakeDatabase:
    def __init__(self, count=25):
        self.rows = []
        for index in range(1, count + 1):
            product = Product(
                marketplace=Marketplace.ALIEXPRESS,
                product_id=str(index),
                title=f"Товар {index}",
                product_url=f"https://www.aliexpress.com/item/{index}.html",
                price=float(index),
                currency="USD",
            )
            self.rows.append({
                "marketplace": "aliexpress",
                "product_id": str(index),
                "product_json": json.dumps(product.model_dump(mode="json"), ensure_ascii=False),
            })

    async def get_or_create_user(self, telegram_id, username, first_name):
        return 1

    async def get_favorites(self, user_id):
        return list(self.rows)


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


class FavoritesPaginationTests(unittest.TestCase):
    def setUp(self):
        self.database = FakeDatabase(25)
        self.context = SimpleNamespace(
            user_data={},
            application=SimpleNamespace(bot_data={"database": self.database}),
        )

    def test_favorites_screen_renders_only_one_page_and_navigation(self):
        query = FakeQuery()
        asyncio.run(favorites.show_favorites(FakeUpdate(query), self.context, page=1))

        text, kwargs = query.edits[-1]
        self.assertIn("Страница 2/3", text)
        self.assertIn("13. Товар 13", text)
        self.assertIn("24. Товар 24", text)
        self.assertNotIn("12. Товар 12", text)
        buttons = [button for row in kwargs["reply_markup"].inline_keyboard for button in row]
        callbacks = [button.callback_data for button in buttons if button.callback_data]
        self.assertIn(f"{CB_FAVORITE_PAGE}:0", callbacks)
        self.assertIn(f"{CB_FAVORITE_PAGE}:2", callbacks)

    def test_last_page_has_only_the_remaining_favorite(self):
        query = FakeQuery()
        asyncio.run(favorites.show_favorites(FakeUpdate(query), self.context, page=99))

        text, kwargs = query.edits[-1]
        self.assertIn("Страница 3/3", text)
        self.assertIn("25. Товар 25", text)
        self.assertNotIn("24. Товар 24", text)

    def test_page_callback_acknowledges_and_renders_requested_page(self):
        query = FakeQuery(f"{CB_FAVORITE_PAGE}:2")
        asyncio.run(favorites.favorite_page_callback(FakeUpdate(query), self.context))

        self.assertEqual(query.answers, [(None, False)])
        self.assertIn("Страница 3/3", query.edits[-1][0])


if __name__ == "__main__":
    unittest.main()
