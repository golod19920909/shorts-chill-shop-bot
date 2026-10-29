# -*- coding: utf-8 -*-
"""G25.2-B Stage 1: production structured query-intent parser tests.

Covered (per Stage 1 spec):

- schema / normalization: unknown keys, invalid types, numeric budget,
  грн/₴/UAH aliases, compatibility-vs-product_type, empty lists,
  model/generation never invented;
- the deterministic fallback for the 15 required queries;
- specification extraction (65W / 1 ТБ / 18V / 20000 мАч / 4K / NVMe);
- AI success path, every AI failure mode, the circuit breaker.

The AI is ALWAYS mocked (``FakeAI``): the suite never touches live Gemini.
"""

from __future__ import annotations

import asyncio
import json
import unittest

from pydantic import ValidationError

from services.query_intent import (
    INTENT_AI_STATS,
    StructuredQueryIntent,
    fallback_intent,
    is_circuit_open,
    normalize_currency,
    normalize_intent,
    parse_query_intent,
    reset_ai_state,
)


def run(coro):
    return asyncio.run(coro)


def parse(text, ai=None):
    """Parse through the real public entry point (``ai`` may be a mock)."""

    return run(parse_query_intent(text, ai))


class FakeAI:
    """Minimal stand-in for ``AIService`` implementing only ``_generate``."""

    def __init__(self, result=None, *, exc=None, client=True):
        self._client = object() if client else None
        self.fast_model = "gemini-test"
        self.last_model = None
        self.calls = 0
        self._result = result
        self._exc = exc

    async def _generate(self, contents, *, models=None):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._result


INTENT_FIELDS = {
    "product_type", "brand", "model", "generation",
    "required_attributes", "optional_attributes", "compatibility",
    "budget", "excluded_product_types",
}


class SchemaNormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_ai_state()

    def test_unknown_keys_are_discarded(self) -> None:
        intent = normalize_intent({
            "product_type": "зарядка",
            "brand": "Apple",
            "invented_field": "x",
            "products": [{"title": "fake"}],
        })
        dumped = intent.model_dump()
        self.assertEqual(set(dumped), INTENT_FIELDS)
        self.assertEqual(intent.product_type, "зарядка")
        self.assertEqual(intent.brand, "Apple")
        self.assertNotIn("invented_field", dumped)
        self.assertNotIn("products", dumped)

    def test_non_dict_payload_yields_empty_intent(self) -> None:
        for bad in ([1, 2, 3], "text", None, 42, True):
            intent = normalize_intent(bad)
            self.assertIsNone(intent.product_type)
            self.assertEqual(intent.compatibility, [])
            self.assertEqual(set(intent.model_dump()), INTENT_FIELDS)

    def test_numeric_budget_is_normalized(self) -> None:
        intent = normalize_intent(
            {"budget": {"max": "3 000,50", "min": "1500"}})
        self.assertEqual(intent.budget.max, 3000.5)
        self.assertEqual(intent.budget.min, 1500)

    def test_bare_budget_number_becomes_max(self) -> None:
        intent = normalize_intent({"budget": 2500})
        self.assertEqual(intent.budget.max, 2500)
        self.assertIsNone(intent.budget.min)

    def test_negative_budget_is_dropped(self) -> None:
        intent = normalize_intent({"budget": {"max": -5, "min": -1}})
        self.assertIsNone(intent.budget.max)
        self.assertIsNone(intent.budget.min)

    def test_currency_aliases_normalize(self) -> None:
        self.assertEqual(normalize_currency("грн"), "UAH")
        self.assertEqual(normalize_currency("₴"), "UAH")
        self.assertEqual(normalize_currency("UAH"), "UAH")
        self.assertEqual(normalize_currency("uah."), "UAH")
        self.assertEqual(normalize_currency("USD"), "USD")
        self.assertIsNone(normalize_currency("биткоин"))
        self.assertIsNone(normalize_currency(None))

    def test_budget_currency_normalizes_to_uah(self) -> None:
        for alias in ("грн", "₴", "UAH"):
            intent = normalize_intent(
                {"budget": {"max": 100, "currency": alias}})
            self.assertEqual(intent.budget.currency, "UAH", alias)

    def test_compatibility_never_overwrites_product_type(self) -> None:
        # Empty product_type + a compat target stays empty (no overwrite).
        intent = normalize_intent(
            {"product_type": None, "compatibility": ["iPhone 15 Pro"]})
        self.assertIsNone(intent.product_type)
        self.assertEqual(intent.compatibility, ["iPhone 15 Pro"])
        # An explicit product_type is never replaced by compatibility.
        intent2 = normalize_intent(
            {"product_type": "чехол", "compatibility": ["iphone"]})
        self.assertEqual(intent2.product_type, "чехол")
        self.assertEqual(intent2.compatibility, ["iphone"])

    def test_empty_lists_stay_empty_and_budget_exists(self) -> None:
        intent = normalize_intent({})
        self.assertEqual(intent.required_attributes, [])
        self.assertEqual(intent.optional_attributes, [])
        self.assertEqual(intent.compatibility, [])
        self.assertEqual(intent.excluded_product_types, [])
        self.assertIsNone(intent.budget.min)
        self.assertIsNone(intent.budget.max)
        self.assertIsNone(intent.budget.currency)

    def test_invalid_scalar_and_list_types_are_normalized(self) -> None:
        intent = normalize_intent({
            "product_type": {"nested": "x"},                # container
            "brand": ["apple"],                             # list scalar
            "model": True,                                  # bool
            "compatibility": 42,                            # not a list
            "required_attributes": [{"x": 1}, "65W", 42],   # junk items
            "budget": "not a number",                       # ignored
        })
        self.assertIsNone(intent.product_type)
        self.assertIsNone(intent.brand)
        self.assertIsNone(intent.model)
        self.assertEqual(intent.compatibility, [])
        self.assertEqual(intent.required_attributes, ["65W", "42"])
        self.assertIsNone(intent.budget.max)

    def test_model_and_generation_are_never_invented(self) -> None:
        intent = normalize_intent(
            {"product_type": "зарядка", "compatibility": ["iphone"]})
        self.assertIsNone(intent.model)
        self.assertIsNone(intent.generation)
        for query in ("зарядка для iPhone 15 Pro",
                      "дворники Volkswagen Golf 7",
                      "SSD 1 ТБ",
                      "до 2000 грн для PS5"):
            fb = fallback_intent(query)
            self.assertIsNone(fb.model, query)
            self.assertIsNone(fb.generation, query)

    def test_schema_rejects_unknown_fields(self) -> None:
        with self.assertRaises(ValidationError):
            StructuredQueryIntent(product_type="x", junk=1)


