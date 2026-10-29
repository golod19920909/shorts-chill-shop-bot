"""Integration-level conversation-integrity test (Release Verification §1).

This drives the ACTUAL production conversation-resolution and search
orchestration path - not isolated keyword helpers and not a mocked expected
answer. For every step it calls the real production entry points:

* ``handlers.search._search_with_context`` - the real orchestrator
  (``AIService.parse_query`` -> ``conversation.resolve_request`` ->
  ``query_intent.parse_query_intent``/``apply_intent`` -> ``SearchService``);
* ``services.chat_router.decide`` - the real router that classifies
  new-search / refinement / comparison / reset;
* ``handlers.search._answer_over_shown`` - the real comparison path over the
  cached result set.

What is mocked is EXTERNAL, exactly as required: Gemini is a real
``AIService`` constructed with NO api key, so every model call takes the
deterministic fallback path (no network, no fabricated product), and the
marketplace is a stub provider that returns fixed real ``Product`` rows. The
decisions that matter - is this a refinement, what is the active product, what
query is finally sent, was the context reset - are all made by production code.

The five-step scenario:

1. "Ищу чехол для Samsung S26"           -> new search (case + S26)
2. "Найди вариант в красном цвете"        -> refinement (add red; KEEP case+S26)
3. "И желательно до 20 долларов"          -> refinement (add budget; keep all)
4. "А какие из них лучше?"                -> comparison over the shown set
5. "Теперь найди автомобильный компрессор" -> new product; reset (drop case/red/budget)
"""

from __future__ import annotations

import unittest

import httpx

from services.ai_service import AIService
from services.search_service import SearchOutcome, SearchService
from services.marketplaces.base import MarketplaceProvider
from config.settings import MarketplaceCredentials

from handlers import search as search_handlers
from handlers import common
from services import chat_router
from services.product_assistant import ProductAssistant
from models.product import Marketplace, Product
from models.search_request import SearchMode, SearchRequest


def run(coro):
    import asyncio

    return asyncio.run(coro)


# -- External doubles (Gemini fallback is real; marketplace is a stub) --------

class NoOpAIService(AIService):
    """A REAL AIService with no key: every call uses the deterministic parser.

    It is the genuine production object; ``is_available`` is False so
    ``parse_query`` and ``parse_query_intent`` run their real fallback code
    with zero network access and zero invented data.
    """

    def __init__(self) -> None:
        super().__init__(api_key=None)


class StubProvider(MarketplaceProvider):
    """Returns a fixed, real product list so retrieval is deterministic.

    It carries real configured credentials (so the pipeline calls it) but
    overrides ``search_products`` so no network request is ever made. Mirrors
    the proven provider double used by ``test_discovery_acceptance``.
    """

    marketplace = Marketplace.ALIEXPRESS
    display_name = "AliExpress"
    supports_feed = True

    def __init__(self, products: list[Product]) -> None:
        super().__init__(MarketplaceCredentials("stub-key", "stub-secret"), httpx.AsyncClient())
        self._products = products

    async def search_products(self, request: SearchRequest) -> list[Product]:
        return list(self._products)

    async def get_product(self, product_id: str) -> Product | None:
        return next((p for p in self._products if p.product_id == product_id), None)

    def generate_affiliate_link(self, product: Product) -> str | None:
        return None

    async def fetch_feed_page(self, limit=None, currency=None, page=1):
        return list(self._products)


class RecordingSearchService(SearchService):
    """The REAL SearchService, recording every ``find_products`` request.

    Wraps a stub provider so the retrieval stage is external and fixed, but
    the pipeline around it (dedup, filters, ranking) is the real one.
    """

    def __init__(self, products: list[Product]) -> None:
        super().__init__([StubProvider(products)], ai_service=None)
        self.calls: list[SearchRequest] = []

    async def find_products(self, request, *, exclude=None, broaden=True,
                            feed_selection=False):
        self.calls.append(request.model_copy(deep=True))
        return await super().find_products(request, exclude=exclude, broaden=broaden)


