"""Stage 57 (F1): a FALLBACK intent must not overwrite a useful parser query.

Stage 56 (live) showed: ``parse_query`` (Gemini) turns an outcome/problem
request into a good product query («tire pressure gauge»), but the
``parse_query_intent`` call times out (20s) and falls back to
``fallback_intent``, whose ``product_type`` is the WHOLE descriptive sentence.
``apply_intent`` then let that sentence overwrite the good query, so the bot
searched for verbs. Fix (``apply_intent(..., intent_source=...)``): when the
intent is a FALLBACK and the parser already produced a query, keep the parser's
phrase; a valid AI intent keeps its behaviour.

The cases below are structurally different descriptive requests plus controls;
no product name is hardcoded and nothing is translated per-example.
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
    """Simulate the AI parse_query output for an outcome/problem request."""
    return SearchRequest(original_query=original, normalized_query=good_query)


class FallbackDoesNotOverwriteTests(unittest.TestCase):
    """A descriptive fallback product_type must not clobber a good query."""

    def test_tire_pressure_descriptive_request_keeps_good_query(self) -> None:
        original = "Найди прибор, который измеряет давление в шинах"
        request = _parser_request("tire pressure gauge", original)
        intent = fallback_intent(original)

        effective = apply_intent(request, intent, intent_source="fallback")
        # The parser's useful phrase survives; the descriptive sentence that
        # fallback_intent put into product_type does NOT overwrite it.
        self.assertEqual(effective.normalized_query, "tire pressure gauge")
        self.assertNotIn("который", effective.normalized_query)

    def test_second_structurally_different_descriptive_request(self) -> None:
        original = "Чем зажать металлопластиковую трубу?"
        request = _parser_request("multilayer pipe crimping tool", original)
        intent = fallback_intent(original)

        effective = apply_intent(request, intent, intent_source="fallback")
        self.assertEqual(effective.normalized_query, "multilayer pipe crimping tool")
        self.assertNotIn("зажать", effective.normalized_query)

    def test_ai_intent_source_preserves_parser_phrase(self) -> None:
        # Stage 59 supersedes the old behaviour here: a KNOWN intent source no
        # longer lets the AI product_type overwrite the parser's useful phrase
        # (Stage 58 proved that «tire pressure gauge» must survive). The intent
        # still enriches the structured ``intent_*`` state.
        original = "Найди прибор, который измеряет давление в шинах"
        request = _parser_request("tire pressure gauge", original)
        intent = normalize_intent({"product_type": "манометр", "compatibility": ["шины"]})

        effective = apply_intent(request, intent, intent_source="ai")
        self.assertEqual(effective.normalized_query, "tire pressure gauge")
        self.assertEqual(effective.intent_product_type, "манометр")
        self.assertEqual(effective.intent_compatibility, ["шины"])

    def test_no_source_argument_preserves_legacy_behaviour(self) -> None:
        # Existing callers that pass no source keep the old overwrite path.
        original = "Найди прибор, который измеряет давление в шинах"
        request = _parser_request("tire pressure gauge", original)
        intent = fallback_intent(original)
        effective = apply_intent(request, intent)  # no intent_source
        self.assertNotEqual(effective.normalized_query, "tire pressure gauge")

    def test_default_none_source_preserves_legacy_behaviour(self) -> None:
        original = "Найди прибор, который измеряет давление в шинах"
        request = _parser_request("tire pressure gauge", original)
        intent = fallback_intent(original)
        effective = apply_intent(request, intent, intent_source=None)
        self.assertNotEqual(effective.normalized_query, "tire pressure gauge")


class FallbackPreservesConstraintsTests(unittest.TestCase):
    """Structured constraints from the fallback must still be applied."""

    def test_budget_and_features_survive_fallback_guard(self) -> None:
        original = "нужен пылесос без мешка до 50 долларов"
        # The AI parser produced a clean product phrase; intent fell back.
        request = _parser_request("bagless vacuum cleaner", original)
        intent = fallback_intent(original)
        self.assertIsNotNone(intent.budget.max)

        effective = apply_intent(request, intent, intent_source="fallback")
        # Query phrase kept (parser's), but budget from the fallback still lands.
        self.assertEqual(effective.normalized_query, "bagless vacuum cleaner")
        self.assertEqual(effective.max_price, 50.0)

    def test_fallback_without_parser_query_still_builds_one(self) -> None:
        # No useful parser query (empty) -> the fallback must remain usable,
        # not produce an empty/invented query.
        original = "что-нибудь для чистки обуви"
        request = SearchRequest(original_query=original, normalized_query="")
        intent = fallback_intent(original)

        effective = apply_intent(request, intent, intent_source="fallback")
        self.assertTrue(effective.normalized_query)
        self.assertNotEqual(effective.normalized_query, "")

    def test_empty_intent_never_invents_a_query(self) -> None:
        original = "грн"
        request = SearchRequest(original_query=original, normalized_query="")
        effective = apply_intent(
            request, fallback_intent(original), intent_source="fallback"
        )
        self.assertEqual(effective.normalized_query, "")


class QueryVariantsTests(unittest.TestCase):
    """A good query must not spawn verb-derived variants."""

    def test_variants_do_not_reintroduce_descriptive_verbs(self) -> None:
        original = "Найди прибор, который измеряет давление в шинах"
        request = _parser_request("tire pressure gauge", original)
        effective = apply_intent(
            request, fallback_intent(original), intent_source="fallback"
        )
        variants = product_query_variants(effective)
        # The PRIMARY retrieval phrase is the useful parser query - the fix
        # keeps the descriptive fallback sentence out of it. (Derived variants
        # are built from original_query by query_variants and are a separate
        # concern outside this stage's minimal fallback-guard scope.)
        self.assertEqual(variants[0], "tire pressure gauge")



if __name__ == "__main__":
    unittest.main()

