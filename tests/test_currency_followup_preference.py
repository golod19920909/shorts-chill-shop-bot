"""Regression tests for natural-language currency preference changes."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers.search import _answer_currency_preference


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text, kwargs))


class FakeDatabase:
    def __init__(self):
        self.updated = []
        self.invalidated = []

    async def update_user_region(self, user_id, *, currency):
        self.updated.append((user_id, currency))

    async def invalidate_tracking_baselines(self, user_id):
        self.invalidated.append(user_id)


def make_update():
    return SimpleNamespace(
        effective_message=SimpleNamespace(chat_id=47),
        effective_user=SimpleNamespace(id=555),
    )


class CurrencyFollowupPreferenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_supported_currency_is_persisted_for_future_searches(self):
        database = FakeDatabase()
        context = SimpleNamespace(bot=FakeBot(), user_data={})
        update = make_update()
        with (
            patch("handlers.search.common.register_user", new=AsyncMock(return_value=9)),
            patch(
                "handlers.search.common.get_user_region",
                new=AsyncMock(return_value={"currency": "USD", "country": None}),
            ),
            patch("handlers.search.common.get_database", return_value=database),
            patch("handlers.search.common.record_event", new=AsyncMock()) as record_event,
        ):
            handled = await _answer_currency_preference(update, context, "UAH")

        self.assertTrue(handled)
        self.assertEqual(database.updated, [(9, "UAH")])
        self.assertEqual(database.invalidated, [9])
        record_event.assert_awaited_once()
        text = context.bot.messages[0][1]
        self.assertIn("Валюта предпочтения сохранена: UAH", text)
        self.assertIn("Уже показанные карточки не пересчитываются", text)

    async def test_same_currency_does_not_reset_price_baselines(self):
        database = FakeDatabase()
        context = SimpleNamespace(bot=FakeBot(), user_data={})
        update = make_update()
        with (
            patch("handlers.search.common.register_user", new=AsyncMock(return_value=9)),
            patch(
                "handlers.search.common.get_user_region",
                new=AsyncMock(return_value={"currency": "GBP", "country": None}),
            ),
            patch("handlers.search.common.get_database", return_value=database),
            patch("handlers.search.common.record_event", new=AsyncMock()) as record_event,
        ):
            await _answer_currency_preference(update, context, "GBP")

        self.assertEqual(database.updated, [])
        self.assertEqual(database.invalidated, [])
        record_event.assert_not_awaited()

    async def test_unavailable_currency_is_not_saved_or_claimed(self):
        context = SimpleNamespace(bot=FakeBot(), user_data={})
        with patch("handlers.search.common.register_user", new=AsyncMock()) as register:
            await _answer_currency_preference(make_update(), context, "CNY")

        register.assert_not_awaited()
        text = context.bot.messages[0][1]
        self.assertIn("UAH, USD, RUB, BYN, EUR, KZT, UZS, PLN, GBP", text)
        self.assertNotIn("сохранена", text)


if __name__ == "__main__":
    unittest.main()