class FallbackQueriesTests(unittest.TestCase):
    """The 15 spec-mandated queries (golden G25.2-A behavior, parity-verified)."""

    def setUp(self) -> None:
        reset_ai_state()

    def test_01_do_3000_grn(self) -> None:
        intent = fallback_intent("до 3000 грн")
        self.assertIsNone(intent.product_type)
        self.assertEqual(intent.budget.max, 3000)
        self.assertEqual(intent.budget.currency, "UAH")

    def test_02_grn_alone(self) -> None:
        intent = fallback_intent("грн")
        self.assertIsNone(intent.product_type)
        self.assertIsNone(intent.brand)
        self.assertEqual(intent.compatibility, [])
        self.assertEqual(intent.required_attributes, [])
        self.assertIsNone(intent.budget.max)
        self.assertIsNone(intent.budget.currency)

    def test_03_zaryadka_dlya_iphone(self) -> None:
        intent = fallback_intent("зарядка для iPhone")
        self.assertEqual(intent.product_type, "зарядка")
        self.assertEqual(intent.brand, "Apple")
        self.assertEqual(intent.compatibility, ["iphone"])

    def test_04_zaryadka_dlya_iphone_15_pro(self) -> None:
        intent = fallback_intent("зарядка для iPhone 15 Pro")
        self.assertEqual(intent.product_type, "зарядка")
        self.assertEqual(intent.brand, "Apple")
        self.assertEqual(intent.compatibility, ["iphone 15 pro"])

    def test_05_akkumulyator_dlya_makita(self) -> None:
        intent = fallback_intent("аккумулятор для Makita 18V")
        self.assertEqual(intent.product_type, "аккумулятор")
        self.assertEqual(intent.brand, "Makita")
        self.assertEqual(intent.compatibility, ["makita"])
        self.assertEqual(intent.required_attributes, ["18 v"])

    def test_06_instrument_dlya_makita(self) -> None:
        intent = fallback_intent("инструмент для Makita 18V")
        self.assertEqual(intent.product_type, "инструмент")
        self.assertEqual(intent.brand, "Makita")
        self.assertEqual(intent.compatibility, ["makita"])
        self.assertEqual(intent.required_attributes, ["18 v"])

    def test_07_dverniki_dlya_golf_7(self) -> None:
        intent = fallback_intent("дворники для Golf 7")
        self.assertEqual(intent.product_type, "дворники")
        self.assertIsNone(intent.brand)
        self.assertEqual(intent.compatibility, ["golf 7"])

    def test_08_dverniki_volkswagen_golf_7(self) -> None:
        intent = fallback_intent("дворники Volkswagen Golf 7")
        self.assertEqual(intent.product_type, "дворники")
        self.assertEqual(intent.brand, "Volkswagen")
        self.assertEqual(intent.compatibility, ["volkswagen golf 7"])

    def test_09_dlya_toyota_camry_70(self) -> None:
        intent = fallback_intent("для Toyota Camry 70")
        self.assertIsNone(intent.product_type)
        self.assertEqual(intent.brand, "Toyota")
        self.assertEqual(intent.compatibility, ["toyota camry 70"])

    def test_10_kamera_dlya_mashiny(self) -> None:
        intent = fallback_intent("камера для машины")
        self.assertEqual(intent.product_type, "камера")
        self.assertEqual(intent.compatibility, ["машины"])

    def test_11_podarok_dlya_kota(self) -> None:
        # The recipient/context must NEVER become the product type.
        intent = fallback_intent("подарок для кота")
        self.assertEqual(intent.product_type, "подарок")
        self.assertNotEqual(intent.product_type, "кот")
        self.assertEqual(intent.compatibility, ["кота"])

    def test_12_ssd_1tb(self) -> None:
        intent = fallback_intent("SSD 1 ТБ")
        self.assertEqual(intent.product_type, "ssd")
        self.assertEqual(intent.required_attributes, ["1 тб"])
        self.assertEqual(intent.compatibility, [])

    def test_13_ssd_dlya_noutbuka(self) -> None:
        intent = fallback_intent("SSD для ноутбука")
        self.assertEqual(intent.product_type, "ssd")
        self.assertEqual(intent.compatibility, ["ноутбука"])

    def test_14_nabor_instrumentov(self) -> None:
        intent = fallback_intent("набор инструментов")
        self.assertEqual(intent.product_type, "набор инструментов")
        self.assertEqual(intent.compatibility, [])

    def test_15_do_2000_grn_dlya_ps5(self) -> None:
        intent = fallback_intent("до 2000 грн для PS5")
        self.assertIsNone(intent.product_type)
        self.assertEqual(intent.brand, "Sony")
        self.assertEqual(intent.compatibility, ["ps5"])
        self.assertEqual(intent.budget.max, 2000)
        self.assertEqual(intent.budget.currency, "UAH")


class FallbackBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_ai_state()

    def test_ot_1000_grn_is_a_min_budget(self) -> None:
        intent = fallback_intent("от 1000 грн")
        self.assertIsNone(intent.product_type)
        self.assertEqual(intent.budget.min, 1000)
        self.assertEqual(intent.budget.currency, "UAH")

    def test_under_50_usd(self) -> None:
        intent = fallback_intent("under 50 USD")
        self.assertIsNone(intent.product_type)
        self.assertEqual(intent.budget.max, 50)
        self.assertEqual(intent.budget.currency, "USD")

    def test_bare_amount_with_hryvnia_symbol(self) -> None:
        intent = fallback_intent("чехол 500 ₴")
        self.assertEqual(intent.product_type, "чехол")
        self.assertEqual(intent.budget.max, 500)
        self.assertEqual(intent.budget.currency, "UAH")

    def test_leading_junk_words_are_stripped(self) -> None:
        intent = fallback_intent("хочу купить повербанк до 1500 грн")
        self.assertEqual(intent.product_type, "повербанк")
        self.assertEqual(intent.budget.max, 1500)
        self.assertEqual(intent.budget.currency, "UAH")

    def test_currency_words_never_remain_search_terms(self) -> None:
        cases = (("до 3000 грн", "грн"),
                 ("чехол 500 ₴", "₴"),
                 ("under 50 USD", "usd"))
        for query, word in cases:
            intent = fallback_intent(query)
            terms = " ".join(
                filter(None,
                       [intent.product_type,
                        *intent.compatibility,
                        *intent.required_attributes])
            ).lower()
            self.assertNotIn(word, terms, query)

    def test_budget_query_still_yields_a_usable_product(self) -> None:
        # Budget extraction must not destroy the product phrase.
        intent = fallback_intent("повербанк до 1500 грн")
        self.assertEqual(intent.product_type, "повербанк")
        self.assertEqual(intent.budget.max, 1500)


class SpecificationExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_ai_state()

    def test_65w(self) -> None:
        intent = fallback_intent("зарядка 65W")
        self.assertEqual(intent.product_type, "зарядка")
        self.assertEqual(intent.required_attributes, ["65 w"])

    def test_1tb(self) -> None:
        intent = fallback_intent("SSD 1 ТБ")
        self.assertEqual(intent.product_type, "ssd")
        self.assertEqual(intent.required_attributes, ["1 тб"])

    def test_18v(self) -> None:
        intent = fallback_intent("TV 18V")
        self.assertEqual(intent.product_type, "tv")
        self.assertEqual(intent.required_attributes, ["18 v"])

    def test_20000mah(self) -> None:
        intent = fallback_intent("повербанк 20000 мАч")
        self.assertEqual(intent.product_type, "повербанк")
        self.assertEqual(intent.required_attributes, ["20000 мач"])

    def test_4k(self) -> None:
        intent = fallback_intent("видеорегистратор 4K")
        self.assertEqual(intent.product_type, "видеорегистратор")
        self.assertEqual(intent.required_attributes, ["4 k"])

    def test_nvme(self) -> None:
        intent = fallback_intent("SSD NVMe")
        self.assertEqual(intent.product_type, "ssd")
        self.assertEqual(intent.required_attributes, ["NVME"])

    def test_specs_are_structured_not_query_noise(self) -> None:
        # The spec must leave the product phrase, not stay inside it.
        intent = fallback_intent("зарядка 65W")
        self.assertNotIn("65", intent.product_type)


class AISuccessTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_ai_state()

    def test_valid_json_is_parsed_and_normalized(self) -> None:
        payload = {
            "product_type": "зарядка",
            "compatibility": ["iPhone 15 Pro"],
            "brand": None,
            "budget": {"max": 2000, "currency": "грн"},
            "unknown_extra": "drop me",
        }
        ai = FakeAI(result=json.dumps(payload))
        intent, meta = parse("зарядка для iPhone 15 Pro до 2000 грн", ai)
        self.assertEqual(meta.source, "ai")
        self.assertIsNone(meta.error)
        self.assertEqual(meta.model, "gemini-test")
        self.assertIsNotNone(meta.latency_ms)
        self.assertEqual(intent.product_type, "зарядка")
        self.assertEqual(intent.compatibility, ["iPhone 15 Pro"])
        self.assertEqual(intent.budget.max, 2000)
        self.assertEqual(intent.budget.currency, "UAH")
        self.assertNotIn("unknown_extra", intent.model_dump())
        self.assertEqual(ai.calls, 1)
        self.assertEqual(INTENT_AI_STATS["ok"], 1)
        self.assertFalse(is_circuit_open())

    def test_fenced_json_is_accepted(self) -> None:
        ai = FakeAI(result='```json\n{"product_type": "SSD"}\n```')
        intent, meta = parse("SSD", ai)
        self.assertEqual(meta.source, "ai")
        self.assertEqual(intent.product_type, "SSD")

    def test_ai_output_is_strictly_validated(self) -> None:
        # Types that violate the schema are normalized, never trusted raw.
        payload = {"product_type": ["not", "a", "string"], "brand": {"x": 1}}
        ai = FakeAI(result=json.dumps(payload))
        intent, meta = parse("чехол iPhone", ai)
        self.assertEqual(meta.source, "ai")
        self.assertIsNone(intent.product_type)
        self.assertIsNone(intent.brand)


class AIFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_ai_state()

    def test_none_result_falls_back(self) -> None:
        ai = FakeAI(result=None)
        intent, meta = parse("зарядка для iPhone", ai)
        self.assertEqual(meta.source, "fallback")
        self.assertEqual(meta.error, "ai_returned_none")
        self.assertEqual(intent.product_type, "зарядка")
        self.assertEqual(ai.calls, 1)
        self.assertEqual(INTENT_AI_STATS["fallback_used"], 1)

    def test_exception_falls_back(self) -> None:
        ai = FakeAI(exc=RuntimeError("gemini down"))
        intent, meta = parse("SSD для ноутбука", ai)
        self.assertEqual(meta.source, "fallback")
        self.assertEqual(meta.error, "ai_exception:RuntimeError")
        self.assertEqual(intent.product_type, "ssd")
        self.assertEqual(ai.calls, 1)

    def test_malformed_json_falls_back(self) -> None:
        for garbage in ("точно не JSON", "```json\n{broken\n```",
                        '{"product_type": "зарядка"'):
            reset_ai_state()
            ai = FakeAI(result=garbage)
            intent, meta = parse("зарядка для iPhone", ai)
            self.assertEqual(meta.source, "fallback", garbage)
            self.assertEqual(meta.error, "invalid_json", garbage)
            self.assertEqual(intent.product_type, "зарядка", garbage)

    def test_schema_invalid_payload_falls_back(self) -> None:
        ai = FakeAI(result=json.dumps({"totally_unrelated": 1}))
        intent, meta = parse("дворники для Golf 7", ai)
        self.assertEqual(meta.source, "fallback")
        self.assertEqual(meta.error, "invalid_schema")
        self.assertEqual(intent.product_type, "дворники")
        self.assertEqual(INTENT_AI_STATS["invalid_schema"], 1)

    def test_json_array_falls_back(self) -> None:
        ai = FakeAI(result=json.dumps([1, 2, 3]))
        intent, meta = parse("набор инструментов", ai)
        self.assertEqual(meta.source, "fallback")
        self.assertEqual(intent.product_type, "набор инструментов")

    def test_quota_like_failure_never_raises(self) -> None:
        ai = FakeAI(exc=TimeoutError("quota"))
        intent, meta = parse("до 3000 грн", ai)
        self.assertEqual(meta.source, "fallback")
        self.assertTrue(meta.error.startswith("ai_exception:"), meta.error)
        self.assertEqual(intent.budget.max, 3000)
        self.assertEqual(intent.budget.currency, "UAH")

    def test_empty_and_garbage_queries_never_raise(self) -> None:
        for query in ("", "   ", None, "!!!", "до" * 40):
            intent, meta = parse(query, None)
            self.assertIsInstance(intent, StructuredQueryIntent)
            self.assertEqual(meta.source, "fallback")


class CircuitBreakerTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_ai_state()

    def test_first_failure_opens_circuit_and_next_parse_skips_ai(self) -> None:
        ai = FakeAI(result=None)
        _, meta1 = parse("наушники", ai)
        self.assertEqual(ai.calls, 1)
        self.assertEqual(meta1.error, "ai_returned_none")
        self.assertEqual(INTENT_AI_STATS["circuit_opens"], 1)
        self.assertTrue(is_circuit_open())

        intent2, meta2 = parse("зарядка для iPhone", ai)
        # The second parse must NOT call Gemini again.
        self.assertEqual(ai.calls, 1)
        self.assertEqual(meta2.source, "fallback")
        self.assertEqual(meta2.error, "circuit_open")
        self.assertEqual(intent2.product_type, "зарядка")
        self.assertEqual(INTENT_AI_STATS["attempted"], 1)

    def test_ai_without_client_is_never_called(self) -> None:
        ai = FakeAI(result=json.dumps({"product_type": "x"}), client=False)
        intent, meta = parse("наушники", ai)
        self.assertEqual(ai.calls, 0)
        self.assertEqual(meta.source, "fallback")
        self.assertEqual(meta.error, "ai_unavailable")
        self.assertEqual(intent.product_type, "наушники")

    def test_missing_ai_service_uses_fallback(self) -> None:
        intent, meta = parse("до 3000 грн", None)
        self.assertEqual(meta.source, "fallback")
        self.assertEqual(meta.error, "ai_unavailable")
        self.assertEqual(intent.budget.max, 3000)
        self.assertEqual(intent.budget.currency, "UAH")

    def test_reset_ai_state_closes_the_circuit(self) -> None:
        _, meta = parse("наушники", FakeAI(result=None))
        self.assertEqual(meta.source, "fallback")
        self.assertTrue(is_circuit_open())

        reset_ai_state()
        self.assertFalse(is_circuit_open())
        ai = FakeAI(result=json.dumps({"product_type": "наушники"}))
        intent, meta = parse("наушники", ai)
        self.assertEqual(meta.source, "ai")
        self.assertEqual(ai.calls, 1)
        self.assertEqual(intent.product_type, "наушники")


if __name__ == "__main__":
    unittest.main()