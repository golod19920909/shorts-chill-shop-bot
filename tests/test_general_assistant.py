"""Offline tests for the standalone assistant mode."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock

from services.ai_service import AIService
from services.assistant.router import AssistantRouter
from services.assistant.state import ChatConversationState


class ChatConversationStateTests(unittest.TestCase):
    def test_history_is_bounded_and_roles_are_validated(self):
        state = ChatConversationState()
        for index in range(20):
            state.add("user", f"message {index}")
        state.add("system", "must not be stored")
        self.assertEqual(len(state.turns), state.MAX_TURNS)
        self.assertEqual(state.turns[-1].content, "message 19")

    def test_prompt_history_keeps_role_and_content_only(self):
        state = ChatConversationState()
        state.add("user", "Привет")
        state.add("assistant", "Здравствуйте")
        self.assertEqual(
            state.as_prompt_history(),
            [
                {"role": "user", "content": "Привет"},
                {"role": "assistant", "content": "Здравствуйте"},
            ],
        )


class AssistantRouterTests(unittest.TestCase):
    def test_router_passes_independent_chat_and_shopping_context(self):
        class FakeAI:
            is_available = True

            async def decide_conversation_turn(self, text, *, context):
                self.context = context
                return {"action": "general_chat", "confidence": 0.99}

        class Slot:
            def as_dict(self):
                return {"kind": "type", "value": "ноутбук"}

        class ShoppingState:
            last_outcome = "results"
            last_question = None

            def active(self):
                return [Slot()]

        ai = FakeAI()
        chat = ChatConversationState()
        chat.add("user", "Почему батареи стареют?")
        result = asyncio.run(
            AssistantRouter(ai).classify(
                "А зимой почему быстрее?",
                chat_state=chat,
                shopping_state=ShoppingState(),
                shown_product_count=3,
            )
        )
        self.assertEqual(result["action"], "general_chat")
        self.assertEqual(ai.context["recent_turns"], ["Пользователь (чат): Почему батареи стареют?"])
        self.assertEqual(ai.context["active_slots"][0]["value"], "ноутбук")
        self.assertEqual(ai.context["shown_product_count"], 3)


class GeneralAnswerTests(unittest.TestCase):
    def test_general_answer_uses_smart_model_and_general_chat_operation(self):
        service = AIService(api_key=None, model="smart-test", fast_model="fast-test")
        service._client = object()
        service._generate = AsyncMock(return_value="Ответ с учётом контекста")

        answer = asyncio.run(
            service.answer_general(
                [
                    {"role": "user", "content": "Объясни аккумуляторы"},
                    {"role": "assistant", "content": "Они накапливают энергию"},
                    {"role": "user", "content": "Почему зимой хуже?"},
                ]
            )
        )

        self.assertEqual(answer, "Ответ с учётом контекста")
        kwargs = service._generate.await_args.kwargs
        self.assertEqual(kwargs["models"], ("smart-test",))
        self.assertEqual(kwargs["operation"], "general_chat")
        self.assertIn("Почему зимой хуже?", service._generate.await_args.args[0])

    def test_general_answer_rejects_history_without_current_user_turn(self):
        service = AIService(api_key=None)
        service._client = object()
        service._generate = AsyncMock(return_value="should not be called")
        result = asyncio.run(
            service.answer_general([{"role": "assistant", "content": "Hello"}])
        )
        self.assertIsNone(result)
        service._generate.assert_not_awaited()

    def test_general_chat_route_is_validated_by_existing_turn_interpreter(self):
        service = AIService(api_key=None)
        service._client = object()
        service._generate = AsyncMock(
            return_value=(
                '{"action":"general_chat","confidence":0.99,'
                '"product_type":null,"question":null,"slot_updates":[]}'
            )
        )
        result = asyncio.run(
            service.decide_conversation_turn(
                "Почему аккумуляторы стареют?",
                context={"recent_turns": [], "active_slots": []},
            )
        )
        self.assertEqual(result["action"], "general_chat")
        self.assertEqual(result["slot_updates"], [])