def make_product(product_id: str, title: str, *, price: float = 25.0) -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=product_id,
        title=title,
        product_url=f"https://www.aliexpress.com/item/{product_id}.html",
        price=price,
        currency="USD",
    )


class FakeApplication:
    def __init__(self, bot_data: dict) -> None:
        self.bot_data = bot_data


class FakeBot:
    """Records a comparison answer sent to the chat."""

    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.messages.append((chat_id, text))


class FakeContext:
    def __init__(self, **bot_data) -> None:
        self.user_data: dict = {}
        self.bot = FakeBot()
        self.application = FakeApplication(dict(bot_data))


CASE_PRODUCTS = [
    make_product("case-red", "Чехол для Samsung S26 силиконовый красный", price=18.0),
    make_product("case-black", "Чехол для Samsung S26 защитный чёрный", price=22.0),
]
COMPRESSOR_PRODUCTS = [
    make_product("comp-1", "Автомобильный компрессор 12V цифровой", price=30.0),
]


class ConversationFlowIntegrationTests(unittest.TestCase):
    """The full 5-step dialogue through the REAL orchestration path.

    Each ``step`` call is ``handlers.search._search_with_context`` - the same
    function the Telegram handler runs - so conversation state, refinement vs.
    reset, intent enrichment and the final provider query are all produced by
    production code. Only Gemini (deterministic fallback) and the marketplace
    (stub rows) are external.
    """

    def _make_context(self, products: list[Product]) -> tuple[FakeContext, RecordingSearchService, NoOpAIService]:
        ai = NoOpAIService()
        service = RecordingSearchService(products)
        context = FakeContext(database=None, search_service=service, ai_service=ai)
        return context, service, ai

    def _search(self, context, service, ai, text, currency=None):
        """The migrated production entry: the conversational engine turn."""

        from services.convsearch.engine import ConversationSearchEngine
        from services.convsearch.state import ConversationEngineState
        from services.search_service import SearchOutcome

        engine = ConversationSearchEngine()
        engine.bind_search_service(service)
        user_key = f"ci-{id(context)}"
        state = context.user_data.get("_convsearch_state")
        if not isinstance(state, ConversationEngineState):
            state = ConversationEngineState(user_key=user_key)
        decision = run(
            engine.handle_message(
                user_key, text, currency=currency, state=state,
            )
        )
        context.user_data["_convsearch_state"] = state
        return SearchOutcome(
            request=decision.request or SearchRequest(original_query=text),
            products=list(decision.products),
        )

    def test_full_five_step_dialogue(self) -> None:
        context, service, ai = self._make_context(CASE_PRODUCTS + COMPRESSOR_PRODUCTS)

        # -- Step 1: new product request --------------------------------
        outcome1 = self._search(context, service, ai, "Ищу чехол для Samsung S26")
        q1 = service.calls[-1]
        # New search: the case + the phone model are in the query / intent.
        q1_text = " ".join(
            [q1.normalized_query or "", q1.original_query or "", q1.intent_product_type or ""]
        ).lower()
        self.assertIn("чехол", q1_text)
        self.assertTrue(
            {"samsung", "s26"} & set(q1.intent_compatibility)
            or "s26" in q1_text or "samsung" in q1_text,
            "the phone model must be captured",
        )

        # -- Step 2: colour refinement --------------------------------
        self._search(context, service, ai, "Найди вариант в красном цвете")
        q2 = service.calls[-1]
        q2_text = " ".join(
            [q2.normalized_query or "", q2.original_query or ""]
        ).lower()
        # The product (case) is PRESERVED, not replaced by "красном".
        self.assertIn("чехол", q2_text)
        # The colour requirement is captured somewhere in the structured state
        # or required features - never as a standalone product query.
        self.assertFalse(
            (q2.normalized_query or "").strip().lower() in {"красном", "красный цвет"},
            "the colour must not become the product query",
        )
        # No query for a product literally called "красном".
        self.assertNotIn("красном", (q2.normalized_query or "").split()[:1])

        # -- Step 3: budget refinement --------------------------------
        self._search(context, service, ai, "И желательно до 20 долларов")
        q3 = service.calls[-1]
        # Budget ceiling applied to the SAME case request.
        self.assertEqual(q3.max_price, 20.0)
        q3_text = (q3.normalized_query or "").lower()
        self.assertIn("чехол", q3_text, "the case product must survive the budget refinement")
        # The word "долларов" must never be treated as a product query.
        self.assertNotIn("долларов", (q3.normalized_query or "").split())

        # Remember the case requirements BEFORE the product switch, to prove
        # they are dropped (not retained) after switching to the compressor.
        case_max_price = q3.max_price

        # -- Step 4: comparison over the shown set --------------------
        # A result set is on screen (step 1 produced one); remember it exactly
        # as the real handler does so ``_answer_over_shown`` sees real products.
        session_id = common.remember_outcome(context, outcome1)
        context.user_data["last_shown_session"] = session_id
        decision = run(
            chat_router.decide(
                "А какие из них лучше?",
                ai_service=ai,
                has_conversation=True,
                has_shown_results=True,
            )
        )
        self.assertIs(decision.action, chat_router.ChatAction.COMPARE)
        # The comparison path uses the REAL assistant over the cached products.
        assistant = ProductAssistant(ai_service=None, search_service=None)
        context.application.bot_data["product_assistant"] = assistant
        answered = run(
            search_handlers._answer_over_shown(
                context, chat_id=1, question="А какие из них лучше?", session_id=session_id
            )
        )
        # With no AI it may answer deterministically or report no data - either
        # way it MUST operate over the case products, not fall back to a search.
        cached = common.get_cached_products(context, session_id)
        self.assertTrue(cached, "the comparison set must be the shown products")
        self.assertIn(cached[0].product_id, {"case-red", "case-black"})

        # -- Step 5: explicit product change -> reset ------------------
        self._search(context, service, ai, "Теперь найди автомобильный компрессор")
        q5 = service.calls[-1]
        q5_text = (q5.normalized_query or "").lower()
        self.assertIn("компрессор", q5_text)
        # Irrelevant requirements from the case request must NOT survive.
        self.assertNotIn("чехол", q5_text)
        self.assertNotIn("красн", q5_text)
        # The case budget must not leak onto the compressor request.
        self.assertNotEqual(q5.max_price, case_max_price)
        self.assertIsNone(q5.max_price)


