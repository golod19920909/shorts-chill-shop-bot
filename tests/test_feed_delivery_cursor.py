from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers import common, discovery
from models.product import Marketplace, Product
from models.search_request import SearchMode, SearchRequest
from services.search_service import SearchOutcome


def run(coro):
    return asyncio.run(coro)


def make_feed_outcome() -> SearchOutcome:
    product = Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id="feed-1",
        title="Bluetooth headphones",
        product_url="https://www.aliexpress.com/item/feed-1.html",
        price=10.0,
        currency="USD",
    )
    return SearchOutcome(
        request=SearchRequest(
            original_query="находки AliExpress",
            mode=SearchMode.FEED,
            country="US",
            currency="USD",
            page_no=1,
        ),
        products=[product],
        products_before_filtering=[product],
        searched_marketplaces=["AliExpress"],
    )


class FeedDeliveryCursorTests(unittest.IsolatedAsyncioTestCase):
    def make_update_context(self):
        query = SimpleNamespace(answer=AsyncMock())
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=555),
            effective_chat=SimpleNamespace(id=47),
        )
        context = SimpleNamespace(user_data={}, bot_data={})
        return update, context

    async def _run_feed(self, send_result):
        update, context = self.make_update_context()
        outcome = make_feed_outcome()

        class FakeSearchService:
            async def find_feed_page(self, **kwargs):
                return outcome

        with (
            patch.object(common, "register_user", new=AsyncMock(return_value=1)),
            patch.object(common, "get_user_region", new=AsyncMock(return_value={"country": "US", "currency": "USD"})),
            patch.object(common, "feed_seen", new=AsyncMock(return_value=set())),
            patch.object(common, "get_search_service", return_value=FakeSearchService()),
            patch.object(discovery, "_feed_start_page", new=AsyncMock(return_value=1)),
            patch.object(discovery, "_bounded_setting", return_value=1),
            patch.object(discovery, "_remember_feed_page", new=AsyncMock()) as remember_page,
            patch.object(discovery, "record_outcome_events", new=AsyncMock()),
            patch.object(discovery, "send_search_results", new=AsyncMock(return_value=send_result)),
            patch.object(common, "remember_shown", new=AsyncMock()) as remember_shown,
        ):
            await discovery.show_feed(update, context)

        return context, remember_page, remember_shown

    async def test_failed_card_delivery_does_not_advance_feed_cursor(self):
        context, remember_page, remember_shown = await self._run_feed(None)

        self.assertNotIn(discovery.FEED_PAGE_KEY, context.user_data)
        remember_page.assert_not_awaited()
        remember_shown.assert_not_awaited()

    async def test_successful_card_delivery_advances_feed_cursor(self):
        context, remember_page, remember_shown = await self._run_feed("session-1")

        self.assertEqual(context.user_data[discovery.FEED_PAGE_KEY], 2)
        remember_page.assert_awaited_once()
        remember_shown.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
