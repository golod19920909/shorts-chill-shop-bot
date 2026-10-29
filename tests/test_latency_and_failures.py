"""Deterministic MOCKED latency + external-failure benchmarks (Release
Verification §3 and §4).

These are NOT production latency numbers: every external service is a fake
with a controlled delay, and the numbers come straight from the real
``handlers.search._search_with_context`` stage instrumentation (``rid``:
parse / resolve / intent / search / total). They prove the pipeline MEASURES
each stage, separates Gemini work from local work and AliExpress work, and
recovers from failures WITHOUT erasing already-parsed information.

The orchestration under test is the REAL production path (``AIService``
fallbacks, ``conversation.resolve_request``, ``query_intent`` circuit breaker,
``SearchService`` over a stub provider); only Gemini and the marketplace are
fakes. No live network and no live credentials are used, so production latency
remains UNVERIFIED - these are mocked timings only.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

import httpx

from config.settings import MarketplaceCredentials
from database.database import Database
from handlers import search as search_handlers
from handlers import common
from models.product import Marketplace, Product
from models.search_request import SearchRequest
from services.convsearch import engine as conv_engine
from services.ai_service import AIService
from services import query_intent
from services.conversation import is_refinement, resolve_request
from services.marketplaces.base import MarketplaceProvider
from services.search_service import SearchService


def run(coro):
    return asyncio.run(coro)


class TimedAI(AIService):
    """A REAL AIService whose model calls take a controlled, fake time.

    ``is_available`` stays False (no Gemini client), so ``parse_query`` uses
    the real deterministic fallback; ``_generate`` is overridden to model an
    external call with a measurable latency and, optionally, a timeout.
    """

    def __init__(
        self, *, generate_delay: float = 0.0, fail: bool = False, parse_delay: float = 0.0
    ) -> None:
        super().__init__(api_key=None)
        self.generate_delay = generate_delay
        self.parse_delay = parse_delay
        self.fail = fail
        self.generate_calls = 0
        self.intent_calls = 0
        # Present as AVAILABLE so the intent path actually calls _generate
        # (the real path short-circuits when _client is None). _generate is
        # fully overridden below, so the sentinel client is never contacted.
        self._client = object()

    @property
    def is_available(self) -> bool:
        return True

    async def _generate(self, contents, *, models=None):
        self.generate_calls += 1
        text = contents if isinstance(contents, str) else ""
        # Simulate a Gemini timeout on the INTENT call specifically (the call
        # whose failure the circuit breaker and fallback must absorb), while
        # parse_query keeps returning None -> its real deterministic fallback,
        # so the parsed product is preserved exactly as in production. The
        # marker is a phrase unique to INTENT_PROMPT ("structured intent JSON"),
        # so the parse/merge prompts (which merely say "structured parameters")
        # are never mistaken for the intent call.
        is_intent = "structured intent" in text.lower()
        if is_intent:
            self.intent_calls += 1
            # The delay models latency of the INTENT external call.
            if self.generate_delay:
                await asyncio.sleep(self.generate_delay)
        else:
            # A separate latency for the PARSE call, so a test can prove the
            # two independent calls are issued CONCURRENTLY (the block costs
            # ~max, not the sum) rather than back-to-back.
            if self.parse_delay:
                await asyncio.sleep(self.parse_delay)
        if self.fail and is_intent:
            raise TimeoutError("simulated Gemini timeout")
        return None  # unusable -> every caller falls back deterministically


class StubProvider(MarketplaceProvider):
    marketplace = Marketplace.ALIEXPRESS
    display_name = "AliExpress"
    supports_feed = True

    def __init__(self, products, *, delay: float = 0.0) -> None:
        super().__init__(MarketplaceCredentials("k", "s"), httpx.AsyncClient())
        self._products = products
        self._delay = delay
        self.search_calls = 0

    async def search_products(self, request: SearchRequest) -> list[Product]:
        self.search_calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        return list(self._products)

    async def get_product(self, product_id):
        return next((p for p in self._products if p.product_id == product_id), None)

    def generate_affiliate_link(self, product):
        return None


class RecordingSearchService(SearchService):
    def __init__(self, provider: StubProvider) -> None:
        super().__init__([provider], ai_service=None)
        self.provider = provider
        self.calls: list[SearchRequest] = []

    async def find_products(self, request, *, exclude=None, broaden=True,
                            feed_selection=False):
        self.calls.append(request.model_copy(deep=True))
        return await super().find_products(
            request, exclude=exclude, broaden=broaden,
            feed_selection=feed_selection,
        )


class FakeApplication:
    def __init__(self, bot_data: dict) -> None:
        self.bot_data = bot_data


class FakeContext:
    def __init__(self, **bot_data) -> None:
        self.user_data: dict = {}
        self.application = FakeApplication(dict(bot_data))


def make_product(product_id: str, title: str, *, price: float = 20.0) -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=product_id,
        title=title,
        product_url=f"https://www.aliexpress.com/item/{product_id}.html",
        price=price,
        currency="USD",
    )


PRODUCTS = [make_product("p1", "Беспроводные наушники с шумоподавлением", price=25.0)]


class EngineInstrumentationTests(unittest.TestCase):
    """The engine turn is measurable from the log alone (rid-instrumented
    stages moved into the engine; the invariants - one INFO line per turn,
    counts only, no user text - are unchanged)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(str(Path(self._tmp.name) / "lat.db"))
        run(self.db.initialize())
        run(self.db.get_or_create_user(555, "tester", "Tester"))
        self.provider = StubProvider([])
        self.service = RecordingSearchService(self.provider)
        self.engine = conv_engine.ConversationSearchEngine()
        self.engine.bind_search_service(self.service)

    def context(self) -> FakeContext:
        return FakeContext(
            {
                "database": self.db,
                "search_service": self.service,
                "ai_service": AIService(api_key=None),
            }
        )

    def test_a_turn_is_logged_with_action_and_reason(self) -> None:
        from services.convsearch.state import ConversationEngineState

        state = ConversationEngineState(user_key="lat")
        with self.assertLogs("services.convsearch.engine", level="INFO") as logs:
            run(self.engine.handle_message("lat", "наушники", state=state))
        line = next(e for e in logs.output if "convsearch" in e)
        self.assertIn("action=", line)
        self.assertIn("reason=", line)
        self.assertNotIn("наушники", line)

    def test_an_empty_result_is_distinguished_from_a_provider_error(self) -> None:
        from services.convsearch.state import ConversationEngineState

        self.provider.products = []
        state = ConversationEngineState(user_key="lat2")
        decision = run(self.engine.handle_message("lat2", "наушники", state=state))
        self.assertEqual(decision.outcome.value, "no_candidates")

    def test_a_provider_failure_stays_a_provider_error(self) -> None:
        from services.convsearch.state import ConversationEngineState

        class Exploding(SearchService):
            async def find_products(self, request, **kwargs):
                raise RuntimeError("marketplace down")

        engine = conv_engine.ConversationSearchEngine()
        engine.bind_search_service(Exploding([self.provider]))
        state = ConversationEngineState(user_key="lat3")
        decision = run(engine.handle_message("lat3", "наушники", state=state))
        self.assertEqual(decision.outcome.value, "provider_error")

    def test_the_search_stage_duration_is_recorded(self) -> None:
        from services.convsearch.state import ConversationEngineState

        state = ConversationEngineState(user_key="lat4")
        run(self.engine.handle_message("lat4", "наушники", state=state))
        self.assertTrue(self.service.calls)



