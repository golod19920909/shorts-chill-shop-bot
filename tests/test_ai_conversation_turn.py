"""Validation tests for the Gemini conversational-turn contract."""
from __future__ import annotations

import json

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

    async def test_long_context_is_compacted_as_valid_json_without_losing_pending_question(self):
        import json

        service = self.make_service(
            '{"action":"clarify","confidence":0.4,'
            '"product_type":null,"question":"Уточните задачу"}'
        )
        context = {
            "recent_turns": [f"старое сообщение {i} " + ("текст " * 40) for i in range(10)],
            "recent_dialogue": [
                {"role": "user", "text": f"вопрос {i} " + ("деталь " * 30)}
                if i % 2 == 0 else
                {"role": "assistant", "text": f"ответ {i} " + ("объяснение " * 30)}
                for i in range(16)
            ],
            "active_slots": [
                {"kind": "feature", "value": f"ограничение {i} " + ("деталь " * 20),
                 "source_turn": i, "status": "active"}
                for i in range(20)
            ],
            "shown_product_count": 12,
            "shown_products": [
                {"id": f"product-{i}", "title": "Название " + ("товар " * 50),
                 "price": 100 + i, "currency": "UAH"}
                for i in range(12)
            ],
            "last_outcome": "results",
            "pending_question": "Для какой задачи нужен товар?",
        }

        await service.decide_conversation_turn("для работы", context=context)

        prompt = service._generate.await_args.args[0]
        start = prompt.index("Контекст диалога (JSON):") + len("Контекст диалога (JSON):")
        end = prompt.index("\n\nЕсли в контексте", start)
        serialized = prompt[start:end].strip()
        self.assertLessEqual(len(serialized), 6000)
        parsed = json.loads(serialized)
        self.assertEqual(parsed["pending_question"], "Для какой задачи нужен товар?")
        self.assertIn("active_slots", parsed)
        self.assertIn("shown_products", parsed)
        self.assertIn("recent_dialogue", parsed)
        self.assertTrue(all(item["role"] in {"user", "assistant"} for item in parsed["recent_dialogue"]))
        self.assertLessEqual(len(parsed["recent_dialogue"]), 12)

    async def test_thirty_shown_cards_fit_in_bounded_conversation_context(self):
        service = self.make_service(
            '{"action":"compare","confidence":0.99,"product_type":null,'
            '"question":null,"slot_updates":[]}'
        )
        context = {
            "recent_turns": [f"turn {i}" for i in range(6)],
            "recent_dialogue": [
                {"role": "user", "text": "Нужен фонарь"},
                {"role": "assistant", "text": "Для какой задачи он нужен?"},
            ],
            "active_slots": [
                {"kind": "feature", "value": f"required feature {i}",
                 "source_turn": i, "status": "active"}
                for i in range(4)
            ],
            "shown_product_count": 30,
            "shown_products": [
                {"id": f"product-{i:02d}", "title": f"Product {i:02d} " + ("detail " * 12),
                 "price": 100 + i, "currency": "UAH"}
                for i in range(30)
            ],
            "last_outcome": "results",
            "pending_question": None,
        }

        await service.decide_conversation_turn("Compare all shown products", context=context)

        prompt = service._generate.await_args.args[0]
        start = prompt.index("Контекст диалога (JSON):") + len("Контекст диалога (JSON):")
        end = prompt.index("\n\nЕсли в контексте", start)
        serialized = prompt[start:end].strip()
        parsed = json.loads(serialized)
        self.assertLessEqual(len(serialized), 6000)
        self.assertEqual(len(parsed["shown_products"]), 30)
        self.assertEqual(
            [item["id"] for item in parsed["shown_products"]],
            [f"product-{i:02d}" for i in range(30)],
        )


    async def test_valid_natural_response_is_returned_without_shopping_slots(self):
        service = self.make_service(
            '{"action":"respond","confidence":0.99,"product_type":null,'
            '"question":null,"response":"Люмен — единица светового потока.",'
            '"slot_updates":[]}'
        )
        result = await service.decide_conversation_turn(
            "Что означает люмен?", context={"active_slots": [], "recent_turns": []}
        )
        self.assertEqual(result["action"], "respond")
        self.assertEqual(result["response"], "Люмен — единица светового потока.")
        self.assertEqual(result["slot_updates"], [])

    async def test_natural_response_cannot_carry_product_type_or_slot_changes(self):
        service = self.make_service(
            '{"action":"respond","confidence":0.99,"product_type":"ноутбук",'
            '"question":null,"response":"Вот ноутбук.","slot_updates":[]}'
        )
        result = await service.decide_conversation_turn(
            "Найди ноутбук", context={"active_slots": [], "recent_turns": []}
        )
        self.assertIsNone(result)

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

    async def test_refinement_accepts_bounded_cross_language_phrase_for_active_constraints(self):
        service = self.make_service(
            '{"action":"refine","confidence":0.96,"product_type":null,'
            '"marketplace_terms":["cordless drill with two batteries"],'
            '"question":null,"slot_updates":[]}'
        )
        result = await service.decide_conversation_turn(
            "Поищи именно с двумя аккумуляторами",
            context={
                "active_slots": [
                    {"kind": "type", "value": "аккумуляторный шуруповерт"},
                    {"kind": "feature", "value": "двумя аккумуляторами"},
                ],
                "recent_turns": [],
            },
        )
        self.assertEqual(result["action"], "refine")
        self.assertEqual(
            result["marketplace_terms"],
            ["cordless drill with two batteries"],
        )

    async def test_non_search_actions_cannot_attach_marketplace_phrases(self):
        service = self.make_service(
            '{"action":"compare","confidence":0.99,"product_type":null,'
            '"marketplace_terms":["cordless drill"],"question":null,"slot_updates":[]}'
        )
        result = await service.decide_conversation_turn(
            "Какой из них лучше?",
            context={"active_slots": [], "recent_turns": []},
        )
        self.assertEqual(result["action"], "compare")
        self.assertEqual(result["marketplace_terms"], [])

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

    async def test_model_cannot_use_a_shared_modifier_as_the_product_head(self):
        service = self.make_service(
            '{"action":"new_search","confidence":0.99,'
            '"product_type":"стол сварочный","question":null}'
        )
        result = await service.decide_conversation_turn(
            "Нужен сварочный аппарат",
            context={"active_slots": [], "recent_turns": []},
        )
        # The modifier «сварочный» occurs in the request, but the requested
        # head noun is «аппарат», not «стол».
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

    async def test_model_cannot_treat_a_standalone_adjective_as_product_type(self):
        service = self.make_service(
            '{"action":"new_search","confidence":0.99,'
            '"product_type":"автомобильный","question":null}'
        )
        result = await service.decide_conversation_turn(
            "Нужен автомобильный",
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

    async def test_remove_target_matching_is_case_insensitive(self):
        service = self.make_service(
            '{"action":"refine","confidence":0.98,'
            '"product_type":null,"question":null,'
            '"slot_updates":[{"operation":"remove","kind":"use_case",'
            '"target":"Дома","value":null}]}'
        )
        result = await service.decide_conversation_turn(
            "Убери Дома",
            context={"active_slots": [{"kind": "use_case", "value": "дома"}]},
        )

        self.assertIsNotNone(result)
        self.assertEqual(
            result["slot_updates"],
            [{
                "operation": "remove",
                "kind": "use_case",
                "target": "Дома",
                "value": None,
            }],
        )

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
            '{"selections":['
            '{"id":"real-2","requirement_evidence":{}},'
            '{"id":"invented-id","requirement_evidence":{}},'
            '{"id":"real-1","requirement_evidence":{}},'
            '{"id":"real-2","requirement_evidence":{}}]}'
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

    async def test_conversation_selector_does_not_silently_drop_requirements_over_prompt_limit(self):
        service = self.make_service('{"selections":[]}') 
        candidate = Product(
            marketplace=Marketplace.ALIEXPRESS,
            product_id="many-requirements",
            title="Product listing",
            product_url="https://www.aliexpress.com/item/many-requirements.html",
        )

        selected = await service.select_conversation_candidates(
            [candidate],
            query="product with many constraints",
            wanted=1,
            required_features=[f"required feature {index}" for index in range(13)],
        )

        self.assertIsNone(selected)
        service._generate.assert_not_awaited()

    async def test_conversation_selector_uses_seller_phrase_only_as_cross_language_context(self):
        service = self.make_service('{"selections":[]}')
        candidate = Product(
            marketplace=Marketplace.ALIEXPRESS,
            product_id="english-drill",
            title="Cordless drill with 2 batteries",
            product_url="https://www.aliexpress.com/item/english-drill.html",
        )

        await service.select_conversation_candidates(
            [candidate],
            query="аккумуляторный шуруповёрт | двумя аккумуляторами",
            wanted=20,
            required_features=["двумя аккумуляторами"],
            identity_review_ids=["english-drill"],
            marketplace_terms=["cordless drill two batteries"],
        )

        prompt = service._generate.await_args.args[0]
        self.assertIn('seller_terms: ["cordless drill two batteries"]', prompt)
        self.assertIn("подсказка для сопоставления языков", prompt)
        self.assertIn("не является доказательством", prompt)
        self.assertIn("точную короткую цитату", prompt)

    async def test_english_listing_quotes_can_ground_russian_mandatory_feature(self):
        service = self.make_service(
            '{"selections":[{"id":"english-drill",'
            '"identity_evidence":"Cordless drill",'
            '"requirement_evidence":{"двумя аккумуляторами":"2 batteries"}}]}'
        )
        candidate = Product(
            marketplace=Marketplace.ALIEXPRESS,
            product_id="english-drill",
            title="Cordless drill with 2 batteries",
            product_url="https://www.aliexpress.com/item/english-drill.html",
        )

        selected = await service.select_conversation_candidates(
            [candidate],
            query="аккумуляторный шуруповёрт | двумя аккумуляторами",
            wanted=1,
            required_features=["двумя аккумуляторами"],
            identity_review_ids=["english-drill"],
            marketplace_terms=["cordless drill two batteries"],
        )

        self.assertEqual(selected, ["english-drill"])

    async def test_conversation_selector_receives_relevant_later_description_text(self):
        service = self.make_service(
            '{"selections":[{"id":"described-item","requirement_evidence":{}}]}'
        )
        description = (
            "Описание товара. " * 50
            + "Основное содержимое: насадка для дрели по дереву."
        )
        candidate = Product(
            marketplace=Marketplace.ALIEXPRESS,
            product_id="described-item",
            title="Универсальный набор инструментов",
            product_url="https://www.aliexpress.com/item/described-item.html",
            description=description,
        )

        selected = await service.select_conversation_candidates(
            [candidate], query="насадка для дрели по дереву", wanted=1
        )

        self.assertEqual(selected, ["described-item"])
        prompt = service._generate.await_args.args[0]
        self.assertIn("Основное содержимое: насадка для дрели по дереву", prompt)

    async def test_conversation_selector_prompt_enforces_exclusions_and_budget(self):
        service = self.make_service('{"selections":[]}')
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
        self.assertIn("Проверяй явно заданную совместимость отдельно", prompt)
        self.assertIn("Не считай запятую сама по себе признаком второго товара", prompt)
        self.assertIn("фактически подтверждённые характеристики", prompt)
        self.assertIn("основной предмет", prompt)
        self.assertNotIn("сварочная маска", prompt)

    async def test_conversation_selector_preserves_explicit_empty_selection(self):
        service = self.make_service('{"selections":[]}')
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
        service = self.make_service(
            '{"selections":[{"id":"not-in-pool","requirement_evidence":{}}]}'
        )
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
