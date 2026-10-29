"""Regression tests for the Favorites deletion stale-index bug.

Earlier releases encoded a POSITION (``fdel:<index>``) from the shared
``FAVORITES_CACHE_KEY`` cache into the 🗑 button. Two open favorites
screens overwrite that one cache slot, so pressing 🗑 on the older screen
deleted whatever product now sat at that position - a different product
than the one next to the button.

The fix: the button carries the product's identity
(``fdel:<marketplace>:<product_id>``). Deletion targets that identity in
the database and never reads a list position. Legacy positional
callbacks from messages sent by older releases are still accepted.
"""

from __future__ import annotations

import asyncio
import json
import unittest

from handlers import favorites
from keyboards.products import CB_PREFIX_FAVORITE_REMOVE, favorites_list_keyboard
from models.product import Marketplace, Product


def run(coro):
    return asyncio.run(coro)


def make_product(product_id: str, title: str) -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=product_id,
        title=title,
        product_url=f"https://www.aliexpress.com/item/{product_id}.html",
        price=10.0,
        currency="USD",
    )


class FakeMessage:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.kwargs: list[dict] = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.texts.append(text)
        self.kwargs.append(kwargs)


class FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.answers: list[tuple[str | None, bool]] = []
        self.edits: list[str] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text: str, **kwargs) -> None:
        self.edits.append(text)


class FakeUser:
    id = 555
    username = "tester"
    first_name = "Tester"


class FakeChat:
    id = 47


class FakeUpdate:
    def __init__(self, *, callback_query=None, message=None) -> None:
        self.callback_query = callback_query
        self.effective_message = message
        self.effective_chat = FakeChat() if (message or callback_query) else None
        self.effective_user = FakeUser()


class FakeApplication:
    def __init__(self, bot_data: dict) -> None:
        self.bot_data = bot_data


class FakeContext:
    def __init__(self, database) -> None:
        self.bot = None
        self.user_data: dict = {}
        self.application = FakeApplication({"database": database})


class FakeDatabase:
    """In-memory favorites + recorded deletions."""

    def __init__(self, favorites_rows: list[dict] | None = None) -> None:
        self.rows = favorites_rows or []
        #: every remove_favorite call as a (marketplace, product_id) tuple
        self.removed: list[tuple[str, str]] = []
        self.events: list[dict] = []

    async def get_or_create_user(self, telegram_id, username, first_name) -> int:
        return 1

    async def get_favorites(self, user_id) -> list[dict]:
        return [dict(row) for row in self.rows]

    async def remove_favorite(
        self, user_id: int, marketplace: str, product_id: str
    ) -> None:
        self.removed.append((marketplace, product_id))
        self.rows = [
            row
            for row in self.rows
            if not (
                row.get("marketplace") == marketplace
                and row.get("product_id") == product_id
            )
        ]

    async def record_event(self, event_type: str, **kwargs) -> None:
        self.events.append({"type": event_type, **kwargs})


def favorite_row(product: Product) -> dict:
    payload = {
        "marketplace": product.marketplace.value,
        "product_id": product.product_id,
        "title": product.title,
        "product_url": product.product_url,
        "price": product.price,
        "currency": product.currency,
    }
    return {
        "id": int(product.product_id) if product.product_id.isdigit() else 1,
        "marketplace": product.marketplace.value,
        "product_id": product.product_id,
        "product_json": json.dumps(payload, ensure_ascii=False),
    }


def remove_buttons(rows: list) -> list[str]:
    """Callback data of every 🗑 button in a favorites keyboard."""

    return [
        button.callback_data
        for row in rows
        for button in row
        if getattr(button, "callback_data", None)
        and button.callback_data.startswith(f"{CB_PREFIX_FAVORITE_REMOVE}:")
    ]


class FavoritesKeyboardIdentityTests(unittest.TestCase):
    def test_delete_button_carries_product_identity_not_position(self) -> None:
        product = make_product("1005001", "Наушники TWS")

        keyboard = favorites_list_keyboard([{"index": 0, "product": product}])

        self.assertEqual(
            remove_buttons(keyboard.inline_keyboard),
            [f"{CB_PREFIX_FAVORITE_REMOVE}:aliexpress:1005001"],
        )

    def test_button_data_fits_telegrams_64_byte_limit(self) -> None:
        # AliExpress ids are numeric and short today, but the guard must
        # hold for any future id: Telegram silently drops oversized data.
        product = make_product("1" * 40, "Товар")

        for data in remove_buttons(
            favorites_list_keyboard([{"index": 0, "product": product}]).inline_keyboard
        ):
            with self.subTest(data=data):
                self.assertLessEqual(len(data.encode("utf-8")), 64)