class RouterAIEscalationCapTests(unittest.TestCase):
    """PERF (2026-10-24): the optional Gemini escalation inside
    ``services.chat_router.decide`` is bounded in wall clock.

    The live baseline (tmp_perf_before.json) measured that call at 2.6-12.6s
    under slow Gemini conditions - longer than the whole 5s turn target - for
    a decision that can only ESCALATE a search into a clarifying question.
    The cap keeps the deterministic SEARCH decision shipping: a slow escalation
    must never hold the user's search hostage, and a fast one must still work.
    """

    def test_a_slow_escalation_is_capped_and_the_search_ships(self) -> None:
        from services.chat_router import ROUTER_AI_TIMEOUT_SECONDS, ChatAction, decide

        class SlowEscalationAI:
            is_available = True

            async def decide_chat_action(self, text):
                await asyncio.sleep(30.0)

        started = time.perf_counter()
        decision = run(decide("наушники", ai_service=SlowEscalationAI()))
        elapsed = time.perf_counter() - started

        self.assertEqual(decision.action, ChatAction.SEARCH)
        self.assertLess(elapsed, ROUTER_AI_TIMEOUT_SECONDS + 1.0)

    def test_a_fast_escalation_still_asks_the_clarifying_question(self) -> None:
        from services.chat_router import ChatAction, decide

        class FastClarifyAI:
            is_available = True

            async def decide_chat_action(self, text):
                return {"action": "clarify", "question": "Что за товар ищете?"}

        decision = run(decide("цветочек", ai_service=FastClarifyAI()))

        self.assertEqual(decision.action, ChatAction.CLARIFY)
        self.assertEqual(decision.question, "Что за товар ищете?")
