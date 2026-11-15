"""Regression tests for structured output on active conversational AI operations."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from models.product import Marketplace, Product
from services.ai_service import (
    AIService,
    CHAT_ACTION_SCHEMA,
    COMPARISON_EVIDENCE_SCHEMA,
    PRODUCT_ADVICE_SCHEMA,
    PRODUCT_QUESTION_SCHEMA,
)


def _product(product_id: str, title: str, description: str = "") -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=product_id,
        title=title,
        product_url=f"https://example.invalid/{product_id}",
        description=description,
    )


class StructuredOperationCallSiteTests(unittest.TestCase):
    def _service(self, responses: dict[str, str]):
        service = AIService(api_key=None)
        service._client = object()
        service.captured = []

        async def fake_generate(
            contents,
            *,
            models=None,
            operation="generate",
            response_schema=None,
            thinking_level=None,
        ):
            service.captured.append((operation, response_schema))
            return responses[operation]

        service._generate = fake_generate
        return service


class TestStructuredOperationCallSites(StructuredOperationCallSiteTests):
    def test_chat_action_uses_schema(self):
        service = self._service({
            "chat_action": '{"action":"search","question":null}',
        })
        result = asyncio.run(service.decide_chat_action("Найди игровой ноутбук"))
        self.assertEqual(result, {"action": "search"})
        self.assertEqual(service.captured, [("chat_action", CHAT_ACTION_SCHEMA)])

    def test_product_advice_uses_schema(self):
        service = self._service({
            "product_advice": '{"observations":[],"recommendation":null}',
        })
        result = asyncio.run(service.analyze_product(
            _product("advice-1", "USB-C power bank 65W")
        ))
        self.assertIsNone(result)
        self.assertEqual(service.captured, [("product_advice", PRODUCT_ADVICE_SCHEMA)])

    def test_comparison_evidence_uses_schema(self):
        service = self._service({
            "product_comparison_evidence": (
                '{"differences":[{"aspect":"other",'
                '"first":{"product_id":"laptop-a","field":"title","quote":"16GB RAM"},'
                '"second":{"product_id":"laptop-b","field":"title","quote":"8GB RAM"}}]}'
            ),
        })
        result = asyncio.run(service.extract_paired_listing_evidence(
            "У какого больше памяти?",
            [
                _product("laptop-a", "Laptop 16GB RAM model A"),
                _product("laptop-b", "Laptop 8GB RAM model B"),
            ],
        ))
        self.assertEqual(result, [("other", "16GB RAM", "8GB RAM")])
        self.assertEqual(
            service.captured,
            [("product_comparison_evidence", COMPARISON_EVIDENCE_SCHEMA)],
        )

    def test_product_question_uses_schema(self):
        service = self._service({
            "product_question_answer": (
                '{"claims":[{"product_id":"power-1","field":"title",'
                '"quote":"USB-C power bank 65W"}],'
                '"recommendation":{"product_id":null,"evidence":[]},'
                '"clarification":null}'
            ),
        })
        answer, product_id = asyncio.run(
            service.answer_product_question_with_recommendation(
                "Есть ли USB-C?", [_product("power-1", "USB-C power bank 65W")]
            )
        )
        self.assertIsNotNone(answer)
        self.assertIsNone(product_id)
        self.assertEqual(
            service.captured,
            [("product_question_answer", PRODUCT_QUESTION_SCHEMA)],
        )


class TestGenericStructuredGenerateConfig(unittest.TestCase):
    def test_generic_structured_operations_set_json_schema_and_output_caps(self):
        operations = (
            ("chat_action", CHAT_ACTION_SCHEMA, 128),
            ("product_advice", PRODUCT_ADVICE_SCHEMA, 512),
            ("product_comparison_evidence", COMPARISON_EVIDENCE_SCHEMA, 1024),
            ("product_question_answer", PRODUCT_QUESTION_SCHEMA, 2048),
        )
        for operation, schema, output_cap in operations:
            with self.subTest(operation=operation):
                class FakeModels:
                    kwargs = None

                    async def generate_content(self, **kwargs):
                        self.kwargs = kwargs
                        return SimpleNamespace(text="{}")

                models = FakeModels()
                service = AIService(api_key=None)
                service._client = SimpleNamespace(aio=SimpleNamespace(models=models))
                raw = asyncio.run(service._generate(
                    "structured prompt",
                    models=("gemini-3.5-flash-lite",),
                    operation=operation,
                    response_schema=schema,
                ))
                self.assertEqual(raw, "{}")
                config = models.kwargs["config"]
                self.assertEqual(config["response_mime_type"], "application/json")
                self.assertEqual(config["response_schema"], schema)
                self.assertEqual(config["max_output_tokens"], output_cap)
                self.assertEqual(
                    config["automatic_function_calling"],
                    {"disable": True},
                )
