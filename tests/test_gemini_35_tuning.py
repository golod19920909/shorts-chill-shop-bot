"""Regression tests for Gemini 3.5 Flash-Lite production request parity."""

from __future__ import annotations

import unittest

from services.ai_service import AIService, CONVERSATION_TURN_SCHEMA
from tests.eval_consultant_benchmark_runner import _google_request


class Gemini35RequestParityTests(unittest.TestCase):
    def test_dialogue_benchmark_matches_production_schema_and_thinking(self) -> None:
        body = _google_request("test prompt")
        config = body["generationConfig"]
        self.assertEqual(config["responseMimeType"], "application/json")
        self.assertEqual(config["responseSchema"], CONVERSATION_TURN_SCHEMA)
        self.assertEqual(config["thinkingConfig"], {"thinkingLevel": "minimal"})

    def test_ai_service_defaults_to_35_primary_and_31_reserve(self) -> None:
        service = AIService(api_key=None)
        self.assertEqual(service.model, "gemini-3.5-flash-lite")
        self.assertEqual(service.fast_model, "gemini-3.5-flash-lite")
        self.assertEqual(service.vision_model, "gemini-3.5-flash-lite")
        self.assertEqual(service.fallback_model, "gemini-3.1-flash-lite")


if __name__ == "__main__":
    unittest.main()
