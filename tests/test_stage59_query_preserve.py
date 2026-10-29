"""Stage 59: preserve the parse_query product query across AI intent.

Stage 58 (live) showed ``parse_query`` identifies the intended product in all
five probes («tire pressure gauge», «flexible grabber pickup tool», ...), but
``apply_intent`` then degraded it in four: a generic/descriptive AI
``product_type`` REPLACED the phrase («устройство достать мелкий предмет...»),
or cross-script compatibility was APPENDED («pet proof cabinet lock шкаф
кошка»). Fix: when the intent parse source is KNOWN (``intent_source`` passed),
the parser's own ``normalized_query`` is the retrieval core - never overwritten
by product_type, never polluted by cross-script compatibility. The intent's
structured fields still enrich ``intent_*`` and relevance reads those (and the
user's own words) directly, so no constraint is lost. Category-independent.
"""

from __future__ import annotations

import unittest

from models.search_request import SearchRequest
from services.query_intent import (
    apply_intent,
    fallback_intent,
    normalize_intent,
)
from services.query_variants import product_query_variants


def _parser_request(good_query: str, original: str) -> SearchRequest:
    """Simulate the parse_query output for an outcome/problem request."""
    return SearchRequest(original_query=original, normalized_query=good_query)


class AIIntentDoesNotReplaceTests(unittest.TestCase):
    """A known intent source keeps the parser's useful phrase."""

    def test_generic_product_type_does_not_replace(self) -> None:
        # Stage 58 case 4: parse_query='flexible grabber pickup tool', but the
        # AI intent product_type is the generic word «устройство».
        original = "Устройство, которое поможет достать мелкий предмет из узкой щели."
        request = _parser_request("flexible grabber pickup tool", original)
        intent = normalize_intent({
            "product_type": "устройство",
            "compatibility": ["достать мелкий предмет из узкой щели"],
        })

        effective = apply_intent(request, intent, intent_source="ai")
        self.assertEqual(effective.normalized_query, "flexible grabber pickup tool")
        # Structured identity still recorded for relevance.
        self.assertEqual(effective.intent_product_type, "устройство")
        self.assertEqual(
            effective.intent_compatibility,
            ["достать мелкий предмет из узкой щели"],
        )

    def test_descriptive_product_type_does_not_replace(self) -> None:
        # Stage 58 case 1: parse_query='tire pressure gauge', AI product_type
        # is a descriptive phrase.
        original = "Найди прибор, который измеряет давление в шинах."
        request = _parser_request("tire pressure gauge", original)
        intent = normalize_intent({
            "product_type": "прибор для измерения давления",
            "compatibility": ["шины"],
        })

        effective = apply_intent(request, intent, intent_source="ai")
        self.assertEqual(effective.normalized_query, "tire pressure gauge")

    def test_cross_script_compatibility_not_appended(self) -> None:
        # Stage 58 case 5: English phrase + Russian compatibility must NOT be
        # unioned into a mixed-script normalized_query.
        original = "Что купить, чтобы кошка не открывала шкаф?"
        request = _parser_request("pet proof cabinet lock", original)
        intent = normalize_intent({
            "product_type": None,
            "compatibility": ["шкаф", "кошка"],
        })

        effective = apply_intent(request, intent, intent_source="ai")
        self.assertEqual(effective.normalized_query, "pet proof cabinet lock")
        self.assertNotIn("шкаф", effective.normalized_query)
        # Compatibility is preserved as structured state for relevance.
        self.assertEqual(effective.intent_compatibility, ["шкаф", "кошка"])


class ConstraintsPreservedTests(unittest.TestCase):
    """Valid structured constraints still apply with a known source."""

    def test_budget_and_brand_survive(self) -> None:
        original = "нужен пылесос без мешка до 50 долларов"
        request = _parser_request("bagless vacuum cleaner", original)
        intent = fallback_intent(original)
        self.assertIsNotNone(intent.budget.max)

        effective = apply_intent(request, intent, intent_source="fallback")
        self.assertEqual(effective.normalized_query, "bagless vacuum cleaner")
        self.assertEqual(effective.max_price, 50.0)


class BackwardCompatTests(unittest.TestCase):
    """Callers that omit intent_source keep the legacy behaviour."""

    def test_no_source_argument_keeps_legacy_overwrite(self) -> None:
        # Direct callers (and legacy tests) still get the old behaviour where a
        # named product_type wins / compatibility is unioned.
        original = "зарядка для iPhone"
        request = _parser_request("зарядка для iphone", original)
        intent = fallback_intent(original)
        effective = apply_intent(request, intent)  # no intent_source
        # Legacy path rebuilds the structured phrase (product + brand + compat),
        # NOT the raw existing phrase.
        self.assertEqual(effective.normalized_query, "зарядка Apple iphone")
        self.assertNotEqual(effective.normalized_query, "зарядка для iphone")

    def test_none_source_argument_keeps_legacy_overwrite(self) -> None:
        original = "зарядка для iPhone"
        request = _parser_request("зарядка для iphone", original)
        intent = fallback_intent(original)
        effective = apply_intent(request, intent, intent_source=None)
        self.assertEqual(effective.normalized_query, "зарядка Apple iphone")


class FallbackStillUsableTests(unittest.TestCase):
    """An empty parser query still lets the fallback reconstruct one."""

    def test_empty_existing_uses_fallback_reconstruction(self) -> None:
        original = "для Toyota Camry 70"
        request = SearchRequest(original_query=original, normalized_query="")
        intent = fallback_intent(original)

        effective = apply_intent(request, intent, intent_source="fallback")
        self.assertTrue(effective.normalized_query)
        self.assertEqual(effective.intent_compatibility, ["toyota camry 70"])

    def test_empty_intent_never_invents_a_query(self) -> None:
        request = SearchRequest(original_query="грн", normalized_query="")
        effective = apply_intent(
            request, fallback_intent("грн"), intent_source="fallback"
        )
        self.assertEqual(effective.normalized_query, "")


class VariantsTests(unittest.TestCase):
    """The preserved phrase is the primary retrieval variant."""

    def test_primary_variant_is_the_parser_phrase(self) -> None:
        original = "Что купить, чтобы кошка не открывала шкаф?"
        request = _parser_request("pet proof cabinet lock", original)
        intent = normalize_intent({"product_type": None, "compatibility": ["шкаф", "кошка"]})
        effective = apply_intent(request, intent, intent_source="ai")
        variants = product_query_variants(effective)
        self.assertEqual(variants[0], "pet proof cabinet lock")


if __name__ == "__main__":
    unittest.main()