class StaleScreenDeletionTests(unittest.TestCase):
    """The exact bug: an old screen must not delete another product."""

    def setUp(self) -> None:
        self.first = make_product("101", "Первый товар")
        self.second = make_product("202", "Второй товар")
        self.database = FakeDatabase(
            favorites_rows=[favorite_row(self.first), favorite_row(self.second)]
        )
        self.context = FakeContext(self.database)

    def _press(self, data: str) -> FakeQuery:
        query = FakeQuery(data)
        run(
            favorites.favorite_remove_callback(
                FakeUpdate(callback_query=query), self.context
            )
        )
        return query

    def test_stale_screen_deletes_the_product_it_names_not_the_new_first(self) -> None:
        # Screen A was rendered when the list was [first, second]...
        screen_a = favorites_list_keyboard(
            [
                {"index": 0, "product": self.first},
                {"index": 1, "product": self.second},
            ]
        )
        stale_button = remove_buttons(screen_a.inline_keyboard)[0]

        # ...then screen B overwrote the shared cache with a different
        # list. The old position 0 now means a DIFFERENT product.
        self.context.user_data[favorites.FAVORITES_CACHE_KEY] = [
            {"index": 0, "product": self.second}
        ]

        query = self._press(stale_button)

        # The identity fix deletes exactly what the old button named...
        self.assertEqual(self.database.removed, [("aliexpress", "101")])
        # ...and the other product is untouched.
        remaining = [row["product_id"] for row in self.database.rows]
        self.assertIn("202", remaining)
        self.assertNotIn("101", remaining)
        self.assertIn("Удалено", " ".join(text for text, _ in query.answers if text))


    def test_identity_deletion_works_with_a_missing_or_wrong_cache(self) -> None:
        # Cache empty (bot restarted) or plainly wrong: the button's
        # identity alone decides what is deleted.
        for cache in ([], [{"index": 0, "product": self.second}]):
            with self.subTest(cache_len=len(cache)):
                self.database.rows = [
                    favorite_row(self.first),
                    favorite_row(self.second),
                ]
                self.database.removed.clear()
                self.context.user_data[favorites.FAVORITES_CACHE_KEY] = cache

                self._press(f"{CB_PREFIX_FAVORITE_REMOVE}:aliexpress:101")

                self.assertEqual(self.database.removed, [("aliexpress", "101")])

    def test_unknown_marketplace_is_refused_without_touching_the_database(self) -> None:
        query = self._press(f"{CB_PREFIX_FAVORITE_REMOVE}:temu:101")

        self.assertEqual(self.database.removed, [])
        self.assertTrue(any(text and "Некорректная" in text for text, _ in query.answers))

    def test_empty_product_id_is_refused(self) -> None:
        query = self._press(f"{CB_PREFIX_FAVORITE_REMOVE}:aliexpress:")

        self.assertEqual(self.database.removed, [])

    def test_malformed_callback_is_refused(self) -> None:
        for data in (CB_PREFIX_FAVORITE_REMOVE, f"{CB_PREFIX_FAVORITE_REMOVE}:a:b:c"):
            with self.subTest(data=data):
                self.database.removed.clear()
                query = self._press(data)
                self.assertEqual(self.database.removed, [])
                self.assertTrue(
                    any(text and "Некорректная" in text for text, _ in query.answers)
                )


class LegacyPositionalCallbackTests(unittest.TestCase):
    """Messages sent by the previous release keep working."""

    def setUp(self) -> None:
        self.first = make_product("101", "Первый товар")
        self.second = make_product("202", "Второй товар")
        self.database = FakeDatabase(
            favorites_rows=[favorite_row(self.first), favorite_row(self.second)]
        )
        self.context = FakeContext(self.database)

    def _press(self, data: str) -> FakeQuery:
        query = FakeQuery(data)
        run(
            favorites.favorite_remove_callback(
                FakeUpdate(callback_query=query), self.context
            )
        )
        return query

    def test_valid_legacy_index_still_deletes(self) -> None:
        self.context.user_data[favorites.FAVORITES_CACHE_KEY] = [
            {"index": 0, "product": self.first},
            {"index": 1, "product": self.second},
        ]

        self._press(f"{CB_PREFIX_FAVORITE_REMOVE}:1")

        self.assertEqual(self.database.removed, [("aliexpress", "202")])

    def test_out_of_range_legacy_index_answers_stale_instead_of_deleting(self) -> None:
        self.context.user_data[favorites.FAVORITES_CACHE_KEY] = [
            {"index": 0, "product": self.second}
        ]

        query = self._press(f"{CB_PREFIX_FAVORITE_REMOVE}:5")

        self.assertEqual(self.database.removed, [])
        self.assertTrue(any(text and "устарел" in text for text, _ in query.answers))

    def test_non_numeric_legacy_index_is_refused(self) -> None:
        self.context.user_data[favorites.FAVORITES_CACHE_KEY] = [
            {"index": 0, "product": self.first}
        ]

        query = self._press(f"{CB_PREFIX_FAVORITE_REMOVE}:abc")

        self.assertEqual(self.database.removed, [])
        self.assertTrue(any(text and "Некорректная" in text for text, _ in query.answers))


if __name__ == "__main__":
    unittest.main()

