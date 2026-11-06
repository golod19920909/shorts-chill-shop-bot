"""Regression tests for inherited currency in the semantic budget review."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from models.product import Marketplace, Product
from services.convsearch.contract import Outcome, SearchDecision
from services.convsearch.engine import ConversationSearchEngine
from services.convsearch.state import ConversationEngineState, Slot


class SemanticBudgetCurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_currency_free_budget_inherits_search_currency_for_gemini(self) -> None:
        engine = ConversationSearchEngine()
        state = ConversationEngineState(user_key="selector-budget-inherited-currency")
        state.slots.append(Slot(
            kind="budget",
            value="2000.0",
            source_turn=1,
            raw_text="до 2000",
        ))
        candidate = Product(
            marketplace=Marketplace.ALIEXPRESS,
            product_id="budget-inherited-currency",
            title="Спиннинговая удочка",
            product_url="https://www.aliexpress.com/item/budget-inherited-currency.html",
            price=1200,
            currency="UAH",
        )
        decision = SearchDecision(
            outcome=Outcome.RESULTS,
            products=[candidate],
            report={},
        )
        selector = AsyncMock(return_value=[candidate.product_id])
        engine.bind_ai_service(SimpleNamespace(
            select_conversation_candidates=selector,
        ))
        strategy = SimpleNamespace(
            request=SimpleNamespace(
                normalized_query="спиннинг",
                original_query="",
                intent_marketplace_terms=[],
                currency="UAH",
            ),
            result_limit=30,
        )

        await engine._apply_ai_candidate_selection(decision, strategy, state)

        self.assertEqual(
            selector.await_args.kwargs["budget_constraint"],
            "2000.0 UAH",
        )
        self.assertIn("ЖЁСТКИЙ БЮДЖЕТ: до 2000.0 UAH", selector.await_args.kwargs["query"])
