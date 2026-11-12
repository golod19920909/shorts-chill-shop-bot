"""Tests for catalog navigation callback identities and keyboards."""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from config.catalog import CATEGORIES
from config.subcategories import get_subcategories
from keyboards.main import (
    CB_CATEGORIES,
    CB_CATEGORY_VIEW_PREFIX,
    CB_SUBCATEGORY_PREFIX,
    categories_keyboard,
    category_view_by_callback,
    subcategories_keyboard,
    subcategory_by_callback,
)
from handlers import discovery


class CatalogNavigationTests(unittest.TestCase):
    def test_main_catalog_buttons_open_category_directory(self):
        keyboard = categories_keyboard()
        callbacks = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data
        ]
        expected = {
            f"{CB_CATEGORY_VIEW_PREFIX}:{category.code}"
            for category in CATEGORIES
        }
        self.assertTrue(expected.issubset(set(callbacks)))
        self.assertNotIn(f"cat:{CATEGORIES[0].code}", callbacks)

    def test_category_directory_callback_resolves_parent(self):
        category = CATEGORIES[0]
        resolved = category_view_by_callback(
            f"{CB_CATEGORY_VIEW_PREFIX}:{category.code}"
        )
        self.assertEqual(resolved, category)
        self.assertIsNone(category_view_by_callback("cat:electronics"))
        self.assertIsNone(category_view_by_callback("catview:unknown"))

    def test_subcategory_callback_resolves_configured_topic(self):
        sub = get_subcategories("electronics")[0]
        self.assertEqual(
            subcategory_by_callback(f"{CB_SUBCATEGORY_PREFIX}:{sub.code}"),
            sub,
        )
        self.assertIsNone(subcategory_by_callback("sub:unknown"))

    def test_subcategory_keyboard_has_back_to_catalog_and_home(self):
        keyboard = subcategories_keyboard("electronics")
        buttons = [
            button
            for row in keyboard.inline_keyboard
            for button in row
        ]
        callback_data = [button.callback_data for button in buttons]
        subcodes = {f"sub:{item.code}" for item in get_subcategories("electronics")}
        self.assertTrue(subcodes.issubset(set(callback_data)))
        self.assertIn(CB_CATEGORIES, callback_data)
        self.assertIn("menu:back", callback_data)




class _FakeQuery:
    def __init__(self, data: str, *, photo: bool = False):
        self.data = data
        self.answers = []
        self.edits = []
        self.captions = []
        self.message = SimpleNamespace(photo=[object()] if photo else [])

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text or "")

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))

    async def edit_message_caption(self, caption, **kwargs):
        self.captions.append((caption, kwargs))


class CatalogHandlerScreenTests(unittest.TestCase):
    def test_catalog_landing_has_text_fallback(self):
        query = _FakeQuery("menu:categories")
        update = SimpleNamespace(
            callback_query=query, effective_chat=SimpleNamespace(id=47)
        )
        context = SimpleNamespace(bot=object())

        asyncio.run(discovery.show_categories(update, context))

        self.assertEqual(len(query.edits), 1)
        self.assertIn("Каталог товаров", query.edits[0][0])
        self.assertEqual(query.edits[0][1]["reply_markup"], categories_keyboard())

    def test_category_directory_edits_photo_caption_when_present(self):
        query = _FakeQuery("catview:electronics", photo=True)
        update = SimpleNamespace(callback_query=query)
        context = SimpleNamespace(bot=object())

        asyncio.run(discovery.category_view_callback(update, context))

        self.assertEqual(len(query.captions), 1)
        caption, kwargs = query.captions[0]
        callbacks = [
            button.callback_data
            for row in kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn("sub:smart_home", callbacks)
        self.assertIn("Гаджеты, устройства", caption)
        self.assertEqual(query.edits, [])


if __name__ == "__main__":
    unittest.main()
