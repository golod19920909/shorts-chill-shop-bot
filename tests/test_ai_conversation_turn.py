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

    async def test_model_cannot_invent_a_product_head_not_in_user_message(self):
        service = self.make_service(
            '{"action":"new_search","confidence":0.99,'
            '"product_type":"сварочный стол","question":null}'
        )
        result = await service.decide_conversation_turn(
            "Нужен сварочный аппарат",
            context={"active_slots": [], "recent_turns": []},
        )
        self.assertIsNone(result)

    async def test_model_cannot_promote_a_relation_target_to_product_type(self):
        service = self.make_service(
            '{"action":"new_search","confidence":0.99,'
            '"product_type":"дрель","question":null}'
        )
        result = await service.decide_conversation_turn(
            "Нужна насадка на дрель по дереву",
            context={"active_slots": [], "recent_turns": []},
        )

        self.assertIsNone(result)

    async def test_model_product_head_accepts_user_inflection(self):
        service = self.make_service(
            '{"action":"new_search","confidence":0.99,'
            '"product_type":"шуруповёрт","question":null}'
        )
        result = await service.decide_conversation_turn(
            "Ищу шуруповерта для дома",
            context={"active_slots": [], "recent_turns": []},
        )
        self.assertEqual(result["product_type"], "шуруповёрт")

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

    async def test_grounded_slot_updates_are_returned(self):
        service = self.make_service(
            '{"action":"refine","confidence":0.98,'
            '"product_type":null,"question":null,'
            '"slot_updates":['
            '{"kind":"use_case","value":"на стройке"},'
            '{"kind":"feature","value":"защита IP67"}]}'
        )
        result = await service.decide_conversation_turn(
            "Буду работать на стройке, нужна защита IP67",
            context={"active_slots": [{"kind": "type", "value": "устройство"}]},
        )

        self.assertEqual(
            result["slot_updates"],
            [
                {"operation": "add", "kind": "use_case", "target": None, "value": "на стройке"},
                {"operation": "add", "kind": "feature", "target": None, "value": "защита IP67"},
            ],
        )

    async def test_grounded_accessory_compatibility_update_is_accepted(self):
        service = self.make_service(
            '{"action":"refine","confidence":0.98,'
            '"product_type":null,"question":null,'
            '"slot_updates":[{"kind":"compatibility","value":"дрели"}]}'
        )
        result = await service.decide_conversation_turn(
            "Нужна насадка для дрели",
            context={"active_slots": [{"kind": "type", "value": "насадка"}]},
        )

        self.assertIsNotNone(result)
        self.assertEqual(
            result["slot_updates"],
            [{
                "operation": "add",
                "kind": "compatibility",
                "target": None,
                "value": "дрели",
            }],
        )

    async def test_targeted_replace_is_accepted_only_for_an_active_slot(self):
        service = self.make_service(
            '{"action":"correction","confidence":0.98,'
            '"product_type":null,"question":null,'
            '"slot_updates":[{"operation":"replace","kind":"feature",'
            '"target":"ДВА аккумулятора","value":"один аккумулятор"}]}'
        )
        result = await service.decide_conversation_turn(
            "Не два аккумулятора, а один аккумулятор",
            context={"active_slots": [
                {"kind": "feature", "value": "два аккумулятора"},
            ]},
        )

        self.assertIsNotNone(result)
        self.assertEqual(
            result["slot_updates"],
            [{
                "operation": "replace",
                "kind": "feature",
                "target": "ДВА аккумулятора",
                "value": "один аккумулятор",
            }],
        )

    async def test_targeted_replace_must_mention_the_old_target_in_current_message(self):
        service = self.make_service(
            '{"action":"correction","confidence":0.98,'
            '"product_type":null,"question":null,'
            '"slot_updates":[{"operation":"replace","kind":"feature",'
            '"target":"два аккумулятора","value":"один аккумулятор"}]}'
        )
        result = await service.decide_conversation_turn(
            "Не один аккумулятор, а два",
            context={"active_slots": [
                {"kind": "feature", "value": "два аккумулятора"},
            ]},
        )

        self.assertIsNone(result)

    async def test_targeted_remove_is_accepted_only_for_an_active_slot(self):
        service = self.make_service(
            '{"action":"correction","confidence":0.98,'
            '"product_type":null,"question":null,'
            '"slot_updates":[{"operation":"remove","kind":"exclusion",'
            '"target":"ударный","value":null}]}'
        )
        result = await service.decide_conversation_turn(
            "Убери ударный из исключений",
            context={"active_slots": [
                {"kind": "exclusion", "value": "ударный"},
            ]},
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["slot_updates"][0]["operation"], "remove")

    async def test_targeted_replace_cannot_change_an_inactive_slot(self):
        service = self.make_service(
            '{"action":"correction","confidence":0.98,'
            '"product_type":null,"question":null,'
            '"slot_updates":[{"operation":"replace","kind":"feature",'
            '"target":"титановый корпус","value":"алюминиевый корпус"}]}'
        )
        result = await service.decide_conversation_turn(
            "Вместо титанового корпуса алюминиевый корпус",
            context={"active_slots": []},
        )

        self.assertIsNone(result)

    async def test_malformed_operation_type_is_rejected_without_raising(self):
        service = self.make_service(
            '{"action":"refine","confidence":0.98,'
            '"product_type":null,"question":null,'
            '"slot_updates":[{"operation":["remove"],"kind":"feature",'
            '"target":"IP67","value":null}]}'
        )
        result = await service.decide_conversation_turn(
            "Убери IP67",
            context={"active_slots": [{"kind": "feature", "value": "IP67"}]},
        )

        self.assertIsNone(result)

    async def test_slot_update_not_grounded_in_current_message_is_rejected(self):
        service = self.make_service(
            '{"action":"refine","confidence":0.98,'
            '"product_type":null,"question":null,'
            '"slot_updates":[{"kind":"feature","value":"титановый корпус"}]}'
        )
        result = await service.decide_conversation_turn(
            "Хочу что-нибудь надёжное",
            context={"active_slots": [{"kind": "type", "value": "устройство"}]},
        )

        self.assertIsNone(result)

    async def test_malformed_slot_kind_is_rejected_without_raising(self):
        service = self.make_service(
            '{"action":"refine","confidence":0.98,'
            '"product_type":null,"question":null,'
            '"slot_updates":[{"kind":["feature"],"value":"IP67"}]}'
        )
        result = await service.decide_conversation_turn(
            "Нужна защита IP67",
            context={},
        )

        self.assertIsNone(result)

    async def test_not_only_is_not_accepted_as_an_exclusion(self):
        service = self.make_service(
            '{"action":"refine","confidence":0.98,'
            '"product_type":null,"question":null,'
            '"slot_updates":[{"kind":"exclusion","value":"пластик"}]}'
        )
        result = await service.decide_conversation_turn(
            "Не только пластик, но и металл",
            context={},
        )

        self.assertIsNone(result)

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

    async def test_conversation_selector_prompt_enforces_exclusions_and_budget(self):
        service = self.make_service('{"ids":[]}')
        candidates = [
            Product(
                marketplace=Marketplace.ALIEXPRESS, product_id="welder-1",
                title="Сварочный аппарат MMA",
                product_url="https://www.aliexpress.com/item/welder-1.html",
                price=1800.0, currency="UAH",
            )
        ]

        await service.select_conversation_candidates(
            candidates,
            query=(
                "насадка | Совместимость: дрель | по дереву | "
                "ИСКЛЮЧИТЬ: полуавтомат | ЖЁСТКИЙ БЮДЖЕТ: до 2000 UAH"
            ),
            wanted=20,
        )

        prompt = service._generate.await_args.args[0]
        self.assertIn("ИСКЛЮЧИТЬ: полуавтомат", prompt)
        self.assertIn("ЖЁСТКИЙ БЮДЖЕТ: до 2000 UAH", prompt)
        self.assertIn("НЕ доказывает соответствие", prompt)
        self.assertIn("неизвестный тип пропускай", prompt)
        self.assertIn("Проверяй совместимость отдельно", prompt)
        self.assertIn("после запятой", prompt)
        self.assertIn("проверь описание и состав", prompt)

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