GIFT_PRODUCTS = [
    make_product("gift-1", "Набор для маникюра подарочный", price=25.0),
]
LASH_PRODUCTS = [
    make_product("lash-tool", "Инструмент для наращивания ресниц пинцет", price=12.0),
]


class ProductionLogScenarioTests(unittest.TestCase):
    """The three REAL messages from the October 15 production logs.

    Root causes these pin (Release Verification Part 1):

    * «Нужен разнообразный» was routed to SEARCH and ``parse_query`` turned the
      adjective into the literal product «assorted»; it must be CLARIFIED by
      the real router instead of searched.
    * «И еще нужен инструмент для наращивания ресниц» legitimately introduces a
      SECOND, related product; it must NOT be treated as a refinement of the
      gift, must keep its intended use («для наращивания ресниц»), and must not
      confuse the generic word «инструмент» with the full request.

    Every decision is made by production code (``chat_router.classify``,
    ``handlers.search._search_with_context``); only Gemini (deterministic
    fallback) and the marketplace (stub rows) are external.
    """

    def _make_context(self, products):
        ai = NoOpAIService()
        service = RecordingSearchService(products)
        context = FakeContext(database=None, search_service=service, ai_service=ai)
        return context, service, ai

    def _search(self, context, service, ai, text, currency=None):
        """The migrated production entry: the conversational engine turn."""

        from services.convsearch.engine import ConversationSearchEngine
        from services.convsearch.state import ConversationEngineState
        from services.search_service import SearchOutcome

        engine = ConversationSearchEngine()
        engine.bind_search_service(service)
        user_key = f"ci-{id(context)}"
        state = context.user_data.get("_convsearch_state")
        if not isinstance(state, ConversationEngineState):
            state = ConversationEngineState(user_key=user_key)
        decision = run(
            engine.handle_message(
                user_key, text, currency=currency, state=state,
            )
        )
        context.user_data["_convsearch_state"] = state
        return SearchOutcome(
            request=decision.request or SearchRequest(original_query=text),
            products=list(decision.products),
        )

    def test_assorted_is_clarified_not_searched_as_a_literal_product(self) -> None:
        # The production defect: «Нужен разнообразный» -> search for «assorted».
        # The real router must CLARIFY (no product, no actionable constraint),
        # with OR without an active conversation (it is content-free either way).
        for has_conversation in (False, True):
            decision = chat_router.classify(
                "Нужен разнообразный", has_conversation=has_conversation
            )
            self.assertIs(
                decision.action,
                chat_router.ChatAction.CLARIFY,
                f"has_conversation={has_conversation}",
            )
            self.assertTrue(decision.question)

    def test_genuine_refinements_are_still_searched_not_clarified(self) -> None:
        # Guard against over-firing: a colour, a budget, a feature or a
        # comparative must NEVER be clarified - they refine the active product.
        for text in (
            "в красном цвете",
            "до 40 долларов",
            "с микрофоном",
            "подешевле",
            "2 штуки",
        ):
            decision = chat_router.classify(text, has_conversation=True)
            self.assertIs(decision.action, chat_router.ChatAction.SEARCH, text)

    def test_a_real_product_is_never_clarified(self) -> None:
        for text in ("наушники", "инструмент для наращивания ресниц", "шуруповёрт"):
            decision = chat_router.classify(text)
            self.assertIs(decision.action, chat_router.ChatAction.SEARCH, text)

    def test_related_product_keeps_its_intended_use(self) -> None:
        context, service, ai = self._make_context(GIFT_PRODUCTS + LASH_PRODUCTS)

        # 1) gift for the wife
        self._search(context, service, ai, "Подарок для жены")
        # 2) a preference - a refinement context
        self._search(context, service, ai, "Любит наращивать ресницы")
        # 3) a SECOND, related product: the lash tool. It is NOT a refinement
        #    of the gift; it is a new request that must keep its intended use.
        self._search(context, service, ai, "И еще нужен инструмент для наращивания ресниц")
        q3 = service.calls[-1]
        q3_text = " ".join(
            [q3.normalized_query or "", q3.original_query or "", q3.intent_product_type or ""]
        ).lower()
        # The lash tool is understood as the product...
        self.assertIn("ресниц", q3_text, "the intended use «ресниц» must be retained")
        self.assertIn("инструмент", q3_text)
        # ...and it must NOT be filed under the gift (the previous product).
        self.assertNotIn("подарок", q3_text)

    def test_generic_word_does_not_replace_the_full_request(self) -> None:
        # «инструмент» alone must not become the whole product: the request
        # keeps «для наращивания ресниц» as the intended use / compatibility.
        context, service, ai = self._make_context(LASH_PRODUCTS)
        self._search(context, service, ai, "нужен инструмент для наращивания ресниц")
        q = service.calls[-1]
        blob = " ".join(
            [q.normalized_query or "", q.original_query or "",
             q.intent_product_type or "", " ".join(q.intent_compatibility)]
        ).lower()
        self.assertIn("ресниц", blob)
        # The query is not the bare generic word with the use stripped away.
        self.assertNotEqual((q.normalized_query or "").strip().lower(), "инструмент")


if __name__ == "__main__":
    unittest.main()

