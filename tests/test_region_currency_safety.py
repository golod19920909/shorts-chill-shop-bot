"""Regression tests for currency changes and price-baseline safety."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers import region


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class FakeDatabase:
    def __init__(self, *, fail_invalidation=False):
        self.fail_invalidation = fail_invalidation
        self.calls = []
        self.updated = []

    async def invalidate_tracking_baselines(self, user_id):
        self.calls.append(("invalidate", user_id))
        if self.fail_invalidation:
            raise RuntimeError("cannot reset price baseline")

    async def update_user_region(self, user_id, **kwargs):
        self.calls.append(("update", user_id, kwargs))
        self.updated.append((user_id, kwargs))


def make_update(data):
    query = FakeQuery(data)
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=555),
    )
    return update, query


class RegionCurrencySafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_baseline_is_invalidated_before_currency_is_saved(self):
        database = FakeDatabase()
        update, query = make_update("currency:EUR")
        with (
            patch("handlers.region.common.register_user", new=AsyncMock(return_value=9)),
            patch(
                "handlers.region.common.get_user_region",
                new=AsyncMock(return_value={"currency": "USD"}),
            ),
            patch("handlers.region.common.get_database", return_value=database),
            patch("handlers.region.common.record_event", new=AsyncMock()),
            patch("handlers.region.show_currency", new=AsyncMock()) as show,
        ):
            await region.currency_callback(update, SimpleNamespace(user_data={}))

        self.assertEqual(
            database.calls,
            [("invalidate", 9), ("update", 9, {"currency": "EUR"})],
        )
        self.assertEqual(query.answers, [("Валюта: EUR", False)])
        show.assert_awaited_once()

    async def test_failed_baseline_reset_keeps_previous_currency(self):
        database = FakeDatabase(fail_invalidation=True)
        update, query = make_update("currency:EUR")
        with (
            patch("handlers.region.common.register_user", new=AsyncMock(return_value=9)),
            patch(
                "handlers.region.common.get_user_region",
                new=AsyncMock(return_value={"currency": "USD"}),
            ),
            patch("handlers.region.common.get_database", return_value=database),
            patch("handlers.region.common.record_event", new=AsyncMock()),
            patch("handlers.region.show_currency", new=AsyncMock()) as show,
        ):
            await region.currency_callback(update, SimpleNamespace(user_data={}))

        self.assertEqual(database.calls, [("invalidate", 9)])
        self.assertEqual(database.updated, [])
        self.assertTrue(query.answers[-1][1])
        show.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
