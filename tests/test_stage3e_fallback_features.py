# -*- coding: utf-8 -*-
"""Stage 3E: deterministic fallback feature extraction + numeric feature preservation.

Covers:
- ``ai_service._fallback_parse`` extracts positive required features
  («с X», «со X», «with X») including numeric specs («12V», «two batteries»).
- ``query_intent.fallback_intent`` extracts positive required attributes
  from the same natural-language patterns.
- ``conversation._clean_feature`` preserves alphanumeric specs («12V»,
  «65W», «20000mAh», «USB-C») while still rejecting bare digit counts («5»).
- ``deterministic_merge`` / ``merge_feature_lists`` keep required features
  across turns, including cancellation («без X» removes «с X»).
- Multi-turn survival: a feature established in turn 1 survives turn 2
  when turn 2 only adjusts price / other non-feature constraints.
"""

from __future__ import annotations

import asyncio
import unittest

from models.search_request import SearchMode, SearchRequest
from services.ai_service import AIService
from services.conversation import (
    _clean_feature,
    deterministic_merge,
    merge_feature_lists,
)
from services.query_intent import fallback_intent


def run(coro):
    return asyncio.run(coro)


def parse(text: str) -> SearchRequest:
    """The real deterministic parser - the same one used without Gemini."""
    return run(AIService(api_key=None).parse_query(text))


class CleanFeatureNumericPreservationTests(unittest.TestCase):
    """_clean_feature must keep alphanumeric specs and reject bare digits."""

    def test_alphanumeric_specs_are_preserved(self) -> None:
        for feature in ("12V", "65W", "20000mAh", "USB-C", "20000 мАч"):
            with self.subTest(feature=feature):
                self.assertEqual(_clean_feature(feature), feature)

    def test_two_batteries_is_preserved(self) -> None:
        self.assertEqual(_clean_feature("two batteries"), "two batteries")

    def test_ru_feature_with_digits_is_preserved(self) -> None:
        self.assertEqual(
            _clean_feature("с двумя аккумуляторами"), "с двумя аккумуляторами"
        )

    def test_bare_digit_counts_are_rejected(self) -> None:
        for feature in ("5", "100", "3", "10"):
            with self.subTest(feature=feature):
                self.assertIsNone(_clean_feature(feature))

    def test_quality_phrases_still_rejected(self) -> None:
        # Captured *after* the «с» introducer, so «с» itself is not in the span.
        self.assertIsNone(_clean_feature("хорошим рейтингом"))
        self.assertIsNone(_clean_feature("отличн"))
        self.assertIsNone(_clean_feature("бюджет"))


class FallbackParseFeatureExtractionTests(unittest.TestCase):
    """ai_service._fallback_parse must fill required_features."""

    def test_ru_s_feature_becomes_required(self) -> None:
        req = parse("наушники с микрофоном")
        self.assertIn("микрофоном", req.required_features)

    def test_en_with_feature_becomes_required(self) -> None:
        req = parse("with two batteries")
        self.assertIn("two batteries", req.required_features)

    def test_numeric_feature_is_preserved(self) -> None:
        req = parse("с 12V зарядное устройство")
        self.assertIn("12V", " ".join(req.required_features))

    def test_ru_with_multiple_words_feature(self) -> None:
        req = parse("с двумя аккумуляторами")
        self.assertIn("двумя аккумуляторами", " ".join(req.required_features))

    def test_no_feature_means_empty_list(self) -> None:
        req = parse("чехол для iPhone 15")
        self.assertEqual(req.required_features, [])

    def test_price_and_feature_coexist(self) -> None:
        req = parse("наушники с микрофоном до 30 долларов")
        # The 2-word capture window may also pull the adjacent price word
        # («микрофоном до»); the microphone stem must still be present, and
        # the budget must be parsed independently of the feature.
        self.assertTrue(
            any("микрофон" in f.lower() for f in req.required_features),
            req.required_features,
        )
        self.assertEqual(req.max_price, 30.0)
        self.assertEqual(req.currency, "USD")


class FallbackIntentFeatureExtractionTests(unittest.TestCase):
    """query_intent.fallback_intent must fill required_attributes."""

    def test_ru_s_attribute(self) -> None:
        intent = fallback_intent("наушники с микрофоном")
        self.assertIn("микрофоном", intent.required_attributes)

    def test_en_with_attribute(self) -> None:
        intent = fallback_intent("with two batteries")
        self.assertIn("two batteries", intent.required_attributes)

    def test_numeric_spec_attribute(self) -> None:
        intent = fallback_intent("с 12V")
        self.assertTrue(
            any("12" in a and "v" in a.lower() for a in intent.required_attributes)
        )

    def test_usb_c_attribute(self) -> None:
        intent = fallback_intent("USB-C кабель")
        self.assertIn("usb-c", intent.required_attributes)

    def test_no_feature_means_empty_attributes(self) -> None:
        intent = fallback_intent("чехол для iPhone 15")
        self.assertEqual(intent.required_attributes, [])

    def test_quality_phrase_not_a_feature(self) -> None:
        intent = fallback_intent("с хорошим рейтингом")
        self.assertNotIn("хорошим", intent.required_attributes)
        self.assertNotIn("рейтингом", intent.required_attributes)


