"""Regression tests for natural-language currency preference changes."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers.search import _answer_currency_preference, _answer_from_shown, _currency_request_state


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
    def test_natural_currency_phrases_cover_all_nine_catalog_options(self):
        cases = {
            "покажи в гривнах": "UAH",
            "покажи в долларах": "USD",
            "покажи в евро": "EUR",
            "покажи в рублях": "RUB",
            "покажи в белорусских рублях": "BYN",
            "покажи в тенге": "KZT",
            "покажи в сумах": "UZS",
            "покажи в злотых": "PLN",
            "покажи в фунтах": "GBP",
        }
        for phrase, expected in cases.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(
                    _currency_request_state(phrase),
                    {"currency": expected, "amount": None},
                )


    def test_russian_sum_word_is_not_mistaken_for_uzbek_currency(self):
        self.assertIsNone(_currency_request_state("покажи цены в сумме 500"))

    def test_currency_and_numeric_budget_are_kept_together(self):
        self.assertEqual(
            _currency_request_state("покажи в гривнах до 1000"),
            {"currency": "UAH", "amount": 1000.0},
        )


    def test_national_currency_adjectives_are_not_mistaken_for_products(self):
        cases = {
            "покажи в белорусских рублях": "BYN",
            "покажи в казахстанских тенге": "KZT",
            "покажи в узбекских сумах": "UZS",
            "покажи в польских злотых": "PLN",
        }
        for phrase, expected in cases.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(
                    _currency_request_state(phrase),
                    {"currency": expected, "amount": None},
                )


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

    async def test_currency_plus_budget_reaches_engine_instead_of_fast_filter(self):
        context = SimpleNamespace(user_data={})
        outcome = SimpleNamespace(products=[object()])
        update = make_update()
        with (
            patch("handlers.search._last_shown_session", return_value="session"),
            patch("handlers.search.common.get_cached_outcome", return_value=outcome),
            patch(
                "handlers.search._currency_request_state",
                return_value={"currency": "UAH", "amount": 1000.0},
            ),
            patch(
                "handlers.search._answer_currency_preference",
                new=AsyncMock(return_value=True),
            ) as fast_handler,
        ):
            handled = await _answer_from_shown(
                update, context, "покажи в гривнах до 1000"
            )

        self.assertFalse(handled)
        fast_handler.assert_not_awaited()

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
