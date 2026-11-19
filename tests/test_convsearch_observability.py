from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx

from config.settings import MarketplaceCredentials
from database.database import Database
from models.product import Marketplace, Product
from models.search_request import SearchRequest
from services.convsearch.engine import ConversationSearchEngine
from services.convsearch.state import ConversationEngineState
from services.marketplaces.base import MarketplaceProvider
from services.search_service import SearchOutcome, SearchService


def run(coro):
    return asyncio.run(coro)


class StubProvider(MarketplaceProvider):
    marketplace = Marketplace.ALIEXPRESS
    display_name = "AliExpress"

    def __init__(self, products):
        super().__init__(MarketplaceCredentials("k", "s"))
        self.products = list(products)
        self.search_calls = 0

    async def search_products(self, request: SearchRequest) -> list[Product]:
        self.search_calls += 1
        return list(self.products)

    async def get_product(self, product_id: str, currency: str | None = None):
        return next((p for p in self.products if p.product_id == product_id), None)

    def generate_affiliate_link(self, product: Product):
        return product.affiliate_url


class RecordingSearchService(SearchService):
    def __init__(self, provider: StubProvider):
        super().__init__([provider], ai_service=None)
        self.provider = provider
        self.calls: list[SearchRequest] = []

    async def find_products(
        self, request, *, exclude=None, broaden=True,
        raw=False, raw_broaden=False
    ):
        self.calls.append(request.model_copy(deep=True))
        return await super().find_products(
            request, exclude=exclude, broaden=broaden,
            raw=raw, raw_broaden=raw_broaden,
        )


class ConversationEngineObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(str(Path(self.tmp.name) / "convsearch.db"))
        run(self.db.initialize())
        run(self.db.get_or_create_user(555, "tester", "Tester"))
        self.provider = StubProvider([])
        self.addCleanup(lambda: run(self.provider.aclose()))
        self.service = RecordingSearchService(self.provider)
        self.engine = ConversationSearchEngine()
        self.engine.bind_search_service(self.service)

    def test_turn_log_has_action_and_reason_but_not_user_text(self):
        state = ConversationEngineState(user_key="observability")
        with self.assertLogs("services.convsearch.engine", level="INFO") as logs:
            run(self.engine.handle_message(
                "observability", "наушники", state=state
            ))
        line = next(entry for entry in logs.output if "convsearch" in entry)
        self.assertIn("action=", line)
        self.assertIn("reason=", line)
        self.assertNotIn("наушники", line)

    def test_empty_provider_result_is_not_reported_as_provider_error(self):
        state = ConversationEngineState(user_key="empty")
        decision = run(self.engine.handle_message("empty", "наушники", state=state))
        self.assertEqual(decision.outcome.value, "no_candidates")
        self.assertEqual(self.provider.search_calls, 1)

    def test_provider_exception_is_reported_as_provider_error(self):
        class ExplodingSearchService(SearchService):
            async def find_products(self, request, **kwargs):
                raise RuntimeError("simulated provider outage")

        engine = ConversationSearchEngine()
        engine.bind_search_service(ExplodingSearchService([self.provider]))
        state = ConversationEngineState(user_key="failure")
        decision = run(engine.handle_message("failure", "наушники", state=state))
        self.assertEqual(decision.outcome.value, "provider_error")

    def test_retrieval_request_is_recorded_for_diagnostics(self):
        state = ConversationEngineState(user_key="recorded")
        run(self.engine.handle_message("recorded", "наушники", state=state))
        self.assertEqual(len(self.service.calls), 1)
        self.assertEqual(self.service.calls[0].normalized_query, "наушники")


if __name__ == "__main__":
    unittest.main()
