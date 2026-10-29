# -*- coding: utf-8 -*-
"""G25.2-D Stage 27 — English «without X» negation parser fix (OFFLINE).

Exercises the REAL production path: services.query_intent._detect_exclusions
+ tests.test_search_intent.resolve_and_apply (AIService(api_key=None) local
fallback, NO Gemini) + services.query_variants.product_query_variants. The
production change is a new English exclusion pattern in _detect_exclusions
(query_intent.py); the Stage-25 short-query guard in query_variants.py then
protects the derived variants unchanged. No network/provider/AI call.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "t_search_intent_27", ROOT / "tests" / "test_search_intent.py")
t_si = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(t_si)

from services.query_intent import _detect_exclusions  # noqa: E402
from services.query_variants import (  # noqa: E402
    MAX_PRODUCT_QUERY_VARIANTS,
    product_query_variants,
    _excluded_stems,
)


def _stem(word: str) -> str:
    return word.casefold()[:4]


def variants_for(text: str) -> list[str]:
    request, _, _ = t_si.resolve_and_apply(text)
    return product_query_variants(request)


def request_for(text: str):
    request, _, _ = t_si.resolve_and_apply(text)
    return request


def _unsafe_derived(query: str, request, variants: list[str]) -> list[str]:
    """Derived variants (beyond primary) that reintroduce an excluded stem."""
    stems = _excluded_stems(request)
    return [
        v for v in variants[1:]
        if "without" not in v.casefold()
        and any(_stem(w) in stems for w in v.split() if len(w) >= 3)
    ]


class ConfirmedStage26ExamplesFixed(unittest.TestCase):
    CASES = [
        ("vacuum cleaner without bag", "bag"),
        ("vacuum cleaner without bag Bosch", "bag"),
        ("headphones without case Samsung", "case"),
        ("phone without charger", "charger"),
        ("drill without battery", "battery"),
        ("laptop without charger", "charger"),
        ("car without battery", "battery"),
    ]

    def test_all_seven_parse_exclusion(self) -> None:
        for q, noun in self.CASES:
            req = request_for(q)
            self.assertIn(
                noun.casefold()[:4], {_stem(t) for t in req
                                      .intent_excluded_product_types},
                f"{q!r} must exclude the noun")

    def test_all_seven_have_no_unsafe_derived_variant(self) -> None:
        for q, _ in self.CASES:
            req = request_for(q)
            variants = product_query_variants(req)
            self.assertEqual(
                _unsafe_derived(q, req, variants), [],
                f"{q!r} must not reintroduce the excluded item positively")

    def test_primary_query_preserved(self) -> None:
        for q, _ in self.CASES:
            variants = variants_for(q)
            self.assertEqual(
                variants[0], " ".join(q.split()),
                "primary normalized query must be preserved first")


class BrandNotSwallowedIntoExclusion(unittest.TestCase):
    def test_bosch_not_in_exclusion(self) -> None:
        self.assertEqual(_detect_exclusions("vacuum cleaner without bag Bosch"),
                         ["bag"])

    def test_samsung_not_in_exclusion(self) -> None:
        self.assertEqual(_detect_exclusions("headphones without case Samsung"),
                         ["case"])

    def test_brand_survives_in_derived_variant(self) -> None:
        # The brand is NOT the excluded stem, so a derived variant may still
        # carry it (preserving brand info) as long as the excluded noun is out.
        variants = variants_for("vacuum cleaner without bag Bosch")
        self.assertEqual(variants[0], "vacuum cleaner without bag Bosch")
        for v in variants[1:]:
            self.assertNotIn("bag", v.casefold())
class ExclusionParsingSafety(unittest.TestCase):
    def test_capitalized_WITHOUT(self) -> None:
        self.assertEqual(_detect_exclusions("vacuum WITHOUT bag"), ["bag"])

    def test_extra_whitespace_and_article(self) -> None:
        self.assertEqual(
            _detect_exclusions("  vacuum   without   a   bag  "), ["bag"])

    def test_article_the(self) -> None:
        self.assertEqual(_detect_exclusions("drill without the battery"),
                         ["battery"])

    def test_trailing_punctuation_boundary(self) -> None:
        self.assertEqual(_detect_exclusions("phone without charger."),
                         ["charger"])

    def test_comma_boundary_takes_first_noun(self) -> None:
        self.assertEqual(_detect_exclusions("drill without battery, brush"),
                         ["battery"])

    def test_bare_without_no_exclusion(self) -> None:
        self.assertEqual(_detect_exclusions("cleaner without"), [])

    def test_without_then_empty_no_exclusion(self) -> None:
        self.assertEqual(_detect_exclusions("x without "), [])


class OrdinaryAndAccessoryUnchanged(unittest.TestCase):
    def test_no_negation_english(self) -> None:
        self.assertEqual(_detect_exclusions("vacuum cleaner"), [])
        self.assertEqual(_detect_exclusions("wireless headphones"), [])
        self.assertEqual(_detect_exclusions("bagless vacuum cleaner"), [])

    def test_accessory_case_for_iphone(self) -> None:
        req = request_for("case for iphone 15")
        self.assertEqual(req.intent_excluded_product_types, [])
        self.assertEqual(variants_for("case for iphone 15")[0],
                         "case Apple iphone 15")

    def test_accessory_battery_for_drill(self) -> None:
        self.assertEqual(
            request_for("battery for drill").intent_excluded_product_types,
            [])
        self.assertEqual(variants_for("battery for drill"), ["battery drill"])


class RussianRegressionControlsSafe(unittest.TestCase):
    def test_vacuum_bagless_bosch(self) -> None:
        req = request_for("пылесос без мешка Bosch")
        self.assertTrue(req.intent_excluded_product_types)
        self.assertEqual(
            _unsafe_derived("пылесос без мешка Bosch", req,
                            product_query_variants(req)), [])

    def test_headphones_no_case_samsung(self) -> None:
        req = request_for("наушники без чехла Samsung")
        self.assertTrue(req.intent_excluded_product_types)
        self.assertEqual(
            _unsafe_derived("наушники без чехла Samsung", req,
                            product_query_variants(req)), [])

    def test_bagless_brandless_single_variant(self) -> None:
        self.assertEqual(variants_for("пылесос без мешка"),
                         ["пылесос"])


class Invariants(unittest.TestCase):
    def _check(self, q: str) -> None:
        variants = variants_for(q)
        self.assertTrue(variants)
        self.assertTrue(all(v.strip() for v in variants))
        folded = [v.casefold() for v in variants]
        self.assertEqual(len(folded), len(set(folded)))
        self.assertLessEqual(len(variants), MAX_PRODUCT_QUERY_VARIANTS)

    def test_invariants_across_cases(self) -> None:
        for q in ("vacuum cleaner without bag",
                  "vacuum cleaner without bag Bosch",
                  "headphones without case Samsung", "phone without charger",
                  "case for iphone 15", "пылесос без мешка Bosch"):
            self._check(q)

    def test_cap_unchanged(self) -> None:
        self.assertEqual(MAX_PRODUCT_QUERY_VARIANTS, 3)

    def test_deterministic(self) -> None:
        for q in ("vacuum cleaner without bag", "drill without battery"):
            self.assertEqual(variants_for(q), variants_for(q))


if __name__ == "__main__":
    unittest.main()

