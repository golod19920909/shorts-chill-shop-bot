from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers import common, search
from models.product import Marketplace, Product
from models.search_request import SearchRequest
from services.search_service import SearchOutcome


def run(coro):
    return asyncio.run(coro)


def make_outcome() -> SearchOutcome:
    return SearchOutcome(
        request=SearchRequest(original_query="наушники"),
        products=[
            Product(
                marketplace=Marketplace.ALIEXPRESS,
                product_id="p1",
                title="Bluetooth headphones",
                product_url="https://www.aliexpress.com/item/p1.html",
                price=10.0,
                currency="USD",
            ),
            Product(
                marketplace=Marketplace.ALIEXPRESS,
                product_id="p2",
                title="USB headphones",
                product_url="https://www.aliexpress.com/item/p2.html",
                price=12.0,
                currency="USD",
            ),
        ],
        searched_marketplaces=["AliExpress"],
    )


class SearchDeliveryIntegrityTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_initial_send_does_not_record_product_as_shown(self):
        outcome = make_outcome()
        context = SimpleNamespace(
            user_data={common.OUTCOME_META_KEY: {"s1": {"header": None}}}
        )

        with (
            patch.object(common, "is_product_tracked", new=AsyncMock(return_value=False)),
            patch.object(
                common,
                "send_product_card",
                new=AsyncMock(side_effect=RuntimeError("Telegram unavailable")),
            ),
            patch.object(common, "record_event", new=AsyncMock()) as event,
            patch.object(common, "remember_shown", new=AsyncMock()) as shown,
            patch.object(common, "remember_views", new=AsyncMock()) as views,
        ):
            delivered = await search._send_product_cards(
                context, 47, outcome, "s1", user_id=1, telegram_id=555
            )

        self.assertFalse(delivered)
        event.assert_not_awaited()
        shown.assert_not_awaited()
        views.assert_not_awaited()

    async def test_successful_initial_send_is_recorded_after_delivery(self):
        outcome = make_outcome()
        context = SimpleNamespace(
            user_data={common.OUTCOME_META_KEY: {"s1": {"header": None}}}
        )
        calls = []

        async def send(*args, **kwargs):
            calls.append("send")

        async def record(*args, **kwargs):
            calls.append("event")

        async def shown(*args, **kwargs):
            calls.append("shown")

        async def views(*args, **kwargs):
            calls.append("views")

        with (
            patch.object(common, "is_product_tracked", new=AsyncMock(return_value=False)),
            patch.object(common, "send_product_card", new=AsyncMock(side_effect=send)),
            patch.object(common, "record_event", new=AsyncMock(side_effect=record)),
            patch.object(common, "remember_shown", new=AsyncMock(side_effect=shown)),
            patch.object(common, "remember_views", new=AsyncMock(side_effect=views)),
            patch.object(common, "session_flow", return_value="search"),
        ):
            delivered = await search._send_product_cards(
                context, 47, outcome, "s1", user_id=1, telegram_id=555
            )

        self.assertTrue(delivered)
        self.assertEqual(calls, ["send", "event", "shown", "views"])

    async def test_optional_summary_failure_does_not_fail_delivered_cards(self):
        outcome = make_outcome()
        context = SimpleNamespace(
            user_data={},
            bot=SimpleNamespace(
                send_message=AsyncMock(side_effect=RuntimeError("summary unavailable"))
            ),
        )

        with (
            patch.object(common, "curate_products", new=AsyncMock(return_value=outcome.products)),
            patch.object(common, "remember_outcome", return_value="session-1"),
            patch.object(search, "_log_unapplied_constraints"),
            patch.object(search, "log_final_pool"),
            patch.object(search, "_send_product_cards", new=AsyncMock(return_value=True)),
            patch.object(search, "_result_summary_text", return_value="summary"),
        ):
            session_id = await search.send_search_results(
                context, 47, outcome, follow_up=True
            )

        self.assertEqual(session_id, "session-1")
        context.bot.send_message.assert_awaited_once()


    async def test_count_summary_failure_does_not_fail_delivered_subset(self):
        outcome = make_outcome()
        update = SimpleNamespace(
            effective_message=SimpleNamespace(chat_id=47),
            effective_user=SimpleNamespace(id=555),
        )
        context = SimpleNamespace(
            user_data={},
            bot=SimpleNamespace(
                send_message=AsyncMock(side_effect=RuntimeError("summary unavailable"))
            ),
        )

        with (
            patch.object(search, "_last_shown_session", return_value="old"),
            patch.object(common, "get_cached_outcome", return_value=outcome),
            patch.object(search, "is_count_only_follow_up", return_value=1),
            patch.object(search, "_poolled_outcome", return_value=outcome),
            patch.object(common, "register_user", new=AsyncMock(return_value=1)),
            patch.object(search, "send_search_results", new=AsyncMock(return_value="new")),
        ):
            handled = await search._answer_from_shown(update, context, "покажи один")

        self.assertTrue(handled)
        context.bot.send_message.assert_awaited_once()


    async def test_failed_browse_edit_does_not_advance_shown_history(self):
        outcome = make_outcome()
        meta = {"track_shown": False, "max_index": 0}
        query = SimpleNamespace(
            data="pnav:s1:1",
            message=SimpleNamespace(chat_id=47, photo=[], message_id=10),
            answer=AsyncMock(),
        )
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=555),
        )
        context = SimpleNamespace(
            user_data={common.AI_QUESTION_KEY: {"session_id": "s1", "index": 0}}
        )

        with (
            patch.object(common, "get_cached_outcome", return_value=outcome),
            patch.object(common, "register_user", new=AsyncMock(return_value=1)),
            patch.object(common, "get_outcome_meta", return_value=meta),
            patch.object(common, "session_flow", return_value="search"),
            patch.object(common, "is_product_tracked", new=AsyncMock(return_value=False)),
            patch.object(search, "_edit_browsed_card", new=AsyncMock(return_value=False)),
            patch.object(common, "remember_shown", new=AsyncMock()) as shown,
            patch.object(common, "remember_views", new=AsyncMock()) as views,
            patch.object(common, "record_event", new=AsyncMock()) as event,
        ):
            await search.browse_callback(update, context)

        query.answer.assert_awaited_once()
        shown.assert_not_awaited()
        views.assert_not_awaited()
        event.assert_not_awaited()
        self.assertEqual(meta["max_index"], 0)
        self.assertIn(common.AI_QUESTION_KEY, context.user_data)

    async def test_successful_browse_cancels_pending_product_question(self):
        outcome = make_outcome()
        meta = {"track_shown": False, "max_index": 0}
        query = SimpleNamespace(
            data="pnav:s1:1",
            message=SimpleNamespace(chat_id=47, photo=[], message_id=10),
            answer=AsyncMock(),
        )
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=555),
        )
        context = SimpleNamespace(
            user_data={common.AI_QUESTION_KEY: {"session_id": "s1", "index": 0}}
        )

        with (
            patch.object(common, "get_cached_outcome", return_value=outcome),
            patch.object(common, "register_user", new=AsyncMock(return_value=1)),
            patch.object(common, "get_outcome_meta", return_value=meta),
            patch.object(common, "session_flow", return_value="search"),
            patch.object(common, "is_product_tracked", new=AsyncMock(return_value=False)),
            patch.object(search, "_edit_browsed_card", new=AsyncMock(return_value=True)),
            patch.object(common, "remember_shown", new=AsyncMock()),
            patch.object(common, "remember_views", new=AsyncMock()),
            patch.object(common, "record_event", new=AsyncMock()),
        ):
            await search.browse_callback(update, context)

        self.assertNotIn(common.AI_QUESTION_KEY, context.user_data)


if __name__ == "__main__":
    unittest.main()
