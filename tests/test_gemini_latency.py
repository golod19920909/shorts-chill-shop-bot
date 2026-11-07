"""Offline regressions for Gemini conversational latency budgets and diagnostics."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.ai_service import (
    AIService,
    CONVERSATION_SELECTION_ATTEMPT_TIMEOUT_SECONDS,
    CONVERSATION_TURN_ATTEMPT_TIMEOUT_SECONDS,
    CONVERSATION_TURN_OUTER_TIMEOUT_SECONDS,
)
from services.convsearch.engine import AI_CONVERSATION_SELECTION_TIMEOUT_SECONDS


class GeminiLatencyBudgetTests(unittest.IsolatedAsyncioTestCase):
    def test_outer_budgets_leave_room_for_primary_and_fallback_attempts(self) -> None:
        self.assertGreater(
            CONVERSATION_TURN_OUTER_TIMEOUT_SECONDS,
            2 * CONVERSATION_TURN_ATTEMPT_TIMEOUT_SECONDS,
        )
        self.assertGreater(
            AI_CONVERSATION_SELECTION_TIMEOUT_SECONDS,
            2 * CONVERSATION_SELECTION_ATTEMPT_TIMEOUT_SECONDS,
        )

    async def test_actual_primary_timeout_advances_to_fallback(self) -> None:
        calls: list[str] = []

        class FakeModels:
            async def generate_content(self, **kwargs):
                model = kwargs["model"]
                calls.append(model)
                if model == "primary-model":
                    await asyncio.sleep(0.05)
                return SimpleNamespace(
                    text='{"action":"refine","confidence":0.9,"product_type":null,"marketplace_terms":[],"question":null,"response":null,"slot_updates":[]}',
                    usage_metadata=None,
                )

        service = AIService(
            api_key=None,
            timeout_seconds=20.0,
            fast_model="primary-model",
            fallback_model="fallback-model",
        )
        service._client = SimpleNamespace(aio=SimpleNamespace(models=FakeModels()))

        with patch(
            "services.ai_service.CONVERSATION_TURN_ATTEMPT_TIMEOUT_SECONDS",
            0.01,
        ):
            result = await service._generate(
                "offline latency prompt",
                models=("primary-model",),
                operation="conversation_turn",
            )

        self.assertEqual(calls, ["primary-model", "fallback-model"])
        self.assertEqual(result, '{"action":"refine","confidence":0.9,"product_type":null,"marketplace_terms":[],"question":null,"response":null,"slot_updates":[]}')

    async def test_invalid_structured_turn_response_advances_to_reserve(self) -> None:
        calls: list[str] = []

        class FakeModels:
            async def generate_content(self, **kwargs):
                model = kwargs["model"]
                calls.append(model)
                if model == "primary-model":
                    return SimpleNamespace(text='{"action":', usage_metadata=None)
                return SimpleNamespace(
                    text='{"action":"refine","confidence":0.9,"product_type":null,"marketplace_terms":[],"question":null,"response":null,"slot_updates":[]}',
                    usage_metadata=None,
                )

        service = AIService(
            api_key=None,
            timeout_seconds=20.0,
            fast_model="primary-model",
            fallback_model="reserve-model",
        )
        service._client = SimpleNamespace(aio=SimpleNamespace(models=FakeModels()))

        result = await service._generate(
            "offline structured prompt",
            models=("primary-model",),
            operation="conversation_turn",
        )

        self.assertEqual(calls, ["primary-model", "reserve-model"])
        self.assertEqual(result, '{"action":"refine","confidence":0.9,"product_type":null,"marketplace_terms":[],"question":null,"response":null,"slot_updates":[]}')

    async def test_transport_error_advances_to_reserve_for_conversation(self) -> None:
        calls: list[str] = []

        class FakeModels:
            async def generate_content(self, **kwargs):
                model = kwargs["model"]
                calls.append(model)
                if model == "primary-model":
                    raise ConnectionError("simulated network failure")
                return SimpleNamespace(
                    text='{"action":"refine","confidence":0.9,"product_type":null,"marketplace_terms":[],"question":null,"response":null,"slot_updates":[]}',
                    usage_metadata=None,
                )

        service = AIService(
            api_key=None,
            timeout_seconds=20.0,
            fast_model="primary-model",
            fallback_model="reserve-model",
        )
        service._client = SimpleNamespace(aio=SimpleNamespace(models=FakeModels()))

        result = await service._generate(
            "offline transport prompt",
            models=("primary-model",),
            operation="conversation_turn",
        )

        self.assertEqual(calls, ["primary-model", "reserve-model"])
        self.assertEqual(result, '{"action":"refine","confidence":0.9,"product_type":null,"marketplace_terms":[],"question":null,"response":null,"slot_updates":[]}')

    async def test_attempt_log_separates_local_setup_from_sdk_wait(self) -> None:
        class FakeModels:
            async def generate_content(self, **kwargs):
                return SimpleNamespace(
                    text='{"action":"refine","confidence":0.9,"product_type":null,"marketplace_terms":[],"question":null,"response":null,"slot_updates":[]}',
                    usage_metadata=None,
                )

        service = AIService(
            api_key=None,
            timeout_seconds=20.0,
            fast_model="primary-model",
            fallback_model="fallback-model",
        )
        service._client = SimpleNamespace(aio=SimpleNamespace(models=FakeModels()))

        with self.assertLogs("services.ai_service", level="INFO") as captured:
            result = await service._generate(
                "latency diagnostic prompt",
                models=("primary-model",),
                operation="conversation_turn",
            )

        self.assertIsNotNone(result)
        logs = "\n".join(captured.output)
        self.assertIn("op=conversation_turn", logs)
        self.assertIn("setup_ms=", logs)
        self.assertIn("sdk_wait_ms=", logs)
        self.assertIn("prompt_chars=25", logs)


if __name__ == "__main__":
    unittest.main()