class DeterministicMergeFeatureTests(unittest.TestCase):
    """merge_feature_lists / deterministic_merge must keep required features."""

    def setUp(self) -> None:
        self.previous = parse("Нужны беспроводные наушники до $30")

    def test_adding_feature_accumulates(self) -> None:
        merged, refined = deterministic_merge(
            self.previous, parse("а с микрофоном"), "а с микрофоном"
        )
        self.assertTrue(refined)
        self.assertTrue(
            any("микрофон" in f.lower() for f in merged.required_features)
        )
        self.assertEqual(merged.max_price, 30.0)

    def test_feature_cancellation(self) -> None:
        previous = SearchRequest(
            original_query="наушники",
            normalized_query="наушники",
            required_features=["микрофон"],
            mode=SearchMode.SEARCH,
        )
        merged_required, merged_excluded = merge_feature_lists(
            previous, "без микрофона"
        )
        self.assertNotIn("микрофон", merged_required)
        # «без микрофона» captures the inflected form; downstream compares
        # by the same stem matcher the filter uses (_same_feature).
        self.assertTrue(
            any("микрофон" in f.lower() for f in merged_excluded),
            merged_excluded,
        )


class SimultaneousFeatureTests(unittest.TestCase):
    """One message can add and cancel features at once."""

    def test_adding_and_canceling_same_turn(self) -> None:
        previous = SearchRequest(
            original_query="наушники",
            normalized_query="наушники",
            mode=SearchMode.SEARCH,
        )

        merged_required, merged_excluded = merge_feature_lists(
            previous, "а с микрофоном, без шумоподавления"
        )
        self.assertTrue(any("микрофон" in f.lower() for f in merged_required))
        self.assertTrue(any("шум" in f.lower() for f in merged_excluded))

class NumericFeatureMergeTests(unittest.TestCase):
    """Numeric features must survive merges and refinements."""

    def test_numeric_feature_survives_merge(self) -> None:
        previous = SearchRequest(
            original_query="зарядное устройство",
            normalized_query="зарядное устройство",
            required_features=["12V"],
            mode=SearchMode.SEARCH,
        )
        merged_required, _ = merge_feature_lists(previous, "а до 20 долларов")
        self.assertIn("12V", merged_required)


    def test_deterministic_merge_keeps_numeric_feature(self) -> None:
        previous = SearchRequest(
            original_query="наушники",
            normalized_query="наушники",
            required_features=["12V"],
            max_price=50.0,
            currency="USD",
            mode=SearchMode.SEARCH,
        )
        follow_up = parse("а с микрофоном")
        merged, refined = deterministic_merge(previous, follow_up, "а с микрофоном")
        self.assertTrue(refined)
        self.assertIn("12V", merged.required_features)


class MultiTurnPreservationTests(unittest.TestCase):
    """Features established in turn 1 survive turn 2 refinements."""

    def test_feature_survives_price_refinement(self) -> None:
        previous = parse("наушники с микрофоном до 50 долларов")
        follow_up = parse("а до 30 долларов")
        merged, refined = deterministic_merge(previous, follow_up, "а до 30 долларов")
        self.assertTrue(refined)
        self.assertTrue(any("микрофон" in f.lower() for f in merged.required_features))
        self.assertEqual(merged.max_price, 30.0)
    def test_feature_added_in_turn_2(self) -> None:
        previous = parse("наушники до 50 долларов")
        follow_up = parse("а с хорошим микрофоном")
        merged, refined = deterministic_merge(
            previous, follow_up, "а с хорошим микрофоном"
        )
        self.assertTrue(refined)
        self.assertTrue(any("микрофон" in f.lower() for f in merged.required_features))
        self.assertEqual(merged.max_price, 50.0)

    def test_feature_canceled_in_turn_2(self) -> None:
        previous = parse("наушники с микрофоном")
        merged_required, merged_excluded = merge_feature_lists(
            previous, "а без микрофона"
        )
        self.assertNotIn("микрофон", merged_required)
        self.assertTrue(any("микрофон" in f.lower() for f in merged_excluded))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

