"""End-to-end conversational turn tests with offline AI/provider doubles."""
from __future__ import annotations

import unittest

from models.product import Marketplace, Product
from services.convsearch.contract import Outcome
from services.convsearch.engine import ConversationSearchEngine
from services.convsearch.retrieval import BackendOutcome
from services.convsearch.state import ConversationEngineState


def listing(pid: str, title: str, price: float = 1000.0) -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=pid,
        title=title,
        product_url=f"https://www.aliexpress.com/item/{pid}.html",
        price=price,
        currency="UAH",
    )


class FakeAI:
    def __init__(self, *turns):
        self.turns = list(turns)
        self.contexts = []

    async def decide_conversation_turn(self, text, *, context=None):
        self.contexts.append((text, context))
        return self.turns.pop(0) if self.turns else None


class FakeBackend:
    def __init__(self, products):
        self.products = products
        self.strategies = []

    async def retrieve(self, strategy, *, exclude=None):
        self.strategies.append(strategy)
        return BackendOutcome(
            ok=True,
            reason="ok" if self.products else "no_candidates",
            products=list(self.products),
            request=strategy.request,
        )


class ConversationalTurnIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_budget_followup_keeps_active_product_and_is_not_searched_as_type(self):
        ai = FakeAI(
            {
                "action": "new_search",
                "confidence": 0.98,
                "product_type": "сварочный аппарат",
                "question": None,
            },
            {
                "action": "refine",
                "confidence": 0.99,
                "product_type": None,
                "question": None,
            },
        )
        backend = FakeBackend([
            listing("welder-1", "Сварочный аппарат инверторный бытовой")
        ])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="integration-budget")

        first = await engine.handle_message(
            "integration-budget", "Сварочный аппарат", state=state, currency="UAH"
        )
        self.assertIs(first.outcome, Outcome.RESULTS)
        self.assertEqual(state.active_by_kind("type").value, "сварочный аппарат")

        followup = "До 2000 тысяч гривен"
        second = await engine.handle_message(
            "integration-budget", followup, state=state, currency="UAH"
        )
        self.assertIs(second.outcome, Outcome.RESULTS)
        self.assertEqual(backend.strategies[-1].product_type, "сварочный аппарат")
        self.assertEqual(
            backend.strategies[-1].request.normalized_query, "сварочный аппарат"
        )
        self.assertEqual(backend.strategies[-1].request.max_price, 2_000_000.0)
        self.assertFalse(backend.strategies[-1].request.required_features)
        self.assertEqual(ai.contexts[-1][1]["active_slots"][0]["value"], "сварочный аппарат")

    async def test_gemini_clarify_asks_question_without_provider_call(self):
        ai = FakeAI({
            "action": "clarify",
            "confidence": 0.96,
            "product_type": None,
            "question": "Что именно хотите подобрать?",
        })
        backend = FakeBackend([listing("x", "Любой товар")])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)

        decision = await engine.handle_message(
            "clarify-user", "Что-нибудь полезное", state=ConversationEngineState(user_key="clarify-user")
        )

        self.assertIs(decision.outcome, Outcome.CLARIFY)
        self.assertEqual(decision.question, "Что именно хотите подобрать?")
        self.assertEqual(backend.strategies, [])

    async def test_semantic_selector_removes_wrong_product_from_results(self):
        class SelectingAI(FakeAI):
            async def select_conversation_candidates(self, candidates, *, query, wanted=20):
                self.selection_input = [item.product_id for item in candidates]
                self.selection_query = query
                self.selection_wanted = wanted
                return ["welder-1"]

        ai = SelectingAI(None)
        backend = FakeBackend([
            listing("mask-1", "Сварочная маска автоматическая"),
            listing("welder-1", "Сварочный аппарат инверторный бытовой"),
        ])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)

        decision = await engine.handle_message(
            "semantic-user", "Сварочный аппарат",
            state=ConversationEngineState(user_key="semantic-user"), currency="UAH"
        )

        self.assertIs(decision.outcome, Outcome.RESULTS)
        self.assertEqual([item.product_id for item in decision.products], ["welder-1"])
        self.assertEqual(decision.report["ai_selection"], "selected")
        self.assertEqual(ai.selection_wanted, 1)
        self.assertIn("сварочный аппарат", ai.selection_query)

    async def test_semantic_selector_empty_result_does_not_restore_irrelevant_items(self):
        class EmptySelectingAI(FakeAI):
            async def select_conversation_candidates(self, candidates, *, query, wanted=20):
                return []

        ai = EmptySelectingAI(None)
        backend = FakeBackend([
            listing("mask-1", "Сварочная маска автоматическая"),
            listing("mask-2", "Маска сварщика защитная"),
        ])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)

        decision = await engine.handle_message(
            "semantic-empty-user", "Сварочный аппарат",
            state=ConversationEngineState(user_key="semantic-empty-user"), currency="UAH"
        )

        self.assertIs(decision.outcome, Outcome.NO_ELIGIBLE)
        self.assertEqual(decision.products, [])
        self.assertEqual(decision.report["reason"], "no_semantically_relevant_candidates")
    async def test_uncertain_or_missing_ai_decision_uses_deterministic_fallback(self):
        ai = FakeAI(None)
        backend = FakeBackend([listing("w1", "Сварочный аппарат инверторный")])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)

        decision = await engine.handle_message(
            "fallback-user", "Сварочный аппарат",
            state=ConversationEngineState(user_key="fallback-user"), currency="UAH"
        )

        self.assertIs(decision.outcome, Outcome.RESULTS)
        self.assertEqual(backend.strategies[0].product_type, "сварочный аппарат")


if __name__ == "__main__":
    unittest.main()
