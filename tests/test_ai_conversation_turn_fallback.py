"""Offline tests for bounded Gemini fallback during turn interpretation."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from services.ai_service import AIService


class ConversationTurnFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_primary_timeout_tries_configured_fallback_model(self) -> None:
        calls: list[str] = []

        class FakeModels:
            async def generate_content(self, **kwargs):
                calls.append(kwargs["model"])
                if len(calls) == 1:
                    raise TimeoutError("simulated primary timeout")
                return SimpleNamespace(text='{"action":"refine","confidence":0.9,"product_type":null,"marketplace_terms":[],"question":null,"response":null,"slot_updates":[]}')

        service = AIService(
            api_key=None,
            timeout_seconds=12.0,
            fast_model="primary-model",
            fallback_model="fallback-model",
        )
        service._client = SimpleNamespace(
            aio=SimpleNamespace(models=FakeModels())
        )

        result = await service._generate(
            "offline prompt",
            models=("primary-model",),
            operation="conversation_turn",
        )

        self.assertEqual(
            calls,
            ["primary-model", "fallback-model"],
        )
        self.assertEqual(
            result,
            '{"action":"refine","confidence":0.9,"product_type":null,"marketplace_terms":[],"question":null,"response":null,"slot_updates":[]}',
        )
