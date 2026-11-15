"""Regression tests for Gemini 3.5 Flash-Lite production request parity."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from services.ai_service import AIService, CONVERSATION_TURN_SCHEMA
from tests.eval_consultant_benchmark_runner import _google_request


class Gemini35RequestParityTests(unittest.TestCase):
    def test_dialogue_benchmark_matches_production_schema_and_thinking(self) -> None:
        body = _google_request("test prompt")
        config = body["generationConfig"]
        self.assertEqual(config["responseMimeType"], "application/json")
        self.assertEqual(config["responseSchema"], CONVERSATION_TURN_SCHEMA)
        self.assertEqual(config["thinkingConfig"], {"thinkingLevel": "minimal"})

    def test_selector_generation_config_uses_requested_thinking_level(self) -> None:
        class FakeModels:
            kwargs = None

            async def generate_content(self, **kwargs):
                self.kwargs = kwargs
                return SimpleNamespace(text='{"selections":[]}')

        models = FakeModels()
        service = AIService(api_key=None)
        service._client = SimpleNamespace(
            aio=SimpleNamespace(models=models)
        )
        raw = asyncio.run(service._generate(
            "selector prompt",
            models=("gemini-3.5-flash-lite",),
            operation="conversation_selection",
            response_schema={"type": "object"},
            thinking_level="medium",
        ))
        self.assertEqual(raw, '{"selections":[]}')
        self.assertEqual(
            models.kwargs["config"]["thinking_config"],
            {"thinking_level": "medium"},
        )
        self.assertEqual(
            models.kwargs["config"]["response_schema"], {"type": "object"}
        )

    def test_turn_generation_config_keeps_schema_and_minimal_thinking(self) -> None:
        class FakeModels:
            kwargs = None

            async def generate_content(self, **kwargs):
                self.kwargs = kwargs
                return SimpleNamespace(text=(
                    '{"action":"clarify","confidence":0.5,"product_type":null,'
                    '"marketplace_terms":[],"question":null,"response":null,'
                    '"slot_updates":[]}'
                ))

        models = FakeModels()
        service = AIService(api_key=None)
        service._client = SimpleNamespace(aio=SimpleNamespace(models=models))
        raw = asyncio.run(service._generate(
            "turn prompt",
            models=("gemini-3.5-flash-lite",),
            operation="conversation_turn",
        ))
        self.assertIsNotNone(raw)
        self.assertEqual(
            models.kwargs["config"]["thinking_config"],
            {"thinking_level": "minimal"},
        )
        self.assertEqual(
            models.kwargs["config"]["response_schema"], CONVERSATION_TURN_SCHEMA
        )

    def test_ai_service_defaults_to_35_primary_and_31_reserve(self) -> None:
        service = AIService(api_key=None)
        self.assertEqual(service.model, "gemini-3.5-flash-lite")
        self.assertEqual(service.fast_model, "gemini-3.5-flash-lite")
        self.assertEqual(service.vision_model, "gemini-3.5-flash-lite")
        self.assertEqual(service.fallback_model, "gemini-3.1-flash-lite")


if __name__ == "__main__":
    unittest.main()
