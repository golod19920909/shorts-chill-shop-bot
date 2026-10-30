"""Validation tests for the Gemini conversational-turn contract."""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from models.product import Marketplace, Product
from services.ai_service import AIService


class ConversationTurnContractTests(unittest.IsolatedAsyncioTestCase):
    def make_service(self, response: str | None) -> AIService:
        service = AIService(api_key=None, timeout_seconds=10.0)
        service._client = object()  # enable the call without a network client
        service._generate = AsyncMock(return_value=response)
        return service

    async def test_valid_new_search_returns_only_validated_fields(self):
        service = self.make_service(
            '{"action":"new_search","confidence":0.94,'
            '"product_type":"аккумуляторный шуруповерт","question":null}'
        )
        result = await service.decide_conversation_turn(
            "Нужен аккумуляторный шуруповерт",
            context={"active_slots": [], "recent_turns": []},
        )
        self.assertEqual(result["action"], "new_search")
        self.assertEqual(result["product_type"], "аккумуляторный шуруповерт")
        self.assertEqual(result["confidence"], 0.94)
        service._generate.assert_awaited_once()

    async def test_budget_followup_action_is_accepted_without_product_type(self):
        service = self.make_service(
            '{"action":"refine","confidence":0.98,'
            '"product_type":null,"question":null}'
        )
        result = await service.decide_conversation_turn(
            "До 2000 гривен",
            context={"active_slots": [{"kind": "type", "value": "шуруповерт"}]},
        )
        self.assertEqual(result["action"], "refine")
        self.assertIsNone(result["product_type"])

    async def test_low_confidence_non_clarify_action_falls_back(self):
        service = self.make_service(
            '{"action":"new_search","confidence":0.3,'
            '"product_type":"телефон","question":null}'
        )
        result = await service.decide_conversation_turn("это", context={})
        self.assertIsNone(result)

    async def test_low_confidence_clarify_can_still_ask_question(self):
        service = self.make_service(
            '{"action":"clarify","confidence":0.4,'
            '"product_type":null,"question":"Какой товар ищете?"}'
        )
        result = await service.decide_conversation_turn("что-нибудь", context={})
        self.assertEqual(result["action"], "clarify")
        self.assertEqual(result["question"], "Какой товар ищете?")

    async def test_conversation_selector_returns_only_ids_from_candidate_pool(self):
        service = self.make_service(
            '{"ids":["real-2","invented-id","real-1","real-2"]}'
        )
        candidates = [
            Product(
                marketplace=Marketplace.ALIEXPRESS, product_id="real-1",
                title="Сварочный аппарат инверторный",
                product_url="https://www.aliexpress.com/item/real-1.html",
                price=100.0, currency="USD",
            ),
            Product(
                marketplace=Marketplace.ALIEXPRESS, product_id="real-2",
                title="Сварочный аппарат бытовой",
                product_url="https://www.aliexpress.com/item/real-2.html",
                price=120.0, currency="USD",
            ),
        ]

        selected = await service.select_conversation_candidates(
            candidates, query="сварочный аппарат", wanted=2
        )

        self.assertEqual(selected, ["real-2", "real-1"])
        service._generate.assert_awaited_once()

    async def test_conversation_selector_preserves_explicit_empty_selection(self):
        service = self.make_service('{"ids":[]}')
        candidates = [
            Product(
                marketplace=Marketplace.ALIEXPRESS, product_id="mask-1",
                title="Сварочная маска",
                product_url="https://www.aliexpress.com/item/mask-1.html",
                price=20.0, currency="USD",
            )
        ]

        selected = await service.select_conversation_candidates(
            candidates, query="сварочный аппарат", wanted=20
        )

        self.assertEqual(selected, [])

    async def test_conversation_selector_rejects_only_unknown_ids_as_unusable(self):
        service = self.make_service('{"ids":["not-in-pool"]}')
        candidates = [
            Product(
                marketplace=Marketplace.ALIEXPRESS, product_id="real-1",
                title="Сварочный аппарат",
                product_url="https://www.aliexpress.com/item/real-1.html",
                price=100.0, currency="USD",
            )
        ]

        selected = await service.select_conversation_candidates(
            candidates, query="сварочный аппарат", wanted=20
        )

        self.assertIsNone(selected)

    async def test_invalid_action_or_product_type_on_refinement_is_rejected(self):
        service = self.make_service(
            '{"action":"refine","confidence":0.99,'
            '"product_type":"новый товар","question":null}'
        )
        result = await service.decide_conversation_turn("подешевле", context={})
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
