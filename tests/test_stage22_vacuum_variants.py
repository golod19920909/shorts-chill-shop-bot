# -*- coding: utf-8 -*-
"""G25.2-D Stage 22 — regression guards for bagless-vacuum query construction
and variant generation (OFFLINE). Uses the exact production pipeline
(tests.test_search_intent.resolve_and_apply, AIService(api_key=None) local
fallback, NO Gemini) and services.query_variants.product_query_variants.

Establishes:
  * the complete-appliance target stays a vacuum cleaner;
  * bagged vs bagless is never silently inverted;
  * excluded product types are never reintroduced by a derived variant;
  * the English "bagless" is a phrase word, not a «без» negation;
  * existing connector/spec guards (Stage 16 A-2/A-3) remain intact;
  * unrelated search behaviour (non-vacuum queries) is untouched.
No production file or existing test is modified.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "t_search_intent_stage22", ROOT / "tests" / "test_search_intent.py")
t_si = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(t_si)

from services.query_variants import product_query_variants  # noqa: E402
from services.query_intent import fallback_intent  # noqa: E402


def variants_for(text: str) -> list[str]:
    request, _, _ = t_si.resolve_and_apply(text)
    return product_query_variants(request)


def request_for(text: str):
    request, _, _ = t_si.resolve_and_apply(text)
    return request


class BaglessTargetIsVacuum(unittest.TestCase):
    def test_bagless_query_target_is_vacuum(self) -> None:
        req = request_for("пылесос без мешка")
        self.assertIn("пылесос", req.normalized_query.casefold())
        self.assertTrue(req.intent_product_type)
        self.assertIn("пылесос", req.intent_product_type.casefold())

    def test_bagged_query_target_is_vacuum(self) -> None:
        req = request_for("пылесос с мешком")
        self.assertIn("пылесос", req.normalized_query.casefold())


class BaggedVsBaglessNotInverted(unittest.TestCase):
    def test_bagless_excludes_bag(self) -> None:
        req = request_for("пылесос без мешка")
        self.assertEqual(req.intent_excluded_product_types, ["мешка"])

    def test_bagged_query_has_no_exclusion(self) -> None:
        req = request_for("пылесос с мешком")
        self.assertEqual(req.intent_excluded_product_types, [])

    def test_bare_мешка_phrase_has_no_exclusion(self) -> None:
        # «пылесос мешка» (no «без») is the CONTROL phrase: it must NOT be
        # treated as a negation.
        req = request_for("пылесос мешка")
        self.assertEqual(req.intent_excluded_product_types, [])


class ExcludedTypeNotReintroduced(unittest.TestCase):
    def test_bagless_produces_no_positive_мешка_variant(self) -> None:
        variants = variants_for("пылесос без мешка")
        # NO derived variant may contain «мешка».
        self.assertEqual(variants[0], "пылесос")
        for phrase in variants[1:]:
            self.assertNotIn("мешк", phrase.casefold())

    def test_bagless_single_variant_no_unsafe_second(self) -> None:
        # The Stage-16 A-3 guard must omit the unsafe «пылесос мешка» variant
        # entirely (not emit it, not replace it with an invented opposite).
        variants = variants_for("пылесос без мешка")
        self.assertNotIn("пылесос мешка", variants)

    def test_headphones_without_case_guard_still_holds(self) -> None:
        # Adjacent Stage-16 A-3 case must keep working (guard intact).
        variants = variants_for("наушники без чехла")
        self.assertEqual(variants[0], "наушники")
        for phrase in variants[1:]:
            self.assertNotIn("чехл", phrase.casefold())


class EnglishBaglessIsNotNegation(unittest.TestCase):
    def test_english_bagless_has_no_exclusion(self) -> None:
        req = request_for("bagless vacuum cleaner")
        self.assertEqual(req.intent_excluded_product_types, [])

    def test_english_bagless_primary_preserved(self) -> None:
        variants = variants_for("bagless vacuum cleaner")
        self.assertEqual(variants[0], "bagless vacuum cleaner")

    def test_english_bagless_secondary_is_broader_vacuum(self) -> None:
        # The English phrase yields a broader «vacuum cleaner» secondary; it
        # must not contain «bag» (no inversion).
        variants = variants_for("bagless vacuum cleaner")
        for phrase in variants[1:]:
            self.assertNotIn("bag", phrase.casefold())
        self.assertIn("vacuum cleaner", " ".join(variants).casefold())


class ConnectorAndSpecGuardsIntact(unittest.TestCase):
    def test_leading_dlya_stripped_for_compatibility_query(self) -> None:
        # Stage-16 A-2 guard: a leading «для» connector is stripped.
        variants = variants_for("Для Toyota Camry 70")
        self.assertFalse(variants[0].casefold().startswith("для"))

    def test_price_preference_not_leaked(self) -> None:
        # Stage-16 A-1 guard: «дешевую» never becomes a product term.
        for phrase in variants_for("Хочу купить дешевую мышку для ноутбука"):
            self.assertNotIn("дешев", phrase.casefold())


class UnrelatedSearchUntouched(unittest.TestCase):
    def test_non_vacuum_query_unaffected(self) -> None:
        # A normal multi-word electronics query keeps its primary and stays
        # within the variant cap (unrelated behaviour unchanged).
        variants = variants_for("беспроводные наушники с шумоподавлением")
        self.assertTrue(variants)
        self.assertLessEqual(len(variants), 3)
        self.assertIn("наушник", variants[0].casefold())

    def test_fallback_intent_no_exclusion_for_plain_phrase(self) -> None:
        intent = fallback_intent("пылесос мешка")
        self.assertEqual(intent.excluded_product_types, [])


if __name__ == "__main__":
    unittest.main()
