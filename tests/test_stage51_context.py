"""Stage 51 - preserve product context during natural follow-ups.

Offline, deterministic: the real regex fallback parser plus the real
conversation refinement logic. Reproduces the Stage 50 live failure (a
use/budget/feature follow-up dropped the product) without any network or
Gemini call, and proves explicit product switches still reset.
"""

from __future__ import annotations

import asyncio
import unittest

from models.search_request import SearchRequest
from services.ai_service import AIService
from services.conversation import resolve_request


def parse(text: str) -> SearchRequest:
    """The real deterministic parser (the one used when Gemini is absent)."""
    return AIService._fallback_parse(text)


def resolve(previous, raw: str):
    return asyncio.run(resolve_request(previous, parse(raw), raw))


class Stage50SequenceTests(unittest.TestCase):
    """The exact Stage 50 live scenario, offline: the product must stay a
    cordless screwdriver/drill through the use, two-battery and cheaper steps."""

    def setUp(self) -> None:
        self.step1 = parse("Помоги выбрать хороший шуруповёрт для дома.")

    def test_step2_intended_use_and_budget_refine_keep_product(self) -> None:
        raw = ("В основном для сборки мебели и иногда для дерева. "
               "Бюджет до 40 долларов.")
        merged, refined = resolve(self.step1, raw)
        self.assertTrue(refined)
        # The original product is still the catalog query.
        self.assertIn("шурупов", merged.normalized_query)
        # The $40 budget is applied.
        self.assertEqual(merged.max_price, 40.0)

    def test_step3_two_batteries_refine_keep_product(self) -> None:
        # Step 2 first, then step 3 off its result (the real chain).
        raw2 = ("В основном для сборки мебели и иногда для дерева. "
                "Бюджет до 40 долларов.")
        after2, _ = resolve(self.step1, raw2)
        raw3 = "Найди вариант с двумя аккумуляторами, но не дороже 40 долларов."
        merged, refined = resolve(after2, raw3)
        self.assertTrue(refined)
        self.assertIn("шурупов", merged.normalized_query)
        # The battery count is a REQUIREMENT, not the searched product.
        self.assertTrue(
            any("аккумулятор" in feature for feature in merged.required_features)
        )
        # Budget preserved.
        self.assertEqual(merged.max_price, 40.0)

    def test_step4_cheaper_preserves_product_and_budget(self) -> None:
        raw2 = "В основном для сборки мебели и иногда для дерева. Бюджет до 40 долларов."
        after2, _ = resolve(self.step1, raw2)
        raw3 = "Найди вариант с двумя аккумуляторами, но не дороже 40 долларов."
        after3, _ = resolve(after2, raw3)
        merged, refined = resolve(after3, "Подешевле.")
        self.assertTrue(refined)
        self.assertIn("шурупов", merged.normalized_query)
        # «Подешевле» must NOT invent or reset the numeric budget.
        self.assertEqual(merged.max_price, 40.0)
        # The two-battery requirement survives.
        self.assertTrue(
            any("аккумулятор" in feature for feature in merged.required_features)
        )



class ProductSwitchTests(unittest.TestCase):
    """An explicit switch to a different product must still reset, and a
    use/feature refinement must never reset - the two directions of the guard."""

    def test_explicit_switch_resets(self) -> None:
        prev = parse("шуруповёрт для дома")
        merged, refined = resolve(prev, "Забудь шуруповёрт, найди набор отвёрток")
        self.assertFalse(refined)
        # ё is folded to е by the parser, so match the folded stem.
        folded = merged.normalized_query.replace("ё", "е")
        self.assertIn("отверт", folded)

    def test_cross_category_switch_resets(self) -> None:
        prev = parse("беспроводные наушники")
        merged, refined = resolve(prev, "чехол для телефона")
        self.assertFalse(refined)
        self.assertIn("чехол", merged.normalized_query)

    def test_feature_add_refines(self) -> None:
        prev = parse("беспроводные наушники")
        merged, refined = resolve(prev, "с микрофоном")
        self.assertTrue(refined)
        self.assertIn("наушник", merged.normalized_query)
        self.assertTrue(merged.required_features)

    def test_intended_use_alone_refines(self) -> None:
        prev = parse("шуруповёрт для дома")
        merged, refined = resolve(prev, "в основном для дерева")
        self.assertTrue(refined)
        self.assertIn("шурупов", merged.normalized_query)

    def test_device_compatibility_target_still_resets(self) -> None:
        # «для iPhone» names a concrete device the previous search never
        # mentioned: a real compatibility change, so it resets (existing
        # Stage 2 semantics) - distinct from «для мебели/дерева» (intended use).
        prev = parse("зарядка")
        merged, refined = resolve(prev, "для iPhone")
        self.assertFalse(refined)
        folded = merged.normalized_query.replace("ё", "е").casefold()
        self.assertIn("iphone", folded)

    def test_budget_only_refines(self) -> None:
        prev = parse("шуруповёрт")
        merged, refined = resolve(prev, "до 40 долларов")
        self.assertTrue(refined)
        self.assertIn("шурупов", merged.normalized_query)
        self.assertEqual(merged.max_price, 40.0)


class AIMergeValidationTests(unittest.TestCase):
    """Both AI-merge directions go through the same is_refinement gate."""

    def setUp(self) -> None:
        self.prev = parse("Помоги выбрать хороший шуруповёрт для дома.")

    def test_good_ai_merge_is_accepted(self) -> None:
        class GoodAI:
            is_available = True

            async def merge_search_requests(self, previous, follow_up, raw_text):
                return SearchRequest(
                    original_query=raw_text,
                    normalized_query=previous.normalized_query,
                    max_price=40.0,
                )

        raw = "В основном для сборки мебели. Бюджет до 40 долларов."
        merged, refined = asyncio.run(
            resolve_request(self.prev, parse(raw), raw, GoodAI())
        )
        self.assertTrue(refined)
        self.assertIn("шурупов", merged.normalized_query)
        self.assertEqual(merged.max_price, 40.0)

    def test_drifting_ai_merge_is_rejected(self) -> None:
        class DriftAI:
            is_available = True

            async def merge_search_requests(self, previous, follow_up, raw_text):
                return SearchRequest(
                    original_query=raw_text,
                    normalized_query="набор отвёрток",
                )

        raw = "Забудь шуруповёрт, найди набор отвёрток"
        merged, refined = asyncio.run(
            resolve_request(self.prev, parse(raw), raw, DriftAI())
        )
        self.assertFalse(refined)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
